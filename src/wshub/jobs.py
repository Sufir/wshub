"""Задачи Claude Code: хранилище, старт, снимки, отмена, resume (ТЗ WSH-2, п. 2.5).

Логика не зависит от MCP и проверяется тестами напрямую. Общее с файловым сервером берётся из Hub:
журнал (`_audit`), служебные каталоги (`protected_kind`), `runtime.pid_alive`/`proc_start`.
"""
from __future__ import annotations

import contextlib
import os
import secrets
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from . import claude_cli, taskfile
from .core import WsError
from .registry import ProjectCode, RegistryError
from .runtime import atomic_write_text, pid_alive, proc_start, read_json

B32 = "abcdefghijklmnopqrstuvwxyz234567"
TASKFILE_MAX = 1024 * 1024
MODELS = ("haiku", "sonnet", "opus", "fable")
EFFORTS = ("low", "medium", "high", "xhigh", "max")
LIVE = ("starting", "prefetch", "running", "cancelling", "orphaned")
TERMINAL = ("completed", "failed", "max_turns", "max_budget", "timeout", "cancelled", "lost")
# сигнал отмены → пауза → эскалация (п. 2.5.4). Паузы можно укоротить env-переменными — только для тестов
CANCEL_SIGTERM_AFTER = 20
CANCEL_SIGKILL_AFTER = 10


def cancel_timers() -> tuple[float, float]:
    """(пауза до SIGTERM, пауза до SIGKILL), секунд. WSHUB_CODE_CANCEL_* — тестовое переопределение."""
    return (float(os.environ.get("WSHUB_CODE_CANCEL_SIGTERM_S", CANCEL_SIGTERM_AFTER)),
            float(os.environ.get("WSHUB_CODE_CANCEL_SIGKILL_S", CANCEL_SIGKILL_AFTER)))


class JobError(WsError):
    pass


def new_id(now: float | None = None) -> str:
    t = datetime.fromtimestamp(now if now is not None else time.time())
    tail = "".join(secrets.choice(B32) for _ in range(6))
    return f"{t:%Y%m%d-%H%M%S}-{tail}"


@dataclass
class Plan:
    """Проверенные параметры задачи: готово к записи и запуску."""
    project: str
    root: Path
    dir: Path
    task: Path
    task_rel: str
    profile: str
    prompt_name: str
    model: str
    effort: str
    max_turns: int
    budget_usd: float
    prompt: str
    warnings: list[str]
    proj: ProjectCode


def _order(value: str, order: tuple[str, ...], name: str) -> int:
    try:
        return order.index(value)
    except ValueError:
        raise JobError(f"{name}: одно из {', '.join(order)}") from None


class Jobs:
    def __init__(self, hub):
        self.hub = hub
        self.root = hub.state_dir / "jobs"
        self.lock_path = self.root / ".lock"

    # ---------- проверка и план ----------

    def _project(self, name: str):
        """(Workspace, ProjectCode) для проекта с code.enabled, иначе отказ. Ошибка в [code] — отказ wshub-code."""
        reg = self.hub.registry.get()
        if reg.code.error:
            raise JobError(f"секция [code] в реестре с ошибкой: {reg.code.error}")
        ws = reg.workspaces.get(name)
        if ws is None:
            raise JobError(f"проект {name} не найден в реестре")
        if name in reg.code.project_errors:
            raise JobError(f"[workspace.{name}.code] с ошибкой: {reg.code.project_errors[name]}")
        proj = reg.code.projects.get(name)
        if proj is None or not proj.enabled:
            raise JobError(f"у проекта {name} не включён запуск Claude Code (code.enabled)")
        return ws, proj, reg.code.glob

    def _dirs(self, root: Path, proj: ProjectCode) -> list[Path]:
        """Каталоги запуска из code.dirs: внутри корня, существуют, строго без пересечения со служебными
        каталогами wshub. Нарушение — ошибка реестра для wshub-code."""
        out = []
        for d in proj.dirs:
            p = (root / d).resolve()
            if not (p == root or p.is_relative_to(root)):
                raise RegistryError(f"code.dirs: {d} вне корня проекта {root}")
            if not p.is_dir():
                raise RegistryError(f"code.dirs: каталога нет — {p}")
            if self.hub.protected_kind(p) is not None:
                raise RegistryError(f"code.dirs: {p} пересекается со служебным каталогом wshub")
            # каждый dir — корень рабочего дерева git (замечание 3): иначе allowRead открывает только cwd,
            # а .git остаётся в родителе под закрытым ~/, и git у Code не работает
            if not (p / ".git").is_dir():
                raise RegistryError(f"code.dirs: {p} — не корень git-репозитория (нет каталога .git); "
                                    "каждый dir должен быть рабочим деревом git")
            out.append(p)
        return out

    def plan(self, project: str, task: str, *, model=None, effort=None, max_turns=None, budget_usd=None,
             note=None, prompt_override: str | None = None) -> Plan:
        ws, proj, _glob = self._project(project)
        root = ws.path.resolve()
        dirs = self._dirs(root, proj)

        task_in = Path(task)
        task_abs = (task_in if task_in.is_absolute() else root / task_in).resolve()
        # «внутри dir» — до чтения файла (замечание 13): иначе моделью можно открыть любой читаемый файл
        container = next((d for d in dirs if task_abs == d or task_abs.is_relative_to(d)), None)
        if container is None:
            raise JobError(f"файл ТЗ {task} вне разрешённых каталогов (code.dirs) проекта {project}")
        if not task_abs.is_file():
            raise JobError(f"файл ТЗ не найден: {task}")
        try:
            size = task_abs.stat().st_size
        except OSError as e:
            raise JobError(f"файл ТЗ недоступен: {e}") from None
        if size > TASKFILE_MAX:
            raise JobError(f"файл ТЗ больше 1 МБ ({size} байт)")
        header = taskfile.parse_header(task_abs.read_text(encoding="utf-8"))

        dir_abs = (root / header["dir"]).resolve()
        if dir_abs not in dirs:
            raise JobError(f"dir из шапки ({header['dir']}) не в code.dirs проекта {project}")
        if dir_abs != container:
            raise JobError(f"файл ТЗ {task_abs} лежит вне своего dir {dir_abs}")
        task_rel = task_abs.relative_to(dir_abs).as_posix()
        profile = header["profile"]
        if profile not in proj.profiles:
            raise JobError(f"профиль {profile} не разрешён проекту (profiles = {', '.join(proj.profiles)})")

        model = model or header.get("model") or proj.model_max
        effort = effort or header.get("effort") or proj.effort_max
        max_turns = int(max_turns or header.get("max_turns") or proj.max_turns)
        budget_usd = float(budget_usd if budget_usd is not None else header.get("budget_usd", proj.max_budget_usd))
        self._check_ceilings(proj, model, effort, max_turns, budget_usd)

        # промпт — только из шаблона (п. 2.3, 8): имя из override (resume) или из шапки, литерального текста нет
        prompt_name = prompt_override or header.get("prompt") or profile
        if not taskfile.NAME_RE.match(prompt_name):
            raise JobError(f"имя шаблона промпта недопустимо: {prompt_name}")
        template_path = (root / proj.prompts / f"{prompt_name}.md").resolve()
        if not template_path.is_file():
            raise JobError(f"шаблон промпта не найден: {proj.prompts}/{prompt_name}.md")
        prompt, warnings = taskfile.render(template_path.read_text(encoding="utf-8"),
                                           ticket=header["ticket"], task=task_rel, note=note)
        return Plan(project, root, dir_abs, task_abs, task_rel, profile, prompt_name, model, effort,
                    max_turns, budget_usd, prompt, warnings, proj)

    def _check_ceilings(self, proj: ProjectCode, model: str, effort: str, max_turns: int, budget_usd: float):
        if _order(model, MODELS, "model") < _order(proj.model_min, MODELS, "model_min"):
            raise JobError(f"модель {model} ниже model_min ({proj.model_min})")
        if _order(model, MODELS, "model") > _order(proj.model_max, MODELS, "model_max"):
            raise JobError(f"модель {model} выше model_max ({proj.model_max})")
        if _order(effort, EFFORTS, "effort") > _order(proj.effort_max, EFFORTS, "effort_max"):
            raise JobError(f"effort {effort} выше effort_max ({proj.effort_max})")
        if max_turns > proj.max_turns:
            raise JobError(f"max_turns {max_turns} выше потолка {proj.max_turns}")
        if max_turns < 1:
            raise JobError("max_turns должен быть ≥ 1")
        if budget_usd > proj.max_budget_usd:
            raise JobError(f"budget_usd {budget_usd:g} выше потолка {proj.max_budget_usd:g}")
        if budget_usd <= 0:
            raise JobError("budget_usd должен быть > 0")

    # ---------- хранилище ----------

    def job_dir(self, jid: str) -> Path:
        return self.root / jid

    def read(self, jid: str) -> dict:
        d = read_json(self.job_dir(jid) / "job.json")
        if not d:
            raise JobError(f"задача {jid} не найдена")
        return d

    def write(self, jid: str, data: dict) -> None:
        import json
        atomic_write_text(self.job_dir(jid) / "job.json", json.dumps(data, ensure_ascii=False, indent=1))

    @contextlib.contextmanager
    def _flock(self):
        self.root.mkdir(parents=True, exist_ok=True)
        with open(self.lock_path, "a") as fh:
            try:
                import fcntl
                fcntl.flock(fh, fcntl.LOCK_EX)
            except ImportError:  # pragma: no cover — не POSIX
                pass
            yield

    def _live_jobs(self, project: str | None = None) -> list[dict]:
        out = []
        for jd in sorted(self.root.glob("*/job.json")) if self.root.is_dir() else []:
            d = read_json(jd)
            if not d:
                continue
            if project and d.get("project") != project:
                continue
            if self.effective_state(d) in LIVE:
                out.append(d)
        return out

    def effective_state(self, d: dict) -> str:
        """Состояние с учётом живости процессов: orphaned (надзиратель мёртв, Code жив) и lost (оба мертвы,
        итога нет) вычисляются при чтении (п. 2.5.5)."""
        state = d.get("state", "lost")
        if state in TERMINAL:
            return state
        sup_alive = _alive(d.get("sup_pid"), d.get("sup_start"))
        code_alive = _alive(d.get("pid"), d.get("proc_start"))
        if sup_alive:
            return state
        if code_alive:
            return "orphaned"
        return state if d.get("result") else "lost"

    # ---------- старт ----------

    def start(self, plan: Plan, *, parent: str | None = None, resume: str | None = None,
              now: float | None = None) -> dict:
        import json
        glob = self.hub.registry.get().code.glob
        # claude и его флаги (вызовы подпроцесса) — до flock: не трогают общее состояние, не держат лок
        claude = claude_cli.resolve_claude(glob.claude, dict(os.environ))
        env = claude_cli.environment(dict(os.environ), glob.pass_env)
        flags = claude_cli.detect_flags(claude, self.hub.state_dir, env)
        with self._flock():
            live = self._live_jobs(plan.project)
            if len(live) >= plan.proj.max_parallel:
                raise JobError(f"у проекта {plan.project} уже {len(live)} живых задач "
                               f"(max_parallel = {plan.proj.max_parallel})")
            self._check_daily_budget(plan, live, now)

            jid = new_id(now)
            jdir = self.job_dir(jid)
            jdir.mkdir(parents=True, exist_ok=False)
            data = {
                "id": jid, "project": plan.project, "root": str(plan.root), "dir": str(plan.dir),
                "task": str(plan.task), "task_rel": plan.task_rel, "profile": plan.profile,
                "prompt_name": plan.prompt_name, "model": plan.model, "effort": plan.effort,
                "max_turns": plan.max_turns, "budget_usd": plan.budget_usd, "prefetch": plan.proj.prefetch,
                "max_hours": plan.proj.max_hours, "status_regex": plan.proj.status_regex,
                "state": "starting", "reason": None, "parent": parent, "resume": resume,
                "session_id": resume, "warnings": plan.warnings,
                "created": datetime.fromtimestamp(now or time.time()).astimezone().isoformat(timespec="seconds"),
                "created_ts": now or time.time(),
            }
            sid = resume or str(_uuid4())
            data["session_id"] = sid if not resume else resume
            data["claude"] = claude
            data["flags"] = flags
            protected = [p for p in (self.hub.protected_data + self.hub.protected_code)]
            hook_cmd = [sys.executable, "-m", "wshub.hook"]  # лёгкий запуск хука (замечание 9)
            settings = claude_cli.settings(plan.profile, plan.task_rel, glob, plan.proj, protected,
                                           self.hub.protected_data, hook_cmd, Path.home(), plan.dir)
            claude_cli.write_job_files(jdir, settings, plan.prompt)
            argv = claude_cli.argv(claude, plan.prompt, job_dir=jdir, model=plan.model, effort=plan.effort,
                                   max_turns=plan.max_turns, budget_usd=plan.budget_usd, flags=flags,
                                   session_id=None if resume else sid, resume=resume)
            atomic_write_text(jdir / "argv.json", json.dumps(argv, ensure_ascii=False))
            atomic_write_text(jdir / "env.json", json.dumps(env, ensure_ascii=False))
            self.write(jid, data)

            sup = self._spawn_supervisor(jid)
            data["sup_pid"] = sup
            data["sup_start"] = proc_start(sup)
            self.write(jid, data)
        self.hub._audit_code("code_start", plan.project, jid, params={
            "model": plan.model, "effort": plan.effort, "max_turns": plan.max_turns,
            "budget_usd": plan.budget_usd, "profile": plan.profile})
        return data

    def _check_daily_budget(self, plan: Plan, live: list[dict], now: float | None):
        day = datetime.fromtimestamp(now or time.time()).date()
        spent = 0.0
        seen = set()
        for d in live:
            spent += float(d.get("budget_usd") or 0)
            seen.add(d.get("id"))
        for jd in sorted(self.root.glob("*/job.json")) if self.root.is_dir() else []:
            d = read_json(jd)
            if not d or d.get("project") != plan.project or d.get("id") in seen:
                continue
            ts = d.get("created_ts")
            if not ts or datetime.fromtimestamp(ts).date() != day:
                continue
            res = d.get("result") or {}
            spent += float(res.get("total_cost_usd") if res.get("total_cost_usd") is not None
                           else d.get("budget_usd") or 0)
        if spent + plan.budget_usd > plan.proj.daily_budget_usd:
            raise JobError(f"дневной бюджет проекта исчерпан: уже {spent:g} + {plan.budget_usd:g} > "
                           f"{plan.proj.daily_budget_usd:g} (daily_budget_usd)")

    def _spawn_supervisor(self, jid: str) -> int:
        proc = subprocess.Popen([*claude_cli.wshub_cmd(), "code-run", jid], stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
                                env={**os.environ})
        return proc.pid

    # ---------- снимки, список ----------

    def snapshot(self, jid: str, now: float | None = None) -> dict:
        d = self.read(jid)
        state = self.effective_state(d)
        now = now or time.time()
        elapsed = int(now - (d.get("started") or d.get("created_ts") or now))
        return {"id": jid, "project": d.get("project"), "state": state, "reason": d.get("reason"),
                "task": d.get("task"), "profile": d.get("profile"), "model": d.get("model"),
                "elapsed_s": max(elapsed, 0), "session_id": d.get("session_id"), "parent": d.get("parent"),
                "result": d.get("result"), "warnings": d.get("warnings") or []}

    def list(self, project: str | None = None, limit: int = 20) -> list[dict]:
        rows = []
        for jd in sorted(self.root.glob("*/job.json"), reverse=True) if self.root.is_dir() else []:
            d = read_json(jd)
            if not d or (project and d.get("project") != project):
                continue
            res = d.get("result") or {}
            rows.append({"id": d.get("id"), "project": d.get("project"), "task": d.get("task"),
                         "state": self.effective_state(d), "cost_usd": res.get("total_cost_usd")})
            if len(rows) >= limit:
                break
        return rows

    # ---------- отмена ----------

    def cancel(self, jid: str) -> dict:
        d = self.read(jid)
        state = self.effective_state(d)
        if state in TERMINAL:
            return self.snapshot(jid)
        atomic_write_text(self.job_dir(jid) / "cancel", "1")
        if state == "orphaned":
            # надзиратель мёртв — гасим группу Code сами, но в фоне: code_cancel отвечает сразу (2.7, замечание 15)
            self._spawn_killer(jid)
        return self.snapshot(jid)

    def _spawn_killer(self, jid: str) -> None:
        """Гасим группу Code отдельным процессом `wshub code-kill`, а не os.fork: в MCP-сервере (многопоточный
        async) fork копирует удерживаемый flock-fd и заклинил бы лок. close_fds (по умолчанию) этого не даёт."""
        subprocess.Popen([*claude_cli.wshub_cmd(), "code-kill", jid], stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
                         env={**os.environ})

    def kill_orphaned(self, jid: str) -> dict:
        """Погасить группу Code осиротевшей задачи и записать cancelled (вызывается процессом `code-kill`)."""
        self._kill_group(self.read(jid))
        with self._flock():
            d = self.read(jid)
            # raw-состояние осиротевшей задачи — «running» (надзиратель умер, не обновив); effective_state дал бы
            # «lost», поэтому смотрим на записанное состояние и не трогаем настоящие терминальные итоги
            keep = {"completed", "failed", "max_turns", "max_budget", "timeout", "cancelled"}
            if d.get("state") not in keep:
                d["state"], d["reason"] = "cancelled", "orphaned"
                self.write(jid, d)
        return d

    def _kill_group(self, d: dict) -> None:
        """SIGINT процессу Code → SIGTERM группе → SIGKILL группе; proc_start защищает от повторного pid."""
        pgid, pid = d.get("pgid"), d.get("pid")
        if not pgid or not _alive(pid, d.get("proc_start")):
            return
        t_term, t_kill = cancel_timers()
        for sig, group, wait in ((signal.SIGINT, False, t_term),
                                 (signal.SIGTERM, True, t_kill), (signal.SIGKILL, True, 0)):
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(pgid, sig) if group else os.kill(int(pid), sig)
            if not wait or not _alive(pid, d.get("proc_start")):
                break
            deadline = time.time() + wait
            while time.time() < deadline and _alive(pid, d.get("proc_start")):
                time.sleep(0.2)

    # ---------- resume ----------

    def resume(self, jid: str, prompt: str, *, note=None, model=None, effort=None, max_turns=None,
               budget_usd=None) -> dict:
        d = self.read(jid)
        # жив надзиратель ИЛИ Code родителя — отказ (любое состояние, кроме терминальных и lost; замечание 5):
        # иначе можно поднять второй claude на тот же session_id
        state = self.effective_state(d)
        if state not in TERMINAL and state != "lost":
            raise JobError(f"родитель {jid} в состоянии {state} — продолжить можно только завершённую "
                           "или потерянную (lost) задачу")
        sid = d.get("session_id")
        if not sid:
            raise JobError(f"у задачи {jid} нет session_id — продолжить нельзя")
        # prompt — имя шаблона (не литеральный текст из чата, замечание 6); рендер тем же taskfile.render
        plan = self.plan(d["project"], d["task"], model=model or d.get("model"),
                         effort=effort or d.get("effort"), max_turns=max_turns or d.get("max_turns"),
                         budget_usd=budget_usd if budget_usd is not None else d.get("budget_usd"),
                         note=note, prompt_override=prompt)
        # тот же dir и профиль, что у родителя (п. 2.5.6): задача-потомок могла править свою шапку
        if str(plan.dir) != d.get("dir") or plan.profile != d.get("profile"):
            raise JobError(f"шапка ТЗ изменилась с момента родителя (dir/profile): было "
                           f"{d.get('dir')}/{d.get('profile')}, стало {plan.dir}/{plan.profile}")
        return self.start(plan, parent=jid, resume=sid)


def _alive(pid, started) -> bool:
    return bool(pid) and pid_alive(int(pid), started)


def _uuid4() -> str:
    import uuid
    return str(uuid.uuid4())

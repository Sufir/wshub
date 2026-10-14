"""Задачи Claude Code: хранилище, старт, снимки, отмена, resume (ТЗ WSH-2, п. 2.5); политика выбора модели,
фазы плана и исполнения одной сессии, цепочка звеньев, статистика (ТЗ WSH-3).

Логика не зависит от MCP и проверяется тестами напрямую. Общее с файловым сервером берётся из Hub:
журнал (`_audit`), служебные каталоги (`protected_kind`), `runtime.pid_alive`/`proc_start`.
"""
from __future__ import annotations

import contextlib
import math
import os
import secrets
import shlex
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from . import claude_cli, code_policy, taskfile
from .core import WsError
from .registry import ProjectCode, RegistryError
from .runtime import atomic_write_text, pid_alive, proc_start, read_json

B32 = "abcdefghijklmnopqrstuvwxyz234567"
TASKFILE_MAX = 1024 * 1024
MODELS = ("haiku", "sonnet", "opus", "fable")
EFFORTS = ("low", "medium", "high", "xhigh", "max")
LIVE = ("starting", "prefetch", "running", "cancelling", "orphaned")
# plan_ready — фаза плана завершена и ждёт code_approve/code_replan: терминально для звена, не для цепочки
TERMINAL = ("completed", "failed", "max_turns", "max_budget", "timeout", "cancelled", "lost", "plan_ready")
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
    """Проверенные параметры звена задачи: готово к записи и запуску."""
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
    ticket: str = ""
    size: str | None = None
    risk: str | None = None
    choice: code_policy.Choice | None = None
    phase: str | None = None  # None — без плана; plan | execute — звено цепочки (ТЗ WSH-3, 2.3)
    plan_mode: str = "no"
    budget_total: float = 0.0  # бюджет всей задачи (цепочки); budget_usd — этого звена
    max_turns_total: int = 0

    @property
    def permission_mode(self) -> str:
        return "plan" if self.phase == "plan" else "dontAsk"

    def session_name(self) -> str:
        return f"{self.project}/{self.ticket}" + ("/plan" if self.phase == "plan" else "")


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

    def plan(self, project: str, task: str, *, model=None, effort=None, plan=None, chosen_by=None, reason=None,
             max_turns=None, budget_usd=None, note=None, prompt_override: str | None = None,
             inherit: dict | None = None, channel: str = "mcp") -> Plan:
        """Проверить задачу и выбрать модель/effort/план. chosen_by="operator" — аргументы model/effort/plan
        выбрал оператор (2.2.1); иначе это отступление чата. inherit — job.json родителя при resume.
        channel — откуда пришли аргументы: «cli» (флаги wshub code) или «mcp» (решение 12)."""
        ws, proj, glob = self._project(project)
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
        if chosen_by not in (None, "", "operator", "chat"):
            raise JobError("chosen_by — \"operator\" (выбор оператора) или пусто")
        if channel not in ("cli", "mcp"):
            raise JobError(f"канал выбора {channel!r} не поддерживается")

        max_turns = int(max_turns or header.get("max_turns") or proj.max_turns)
        budget_usd = float(budget_usd if budget_usd is not None else header.get("budget_usd", proj.max_budget_usd))
        self._check_limits(proj, max_turns, budget_usd)
        if inherit is None:
            missing = taskfile.missing_for_profile(header)
            if missing:
                rows = "; ".join(code_policy.profile_rows(glob, proj, profile))
                raise JobError(f"в шапке нет {', '.join(missing)} — по ним политика выбирает модель, effort "
                               f"и нужен ли план (профиль {profile}: {rows}). {code_policy.GUIDE}")
            choice = self._choose(glob, proj, header, profile, model, effort, plan, chosen_by, reason, channel)
        else:
            choice = self._inherit(proj, inherit, model, effort, chosen_by, reason, channel)

        # промпт — только из шаблона (п. 2.3, 8): имя из override (resume) или из шапки, литерального текста нет
        prompt_name = prompt_override or header.get("prompt") or profile
        prompt, warnings = self._render(root, proj, prompt_name, header["ticket"], task_rel, note)
        out = Plan(project, root, dir_abs, task_abs, task_rel, profile, prompt_name, choice.model, choice.effort,
                   max_turns, budget_usd, prompt, warnings, proj, ticket=header["ticket"],
                   size=header.get("size"), risk=header.get("risk"), choice=choice, plan_mode=choice.plan,
                   budget_total=budget_usd, max_turns_total=max_turns)
        if choice.plan != "no":
            # шаблон исполнения проверяем на старте, а не после оплаченной фазы плана (2.6.3)
            self._render(root, proj, proj.execute_prompt, header["ticket"], task_rel,
                         None if choice.plan == "auto" else "проверка шаблона", what="исполнения")
            if choice.plan == "review":  # переделка плана возможна только при review — шаблон нужен заранее
                self._replan_template(root, proj)
            out.phase = "plan"
            out.model, out.effort = choice.plan_model, choice.plan_effort
            out.budget_usd = _cents(budget_usd * proj.plan_budget_share)
            out.max_turns = min(proj.plan_max_turns, max_turns)
            if out.budget_usd <= 0:
                raise JobError(f"бюджет фазы плана {budget_usd:g} × {proj.plan_budget_share:g} меньше цента")
        return out

    def _replan_template(self, root: Path, proj: ProjectCode) -> None:
        path = (root / proj.prompts / f"{REPLAN_PROMPT}.md").resolve()
        if not path.is_file():
            raise JobError(f"шаблон промпта переделки плана не найден: {proj.prompts}/{REPLAN_PROMPT}.md")
        if "{note}" not in path.read_text(encoding="utf-8"):
            raise JobError(f"в шаблоне {proj.prompts}/{REPLAN_PROMPT}.md нет {{note}} — замечание к плану обязательно")

    def _render(self, root: Path, proj: ProjectCode, name: str, ticket: str, task_rel: str, note,
                what: str = "") -> tuple[str, list[str]]:
        if not taskfile.NAME_RE.match(name):
            raise JobError(f"имя шаблона промпта недопустимо: {name}")
        template_path = (root / proj.prompts / f"{name}.md").resolve()
        if not template_path.is_file():
            raise JobError(f"шаблон промпта{' ' + what if what else ''} не найден: {proj.prompts}/{name}.md")
        return taskfile.render(template_path.read_text(encoding="utf-8"), ticket=ticket, task=task_rel, note=note)

    def _choose(self, glob, proj, header, profile, model, effort, plan, chosen_by, reason,
                channel) -> code_policy.Choice:
        """Источники выбора (2.2): аргументы, затем шапка, затем политика; у каждого поля — свой источник
        и канал (решение 12)."""
        arg_src = "operator" if chosen_by == "operator" else "chat"
        head_src = "operator" if header.get("choice") == "operator" else "chat"
        given, chan = {}, {}
        for f, v in (("model", model), ("effort", effort), ("plan", plan)):
            if v:
                given[f], chan[f] = (v, arg_src), channel
            elif header.get(f):
                given[f], chan[f] = (header[f], head_src), "header"
        why = reason or header.get("choice_reason")
        try:
            c = code_policy.choose(glob, proj, profile, header.get("size"), header.get("risk"), given, why)
        except code_policy.PolicyError as e:
            raise JobError(str(e)) from None
        # каналы всех полей не из политики, а не только итогового источника (уточнение решения 12)
        c.channel = _channel(chan.values())
        c.choice_fields = {f: f"{src}:{chan[f]}" for f, (_, src) in given.items()}
        return c

    def _inherit(self, proj, parent: dict, model, effort, chosen_by, reason, channel) -> code_policy.Choice:
        """resume: выбор родителя; явные model/effort — по тем же правилам источника, что у code_start."""
        base = parent.get("choice") or {}
        vals = {"model": parent.get("model"), "effort": parent.get("effort")}
        fields = {"model": "inherited", "effort": "inherited", "plan": "inherited"}
        src = "operator" if chosen_by == "operator" else "chat"
        for f, v in (("model", model), ("effort", effort)):
            if v and v != vals[f]:
                vals[f], fields[f] = v, src
        changed = [f for f in ("model", "effort") if fields[f] in ("operator", "chat")]
        source, why, chan = base.get("source") or "policy", base.get("reason"), base.get("channel")
        cf = dict(base.get("choice_fields") or {})
        if changed:
            if src == "chat":
                try:
                    code_policy._check_bounds(proj, vals["model"] if "model" in changed else None,
                                              vals["effort"] if "effort" in changed else None, "чат")
                except code_policy.PolicyError as e:
                    raise JobError(code_policy._bounds_hint(str(e), "chat")) from None
                why = code_policy.clean_reason(reason)
                if len(why) < code_policy.REASON_MIN:
                    raise JobError(f"чат задал {', '.join(changed)} в обход выбора задачи: укажи причину "
                                   f"(reason, ≥ {code_policy.REASON_MIN} символов) или не меняй их")
            source = src
            cf.update({f: f"{src}:{channel}" for f in changed})
            chan = _channel(x.split(":", 1)[1] for x in cf.values())
            why = code_policy.clean_reason(reason) or why
        return code_policy.Choice(vals["model"], vals["effort"], "no", None, None, source, base.get("row"), why,
                                  fields, chan, cf or None)

    def _check_limits(self, proj: ProjectCode, max_turns: int, budget_usd: float):
        """Бюджеты и ходы — в силе для любого источника выбора (2.2.1); модель и effort проверяет политика."""
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
              chain_root: str | None = None, link: tuple[str, str] | None = None, now: float | None = None) -> dict:
        """Записать и запустить звено. link = (id фазы плана, причина) — переход из plan_ready: под тем же flock
        проверяем, что план всё ещё plan_ready и не утверждён, и отмечаем его (code_approve/replan/auto)."""
        import json
        glob = self.hub.registry.get().code.glob
        # claude и его флаги (вызовы подпроцесса) — до flock: не трогают общее состояние, не держат лок
        claude = claude_cli.resolve_claude(glob.claude, dict(os.environ))
        env = claude_cli.environment(dict(os.environ), glob.pass_env)
        problem = claude_cli.tmpdir_problem(env)  # решение 8: иначе claude не стартует
        if problem:
            raise JobError(problem)
        flags = claude_cli.detect_flags(claude, self.hub.state_dir, env)
        with self._flock():
            if link is not None:
                ld = self.read(link[0])
                if (self.job_dir(link[0]) / "cancel").exists():
                    raise JobError(f"план {link[0]} отменён (code_cancel) — продолжения не будет")
                if ld.get("state") != "plan_ready" or ld.get("next"):
                    raise JobError(f"план {link[0]} в состоянии {ld.get('state')}"
                                   + (f", уже продолжен задачей {ld['next']}" if ld.get("next") else "")
                                   + " — нужен plan_ready")
            live = self._live_jobs(plan.project)
            if len(live) >= plan.proj.max_parallel:
                raise JobError(f"у проекта {plan.project} уже {len(live)} живых задач "
                               f"(max_parallel = {plan.proj.max_parallel})")
            self._check_daily_budget(plan, live, now)

            jid = new_id(now)
            jdir = self.job_dir(jid)
            jdir.mkdir(parents=True, exist_ok=False)
            choice = plan.choice.to_dict() if plan.choice else None
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
                "ticket": plan.ticket, "size": plan.size, "risk": plan.risk, "choice": choice,
                "phase": plan.phase, "plan_mode": plan.plan_mode, "budget_total": plan.budget_total,
                "max_turns_total": plan.max_turns_total, "chain_root": chain_root or jid,
                "session_name": plan.session_name(),
            }
            sid = resume or str(_uuid4())
            data["session_id"] = sid if not resume else resume
            data["claude"] = claude
            data["flags"] = flags
            protected = [p for p in (self.hub.protected_data + self.hub.protected_code)]
            hook_cmd = [sys.executable, "-m", "wshub.hook"]  # лёгкий запуск хука (замечание 9)
            settings = claude_cli.settings(plan.profile, plan.task_rel, glob, plan.proj, protected,
                                           self.hub.protected_data, hook_cmd, Path.home(), plan.dir,
                                           phase="plan" if plan.phase == "plan" else None)
            claude_cli.write_job_files(jdir, settings, plan.prompt)
            argv = claude_cli.argv(claude, plan.prompt, job_dir=jdir, model=plan.model, effort=plan.effort,
                                   max_turns=plan.max_turns, budget_usd=plan.budget_usd, flags=flags,
                                   session_id=None if resume else sid, resume=resume,
                                   permission_mode=plan.permission_mode, name=plan.session_name())
            atomic_write_text(jdir / "argv.json", json.dumps(argv, ensure_ascii=False))
            atomic_write_text(jdir / "env.json", json.dumps(env, ensure_ascii=False))
            self.write(jid, data)

            sup = self._spawn_supervisor(jid)
            data["sup_pid"] = sup
            data["sup_start"] = proc_start(sup)
            self.write(jid, data)
            if link is not None:
                ld = self.read(link[0])
                ld.update(state="completed", reason=link[1], next=jid)
                self.write(link[0], ld)
        c = plan.choice
        self.hub._audit_code("code_start", plan.project, jid, params={
            "model": plan.model, "effort": plan.effort, "max_turns": plan.max_turns,
            "budget_usd": plan.budget_usd, "profile": plan.profile, "phase": plan.phase, "plan": plan.plan_mode,
            "model_source": c.source if c else None, **({"channel": c.channel} if c and c.channel else {}),
            "choice_fields": c.choice_fields or {} if c else None,
            "policy_row": c.row if c else None,
            "reason": c.reason if c else None, "parent": parent})
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
            cost = phase_cost(d)  # стоимость звена один раз: у resume total_cost_usd накопительный (решение 6)
            spent += float(cost if cost is not None else d.get("budget_usd") or 0)
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
        snap = {"id": jid, "project": d.get("project"), "state": state, "reason": d.get("reason"),
                "task": d.get("task"), "profile": d.get("profile"), "model": d.get("model"),
                "elapsed_s": max(elapsed, 0), "session_id": d.get("session_id"), "parent": d.get("parent"),
                "result": d.get("result"), "warnings": d.get("warnings") or []}
        if d.get("reason") in REASON_HINT:
            snap["reason_hint"] = REASON_HINT[d["reason"]]
        if d.get("choice"):
            snap["choice"] = d["choice"]
        if d.get("phase"):
            snap.update(phase=d["phase"], plan_mode=d.get("plan_mode"), next=d.get("next"),
                        chain_error=d.get("chain_error"))
            if d.get("chain_root") == jid:
                # корень цепочки (2.3.6): состояние и фаза — активного звена, плюс все звенья
                links = self.chain(jid)
                active = links[-1]
                snap.update(own_state=state, state=self._chain_state(active), phase=active.get("phase"),
                            active=active["id"], chain=[self._link_row(x) for x in links],
                            cost_usd=_sum(phase_cost(x) for x in links))
        return snap

    def _link_row(self, d: dict) -> dict:
        res = d.get("result") or {}
        return {"id": d["id"], "phase": d.get("phase"), "state": self.effective_state(d), "reason": d.get("reason"),
                "model": d.get("model"), "effort": d.get("effort"), "cost_usd": phase_cost(d),
                "turns": res.get("num_turns")}

    def _chain_state(self, active: dict) -> str:
        st = self.effective_state(active)
        if st == "plan_ready" and active.get("plan_mode") == "auto" and not active.get("chain_error") \
                and _alive(active.get("sup_pid"), active.get("sup_start")):
            return "starting"  # план готов, живой надзиратель сам запускает исполнение
        if st == "completed" and active.get("next"):
            return "starting"  # звено утверждено, следующее ещё не записано
        return st

    def chain(self, root: str) -> list[dict]:
        """Звенья цепочки по порядку создания: фаза плана, переделки плана, исполнение, resume."""
        out = []
        for jd in self.root.glob("*/job.json") if self.root.is_dir() else []:
            d = read_json(jd)
            if d and (d.get("chain_root") == root or d.get("id") == root):
                out.append(d)
        return sorted(out, key=lambda x: (x.get("created_ts") or 0, x.get("id")))

    def list(self, project: str | None = None, limit: int = 20) -> list[dict]:
        rows = []
        for jd in sorted(self.root.glob("*/job.json"), reverse=True) if self.root.is_dir() else []:
            d = read_json(jd)
            if not d or (project and d.get("project") != project):
                continue
            rows.append({"id": d.get("id"), "project": d.get("project"), "task": d.get("task"),
                         "state": self.effective_state(d), "cost_usd": phase_cost(d), "phase": d.get("phase")})
            if len(rows) >= limit:
                break
        return rows

    # ---------- отмена ----------

    def cancel(self, jid: str) -> dict:
        d = self.read(jid)
        target = d
        if d.get("phase") and d.get("chain_root") == jid:
            target = self.chain(jid)[-1]  # корень цепочки — отменяем активное звено (2.3.6)
        tid = target["id"]
        state = self.effective_state(target)
        if state == "plan_ready":
            # цепочка стоит на плане: отмена плана — отмена цепочки (исполнения не будет)
            with self._flock():
                t = self.read(tid)
                if t.get("state") == "plan_ready" and not t.get("next"):
                    t.update(state="cancelled", reason="cancel")
                    self.write(tid, t)
            return self.snapshot(jid)
        if state in TERMINAL:
            return self.snapshot(jid)
        atomic_write_text(self.job_dir(tid) / "cancel", "1")
        if state == "orphaned":
            # надзиратель мёртв — гасим группу Code сами, но в фоне: code_cancel отвечает сразу (2.7, замечание 15)
            self._spawn_killer(tid)
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
               budget_usd=None, chosen_by=None, reason=None) -> dict:
        d = self.read(jid)
        if d.get("phase") == "plan":
            # resume поднял бы dontAsk с правами профиля без утверждения плана
            raise JobError(f"{jid} — фаза плана: продолжить можно только code_approve или code_replan")
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
        plan = self.plan(d["project"], d["task"], model=model, effort=effort, chosen_by=chosen_by, reason=reason,
                         max_turns=max_turns or d.get("max_turns"),
                         budget_usd=budget_usd if budget_usd is not None else d.get("budget_usd"),
                         note=note, prompt_override=prompt, inherit=d)
        # тот же dir и профиль, что у родителя (п. 2.5.6): задача-потомок могла править свою шапку
        if str(plan.dir) != d.get("dir") or plan.profile != d.get("profile"):
            raise JobError(f"шапка ТЗ изменилась с момента родителя (dir/profile): было "
                           f"{d.get('dir')}/{d.get('profile')}, стало {plan.dir}/{plan.profile}")
        # size/risk — тоже: resume наследует план родителя, иначе задача L/high пошла бы без плана (решение 10)
        if (plan.size, plan.risk) != (d.get("size"), d.get("risk")):
            raise JobError(f"шапка ТЗ изменилась с момента родителя (size/risk): было "
                           f"{d.get('size')}/{d.get('risk')}, стало {plan.size}/{plan.risk} — новая задача "
                           "через code_start, план выберет политика")
        return self.start(plan, parent=jid, resume=sid, chain_root=d.get("chain_root") or jid)

    # ---------- фазы плана и исполнения (ТЗ WSH-3, 2.3) ----------

    def _plan_ready(self, jid: str) -> dict:
        d = self.read(jid)
        if d.get("phase") and d.get("chain_root") == jid:
            d = self.chain(jid)[-1]  # по корню цепочки — активное звено, как code_cancel и code_plan
            jid = d["id"]
        if d.get("phase") != "plan":
            raise JobError(f"{jid} — не фаза плана (утвердить или переделать можно только план)")
        state = self.effective_state(d)
        if state != "plan_ready" or d.get("next"):
            raise JobError(f"план {jid} в состоянии {state}" + (f", уже продолжен задачей {d['next']}"
                                                                 if d.get("next") else "") + " — нужен plan_ready")
        return d

    def _remaining(self, d: dict) -> tuple[float, int]:
        """Остаток бюджета и ходов задачи после всех звеньев цепочки (стоимость звена — разностью)."""
        links = self.chain(d.get("chain_root") or d["id"])
        spent = _sum(phase_cost(x) for x in links)
        turns = sum(int((x.get("result") or {}).get("num_turns") or 0) for x in links)
        return _cents(float(d.get("budget_total") or d.get("budget_usd") or 0) - spent), \
            int(d.get("max_turns_total") or d.get("max_turns") or 0) - turns

    def _link_plan(self, d: dict, phase: str, note, template: str | None = None) -> Plan:
        """Звено цепочки из job.json фазы плана: те же проект, dir, ТЗ, профиль и выбор; бюджет — остаток."""
        ws, proj, _glob = self._project(d["project"])
        root = ws.path.resolve()
        dir_abs = Path(d["dir"])
        if dir_abs not in self._dirs(root, proj):
            raise JobError(f"dir задачи {dir_abs} больше не в code.dirs проекта {d['project']}")
        if d["profile"] not in proj.profiles:
            raise JobError(f"профиль {d['profile']} больше не разрешён проекту (profiles = {', '.join(proj.profiles)})")
        budget, turns = self._remaining(d)
        choice = code_policy.Choice(**d["choice"])
        template = template or proj.execute_prompt
        if phase == "plan":
            budget = min(budget, _cents(float(d["budget_total"]) * proj.plan_budget_share))
            turns = min(turns, proj.plan_max_turns)
            model, effort = choice.plan_model, choice.plan_effort
        else:
            model, effort = choice.model, choice.effort
        if budget <= 0:
            raise JobError(f"бюджет задачи исчерпан: остаток {budget:g} из {d.get('budget_total')}")
        if turns < 1:
            raise JobError(f"ходы задачи исчерпаны: из {d.get('max_turns_total')} не осталось")
        prompt, warnings = self._render(root, proj, template, d.get("ticket") or "", d["task_rel"], note)
        return Plan(d["project"], root, dir_abs, Path(d["task"]), d["task_rel"], d["profile"], template, model,
                    effort, turns, budget, prompt, warnings, proj, ticket=d.get("ticket") or "",
                    size=d.get("size"), risk=d.get("risk"), choice=choice, phase=phase,
                    plan_mode=d.get("plan_mode") or "no", budget_total=float(d["budget_total"]),
                    max_turns_total=int(d["max_turns_total"]))

    def approve(self, jid: str, note=None, *, auto: bool = False) -> dict:
        """Запуск фазы исполнения после plan_ready: --resume той же сессии, dontAsk, модель исполнения,
        остаток бюджета. auto=True — переход сам, из надзирателя фазы плана (plan = auto)."""
        d = self._plan_ready(jid)
        plan = self._link_plan(d, "execute", note)
        return self.start(plan, parent=d["id"], resume=d["session_id"], chain_root=d.get("chain_root") or d["id"],
                          link=(d["id"], "auto" if auto else "approved"))

    def replan(self, jid: str, note: str) -> dict:
        """Новая фаза плана в той же сессии: --permission-mode plan, шаблон replan с {note}."""
        if not (taskfile.check_note(note) or "").strip():
            raise JobError("note обязателен: что исправить в плане (одна строка, ≤ 300 символов)")
        d = self._plan_ready(jid)
        ws, proj, _glob = self._project(d["project"])
        self._replan_template(ws.path.resolve(), proj)
        plan = self._link_plan(d, "plan", note, REPLAN_PROMPT)
        return self.start(plan, parent=d["id"], resume=d["session_id"], chain_root=d.get("chain_root") or d["id"],
                          link=(d["id"], "replanned"))

    def plan_text(self, jid: str) -> tuple[str, Path]:
        d = self.read(jid)
        if d.get("phase") == "plan" and d.get("chain_root") == jid:
            plans = [x for x in self.chain(jid) if x.get("phase") == "plan"]
            d = next((x for x in reversed(plans) if (self.job_dir(x["id"]) / "plan.md").is_file()), d)
        if d.get("phase") != "plan":
            raise JobError(f"{jid} — не фаза плана")
        path = self.job_dir(d["id"]) / "plan.md"
        if not path.is_file():
            raise JobError(f"плана у {d['id']} нет (состояние {self.effective_state(d)})")
        return path.read_text(encoding="utf-8", errors="replace"), path

    def result_view(self, job: str) -> dict:
        """Итог задачи для code_result и wshub code result: у корня цепочки — все звенья, стоимость обеих фаз
        и итог активного звена; строка, чтобы открыть сессию в терминале (2.5.2)."""
        snap = self.snapshot(job)
        d = self.read(snap.get("active") or job)
        jdir = self.job_dir(d["id"])
        sid = d.get("session_id")
        out = {"job": job, "project": snap.get("project"), "state": snap["state"], "reason": d.get("reason"),
               "choice": snap.get("choice"), "result": d.get("result") or {},
               **({"reason_hint": REASON_HINT[d["reason"]]} if d.get("reason") in REASON_HINT else {}),
               "files": {"events": str(jdir / "events.jsonl"), "stderr": str(jdir / "stderr.log"),
                         "task": d.get("task")}}
        if snap.get("chain"):
            out.update(phase=snap.get("phase"), active=snap.get("active"), chain=snap["chain"],
                       cost_usd=snap.get("cost_usd"))
        plan_md = next((self.job_dir(x["id"]) / "plan.md" for x in reversed(snap.get("chain") or [d])
                        if (self.job_dir(x["id"]) / "plan.md").is_file()), None)
        if plan_md:
            out["files"]["plan"] = str(plan_md)
        if sid and d.get("dir"):
            out["open_in_terminal"] = f"cd {shlex.quote(d['dir'])} && claude --resume {sid}"
        return out

    # ---------- статистика (2.4.5) ----------

    def stats(self, project: str | None = None, days: int = 30, now: float | None = None) -> list[dict]:
        """По строке политики и источнику: задачи (цепочки), средние стоимость и ходы, доля completed, доля
        планов, отправленных на replan. Каждое звено считается один раз (стоимость — разностью)."""
        since = (now or time.time()) - days * 86400
        chains: dict[str, list[dict]] = {}
        for jd in self.root.glob("*/job.json") if self.root.is_dir() else []:
            d = read_json(jd)
            if not d or (project and d.get("project") != project):
                continue
            chains.setdefault(d.get("chain_root") or d.get("id"), []).append(d)
        agg: dict[tuple, dict] = {}
        for links in chains.values():
            links.sort(key=lambda x: (x.get("created_ts") or 0, x.get("id")))
            first = links[0]
            if (first.get("created_ts") or 0) < since:
                continue
            c = first.get("choice") or {}
            key = (first.get("project"), c.get("row") or "—", c.get("source") or "—", c.get("channel") or "—")
            a = agg.setdefault(key, {"tasks": 0, "cost": 0.0, "turns": 0, "completed": 0, "plans": 0,
                                     "replanned": 0})
            a["tasks"] += 1
            a["cost"] += _sum(phase_cost(x) for x in links)
            a["turns"] += sum(int((x.get("result") or {}).get("num_turns") or 0) for x in links)
            a["completed"] += self.effective_state(links[-1]) == "completed"
            plans = [x for x in links if x.get("phase") == "plan"]
            a["plans"] += len(plans)
            a["replanned"] += sum(x.get("reason") == "replanned" for x in plans)
        return [{"project": k[0], "row": k[1], "source": k[2], "channel": k[3], "tasks": a["tasks"],
                 "avg_cost_usd": round(a["cost"] / a["tasks"], 4), "avg_turns": round(a["turns"] / a["tasks"], 1),
                 "completed_share": round(a["completed"] / a["tasks"], 3),
                 "replan_share": round(a["replanned"] / a["plans"], 3) if a["plans"] else None}
                for k, a in sorted(agg.items())]


REPLAN_PROMPT = "replan"
# подсказки к коду причины — в снимке и итоге рядом с reason
REASON_HINT = {"no_plan": "план ищется только в ~/.claude/plans/: настройка plansDirectory в "
                          ".claude/settings*.json проекта не поддерживается"}  # решение 13


def _channel(chans) -> str | None:
    """Каналы через «+» в порядке cli, mcp, header (решение 12)."""
    got = set(chans)
    return "+".join(c for c in code_policy.CHANNELS if c in got) or None


def phase_cost(d: dict) -> float | None:
    """Стоимость звена: phase_cost_usd из итога (разность накопительного total_cost_usd с предыдущим звеном
    сессии, решение 6), для старых задач — total_cost_usd. Нет итога — None."""
    res = d.get("result") or {}
    for k in ("phase_cost_usd", "total_cost_usd"):
        if isinstance(res.get(k), (int, float)):
            return float(res[k])
    return None


def _sum(values) -> float:
    return round(sum(v for v in values if v is not None), 6)


def _cents(v: float) -> float:
    """Бюджет звена — вниз до цента: --max-budget-usd не превышает остаток."""
    return math.floor(round(v * 100, 6)) / 100


def _alive(pid, started) -> bool:
    return bool(pid) and pid_alive(int(pid), started)


def _uuid4() -> str:
    import uuid
    return str(uuid.uuid4())

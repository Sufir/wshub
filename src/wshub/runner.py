"""Надзиратель задачи Claude Code: `wshub code-run <id>` (ТЗ WSH-2, п. 2.5.3).

Запускается в своём сеансе (start_new_session). prefetch силами хаба, запуск Code в собственной группе
процессов, слежение за отменой и пределом времени, разбор итога, снимок git, запись result.json и job.json.
Фаза плана (ТЗ WSH-3, 2.3): копия плана в plan.md, состояние plan_ready, при plan = auto — запуск исполнения.
"""
from __future__ import annotations

import hashlib
import json
import os
import signal
import subprocess
import time
from datetime import datetime
from pathlib import Path

from . import gitsafe
from .jobs import JobError, Jobs, cancel_timers
from .runtime import atomic_write_text, proc_start

PREFETCH_TIMEOUT = 120
POLL = 1.0
RESULT_MAX = 8 * 1024
STATUS_LINES = 200
PLAN_MAX = 200 * 1024  # байт: копия плана в каталоге задачи


def _clip_bytes(text: str, limit: int) -> str:
    """Обрезка по БАЙТАМ UTF-8 (замечание 17): кириллица — 2 байта, символьный предел ломал бы лимит."""
    b = (text or "").encode("utf-8")
    return text if len(b) <= limit else b[:limit].decode("utf-8", "ignore")


def _map_state(subtype: str | None, is_error: bool) -> tuple[str, str | None]:
    if subtype == "success":
        return "completed", None
    if subtype and "max_turns" in subtype:
        return "max_turns", subtype
    if subtype and ("budget" in subtype or "cost" in subtype):
        return "max_budget", subtype
    return "failed", subtype or ("is_error" if is_error else "no_result")


def _tail_result(events_path: Path) -> dict | None:
    last = None
    try:
        for line in events_path.read_text(encoding="utf-8").splitlines():
            try:
                e = json.loads(line)
            except ValueError:
                continue
            if isinstance(e, dict) and e.get("type") == "result":
                last = e
    except OSError:
        return None
    return last


PLAN_TOOLS = ("Write", "Edit", "MultiEdit")


def _plan_source(events_path: Path, plans_dir: Path, since: float | None = None) -> Path | None:
    """Файл плана (решение 7): путь из последнего успешного вызова Write (или Edit того же файла — переделка
    плана в той же сессии) внутрь ~/.claude/plans/ в потоке событий. Путь пишет модель — принимаем только .md,
    который после resolve() лежит прямо в каталоге планов, вызов с парным tool_result без ошибки и файл,
    изменённый не раньше старта звена (иначе — чужой или прошлый план)."""
    plans = plans_dir.resolve()
    uses: list[tuple[str, Path]] = []
    ok: set[str] = set()
    try:
        lines = events_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    for line in lines:
        try:
            e = json.loads(line)
        except ValueError:
            continue
        msg = e.get("message") if isinstance(e, dict) else None
        content = msg.get("content") if isinstance(msg, dict) else None
        for c in content if isinstance(content, list) else []:
            if not isinstance(c, dict):
                continue
            if c.get("type") == "tool_result" and not c.get("is_error") and isinstance(c.get("tool_use_id"), str):
                ok.add(c["tool_use_id"])
            if not (e.get("type") == "assistant" and c.get("type") == "tool_use" and c.get("name") in PLAN_TOOLS):
                continue
            fp = (c.get("input") or {}).get("file_path") if isinstance(c.get("input"), dict) else None
            if not isinstance(fp, str) or not os.path.isabs(fp) or not isinstance(c.get("id"), str):
                continue
            p = Path(fp).resolve()
            if p.suffix == ".md" and p.parent == plans:
                uses.append((c["id"], p))
    good = [p for uid, p in uses if uid in ok]
    if not good or not good[-1].is_file():
        return None
    if since is not None and good[-1].stat().st_mtime < since - 1:
        return None
    return good[-1]


def _git_snapshot(git: str, cwd: Path, environ: dict) -> dict:
    out: dict = {}
    try:
        r = gitsafe.run(git, cwd, ["status", "--short", "--ignore-submodules=all"], environ, 30)
        out["status"] = "\n".join(r.stdout.splitlines()[:STATUS_LINES])
        r = gitsafe.run(git, cwd, ["log", "-1", "--oneline"], environ, 30)
        out["head"] = r.stdout.strip()
    except gitsafe.GitUnsafe as e:
        out["error"] = f"{e.reason}: {e}"
    except (OSError, subprocess.TimeoutExpired) as e:
        out["error"] = str(e)
    return out


def _find_status(text: str, regex: str | None) -> str | None:
    if not regex:
        return None
    import re
    try:
        m = re.search(regex, text)
    except re.error:
        return None
    return m.group(1) if m and m.groups() else (m.group(0) if m else None)


class Supervisor:
    def __init__(self, hub, jid: str):
        self.hub = hub
        self.jobs = Jobs(hub)
        self.jid = jid
        self.jdir = self.jobs.job_dir(jid)
        self.d = self.jobs.read(jid)

    def _save(self, **kw):
        self.d.update(kw)
        self.jobs.write(self.jid, self.d)

    def _cancelled(self) -> bool:
        return (self.jdir / "cancel").exists()

    def run(self) -> None:
        # перечитываем job.json под тем же flock, что держит старт: так гарантированно видим sup_pid/sup_start,
        # записанные родителем после Popen (иначе наш _save затёр бы их → ложный orphaned/lost, замечание 20)
        with self.jobs._flock():
            self.d = self.jobs.read(self.jid)
        git = None
        environ = dict(os.environ)
        cwd = Path(self.d["dir"])
        if self.d.get("prefetch"):
            self._save(state="prefetch")
            git = self._git()
            if not self._prefetch(git, cwd, environ):
                return
        self._run_code(cwd, environ)
        self._finish(git or self._git(), cwd, environ)

    def _git(self) -> str:
        import shutil
        return shutil.which("git") or "git"

    def _prefetch(self, git: str, cwd: Path, environ: dict) -> bool:
        try:
            r = gitsafe.run(git, cwd, ["fetch", "--all", "--prune", "--no-recurse-submodules"], environ,
                            PREFETCH_TIMEOUT, ssh_agent=True)
            (self.jdir / "prefetch.log").write_text(r.stdout + r.stderr, encoding="utf-8")
            if r.returncode != 0:
                self._fail("prefetch", f"git fetch вернул {r.returncode}")
                return False
        except gitsafe.GitUnsafe as e:
            self._fail(e.reason, str(e))
            return False
        except (OSError, subprocess.TimeoutExpired) as e:
            self._fail("prefetch", str(e))
            return False
        return True

    def _fail(self, reason: str, message: str) -> None:
        self.d.update(state="failed", reason=reason, error=message,
                      finished=datetime.now().astimezone().isoformat(timespec="seconds"))
        self._journal()  # журнал до записи терминального состояния (замечание 10)
        self.jobs.write(self.jid, self.d)

    def _run_code(self, cwd: Path, environ: dict):
        argv = json.loads((self.jdir / "argv.json").read_text(encoding="utf-8"))
        env = json.loads((self.jdir / "env.json").read_text(encoding="utf-8"))
        events = (self.jdir / "events.jsonl").open("w", encoding="utf-8")
        stderr = (self.jdir / "stderr.log").open("w", encoding="utf-8")
        proc = subprocess.Popen(argv, cwd=os.fspath(cwd), env=env, stdin=subprocess.DEVNULL,
                                stdout=events, stderr=stderr, start_new_session=True)
        pgid = os.getpgid(proc.pid)
        self._save(state="running", pid=proc.pid, pgid=pgid, proc_start=proc_start(proc.pid),
                   started=time.time())
        max_s = float(self.d.get("max_hours") or 4) * 3600
        escalated = None
        while proc.poll() is None:
            time.sleep(POLL)
            over = time.time() - self.d["started"] > max_s
            if (self._cancelled() or over) and escalated is None:
                self._save(state="cancelling", reason="timeout" if over else "cancel")
                escalated = self._escalate(proc)
        proc.wait()
        events.close()
        stderr.close()

    def _escalate(self, proc) -> bool:
        """SIGINT процессу Code → пауза → SIGTERM его группе → пауза → SIGKILL группе (п. 2.5.4)."""
        pid, pgid = proc.pid, self.d["pgid"]
        t_term, t_kill = cancel_timers()
        steps = ((signal.SIGINT, False, t_term), (signal.SIGTERM, True, t_kill), (signal.SIGKILL, True, 0))
        for sig, group, wait in steps:
            try:
                os.killpg(pgid, sig) if group else os.kill(pid, sig)
            except (ProcessLookupError, PermissionError):
                return True
            if not wait:
                break
            deadline = time.time() + wait
            while time.time() < deadline:
                if proc.poll() is not None:
                    return True
                time.sleep(0.2)
        return True

    def _finish(self, git: str, cwd: Path, environ: dict):
        res_event = _tail_result(self.jdir / "events.jsonl") or {}
        denials = res_event.get("permission_denials") or []
        result = {
            "subtype": res_event.get("subtype"),
            "session_id": res_event.get("session_id") or self.d.get("session_id"),
            "total_cost_usd": res_event.get("total_cost_usd"),
            "num_turns": res_event.get("num_turns"),
            "duration_ms": res_event.get("duration_ms"),
            "is_error": res_event.get("is_error"),
            "permission_denials_count": len(denials),
            "permission_denials": denials[:10],
            "result": _clip_bytes(res_event.get("result") or "", RESULT_MAX),
            "git": _git_snapshot(git, cwd, environ),
        }
        if self.d.get("state") == "cancelling":
            state, reason = ("timeout", "max_hours") if self.d.get("reason") == "timeout" else ("cancelled", "cancel")
        elif not res_event:
            state, reason = "failed", "no_result"
        else:
            state, reason = _map_state(res_event.get("subtype"), res_event.get("is_error"))
        result["session_id"] = result["session_id"] or self.d.get("session_id")
        result["taskfile_status"] = self._taskfile_status(cwd)
        result["phase_cost_usd"] = self._phase_cost(result.get("total_cost_usd"))
        if self.d.get("phase") == "plan" and state == "completed":
            if self._cancelled():  # отмена пришла после выхода claude — план не идёт дальше (ни в plan_ready)
                state, reason = "cancelled", "cancel"
            else:
                state, reason = self._save_plan(result)
        atomic_write_text(self.jdir / "result.json", json.dumps(result, ensure_ascii=False, indent=1))
        # журнал пишем до терминального состояния в job.json: читатель, увидевший терминал, уже видит строку
        self.d.update(state=state, reason=reason, session_id=result["session_id"], result=result,
                      finished=datetime.now().astimezone().isoformat(timespec="seconds"))
        self._journal()
        self.jobs.write(self.jid, self.d)
        if state == "plan_ready" and self.d.get("plan_mode") == "auto":
            self._auto_execute()

    def _phase_cost(self, total) -> float | None:
        """Стоимость звена (решение 6): после --resume total_cost_usd накопительный за сессию (факт PoC 0.3.3),
        поэтому вычитаем накопленное к концу предыдущего звена той же сессии."""
        if not isinstance(total, (int, float)):
            return None
        prev = None
        parent, sid, hops = self.d.get("parent"), self.d.get("session_id"), 0
        # ближайший предок той же сессии с числовым итогом: у звена без итога (lost, no_result) его нет
        while self.d.get("resume") and parent and hops < 100:
            try:
                pd = self.jobs.read(parent)
            except JobError:
                break
            if pd.get("session_id") != sid:
                break
            prev = (pd.get("result") or {}).get("total_cost_usd")
            if isinstance(prev, (int, float)):
                break
            prev, parent, hops = None, pd.get("parent"), hops + 1
        if isinstance(prev, (int, float)) and 0 <= prev <= total:
            return round(total - prev, 6)
        return float(total)

    def _save_plan(self, result: dict) -> tuple[str, str | None]:
        """Копия плана в plan.md каталога задачи (≤ 200 КБ). Нет вызова Write в каталог планов — no_plan."""
        try:
            env = json.loads((self.jdir / "env.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            env = {}
        base = Path(env["CLAUDE_CONFIG_DIR"]) if env.get("CLAUDE_CONFIG_DIR") else \
            Path(env.get("HOME") or Path.home()) / ".claude"
        src = _plan_source(self.jdir / "events.jsonl", base / "plans", self.d.get("started"))
        if src is None:
            return "failed", "no_plan"
        try:
            with src.open("rb") as fh:
                data = fh.read(PLAN_MAX + 1)
        except OSError:
            return "failed", "no_plan"
        if len(data) > PLAN_MAX:
            data = data[:PLAN_MAX]
            self.d.setdefault("warnings", []).append("план длиннее 200 КБ — в plan.md обрезан")
        atomic_write_text(self.jdir / "plan.md", data.decode("utf-8", "ignore"))
        result.update(plan_file=str(src), plan_bytes=len(data))
        return "plan_ready", None

    def _auto_execute(self) -> None:
        """plan = auto (2.3.4): исполнение тем же путём, что code_approve — под тем же flock, с проверками
        max_parallel и дневного бюджета. Не вышло — план остаётся plan_ready с причиной, решает человек."""
        try:
            child = self.jobs.approve(self.jid, auto=True)
            self.hub._audit_code("code_approve", self.d.get("project"), self.jid, params={"auto": True,
                                                                                         "next": child["id"]})
        except Exception as e:  # noqa: BLE001 — любой отказ оставляет план ждать решения
            with self.jobs._flock():
                d = self.jobs.read(self.jid)
                if d.get("state") == "plan_ready" and not d.get("next"):
                    d["chain_error"] = str(e)[:500]
                    self.jobs.write(self.jid, d)
            self.hub._audit_code("code_approve", self.d.get("project"), self.jid, status="error",
                                 error=str(e), params={"auto": True})

    def _taskfile_status(self, cwd: Path) -> str | None:
        """Статус из ФАЙЛА ТЗ (а не из текста Code): тот же путь, внутри dir, ≤ 1 МБ (замечание 8)."""
        regex = self.d.get("status_regex")
        task = self.d.get("task")
        if not regex or not task:
            return None
        p = Path(task)
        try:
            if not p.resolve().is_relative_to(cwd.resolve()) or p.stat().st_size > 1024 * 1024:
                return None
            return _find_status(p.read_text(encoding="utf-8", errors="replace"), regex)
        except OSError:
            return None

    def _journal(self):
        res = self.d.get("result") or {}
        sha = hashlib.sha256((self.d.get("prompt") or "").encode()).hexdigest()
        try:
            prompt = (self.jdir / "prompt.txt").read_text(encoding="utf-8")
            sha = hashlib.sha256(prompt.encode()).hexdigest()
        except OSError:
            pass
        self.hub._audit_code("code_finish", self.d.get("project"), self.jid, prompt_sha=sha,
                             status="ok" if self.d.get("state") in ("completed", "plan_ready") else "error",
                             error=self.d.get("error"), params={
                                 "state": self.d.get("state"), "reason": self.d.get("reason"),
                                 "cost_usd": res.get("phase_cost_usd", res.get("total_cost_usd")),
                                 "session_cost_usd": res.get("total_cost_usd"), "num_turns": res.get("num_turns"),
                                 "session_id": res.get("session_id"), "phase": self.d.get("phase")})


def run_job(hub, jid: str) -> int:
    # POSIX сохраняет SIG_IGN при exec: если сервер игнорировал SIGCHLD, надзиратель унаследовал бы игнор и
    # его subprocess (git fetch, claude) получали бы ECHILD и код 0. Возвращаем штатное поведение.
    try:
        signal.signal(signal.SIGCHLD, signal.SIG_DFL)
    except (AttributeError, ValueError, OSError):  # pragma: no cover — не POSIX
        pass
    try:
        Supervisor(hub, jid).run()
        return 0
    except Exception as e:  # noqa: BLE001 — надзиратель не должен падать молча
        try:
            jobs = Jobs(hub)
            d = jobs.read(jid)
            d.update(state="failed", reason="supervisor", error=str(e)[:500])
            jobs.write(jid, d)
        except Exception:  # noqa: BLE001
            pass
        return 1

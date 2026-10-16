"""Подкоманды wshub code*: сервер wshub-code, терминальный CLI, надзиратель, хук (ТЗ WSH-2, п. 2.1)."""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

from .core import Hub, WsError
from .registry import RegistryError
from .taskfile import TaskError

TERMINAL = ("completed", "failed", "max_turns", "max_budget", "timeout", "cancelled", "lost", "plan_ready")
USER_ERR = (WsError, TaskError, RegistryError, OSError)


def home_cwd() -> None:
    """Сервер и надзиратель работают из домашнего каталога: каталог, из которого их запустили, может быть удалён,
    и тогда любой git без -C падает «Unable to read current working directory» (ревью WSH-3.4, п. 11)."""
    import os
    try:
        os.chdir(os.path.expanduser("~"))
    except OSError:
        os.chdir("/")


def code_main(cmd: str, rest: list[str], config: Path, state: Path) -> int:
    if cmd in ("code-run", "code-kill") or (cmd == "code" and not rest):
        home_cwd()
    if cmd == "code-hook":
        from .hook import main as hook_main
        return hook_main(rest)
    if cmd == "code-run":
        if len(rest) != 1:
            print("wshub code-run <job> — служебная команда надзирателя", file=sys.stderr)
            return 2
        from .runner import run_job
        return run_job(Hub(config, state), rest[0])
    if cmd == "code-kill":  # служебная: гашение группы Code осиротевшей задачи (из code_cancel)
        if len(rest) != 1:
            print("wshub code-kill <job> — служебная команда", file=sys.stderr)
            return 2
        from .jobs import Jobs
        Jobs(Hub(config, state)).kill_orphaned(rest[0])
        return 0
    # cmd == "code"
    if not rest:
        return _serve(config, state)
    sub, args = rest[0], rest[1:]
    if sub not in ("start", "status", "result", "stop", "stats"):
        print("wshub code [start|status|result|stop|stats] (без аргументов — MCP-сервер wshub-code)",
              file=sys.stderr)
        return 2
    hub = Hub(config, state)
    try:
        return {"start": _start, "status": _status, "result": _result, "stop": _stop, "stats": _stats}[sub](
            hub, args)
    except USER_ERR as e:
        hub._audit_code(f"cli_code_{sub}", None, None, status="error", error=str(e))
        print(f"wshub code: отказ: {e}", file=sys.stderr)  # без трассировки (замечание 14)
        return 1


def _reap(signum=None, frame=None) -> None:
    """Снять завершённых детей (надзирателей), не теряя их код выхода. SIG_IGN здесь нельзя: он наследуется
    надзирателем через fork+exec (POSIX сохраняет SIG_IGN при exec) и обнулил бы коды его subprocess."""
    import os
    while True:
        try:
            pid, _ = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            return
        if pid == 0:
            return


def _serve(config: Path, state: Path) -> None:
    import signal

    from .code_server import build_code_server
    # перехватываемый обработчик (не SIG_IGN): при exec надзирателя сбрасывается в SIG_DFL — утечки нет
    try:
        signal.signal(signal.SIGCHLD, _reap)
    except (AttributeError, ValueError, OSError):  # pragma: no cover — не POSIX
        pass
    hub = Hub(config, state)
    hub.runtime.start()
    hub.publish()
    build_code_server(hub).run()
    return 0


def _parse(args: list[str]) -> tuple[list[str], dict]:
    """Позиционные аргументы и опции --model/--effort/--plan/--max-turns/--budget-usd/--days/--follow."""
    pos, opts = [], {}
    i = 0
    val = {"--model": "model", "--effort": "effort", "--plan": "plan", "--max-turns": "max_turns",
           "--budget-usd": "budget_usd", "--days": "days"}
    while i < len(args):
        a = args[i]
        if a == "--follow":
            opts["follow"] = True
            i += 1
        elif a == "--all":
            opts["all"] = True
            i += 1
        elif a in val:
            if i + 1 >= len(args):
                raise WsError(f"после {a} нужно значение")
            opts[val[a]] = args[i + 1]
            i += 2
        elif a.startswith("--"):
            raise WsError(f"неизвестный параметр {a}")
        else:
            pos.append(a)
            i += 1
    return pos, opts


def _start(hub: Hub, args: list[str]) -> int:
    from .jobs import Jobs
    pos, opts = _parse(args)
    if len(pos) != 2:
        raise WsError("нужно: wshub code start <project> <task> [--model M --effort E --plan no|auto|review "
                      "--max-turns N --budget-usd B --follow]")
    jobs = Jobs(hub)
    # флаги терминала — выбор оператора (ТЗ WSH-3, 2.2.1): применяется как есть, без пола и потолка
    plan = jobs.plan(pos[0], pos[1], model=opts.get("model"), effort=opts.get("effort"), plan=opts.get("plan"),
                     chosen_by="operator", channel="cli",
                     max_turns=int(opts["max_turns"]) if "max_turns" in opts else None,
                     budget_usd=float(opts["budget_usd"]) if "budget_usd" in opts else None)
    d = jobs.start(plan)
    print(d["id"])
    print(f"  {plan.choice.line()}", file=sys.stderr)
    for w in plan.warnings:
        print(f"  предупреждение: {w}", file=sys.stderr)
    if opts.get("follow"):
        _follow(jobs, d["id"])
    return 0


def _follow(jobs, jid: str) -> None:
    seen = 0
    path = jobs.job_dir(jid) / "events.jsonl"
    while True:
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            lines = []
        for line in lines[seen:]:
            print(line)
        seen = len(lines)
        if jobs.effective_state(jobs.read(jid)) in TERMINAL:
            break
        time.sleep(1.0)


def _status(hub: Hub, args: list[str]) -> int:
    from .jobs import Jobs
    from .runner import apply_billing
    jobs = Jobs(hub)
    pos, _ = _parse(args)
    if pos:
        snap = apply_billing(jobs, jobs.snapshot(pos[0]))  # строка оплаты (ТЗ WSH-4.1, п. 2.6)
        print(json.dumps(snap, ensure_ascii=False, indent=1))
        hub._audit_code("cli_code_status", snap.get("project"), pos[0])
    else:
        for row in jobs.list(limit=20):
            print(f"{row['id']}  {row['state']:10}  {row['project']}  {row.get('cost_usd') or ''}")
        hub._audit_code("cli_code_status", None, None)
    return 0


def _result(hub: Hub, args: list[str]) -> int:
    from .jobs import Jobs, meta_warning
    from .runner import apply_billing
    jobs = Jobs(hub)
    pos, _ = _parse(args)
    if len(pos) != 1:
        raise WsError("нужно: wshub code result <job>")
    out = apply_billing(jobs, jobs.result_view(pos[0]), model=True)  # строка оплаты и модель (ТЗ WSH-4.1)
    warn = meta_warning(out)
    if warn:
        print(warn)
    print(json.dumps(out, ensure_ascii=False, indent=1))
    if out.get("open_in_terminal"):
        print(f"открыть сессию: {out['open_in_terminal']}")
    hub._audit_code("cli_code_result", out.get("project"), pos[0])
    return 0


def _stats(hub: Hub, args: list[str]) -> int:
    from .jobs import Jobs
    pos, opts = _parse(args)
    if len(pos) > 1:
        raise WsError("нужно: wshub code stats [project] [--days N]")
    days = int(opts.get("days", 30))
    rows = Jobs(hub).stats(pos[0] if pos else None, days)
    print(f"{'проект':12} {'строка политики':34} {'источник':9} {'канал':10} {'задач':>5} {'ср. $':>8} "
          f"{'ср. ходов':>9} {'completed':>9} {'replan':>6}")
    for r in rows:
        rp = "—" if r["replan_share"] is None else f"{r['replan_share']:.0%}"
        print(f"{r['project'] or '':12} {r['row']:34} {r['source']:9} {r['channel']:10} {r['tasks']:>5} "
              f"{r['avg_cost_usd']:>8.2f} {r['avg_turns']:>9} {r['completed_share']:>9.0%} {rp:>6}")
    if not rows:
        print(f"(задач за {days} дн. нет)")
    hub._audit_code("cli_code_stats", pos[0] if pos else None, None)
    return 0


def _stop(hub: Hub, args: list[str]) -> int:
    from .jobs import Jobs
    jobs = Jobs(hub)
    pos, opts = _parse(args)
    if opts.get("all"):
        targets = [r["id"] for r in jobs.list(limit=1000) if r["state"] not in TERMINAL]
    elif len(pos) == 1:
        targets = [pos[0]]
    else:
        raise WsError("нужно: wshub code stop <job> | wshub code stop --all")
    for jid in targets:
        snap = jobs.cancel(jid)
        print(f"{jid}: {snap['state']}")
        hub._audit_code("cli_code_stop", snap.get("project"), jid)
    return 0

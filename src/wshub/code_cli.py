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
        if len(rest) not in (1, 2) or rest[1:] not in ([], ["--gate"]):
            print("wshub code-kill <job> [--gate] — служебная команда", file=sys.stderr)
            return 2
        from .helper import JOB_RE
        from .jobs import Jobs, kill_gate
        jobs = Jobs(Hub(config, state))
        if rest[1:] == ["--gate"]:  # только гейт конечного звена, состояние не меняется (ТЗ WSH-3.5, круг 2, Р7)
            if not JOB_RE.match(rest[0]):
                print(f"wshub code-kill: {rest[0][:40]!r} — не id задачи", file=sys.stderr)
                return 2
            kill_gate(jobs.job_dir(rest[0]))
        else:
            jobs.kill_orphaned(rest[0])
        return 0
    # cmd == "code"
    if not rest:
        return _serve(config, state)
    if rest[0] == "key-helper":  # служебная: apiKeyHelper задачи на ключе (ТЗ WSH-4.6, п. 2.3) — до Hub
        return _key_helper(config, rest[1:])
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


def _key_helper(config: Path, args: list[str]) -> int:
    """Ключ anthropic в stdout и больше ничего — его запрашивает claude задачи на ключе (apiKeyHelper в --settings).
    Права и чтение — wshub.llm.keys; любая проблема — код 1 и текст в stderr без значения ключа."""
    from .jobs import KEY_PROVIDER
    from .llm import keys
    if args:
        print("wshub code key-helper — служебная команда задачи на ключе, без аргументов", file=sys.stderr)
        return 2
    try:
        value = keys.read(keys.key_path(Path(config).parent, KEY_PROVIDER))
    except (keys.KeyProblem, OSError) as e:
        print(f"wshub code key-helper: {e}", file=sys.stderr)
        return 1
    sys.stdout.write(value + "\n")
    sys.stdout.flush()
    return 0


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
    """Позиционные аргументы и опции --model/--effort/--plan/--max-turns/--budget-usd/--billing/--days/--follow."""
    pos, opts = [], {}
    i = 0
    val = {"--model": "model", "--effort": "effort", "--plan": "plan", "--max-turns": "max_turns",
           "--budget-usd": "budget_usd", "--billing": "billing", "--days": "days"}
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
                      "--max-turns N --budget-usd B --billing subscription|key --follow]")
    jobs = Jobs(hub)
    # флаги терминала — выбор оператора (ТЗ WSH-3, 2.2.1): применяется как есть, без пола и потолка; лимиты — без
    # потолка реестра, на ключе — не выше его потолков (ТЗ WSH-4.6, п. 2.6.1); --billing key — выбор оператора
    plan = jobs.plan(pos[0], pos[1], model=opts.get("model"), effort=opts.get("effort"), plan=opts.get("plan"),
                     chosen_by="operator", channel="cli", billing=opts.get("billing"),
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
    from .jobs import Jobs, cost_shown
    from .runner import apply_billing
    jobs = Jobs(hub)
    pos, _ = _parse(args)
    if pos:
        snap = apply_billing(jobs, jobs.snapshot(pos[0]))  # строка оплаты (ТЗ WSH-4.1, п. 2.6)
        print(json.dumps(snap, ensure_ascii=False, indent=1))
        hub._audit_code("cli_code_status", snap.get("project"), pos[0])
    else:
        for row in jobs.list(limit=20):
            # сумма на подписке не показывается (WSH-4.7), на ключе — да (ТЗ WSH-4.6, п. 2.6.2)
            cost = f"  ≈${row['cost_usd']:.2f}" if cost_shown(row) and row.get("cost_usd") is not None else ""
            print(f"{row['id']}  {row['state']:10}  {row['project']}{cost}")
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
    for warn in (meta_warning(out), out.get("eol_warning")):  # ⚠ концов строк — тоже до JSON (ТЗ WSH-3.8, Э2.3)
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
    # средняя цена в долларах на подписке не показывается (WSH-4.7); колонка «ср. $» — только при задачах на ключе
    key = any(r["billing"] == "key" for r in rows)
    print(f"{'проект':12} {'строка политики':34} {'источник':9} {'канал':10} {'задач':>5} "
          f"{'ср. ходов':>9} {'completed':>9} {'replan':>6}" + (f" {'оплата':12} {'ср. $':>8}" if key else ""))
    for r in rows:
        rp = "—" if r["replan_share"] is None else f"{r['replan_share']:.0%}"
        pay = (f" {'ключ':12} {r['avg_cost_usd']:>8.2f}" if r["billing"] == "key" else f" {'подписка':12} {'—':>8}") \
            if key else ""
        print(f"{r['project'] or '':12} {r['row']:34} {r['source']:9} {r['channel']:10} {r['tasks']:>5} "
              f"{r['avg_turns']:>9} {r['completed_share']:>9.0%} {rp:>6}" + pay)
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

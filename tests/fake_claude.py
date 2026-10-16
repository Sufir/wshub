#!/usr/bin/env python3
"""Фейковый Claude Code для тестов wshub-code: печатает события stream-json, пишет свои argv и окружение
в файл, умеет спать и реагировать на SIGINT. Настоящий claude в тестах не вызывается (ТЗ WSH-2, п. 5).

Управление через окружение:
  FAKE_REC     — путь, куда записать {"argv", "env", "cwd"} (JSON)
  FAKE_SLEEP   — сколько секунд «работать» (по умолчанию 0); FAKE_SLEEP_PLAN — для --permission-mode plan
  FAKE_SIGINT  — "exit" (по SIGINT выйти с итогом error_during_execution) | "ignore" (игнорировать)
  FAKE_TOUCH   — имя файла, который создать в cwd (чтобы git status что-то показал)
  FAKE_COST    — стоимость этого запуска (по умолчанию 0.5); FAKE_COST_PLAN — для --permission-mode plan.
                 total_cost_usd в итоге — накопительный за сессию, как у настоящего claude (PoC WSH-3, 0.3.3)
  FAKE_SUBTYPE — subtype итога (по умолчанию success)
  FAKE_RECLOG  — файл, куда дописать строку {"argv", "cwd"} каждого запуска (цепочки фаз)
  FAKE_NO_PLAN — в режиме plan не писать план (кейс no_plan)
  FAKE_PLAN    — текст плана (по умолчанию короткий план)
  FAKE_SELF_CANCEL — перед итогом создать файл cancel в каталоге задачи (отмена «между выходом и итогом»)
  FAKE_DOCKER  — Bash-вызовы docker compose: успешный, отказ хука и ошибка (is_error) в tool_result,
                 плюс не-docker команды
  FAKE_SWITCH  — git switch на эту ветку (смена ветки задачей)
  FAKE_RESET   — git reset --hard HEAD~1 (HEAD назад)
  FAKE_GIT_META — создать .git/hooks/pre-commit и дописать секцию в .git/config (обход через смонтированный каталог)
  FAKE_COMMIT  — сделать коммит в cwd
  FAKE_SOURCE  — apiKeySource в init (по умолчанию "none" — подписка; "-" — поля нет). Не FAKE_KEY_SOURCE: имя
                 с KEY реестр не пускает в pass_env (ТЗ WSH-4.1, 7.1)
  FAKE_INIT_MODEL — model в init (по умолчанию — значение --model)
  FAKE_RATE    — JSON rate_limit_info: событие rate_limit_event; FAKE_RATE_AT — напечатать после N-го события
                 (по умолчанию — перед итогом, как на подписке, PoC WSH-4.0); FAKE_RATE_COUNT — сколько раз (1)
  FAKE_LIMIT_ERROR — итог с is_error: true (исчерпан лимит подписки)
  FAKE_NO_INIT — не печатать init (источник оплаты не подтверждён)
  FAKE_WRITE_SETTINGS — на середине FAKE_SLEEP записать этот текст в .claude/settings.local.json cwd (настройки,
                 изменённые посреди звена мимо deny)
В режиме plan фейк, как настоящий claude 2.1.291 (PoC WSH-3, 0.3.1), пишет план инструментом Write
в $HOME/.claude/plans/<slug>.md и завершается success.
"""
import json
import os
import signal
import subprocess
import sys
import time

EMITTED = [0]  # сколько событий напечатано: после FAKE_RATE_AT-го — rate_limit_event


def emit(obj):
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()
    EMITTED[0] += 1
    if os.environ.get("FAKE_RATE_AT") and EMITTED[0] == int(os.environ["FAKE_RATE_AT"]):
        emit_rate()


def emit_rate():
    if not os.environ.get("FAKE_RATE"):
        return
    info = json.loads(os.environ["FAKE_RATE"])
    for _ in range(int(os.environ.get("FAKE_RATE_COUNT", "1"))):
        sys.stdout.write(json.dumps({"type": "rate_limit_event", "rate_limit_info": info}, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def main() -> int:
    argv = sys.argv[1:]
    if argv == ["--version"]:
        print("9.9.9 (fake Claude Code)")
        return 0
    if argv == ["--help"]:  # detect_flags проверяет наличие флагов по тексту справки
        print("--permission-prompts <target>\n--setting-sources <sources>\n--max-turns <n>")
        return 0
    sid = None
    for i, a in enumerate(argv):
        if a == "--session-id" and i + 1 < len(argv):
            sid = argv[i + 1]
        if a == "--resume" and i + 1 < len(argv):
            sid = argv[i + 1]
    rec = os.environ.get("FAKE_REC")
    if rec:
        with open(rec, "w", encoding="utf-8") as f:
            json.dump({"argv": argv, "env": dict(os.environ), "cwd": os.getcwd()}, f, ensure_ascii=False)
    reclog = os.environ.get("FAKE_RECLOG")
    if reclog:
        with open(reclog, "a", encoding="utf-8") as f:
            f.write(json.dumps({"argv": argv, "cwd": os.getcwd()}, ensure_ascii=False) + "\n")
    mode = argv[argv.index("--permission-mode") + 1] if "--permission-mode" in argv else "dontAsk"

    interrupted = {"v": False}
    perm_mode = mode
    mode = os.environ.get("FAKE_SIGINT", "exit")  # exit | ignore (игнор SIGINT, смерть по SIGTERM) | ignore-all
    mark = os.environ.get("FAKE_SIGMARK")

    def _mark(name):
        if mark:
            try:
                open(mark, "w").write(name)
            except OSError:
                pass

    def on_sig(signum, frame):
        interrupted["v"] = True
        name = {signal.SIGINT: "INT", signal.SIGTERM: "TERM"}.get(signum, str(signum))
        if signum == signal.SIGINT and mode in ("ignore", "ignore-all"):
            return  # SIGINT игнорируем
        if signum == signal.SIGTERM and mode == "ignore-all":
            return  # SIGTERM тоже игнорируем — остановит только SIGKILL
        _mark(name)
        if signum == signal.SIGINT:
            emit({"type": "result", "subtype": "error_during_execution", "session_id": sid,
                  "total_cost_usd": 0.0, "num_turns": 1, "is_error": True, "result": "interrupted",
                  "permission_denials": []})
        sys.exit(0)

    signal.signal(signal.SIGINT, on_sig)
    signal.signal(signal.SIGTERM, on_sig)

    init = {"type": "system", "subtype": "init", "session_id": sid, "tools": ["Read", "Edit", "Bash", "Task"],
            "permissionMode": perm_mode, "cwd": os.getcwd(),
            "model": os.environ.get("FAKE_INIT_MODEL") or (argv[argv.index("--model") + 1] if "--model" in argv
                                                            else "fake")}
    if os.environ.get("FAKE_SOURCE", "none") != "-":
        init["apiKeySource"] = os.environ.get("FAKE_SOURCE", "none")
    if not os.environ.get("FAKE_NO_INIT"):
        emit(init)
    emit({"type": "assistant", "message": {"content": [{"type": "text", "text": "начинаю"}]}})
    if os.environ.get("FAKE_TOUCH"):
        open(os.path.join(os.getcwd(), os.environ["FAKE_TOUCH"]), "w").close()
    emit({"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Read",
          "input": {"file_path": "x"}}]}})

    if os.environ.get("FAKE_DOCKER"):
        hook_err = "PreToolUse:Bash hook error: [python -m wshub.hook]: wshub: docker: отказ"
        calls = (("dk_ok", "docker compose up -d db", None),
                 ("dk_err", "cd app && docker compose run --rm app pytest -v tests/x.py::t", hook_err),
                 ("dk_fail", "docker compose run --rm app false", "Exit code 1"),
                 ("sh_ls", "ls docker && echo docker-compose", None))
        for uid, cmd, err in calls:
            uid = f"{uid}_{os.getpid()}"
            emit({"type": "assistant", "message": {"content": [{"type": "tool_use", "id": uid, "name": "Bash",
                  "input": {"command": cmd}}]}})
            emit({"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": uid,
                  "is_error": bool(err), "content": err or "ok"}]}})
    if os.environ.get("FAKE_GIT_META"):
        hook = os.path.join(os.getcwd(), ".git", "hooks", "pre-commit")
        with open(hook, "w") as f:
            f.write("#!/bin/sh\necho fake\n")
        os.chmod(hook, 0o755)
        with open(os.path.join(os.getcwd(), ".git", "config"), "a") as f:
            f.write("[wshubfake]\n\tmark = 1\n")
    git_env = ["git", "-c", "user.name=fake", "-c", "user.email=fake@e", "-c", "core.hooksPath=/dev/null"]
    if os.environ.get("FAKE_SWITCH"):
        subprocess.run([*git_env, "switch", "-q", os.environ["FAKE_SWITCH"]], check=True, stdin=subprocess.DEVNULL,
                       capture_output=True)
    if os.environ.get("FAKE_RESET"):
        subprocess.run([*git_env, "reset", "-q", "--hard", "HEAD~1"], check=True, stdin=subprocess.DEVNULL,
                       capture_output=True)
    if os.environ.get("FAKE_COMMIT"):
        subprocess.run(["git", "-c", "user.name=fake", "-c", "user.email=fake@e", "-c", "core.hooksPath=/dev/null",
                        "commit", "-q", "--allow-empty", "-m", f"fake commit {perm_mode}"], check=True,
                       stdin=subprocess.DEVNULL, capture_output=True)

    slept = 0.0
    step = 0.05
    target = float(os.environ.get("FAKE_SLEEP_PLAN") if perm_mode == "plan" and os.environ.get("FAKE_SLEEP_PLAN")
                   else os.environ.get("FAKE_SLEEP", "0"))
    wrote = False
    while slept < target:
        time.sleep(step)
        slept += step
        if os.environ.get("FAKE_WRITE_SETTINGS") and not wrote and slept >= target / 2:
            os.makedirs(os.path.join(os.getcwd(), ".claude"), exist_ok=True)
            with open(os.path.join(os.getcwd(), ".claude", "settings.local.json"), "w", encoding="utf-8") as f:
                f.write(os.environ["FAKE_WRITE_SETTINGS"])
            wrote = True
        if interrupted["v"] and os.environ.get("FAKE_SIGINT") == "ignore":
            continue

    if perm_mode == "plan" and not os.environ.get("FAKE_NO_PLAN"):
        plans = os.path.join(os.environ["HOME"], ".claude", "plans")
        os.makedirs(plans, exist_ok=True)
        path = os.path.join(plans, f"fake-{(sid or 'x')[:8]}-{os.getpid()}.md")
        text = os.environ.get("FAKE_PLAN", "# План\n1. Сделать.\n2. Проверить.\n")
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        uid = f"toolu_fake_{os.getpid()}"
        emit({"type": "assistant", "message": {"content": [{"type": "tool_use", "id": uid, "name": "Write",
              "input": {"file_path": path, "content": text}}]}})
        emit({"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": uid,
              "is_error": None, "content": f"File created successfully at: {path}"}]}})

    if os.environ.get("FAKE_SELF_CANCEL") and "--settings" in argv:
        open(os.path.join(os.path.dirname(argv[argv.index("--settings") + 1]), "cancel"), "w").close()

    # накопительная стоимость сессии: total_cost_usd после --resume включает прежние запуски
    cost = float(os.environ.get("FAKE_COST_PLAN" if perm_mode == "plan" and os.environ.get("FAKE_COST_PLAN")
                                else "FAKE_COST", "0.5"))
    ledger = os.path.join(os.environ.get("HOME", "/tmp"), ".fake_claude_sessions", sid or "none")
    prev = 0.0
    try:
        prev = float(open(ledger).read())
    except (OSError, ValueError):
        pass
    total = round(prev + cost, 6)
    os.makedirs(os.path.dirname(ledger), exist_ok=True)
    with open(ledger, "w") as f:
        f.write(str(total))
    denials = [{"tool_name": t, "tool_input": {"command": t}}
               for t in os.environ.get("FAKE_DENIALS", "").split(",") if t]
    if not os.environ.get("FAKE_RATE_AT"):
        emit_rate()
    limit = bool(os.environ.get("FAKE_LIMIT_ERROR"))
    emit({"type": "result", "subtype": os.environ.get("FAKE_SUBTYPE", "success"), "session_id": sid,
          "total_cost_usd": total, "num_turns": 2, "duration_ms": 10,
          "is_error": limit, "result": "You've hit your limit" if limit else "готово", "permission_denials": denials})
    return 0


if __name__ == "__main__":
    sys.exit(main())

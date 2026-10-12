#!/usr/bin/env python3
"""Фейковый Claude Code для тестов wshub-code: печатает события stream-json, пишет свои argv и окружение
в файл, умеет спать и реагировать на SIGINT. Настоящий claude в тестах не вызывается (ТЗ WSH-2, п. 5).

Управление через окружение:
  FAKE_REC     — путь, куда записать {"argv", "env", "cwd"} (JSON)
  FAKE_SLEEP   — сколько секунд «работать» (по умолчанию 0)
  FAKE_SIGINT  — "exit" (по SIGINT выйти с итогом error_during_execution) | "ignore" (игнорировать)
  FAKE_TOUCH   — имя файла, который создать в cwd (чтобы git status что-то показал)
  FAKE_COST    — total_cost_usd в итоге (по умолчанию 0.5)
  FAKE_SUBTYPE — subtype итога (по умолчанию success)
"""
import json
import os
import signal
import sys
import time


def emit(obj):
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
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

    interrupted = {"v": False}

    def on_int(signum, frame):
        interrupted["v"] = True
        if os.environ.get("FAKE_SIGINT", "exit") == "exit":
            emit({"type": "result", "subtype": "error_during_execution", "session_id": sid,
                  "total_cost_usd": 0.0, "num_turns": 1, "is_error": True, "result": "interrupted",
                  "permission_denials": []})
            sys.exit(0)

    signal.signal(signal.SIGINT, on_int)
    signal.signal(signal.SIGTERM, on_int if os.environ.get("FAKE_SIGINT") != "ignore" else signal.SIG_DFL)

    emit({"type": "system", "subtype": "init", "session_id": sid, "tools": ["Read", "Edit", "Bash", "Task"],
          "permissionMode": "dontAsk", "cwd": os.getcwd()})
    emit({"type": "assistant", "message": {"content": [{"type": "text", "text": "начинаю"}]}})
    if os.environ.get("FAKE_TOUCH"):
        open(os.path.join(os.getcwd(), os.environ["FAKE_TOUCH"]), "w").close()
    emit({"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Read",
          "input": {"file_path": "x"}}]}})

    slept = 0.0
    step = 0.05
    target = float(os.environ.get("FAKE_SLEEP", "0"))
    while slept < target:
        time.sleep(step)
        slept += step
        if interrupted["v"] and os.environ.get("FAKE_SIGINT") == "ignore":
            continue

    denials = [{"tool_name": t, "tool_input": {"command": t}}
               for t in os.environ.get("FAKE_DENIALS", "").split(",") if t]
    emit({"type": "result", "subtype": os.environ.get("FAKE_SUBTYPE", "success"), "session_id": sid,
          "total_cost_usd": float(os.environ.get("FAKE_COST", "0.5")), "num_turns": 2, "duration_ms": 10,
          "is_error": False, "result": "готово", "permission_denials": denials})
    return 0


if __name__ == "__main__":
    sys.exit(main())

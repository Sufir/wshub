"""wshub code-hook: PreToolUse-хук Claude Code для Bash (ТЗ WSH-2, п. 2.6).

ЗАГЛУШКА до реализации архитектором: отказывает любой Bash-команде (fail closed). Интерфейс ниже — контракт,
на который опираются claude_cli.settings, диспетчер server.main и тесты.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path


def check(command: str, cwd: str, protected: list[Path]) -> str | None:
    """Причина отказа для Bash-команды или None — разрешено. protected — служебные каталоги wshub (данные)."""
    return "хук wshub code-hook ещё не реализован — Bash-команды отклоняются"


def main(argv: list[str], stdin=None, stderr=None) -> int:
    """argv — служебные каталоги wshub (как их передаёт settings.json). 0 — разрешить, 2 — отказ (причина
    в stderr). Любая ошибка — 2."""
    stdin = stdin or sys.stdin
    stderr = stderr or sys.stderr
    try:
        event = json.loads(stdin.read())
        if event.get("tool_name") != "Bash":
            return 0
        command = event["tool_input"]["command"]
        if not isinstance(command, str):
            raise TypeError("tool_input.command — не строка")
        reason = check(command, str(event.get("cwd") or "."), [Path(p) for p in argv])
    except Exception as e:  # noqa: BLE001 — fail closed
        reason = f"хук не разобрал событие: {e}"
    if reason is None:
        return 0
    print(f"wshub: {reason}", file=stderr)
    return 2

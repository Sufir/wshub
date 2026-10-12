"""Шапка запуска в начале файла ТЗ и шаблоны промптов для Claude Code (wshub-code).

Шапка — плоское подмножество YAML между строками «---»: только «ключ: значение», комментарии и пустые строки.
Свой разбор, без PyYAML: вложенность, списки и многострочные значения — ошибка.
"""
from __future__ import annotations

import re

HEADER_KEYS = ("ticket", "dir", "profile", "prompt", "model", "effort", "max_turns", "budget_usd")
REQUIRED = ("ticket", "dir", "profile")
NOTE_MAX = 300
TICKET_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")  # имя шаблона промпта: файл <prompts>/<имя>.md
_KEY_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*):(.*)$")
_PLACEHOLDER_RE = re.compile(r"\{([^{}]*)\}")


class TaskError(Exception):
    pass


def _value(raw: str, key: str, n: int) -> str:
    v = raw.strip()
    if v[:1] in ('"', "'"):
        q = v[0]
        end = v.find(q, 1)
        if end < 0:
            raise TaskError(f"шапка, строка {n}: {key} — нет закрывающей кавычки")
        rest = v[end + 1:].strip()
        if rest and not rest.startswith("#"):
            raise TaskError(f"шапка, строка {n}: {key} — лишнее после кавычки")
        return v[1:end]
    v = re.split(r"\s+#", v, maxsplit=1)[0].strip()
    if not v:
        raise TaskError(f"шапка, строка {n}: {key} без значения (вложенность и многострочные значения "
                        "не поддерживаются)")
    if v[0] in "[{|>&*!" or v == "-" or v.startswith("- "):
        raise TaskError(f"шапка, строка {n}: {key} — списки, вложенность и многострочные значения "
                        "не поддерживаются")
    return v


def parse_header(text: str) -> dict:
    """Шапка ТЗ → словарь значений с проверенными типами. Обязательные ключи — REQUIRED."""
    lines = text.split("\n")
    if not lines or lines[0].rstrip("\r") != "---":
        raise TaskError("нет шапки запуска: первая строка файла ТЗ должна быть ровно «---»")
    out: dict = {}
    for n, line in enumerate(lines[1:], start=2):
        line = line.rstrip("\r")
        if line == "---":
            break
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if line[0].isspace():
            raise TaskError(f"шапка, строка {n}: вложенность не поддерживается")
        m = _KEY_RE.match(line)
        if not m:
            raise TaskError(f"шапка, строка {n}: ожидается «ключ: значение»")
        key = m.group(1)
        if key not in HEADER_KEYS:
            raise TaskError(f"шапка, строка {n}: неизвестный ключ {key}")
        if key in out:
            raise TaskError(f"шапка, строка {n}: ключ {key} повторяется")
        out[key] = _value(m.group(2), key, n)
    else:
        raise TaskError("шапка запуска не закрыта строкой «---»")
    missing = [k for k in REQUIRED if k not in out]
    if missing:
        raise TaskError(f"в шапке нет обязательных ключей: {', '.join(missing)}")
    if not TICKET_RE.match(out["ticket"]):
        raise TaskError("ticket — латиница, цифры, «.», «_», «-»")
    if "prompt" in out and not NAME_RE.match(out["prompt"]):
        raise TaskError("prompt — имя шаблона: латиница, цифры, «_», «-»")
    if "max_turns" in out:
        try:
            out["max_turns"] = int(out["max_turns"])
        except ValueError:
            raise TaskError("max_turns — целое число") from None
    if "budget_usd" in out:
        try:
            out["budget_usd"] = float(out["budget_usd"])
        except ValueError:
            raise TaskError("budget_usd — число") from None
    return out


def check_note(note: str | None) -> str | None:
    if note is None or note == "":
        return None
    if not isinstance(note, str) or "\n" in note or "\r" in note:
        raise TaskError("note — одна строка без перевода строки")
    if len(note) > NOTE_MAX:
        raise TaskError(f"note длиннее {NOTE_MAX} символов")
    return note


def render(template: str, ticket: str, task: str, note: str | None) -> tuple[str, list[str]]:
    """Подстановка {ticket}, {task}, {note}. Другие {…} — отказ; {note} без значения — отказ.
    Возвращает (промпт, предупреждения)."""
    names = _PLACEHOLDER_RE.findall(template)
    unknown = sorted({n for n in names if n not in ("ticket", "task", "note")})
    if unknown:
        raise TaskError(f"в шаблоне неизвестные подстановки: {', '.join('{' + n + '}' for n in unknown)}")
    note = check_note(note)
    warnings = []
    if "note" in names and note is None:
        raise TaskError("шаблон ждёт {note} — передай note (строка оператора, ≤ 300 символов)")
    if note is not None and "note" not in names:
        warnings.append("note передан, но в шаблоне нет {note} — не использован")
    values = {"ticket": ticket, "task": task, "note": note or ""}
    return _PLACEHOLDER_RE.sub(lambda m: values[m.group(1)], template), warnings

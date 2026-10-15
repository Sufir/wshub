"""Разбор events.jsonl задачи Claude Code: ходы, действия, лента, «отказ» и «ошибка», окно лимита
(ТЗ WSH-3.4, Б1, Б5, В1.5). Одни и те же цифры — в code_status, code_wait, code_result и в карточке.

«Ходы» — как num_turns у claude и --max-turns: результаты инструментов основного потока (без субагентов,
parent_tool_use_id пуст) плюс один ход на каждый прогон (событие init). Сверено на задачах claude 2.1.291:
сумма num_turns итоговых событий совпадает с этим счётом (журнал WSH-3.4, Б1). Уникальные message.id
(ответы API) и события assistant (по событию на блок ответа) — другие счёты, они расходятся с --max-turns.
«Действия» — все вызовы инструментов (tool_use), включая субагентов.

Разбор инкрементальный: состояние по файлу кэшируется в процессе и дочитывается с последнего байта.
"""
from __future__ import annotations

import json
import os
from collections import OrderedDict, deque
from datetime import datetime
from pathlib import Path

FEED_KEEP = 50  # действий в памяти разбора: «Журнал» карточки
CMD_MAX = 80  # символов команды Bash в строке ленты
ARG_MAX = 200
CACHE_MAX = 64  # файлов в кэше разбора
FILE_ARG_TOOLS = ("Read", "Edit", "Write", "MultiEdit", "NotebookEdit")
PATTERN_TOOLS = ("Grep", "Glob")
# отказ прав или хука в тексте tool_result (claude 2.1.291): хук — «PreToolUse:<инструмент> hook error: …»,
# права — «Permission to use <инструмент> … has been denied …»
DENY_PREFIXES = ("PreToolUse:", "Permission to use ")
DENIED = "отказ"
FAILED = "ошибка"


def mark(block: dict, denied_ids: set[str]) -> str | None:
    """Пометка вызова по его tool_result (Б5): «отказ» — отклонён правами или хуком (событие permission_denied,
    permission_denials итога или текст отказа), «ошибка» — прочий is_error (упавшая команда, тест)."""
    if not block.get("is_error"):
        return None
    if block.get("tool_use_id") in denied_ids:
        return DENIED
    return DENIED if _result_text(block).lstrip().startswith(DENY_PREFIXES) else FAILED


def _result_text(block: dict) -> str:
    c = block.get("content")
    if isinstance(c, list):
        return " ".join(x.get("text", "") for x in c if isinstance(x, dict) and isinstance(x.get("text"), str))
    return c if isinstance(c, str) else ""


def _ts(value) -> float | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def main_arg(name: str, inp, cwd: str | None) -> str:
    """Главный аргумент вызова для ленты: путь от рабочего каталога, команда Bash ≤ 80 символов, шаблон."""
    if not isinstance(inp, dict):
        return ""
    if name in FILE_ARG_TOOLS:
        p = inp.get("file_path") or inp.get("notebook_path")
        if isinstance(p, str):
            if cwd and os.path.isabs(p):
                rel = os.path.relpath(p, cwd)
                p = rel if not rel.startswith("..") else p
            return p[:ARG_MAX]
        return ""
    if name == "Bash":
        cmd = inp.get("command")
        if isinstance(cmd, str):
            one = " ".join(cmd.split())
            return one if len(one) <= CMD_MAX else one[:CMD_MAX - 1] + "…"
        return ""
    if name in PATTERN_TOOLS:
        return str(inp.get("pattern") or "")[:ARG_MAX]
    for k in ("description", "url", "query", "skill"):
        if isinstance(inp.get(k), str):
            return inp[k][:CMD_MAX]
    return ""


class Scan:
    """Состояние разбора одного events.jsonl. feed — последние FEED_KEEP действий: {id, ts, tool, arg, sub,
    mark}; marks — пометки по id, в том числе для действий, которых в feed уже нет."""

    def __init__(self, cwd: str | None = None):
        self.cwd = cwd
        self.offset = 0
        self.inits = 0
        self.main_results = 0
        self.seen_turn = False  # было ли хоть одно сообщение (для событий без init, например фейкового claude)
        self.actions = 0
        self.feed: deque[dict] = deque(maxlen=FEED_KEEP)
        self.marks: dict[str, str] = {}
        self.denied: set[str] = set()
        self.results: list[dict] = []
        self.rate: dict | None = None
        self.denials = 0
        self.errors = 0
        self.api_key_source: str | None = None  # из init: "none" — подписка (карточка, В1.5)

    @property
    def turns(self) -> int:
        return self.main_results + (self.inits or (1 if self.seen_turn else 0))

    def counts(self) -> dict:
        return {"turns": self.turns, "actions": self.actions, "denials": self.denials, "errors": self.errors}

    def feed_from(self, data: bytes, base: int) -> tuple[list[dict], dict[str, str], int]:
        """Разобрать новые байты: (новые действия, новые пометки, сколько байт съедено — до последнего \\n)."""
        end = data.rfind(b"\n") + 1
        new_actions: list[dict] = []
        new_marks: dict[str, str] = {}
        for raw in data[:end].splitlines():
            try:
                e = json.loads(raw)
            except ValueError:
                continue
            if isinstance(e, dict):
                self._event(e, new_actions, new_marks)
        self.offset = base + end
        return new_actions, new_marks, end

    def _event(self, e: dict, acts: list[dict], marks: dict[str, str]) -> None:
        t = e.get("type")
        if t == "system":
            if e.get("subtype") == "init":
                self.inits += 1
                if isinstance(e.get("apiKeySource"), str):
                    self.api_key_source = e["apiKeySource"]
            elif e.get("subtype") == "permission_denied" and isinstance(e.get("tool_use_id"), str):
                self._deny(e["tool_use_id"], marks)
            return
        if t == "rate_limit_event":
            info = e.get("rate_limit_info") if isinstance(e.get("rate_limit_info"), dict) else {}
            wins = info.get("unifiedWindows") if isinstance(info.get("unifiedWindows"), dict) else {}
            rate = {k: v.get("utilization") for k, v in wins.items()
                    if isinstance(v, dict) and isinstance(v.get("utilization"), (int, float))}
            if rate:
                self.rate = rate
            return
        if t == "result":
            self.results.append({"num_turns": e.get("num_turns"), "subtype": e.get("subtype")})
            for d in e.get("permission_denials") or []:
                if isinstance(d, dict) and isinstance(d.get("tool_use_id"), str):
                    self._deny(d["tool_use_id"], marks)
            return
        msg = e.get("message") if isinstance(e.get("message"), dict) else {}
        content = msg.get("content") if isinstance(msg.get("content"), list) else []
        sub = bool(e.get("parent_tool_use_id"))
        if t in ("assistant", "user"):
            self.seen_turn = True
        for c in content:
            if not isinstance(c, dict):
                continue
            if t == "assistant" and c.get("type") == "tool_use":
                self.actions += 1
                name = c.get("name") if isinstance(c.get("name"), str) else "?"
                act = {"id": c.get("id") if isinstance(c.get("id"), str) else None, "ts": _ts(e.get("timestamp")),
                       "tool": name, "arg": main_arg(name, c.get("input"), self.cwd), "sub": sub, "mark": None}
                self.feed.append(act)
                acts.append(act)
            elif t == "user" and c.get("type") == "tool_result":
                if not sub:
                    self.main_results += 1
                m = mark(c, self.denied)
                uid = c.get("tool_use_id")
                if m and isinstance(uid, str):
                    self._set_mark(uid, m, marks)

    def _deny(self, uid: str, marks: dict[str, str]) -> None:
        """Признак отказа (permission_denied, permission_denials итога): до tool_result — запоминаем, после —
        «ошибка» по тексту становится «отказом»."""
        self.denied.add(uid)
        if self.marks.get(uid) == FAILED:
            self._set_mark(uid, DENIED, marks)

    def _set_mark(self, uid: str, m: str, marks: dict[str, str]) -> None:
        old = self.marks.get(uid)
        if old == m:
            return
        if old == DENIED:
            self.denials -= 1
        elif old == FAILED:
            self.errors -= 1
        self.marks[uid] = marks[uid] = m
        if m == DENIED:
            self.denials += 1
        else:
            self.errors += 1
        for a in self.feed:
            if a["id"] == uid:
                a["mark"] = m


_CACHE: OrderedDict[str, tuple[tuple, Scan]] = OrderedDict()


def scan(path: Path, cwd: str | None = None) -> Scan:
    """Разбор файла с кэшем: дочитываем только новые байты. Файл пересоздан (меньше, другой inode) — заново."""
    key = str(path)
    try:
        st = os.stat(path)
    except OSError:
        return Scan(cwd)
    ident = (st.st_ino, st.st_dev)
    hit = _CACHE.get(key)
    s = hit[1] if hit and hit[0] == ident and hit[1].offset <= st.st_size else Scan(cwd)
    if st.st_size > s.offset:
        try:
            with open(path, "rb") as fh:
                fh.seek(s.offset)
                s.feed_from(fh.read(st.st_size - s.offset), s.offset)
        except OSError:
            pass
    _CACHE[key] = (ident, s)
    _CACHE.move_to_end(key)
    while len(_CACHE) > CACHE_MAX:
        _CACHE.popitem(last=False)
    return s


def read_from(path: Path, offset: int, cwd: str | None = None, limit: int = 256 * 1024) -> tuple[list[dict],
                                                                                             dict[str, str], int]:
    """Действия и пометки из байт [offset, …) — для карточки: (действия, пометки, новый offset). Признаки
    отказа из более ранних байт учитываются через общий разбор файла (scan)."""
    full = scan(path, cwd)
    try:
        with open(path, "rb") as fh:
            fh.seek(max(0, offset))
            data = fh.read(limit)
    except OSError:
        return [], {}, offset
    part = Scan(cwd)
    part.denied = set(full.denied)
    acts, marks, used = part.feed_from(data, offset)
    # пометка могла прийти в байтах, которые общий разбор уже прочёл (признак отказа в итоге — после результата)
    ids = {a["id"] for a in acts if a["id"]} | set(marks)
    marks = {**marks, **{k: v for k, v in full.marks.items() if k in ids}}
    for a in acts:
        a["mark"] = marks.get(a["id"]) if a["id"] else None
    return acts, marks, offset + used

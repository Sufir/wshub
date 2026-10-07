"""Точечная правка claude_desktop_config.json: добавить, обновить или убрать один сервер в mcpServers.

Файл не пересобирается через json.dumps: меняется только текст записи сервера, остальные байты
(отступы, порядок ключей, BOM, переводы строк, чужие серверы и настройки) остаются как были.
Поэтому «добавить, затем убрать» возвращает файл к исходному побайтово.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field

BOM = "﻿"


class ConfigError(Exception):
    pass


@dataclass
class Member:
    key: str
    kstart: int  # позиция открывающей кавычки ключа
    vstart: int  # начало значения
    vend: int  # конец значения (не включая)


@dataclass
class Obj:
    open: int  # позиция «{»
    close: int  # позиция «}»
    members: list[Member] = field(default_factory=list)
    children: dict[int, Obj] = field(default_factory=dict)  # vstart → вложенный объект

    def get(self, key: str) -> tuple[int, Member] | None:
        found = None
        for i, m in enumerate(self.members):
            if m.key == key:
                found = (i, m)  # как json.loads: при повторе ключа действует последний
        return found


class _Scanner:
    """Разметка уже проверенного json.loads текста: границы объектов и их членов."""

    def __init__(self, s: str):
        self.s = s

    def ws(self, i: int) -> int:
        s = self.s
        while i < len(s) and s[i] in " \t\r\n":
            i += 1
        return i

    def string(self, i: int) -> int:
        s, i = self.s, i + 1
        while s[i] != '"':
            i += 2 if s[i] == "\\" else 1
        return i + 1

    def value(self, i: int) -> tuple[int, Obj | None]:
        s = self.s
        c = s[i]
        if c == "{":
            return self.obj(i)
        if c == "[":
            i = self.ws(i + 1)
            if s[i] == "]":
                return i + 1, None
            while True:
                i, _ = self.value(i)
                i = self.ws(i)
                if s[i] == "]":
                    return i + 1, None
                i = self.ws(i + 1)  # «,»
        if c == '"':
            return self.string(i), None
        while i < len(s) and s[i] not in ",]} \t\r\n":
            i += 1
        return i, None

    def obj(self, i: int) -> tuple[int, Obj]:
        s = self.s
        o = Obj(open=i, close=-1)
        i = self.ws(i + 1)
        if s[i] == "}":
            o.close = i
            return i + 1, o
        while True:
            kstart = i
            kend = self.string(i)
            key = json.loads(s[kstart:kend])
            i = self.ws(self.ws(kend) + 1)  # «:»
            vstart = i
            vend, child = self.value(i)
            o.members.append(Member(key, kstart, vstart, vend))
            if child is not None:
                o.children[vstart] = child
            i = self.ws(vend)
            if s[i] == "}":
                o.close = i
                return i + 1, o
            i = self.ws(i + 1)  # «,»


def _line_indent(s: str, pos: int) -> str:
    start = s.rfind("\n", 0, pos) + 1
    lead = s[start:pos]
    return lead[: len(lead) - len(lead.lstrip(" \t"))]


def _newline(s: str) -> str:
    return "\r\n" if "\r\n" in s else "\n"


def _sep_before(s: str, o: Obj, idx: int) -> str:
    """Пробелы и переводы строк перед ключом члена idx (после «{» или после «,»)."""
    m = o.members[idx]
    if idx == 0:
        return s[o.open + 1:m.kstart]
    prev = o.members[idx - 1]
    comma = s.index(",", prev.vend)
    return s[comma + 1:m.kstart]


def _unit(s: str, root: Obj) -> str | None:
    """Шаг отступа файла по первому члену корня; None — файл записан в одну строку."""
    if not root.members:
        return "  "
    sep = _sep_before(s, root, 0)
    if "\n" not in sep:
        return None
    member = sep.rsplit("\n", 1)[1]
    base = _line_indent(s, root.open)
    return member[len(base):] if member.startswith(base) and len(member) > len(base) else "  "


def _dump(value, indent: str, unit: str | None, nl: str) -> str:
    if unit is None:
        return json.dumps(value, ensure_ascii=False)
    text = json.dumps(value, ensure_ascii=False, indent=unit)
    return text.replace("\n", nl + indent)


def _insert(s: str, o: Obj, key: str, value, unit: str | None, nl: str) -> str:
    if o.members:
        last = o.members[-1]
        sep = _sep_before(s, o, len(o.members) - 1)
        indent = sep.rsplit("\n", 1)[1] if "\n" in sep else ""
        member = json.dumps(key, ensure_ascii=False) + ": " + _dump(value, indent, unit if "\n" in sep else None, nl)
        return s[:last.vend] + "," + sep + member + s[last.vend:]
    if unit is None:
        member = json.dumps(key, ensure_ascii=False) + ": " + _dump(value, "", None, nl)
        return s[:o.open + 1] + member + s[o.close:]
    base = _line_indent(s, o.open)
    indent = base + unit
    member = json.dumps(key, ensure_ascii=False) + ": " + _dump(value, indent, unit, nl)
    return s[:o.open + 1] + nl + indent + member + nl + base + s[o.close:]


def _remove(s: str, o: Obj, idx: int) -> str:
    ms = o.members
    if len(ms) == 1:
        return s[:o.open + 1] + s[o.close:]
    if idx > 0:
        return s[:ms[idx - 1].vend] + s[ms[idx].vend:]
    return s[:ms[0].kstart] + s[ms[1].kstart:]


def _parse(text: str) -> tuple[str, str, Obj]:
    bom = BOM if text.startswith(BOM) else ""
    body = text[len(bom):]
    try:
        data = json.loads(body)
    except ValueError as e:
        raise ConfigError(f"не читается как JSON: {e}") from None
    if not isinstance(data, dict):
        raise ConfigError("в файле не JSON-объект")
    start = _Scanner(body).ws(0)
    _end, root = _Scanner(body).obj(start)
    return bom, body, root


def _servers(body: str, root: Obj) -> tuple[int, Member, Obj] | None:
    hit = root.get("mcpServers")
    if hit is None:
        return None
    idx, m = hit
    child = root.children.get(m.vstart)
    if child is None:
        raise ConfigError("mcpServers — не объект; исправь файл вручную")
    return idx, m, child


def get_server(text: str, name: str):
    """Текущая запись сервера или None."""
    _bom, body, root = _parse(text)
    srv = _servers(body, root)
    if srv is None:
        return None
    hit = srv[2].get(name)
    return None if hit is None else json.loads(body[hit[1].vstart:hit[1].vend])


def set_server(text: str, name: str, entry: dict) -> tuple[str, str]:
    """Добавить или обновить mcpServers[name]. В существующей записи меняются только ключи из entry
    (например, свой env остаётся). Возвращает (новый текст, действие): unchanged | updated | added |
    added-parent (заодно создан mcpServers) | created (файла не было или он пуст)."""
    if not text.strip():
        return json.dumps({"mcpServers": {name: entry}}, ensure_ascii=False, indent=2) + "\n", "created"
    bom, body, root = _parse(text)
    nl, unit = _newline(body), _unit(body, root)
    srv = _servers(body, root)
    if srv is None:
        return bom + _insert(body, root, "mcpServers", {name: entry}, unit, nl), "added-parent"
    _i, _m, servers = srv
    hit = servers.get(name)
    if hit is None:
        return bom + _insert(body, servers, name, entry, unit, nl), "added"
    m = hit[1]
    old = json.loads(body[m.vstart:m.vend])
    new = {**old, **entry} if isinstance(old, dict) else dict(entry)
    if new == old:
        return text, "unchanged"
    indent = _line_indent(body, m.kstart)
    multiline = "\n" in body[m.vstart:m.vend] or "\n" in _sep_before(body, servers, hit[0])
    return bom + body[:m.vstart] + _dump(new, indent, unit if multiline else None, nl) + body[m.vend:], "updated"


def remove_server(text: str, name: str, created: str | None = None) -> tuple[str | None, bool]:
    """Убрать mcpServers[name]. created — что создал setup: "mcpServers" (тогда пустой mcpServers убирается),
    "file" (тогда файл удаляется — возвращается None, если в нём больше ничего нет), "empty" (файл был пуст).
    Возвращает (новый текст или None — удалить файл, убрано ли что-то)."""
    if not text.strip():
        return text, False
    bom, body, root = _parse(text)
    srv = _servers(body, root)
    if srv is None:
        return text, False
    idx, _m, servers = srv
    hit = servers.get(name)
    if hit is None:
        return text, False
    only = len(servers.members) == 1
    if only and created in ("mcpServers", "file", "empty"):
        out = _remove(body, root, idx)
        if created == "mcpServers":
            return bom + out, True
        if json.loads(out) == {}:
            return (None if created == "file" else ""), True
        return bom + out, True
    return bom + _remove(body, servers, hit[0]), True

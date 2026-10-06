"""Маски deny: глоб с ** в стиле gitignore, без учёта регистра.

Маска без «/» сравнивается с каждым компонентом пути (именем файла и именами папок над ним),
маска с «/» — с каждым префиксом пути от корня. Поэтому запрет папки запрещает и всё внутри неё.
"""
from __future__ import annotations

import re
from functools import lru_cache


@lru_cache(maxsize=512)
def glob_regex(pat: str) -> re.Pattern:
    pat = pat.lower().strip("/")
    out, i, n = [], 0, len(pat)
    while i < n:
        if pat.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pat.startswith("/**", i) and i + 3 == n:
            out.append("(?:/.*)?")
            i += 3
        elif pat.startswith("**", i):
            out.append(".*")
            i += 2
        elif pat[i] == "*":
            out.append("[^/]*")
            i += 1
        elif pat[i] == "?":
            out.append("[^/]")
            i += 1
        elif pat[i] == "[" and (j := pat.find("]", i + 2)) != -1:
            body = pat[i + 1 : j]
            if body.startswith("!"):
                body = "^" + body[1:]
            out.append("[" + body.replace("\\", "\\\\") + "]")
            i = j + 1
        else:
            out.append(re.escape(pat[i]))
            i += 1
    return re.compile("".join(out), re.DOTALL)


def glob_match(pat: str, rel: str) -> bool:
    """Совпадение маски с путём от корня (для find/grep): без «/» — по имени, с «/» — по всему пути."""
    rel = rel.lower()
    if "/" in pat.strip("/"):
        return bool(glob_regex(pat).fullmatch(rel))
    return bool(glob_regex(pat).fullmatch(rel.rsplit("/", 1)[-1]))


class Policy:
    def __init__(self, masks: tuple[str, ...]):
        self.masks = masks
        self._name = [glob_regex(m) for m in masks if "/" not in m.strip("/")]
        self._path = [glob_regex(m) for m in masks if "/" in m.strip("/")]

    def denied(self, rel: str) -> bool:
        """rel — путь от корня проекта в posix-виде («.» или «» — сам корень)."""
        if rel in ("", "."):
            return False
        parts = rel.lower().split("/")
        for k, name in enumerate(parts):
            if any(r.fullmatch(name) for r in self._name):
                return True
            prefix = "/".join(parts[: k + 1])
            if any(r.fullmatch(prefix) for r in self._path):
                return True
        return False

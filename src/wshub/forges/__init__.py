"""Пресеты хостингов репозитория для помощника `wshub forge` (ТЗ WSH-3.4, А3.3).

Пресет — модуль на тип хостинга: заголовки токена, адрес API по умолчанию, как путь вызова становится URL, и
пол — пути, по которым запись через API запрещена при любом регламенте (мерж, одобрение, запись в репозиторий;
это делает `wshub push` по своим правилам). Новый хостинг — новый модуль с PRESET и строка в PRESETS; реестр,
помощник и документация берут список отсюда.
"""
from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass


@dataclass(frozen=True)
class Preset:
    name: str
    default_url: str | None  # None — forge_url в реестре обязателен
    headers: Callable[[str], dict]  # токен → заголовки запроса
    prefix: str  # префикс пути API; путь без него получает его спереди
    floor: tuple[tuple[re.Pattern, str], ...]  # (шаблон пути без query, что это) — запись запрещена всегда

    def path(self, path: str) -> str:
        return path if not self.prefix or path == self.prefix or path.startswith(self.prefix + "/") \
            else self.prefix + path

    def floor_hit(self, path: str) -> str | None:
        """Что запрещает пол для пути (уже раскодированного, без query, в нижнем регистре), или None."""
        for rx, what in self.floor:
            if rx.search(path):
                return what
        return None


def floor(*rows: tuple[str, str]) -> tuple[tuple[re.Pattern, str], ...]:
    """Конец сегмента «(/|$)» в шаблонах — ещё и «.»: GitLab (Grape) принимает суффикс формата, `…/merge.json`
    — тот же мерж (ревью WSH-3.4)."""
    return tuple((re.compile(rx.replace("(/|$)", r"(/|\.|$)")), what) for rx, what in rows)


from . import github, gitlab  # noqa: E402 — модулям нужен Preset выше

PRESETS: dict[str, Preset] = {p.name: p for p in (gitlab.PRESET, github.PRESET)}

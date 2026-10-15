"""Пресеты хостингов репозитория для помощника `wshub forge` (ТЗ WSH-3.4, А3.3).

Пресет — модуль на тип хостинга: заголовки токена, адрес API по умолчанию, как путь вызова становится URL;
список разрешённой записи — MR/PR, issues и комментарии (ревью WSH-3.4, круг 1: запись — по списку
разрешений, а не запретов); пол — пути, по которым запись запрещена при любом регламенте (мерж, одобрение,
запись в репозиторий — это делает `wshub push` по своим правилам), второй слой; чтение секретов — пути, GET по
которым запрещён (ответ уходит в транскрипт). Новый хостинг — новый модуль с PRESET и строка в PRESETS; реестр,
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
    writes: tuple[tuple[str, re.Pattern], ...] = ()  # (метод, шаблон сырого пути с префиксом) — разрешённая запись
    secret_reads: tuple[tuple[re.Pattern, str], ...] = ()  # GET запрещён: CI-переменные, webhooks, токены

    def path(self, path: str) -> str:
        return path if not self.prefix or path == self.prefix or path.startswith(self.prefix + "/") \
            else self.prefix + path

    def floor_hit(self, path: str) -> str | None:
        """Что запрещает пол для пути (уже раскодированного, без query, в нижнем регистре), или None."""
        for rx, what in self.floor:
            if rx.search(path):
                return what
        return None

    def write_allowed(self, method: str, raw_path: str) -> bool:
        """Запись разрешена списком пресета: сырой путь с префиксом, без query, целиком."""
        return any(m == method and rx.fullmatch(raw_path) for m, rx in self.writes)

    def secret_hit(self, path: str) -> str | None:
        """Что запрещает GET по пути (раскодированному, в нижнем регистре), или None."""
        for rx, what in self.secret_reads:
            if rx.search(path):
                return what
        return None


def floor(*rows: tuple[str, str]) -> tuple[tuple[re.Pattern, str], ...]:
    """Конец сегмента «(/|$)» в шаблонах — ещё и «.»: GitLab (Grape) принимает суффикс формата, `…/merge.json`
    — тот же мерж (ревью WSH-3.4)."""
    return tuple((re.compile(rx.replace("(/|$)", r"(/|\.|$)")), what) for rx, what in rows)


NUM = r"\d+"  # iid, номер, id заметки — только цифры: закодированный «/» в номере не проходит
JSON = r"(\.json)?"  # суффикс формата допускается


def writes(base: str, *rows: tuple[str, str]) -> tuple[tuple[str, re.Pattern], ...]:
    """Список разрешённой записи: base — начало сырого пути (проект, репозиторий), строки — (метод, хвост)."""
    return tuple((m, re.compile(base + tail.replace("<n>", NUM) + JSON)) for m, tail in rows)


from . import github, gitlab  # noqa: E402 — модулям нужен Preset выше

PRESETS: dict[str, Preset] = {p.name: p for p in (gitlab.PRESET, github.PRESET)}

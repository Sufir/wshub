"""Нейтральные типы запроса и ответа: ядро и провайдеры говорят только на них (ТЗ WSH-4.2, п. 2.1)."""
from __future__ import annotations

from dataclasses import asdict, dataclass

TIERS = ("cheap", "standard", "strong")  # уровни модели — от слабого к сильному
CACHE = ("none", "short", "long")  # подсказка провайдеру о кэше
CAPS = frozenset({"batch", "cache", "schema", "count_tokens", "effort"})


@dataclass(frozen=True)
class Request:
    system: str
    messages: tuple[dict, ...]  # {"role": "user" | "assistant", "text": …}
    project: str
    tier: str | None = None  # cheap | standard | strong — или явная model
    model: str | None = None
    schema: dict | None = None  # JSON-схема ответа
    effort: str | None = None
    max_tokens: int = 1024
    cache: str = "none"
    template: str | None = None


@dataclass(frozen=True)
class Usage:
    input: int = 0
    output: int = 0
    cache_read: int = 0
    cache_write: int = 0

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class Response:
    text: str
    data: object  # разобранный JSON при schema, иначе None
    usage: Usage
    model: str  # фактическая модель
    provider: str
    cost_usd: float = 0.0  # оценка хаба по таблице цен провайдера
    raw_ref: str | None = None  # путь к сырому ответу в каталоге состояния

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
    cache_write_1h: int = 0  # часть cache_write, записанная с TTL 1 ч (для цены по разбивке, ТЗ WSH-4.3, 5.1)

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
    warnings: tuple[str, ...] = ()  # «⚠ журнал трат: …» — ответ оплачен, строка журнала не записана (ТЗ WSH-4.8)


@dataclass(frozen=True)
class BatchItem:
    """Результат одного запроса пакета (ТЗ WSH-4.5): ответ — или ошибка. data ответа — None: схему разбирает ядро
    по учёту пакета. model при ошибке — ответ получен (оплачен), но не разобран (usage): ядро считает его по оценке
    запроса; None — результат не оплачен (errored, canceled, expired)."""
    custom_id: str
    response: Response | None
    error: str | None = None
    model: str | None = None

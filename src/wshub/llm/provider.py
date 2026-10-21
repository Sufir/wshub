"""Интерфейс провайдера (ТЗ WSH-4.2, п. 2.1). Модуль провайдера отдаёт create(conf) -> Provider, где conf —
его секция [api.providers.<имя>] (registry.ApiProvider). Чего нет в caps, ядро отклоняет до запроса."""
from __future__ import annotations

from collections.abc import Iterator
from typing import Protocol

from .types import BatchItem, Request, Response, Usage


class Provider(Protocol):
    name: str
    caps: frozenset[str]  # подмножество types.CAPS

    def models(self) -> dict[str, str]:
        """Уровень (cheap / standard / strong) → id модели."""

    def price(self, model: str, usage: Usage) -> float:
        """Оценка в USD по таблице провайдера; неизвестная модель — KeyError."""

    def estimate(self, request: Request, key: str | None = None) -> Usage:
        """Оценка входа: точная при count_tokens, иначе грубая (символы / 4). Ключ ядро передаёт, только если у
        провайдера есть count_tokens и ключ есть; без ключа — грубо (ТЗ WSH-4.3, п. 2.1.4)."""

    def run(self, request: Request, key: str) -> Response:
        """Один запрос. request.model уже выбран ядром."""

    # пакеты (ТЗ WSH-4.5) — только у провайдера с batch в caps
    def batch_submit(self, requests: list[tuple[str, Request]], key: str) -> str:
        """Пакет из (custom_id, запрос): тела — тем же кодом, что у run. Ответ — id пакета."""

    def batch_status(self, batch_id: str, key: str) -> dict:
        """{"status": in_progress | canceling | ended, "counts": {…}, "ended_at", "expires_at"}."""

    def batch_results(self, batch_id: str, key: str) -> Iterator[BatchItem]:
        """Результаты законченного пакета — в порядке ответа провайдера, сопоставлять по custom_id."""

    def batch_cancel(self, batch_id: str, key: str) -> dict:
        """Отмена пакета; ответ — как у batch_status."""


def rough_tokens(text: str) -> int:
    """Грубая оценка числа токенов: символы / 4, с округлением вверх."""
    return (len(text) + 3) // 4


def prompt_text(request: Request) -> str:
    return "\n".join([request.system, *(m.get("text", "") for m in request.messages)])

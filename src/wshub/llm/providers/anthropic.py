"""Провайдер anthropic — заготовка (ТЗ WSH-4.6, п. 2.2): имя, уровни моделей и ключ ~/.config/wshub/keys/anthropic
для задач wshub-code на ключе. Запросы Claude API, цены и оценка входа — задача WSH-4.3."""
from __future__ import annotations

from ..types import Request, Response, Usage

# id моделей — из ответа /v1/models (PoC WSH-4.0, api/models.json)
MODELS = {"cheap": "claude-haiku-5-5", "standard": "claude-sonnet-5-5", "strong": "claude-opus-5-5"}
TODO = "провайдер anthropic: запросы Claude API — задача WSH-4.3"


def _todo():
    from ..core import ApiError  # не на уровне модуля: ядро само импортирует словарь провайдеров
    return ApiError(TODO)


class AnthropicProvider:
    name = "anthropic"
    caps = frozenset()

    def models(self) -> dict[str, str]:
        return dict(MODELS)

    def price(self, model: str, usage: Usage) -> float:
        raise _todo()

    def estimate(self, request: Request) -> Usage:
        raise _todo()

    def run(self, request: Request, key: str) -> Response:
        raise _todo()


def create(conf) -> AnthropicProvider:
    return AnthropicProvider()

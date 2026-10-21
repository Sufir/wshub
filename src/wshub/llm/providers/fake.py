"""Фейковый провайдер: без сети, детерминированный. Нужен тестам и как образец модуля провайдера
(ТЗ WSH-4.2, п. 2.1.4). Доступен при [api.providers.fake] в реестре или при ключе keys/fake (ТЗ WSH-4.4, п. 2.3б)."""
from __future__ import annotations

import json

from ..provider import prompt_text, rough_tokens
from ..types import CAPS, Request, Response, Usage

MODELS = {"cheap": "fake-cheap", "standard": "fake-standard", "strong": "fake-strong"}
# USD за миллион токенов: вход, выход, чтение кэша, запись кэша
PRICES = {
    "fake-cheap": (1.0, 5.0, 0.1, 1.25),
    "fake-standard": (3.0, 15.0, 0.3, 3.75),
    "fake-strong": (15.0, 75.0, 1.5, 18.75),
}
_SAMPLE = {"string": "", "integer": 0, "number": 0, "boolean": False, "array": [], "null": None}


def sample(schema: dict):
    """Минимальное значение по JSON-схеме: объект — по properties, остальное — пустое значение типа."""
    if schema.get("type") == "object" or "properties" in schema:
        return {k: sample(v) for k, v in schema.get("properties", {}).items() if isinstance(v, dict)}
    return _SAMPLE.get(schema.get("type"), None)


class FakeProvider:
    def __init__(self, name: str = "fake", caps=CAPS, models: dict | None = None, prices: dict | None = None,
                 reply=None, fail: str | None = None):
        self.name = name
        self.caps = frozenset(caps)
        self._models = dict(MODELS if models is None else models)
        self._prices = dict(PRICES if prices is None else prices)
        self.reply = reply  # ответ: строка, или JSON при schema; None — эхо последнего сообщения
        self.fail = fail  # шаблон исключения с {key} — для проверки, что ключ не утекает
        self.calls: list[Request] = []

    def models(self) -> dict[str, str]:
        return dict(self._models)

    def price(self, model: str, usage: Usage) -> float:
        inp, out, cr, cw = self._prices[model]
        return (usage.input * inp + usage.output * out + usage.cache_read * cr + usage.cache_write * cw) / 1e6

    def estimate(self, request: Request, key: str | None = None) -> Usage:
        return Usage(input=rough_tokens(prompt_text(request)))

    def run(self, request: Request, key: str) -> Response:
        self.calls.append(request)
        if self.fail is not None:
            raise RuntimeError(self.fail.format(key=key))
        data = None
        if request.schema is not None:
            data = self.reply if self.reply is not None else sample(request.schema)
            text = json.dumps(data, ensure_ascii=False)
        elif self.reply is not None:
            text = str(self.reply)
        else:
            text = request.messages[-1].get("text", "") if request.messages else ""
        inp = rough_tokens(prompt_text(request))
        cached = inp // 2 if request.cache != "none" else 0
        usage = Usage(input=inp - cached, output=min(rough_tokens(text), request.max_tokens), cache_read=cached)
        return Response(text, data, usage, request.model or "", self.name)


def create(conf) -> FakeProvider:
    return FakeProvider()

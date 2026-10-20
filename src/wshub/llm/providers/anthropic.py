"""Провайдер anthropic — Claude API (ТЗ WSH-4.3): POST /v1/messages и /v1/messages/count_tokens через urllib, цены по
таблице, кэш, effort, схема ответа. Ключ — только в заголовке x-api-key: ни он, ни заголовки запроса не попадают в
тексты исключений; из заголовков ответа берётся только request-id — в текст ошибки, для поддержки. Ключ
~/.config/wshub/keys/anthropic берут и задачи wshub-code на ключе (ТЗ WSH-4.6)."""
from __future__ import annotations

import json
import ssl
import time
import urllib.error
import urllib.request

from ..provider import prompt_text, rough_tokens
from ..types import Request, Response, Usage

# id моделей — из ответа /v1/models (PoC WSH-4.0, api/models.json)
MODELS = {"cheap": "claude-haiku-5-5", "standard": "claude-sonnet-5-5", "strong": "claude-opus-5-5"}
# USD за миллион токенов: вход, выход, запись кэша 5 мин, запись кэша 1 ч, чтение кэша
# (platform.claude.com/docs/en/about-claude/pricing, сверка 2026-10-20)
PRICES = {
    "claude-opus-5-5": (4.0, 20.0, 5.0, 8.0, 0.20),
    "claude-sonnet-5-5": (2.0, 10.0, 2.50, 4.0, 0.10),
    "claude-haiku-5-5": (0.10, 0.50, 0.125, 0.20, 0.01),
}
# промпт больше LONG_FROM токенов (вход + запись + чтение кэша) — другая строка цен
LONG_FROM = 100_000
LONG_PRICES = {"claude-haiku-5-5": (0.50, 2.50, 0.625, 1.0, 0.05)}
BASE_URL = "https://api.anthropic.com"
API_VERSION = "2023-06-01"
TIMEOUT_S = 300
RETRY = (429, 529)  # один повтор после retry-after
RETRY_MAX_S = 60
RETRY_DEFAULT_S = 1  # retry-after нет или он не число секунд
CACHE_CONTROL = {"none": None, "short": {"type": "ephemeral"}, "long": {"type": "ephemeral", "ttl": "1h"}}


def _error(msg: str):
    from ..core import ApiError  # не на уровне модуля: ядро само импортирует словарь провайдеров
    return ApiError(msg)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Редирект не выполняется: urllib переслал бы x-api-key по новому адресу. 3xx — ошибка HTTP."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _retry_after(value: str | None) -> float:
    try:
        s = float(value) if value is not None else RETRY_DEFAULT_S
    except ValueError:
        s = RETRY_DEFAULT_S
    return min(max(s, 0.0), RETRY_MAX_S) if s == s else RETRY_DEFAULT_S  # nan — по умолчанию


def _http_error(e: urllib.error.HTTPError) -> str:
    """Код, error.type и error.message из тела; тело не JSON — его начало. Заголовки запроса сюда не попадают."""
    try:
        raw = e.read()
    except Exception:
        raw = b""
    msg = f"HTTP {e.code}"
    try:
        err = json.loads(raw).get("error")
        if not isinstance(err, dict):
            raise ValueError
        msg += f" {err.get('type', '')}: {str(err.get('message', ''))[:500]}"
    except (ValueError, AttributeError):
        text = raw.decode("utf-8", "replace").strip()[:200]
        if text:
            msg += f": {text}"
    rid = e.headers.get("request-id") if e.headers is not None else None
    return msg + (f" (request-id {rid})" if rid else "")


class AnthropicProvider:
    name = "anthropic"
    caps = frozenset({"cache", "schema", "count_tokens", "effort"})  # batch — задача WSH-4.5

    def __init__(self, base_url: str = BASE_URL, timeout_s: int = TIMEOUT_S):
        self.base_url = base_url.rstrip("/")
        self.timeout_s = timeout_s
        self._opener = urllib.request.build_opener(_NoRedirect(),
                                                   urllib.request.HTTPSHandler(context=ssl.create_default_context()))
        self._sleep = time.sleep

    def models(self) -> dict[str, str]:
        return dict(MODELS)

    def price(self, model: str, usage: Usage) -> float:
        """Запись в кэш — по цене 5 мин, кроме части с TTL 1 ч (cache_write_1h, если ответ дал разбивку)."""
        inp, out, w5m, w1h, read = PRICES[model]
        if model in LONG_PRICES and usage.input + usage.cache_write + usage.cache_read > LONG_FROM:
            inp, out, w5m, w1h, read = LONG_PRICES[model]
        long_write = min(usage.cache_write_1h, usage.cache_write)
        return (usage.input * inp + usage.output * out + (usage.cache_write - long_write) * w5m
                + long_write * w1h + usage.cache_read * read) / 1e6

    def estimate(self, request: Request, key: str | None = None) -> Usage:
        if key is None:
            return Usage(input=rough_tokens(prompt_text(request)))
        data = self._post("/v1/messages/count_tokens", self._body(request, with_max=False), key)
        n = data.get("input_tokens")
        if not isinstance(n, int) or isinstance(n, bool):
            raise _error("count_tokens: в ответе нет input_tokens")
        return Usage(input=n)

    def run(self, request: Request, key: str) -> Response:
        data = self._post("/v1/messages", self._body(request), key)
        content = data.get("content")
        text = "".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text") \
            if isinstance(content, list) else ""
        parsed, broken = None, False
        if request.schema is not None:
            try:
                parsed = json.loads(text)
            except ValueError:
                broken = True
        if broken:  # вне except: без цепочки исключений
            raise _error(f"ответ не разобран как JSON по схеме: {text[:200]}")
        u = data.get("usage") if isinstance(data.get("usage"), dict) else {}
        cc = u.get("cache_creation") if isinstance(u.get("cache_creation"), dict) else {}
        usage = Usage(input=int(u.get("input_tokens") or 0), output=int(u.get("output_tokens") or 0),
                      cache_read=int(u.get("cache_read_input_tokens") or 0),
                      cache_write=int(u.get("cache_creation_input_tokens") or 0),
                      cache_write_1h=int(cc.get("ephemeral_1h_input_tokens") or 0))
        return Response(text, parsed, usage, str(data.get("model") or request.model or ""), self.name)

    def _body(self, request: Request, with_max: bool = True) -> dict:
        body: dict = {"model": request.model,
                      "messages": [{"role": m.get("role", "user"), "content": m.get("text", "")}
                                   for m in request.messages]}
        if with_max:
            body["max_tokens"] = request.max_tokens
        if request.system:
            block = {"type": "text", "text": request.system}
            cc = CACHE_CONTROL.get(request.cache)
            if cc:
                block["cache_control"] = dict(cc)
            body["system"] = [block]
        oc: dict = {}
        if request.schema is not None:
            oc["format"] = {"type": "json_schema", "schema": request.schema}
        if request.effort is not None:
            oc["effort"] = request.effort
        if oc:
            body["output_config"] = oc
        return body

    def _post(self, path: str, body: dict, key: str) -> dict:
        """POST с ключом в заголовке. 429 и 529 — один повтор после retry-after (не больше 60 с), остальное — без
        повторов. Ошибка — ApiError без ключа и заголовков запроса; поднимается вне except (без цепочки)."""
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        for attempt in (1, 2):
            req = urllib.request.Request(self.base_url + path, data=data, method="POST",
                                         headers={"x-api-key": key, "anthropic-version": API_VERSION,
                                                  "content-type": "application/json"})
            error, wait, raw = None, None, None
            try:
                with self._opener.open(req, timeout=self.timeout_s) as r:
                    raw = r.read()
            except urllib.error.HTTPError as e:
                if e.code in RETRY and attempt == 1:
                    wait = _retry_after(e.headers.get("retry-after") if e.headers is not None else None)
                else:
                    error = _http_error(e)
                e.close()
            except urllib.error.URLError as e:
                error = (f"нет ответа за {self.timeout_s} с" if isinstance(e.reason, TimeoutError)
                         else f"соединение: {e.reason}")
            except TimeoutError:
                error = f"нет ответа за {self.timeout_s} с"
            except OSError as e:
                error = f"соединение: {type(e).__name__}"
            if error is not None:
                raise _error(error)
            if wait is not None:
                self._sleep(wait)
                continue
            try:
                out = json.loads(raw)
            except ValueError:
                out = None
            if not isinstance(out, dict):
                raise _error(f"ответ не JSON-объект: {(raw or b'').decode('utf-8', 'replace')[:200]}")
            return out
        raise _error("повтор не удался")  # недостижимо: вторая попытка либо отвечает, либо ошибка


def create(conf) -> AnthropicProvider:
    """conf — секция [api.providers.anthropic]; base_url и timeout_s не заданы — умолчания провайдера."""
    return AnthropicProvider(getattr(conf, "base_url", None) or BASE_URL,
                             getattr(conf, "timeout_s", None) or TIMEOUT_S)

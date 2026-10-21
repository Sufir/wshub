"""Провайдер anthropic — Claude API (ТЗ WSH-4.3): POST /v1/messages и /v1/messages/count_tokens через urllib, цены по
таблице, кэш, effort, схема ответа; пакеты /v1/messages/batches (ТЗ WSH-4.5). Ключ — только в заголовке x-api-key:
ни он, ни заголовки запроса не попадают в тексты исключений; из заголовков ответа берётся только request-id — в текст
ошибки, для поддержки. Ключ
~/.config/wshub/keys/anthropic берут и задачи wshub-code на ключе (ТЗ WSH-4.6)."""
from __future__ import annotations

import http.client
import json
import re
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request

from ..provider import prompt_text, rough_tokens
from ..types import BatchItem, Request, Response, Usage

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
BATCHES = "/v1/messages/batches"
BATCH_ID = re.compile(r"[A-Za-z0-9_-]{1,128}")  # id пакета идёт в путь запроса и в имя файла учёта


def _error(msg: str):
    from ..core import ApiError  # не на уровне модуля: ядро само импортирует словарь провайдеров
    return ApiError(msg)


def _paid(msg: str, usage: Usage | None = None, model: str | None = None, preview: str = ""):
    """Ответ получен (запрос оплачен), но не разобран — ядро запишет стоимость по usage (ТЗ WSH-4.4, п. 2.3а.1)."""
    from ..core import PaidError
    return PaidError(msg, usage, model, preview)


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
    caps = frozenset({"batch", "cache", "schema", "count_tokens", "effort"})

    def __init__(self, base_url: str = BASE_URL, timeout_s: int = TIMEOUT_S):
        self.base_url = base_url.rstrip("/")
        self.timeout_s = timeout_s
        handlers = [_NoRedirect(), urllib.request.HTTPSHandler(context=ssl.create_default_context())]
        if urllib.parse.urlsplit(self.base_url).hostname in ("127.0.0.1", "localhost"):
            handlers.append(urllib.request.ProxyHandler({}))  # http на loopback — мимо прокси окружения
        self._opener = urllib.request.build_opener(*handlers)
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
        data = self._post("/v1/messages/count_tokens", self._body(request, with_max=False), key, paid=False)
        n = data.get("input_tokens")
        if not isinstance(n, int) or isinstance(n, bool):
            raise _error("count_tokens: в ответе нет input_tokens")
        return Usage(input=n)

    def run(self, request: Request, key: str) -> Response:
        return self._message(self._post("/v1/messages", self._body(request), key), request)

    def _message(self, data: dict, request: Request | None) -> Response:
        """Ответ messages → Response; общий разбор run и результатов пакета (там request = None: модель — из ответа,
        схему разбирает ядро по учёту пакета)."""
        content = data.get("content")
        # текстовый блок без строки text — пропускается: ошибка разбора не должна обнулить оплаченный запрос
        text = "".join(b["text"] for b in content if isinstance(b, dict) and b.get("type") == "text"
                       and isinstance(b.get("text"), str)) if isinstance(content, list) else ""
        model = str(data.get("model") or (request.model if request else None) or "")
        raw_usage = data.get("usage")
        u = raw_usage if isinstance(raw_usage, dict) else {}
        cc = u.get("cache_creation") if isinstance(u.get("cache_creation"), dict) else {}
        usage, bad_usage = None, False
        try:  # usage — до разбора JSON: оплаченный ответ с битой схемой пишется по нему (ТЗ WSH-4.4, п. 2.3а.1)
            usage = Usage(input=int(u.get("input_tokens") or 0), output=int(u.get("output_tokens") or 0),
                          cache_read=int(u.get("cache_read_input_tokens") or 0),
                          cache_write=int(u.get("cache_creation_input_tokens") or 0),
                          cache_write_1h=int(cc.get("ephemeral_1h_input_tokens") or 0))
        except (TypeError, ValueError):
            bad_usage = True
        if bad_usage:  # вне except: без цепочки исключений
            raise _paid("usage ответа не разобран", None, model)
        if request is None and not isinstance(raw_usage, dict):  # результат пакета без usage — по оценке, а не $0
            raise _paid("в ответе нет usage", None, model)
        parsed, broken = None, False
        if request is not None and request.schema is not None:
            try:
                parsed = json.loads(text)
            except ValueError:
                broken = True
        if broken:  # вне except: без цепочки исключений; usage в ответе нет — ядро запишет оценку до запроса
            raise _paid("ответ не разобран как JSON по схеме", usage if isinstance(raw_usage, dict) else None, model,
                        text[:200])
        return Response(text, parsed, usage, model, self.name)

    # ---------- пакеты (ТЗ WSH-4.5; PoC WSH-4.0, п. 3.5.6) ----------

    def batch_submit(self, requests: list[tuple[str, Request]], key: str) -> str:
        body = {"requests": [{"custom_id": cid, "params": self._body(r)} for cid, r in requests]}
        data = self._post(BATCHES, body, key, paid=False)  # создание пакета не оплачивается
        bid = data.get("id")
        if not isinstance(bid, str) or not BATCH_ID.fullmatch(bid):
            raise _error("пакет: в ответе нет id — пакет мог быть создан: проверь консоль провайдера, прежде чем "
                         "повторять")
        return bid

    def batch_status(self, batch_id: str, key: str) -> dict:
        return self._batch_state(self._json("GET", self._batch_path(batch_id), None, key, paid=False))

    def batch_cancel(self, batch_id: str, key: str) -> dict:
        return self._batch_state(self._post(self._batch_path(batch_id) + "/cancel", None, key, paid=False))

    def batch_results(self, batch_id: str, key: str):
        """JSONL по адресу от base_url, а не по results_url ответа: ключ не уходит на чужой адрес. Порядок строк —
        как у провайдера (не как у запросов)."""
        raw = self._request("GET", self._batch_path(batch_id) + "/results", None, key)
        for line in raw.splitlines():
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except ValueError:
                row = None
            if not isinstance(row, dict) or not isinstance(row.get("custom_id"), str):
                raise _error("результаты пакета: строка без custom_id")
            yield self._batch_item(row["custom_id"], row.get("result"))

    def _batch_item(self, cid: str, result) -> BatchItem:
        result = result if isinstance(result, dict) else {}
        kind = result.get("type")
        if kind == "succeeded" and isinstance(result.get("message"), dict):
            from ..core import PaidError
            error = None
            try:
                resp = self._message(result["message"], None)
            except PaidError as e:  # usage не разобран: оплачен, ядро возьмёт оценку запроса
                error = (e.msg, e.model)
            if error is not None:
                return BatchItem(cid, None, error[0], error[1] or "?")
            return BatchItem(cid, resp)
        if kind == "errored":
            err = result.get("error")
            err = err.get("error", err) if isinstance(err, dict) else {}
            err = err if isinstance(err, dict) else {}
            return BatchItem(cid, None, f"errored {err.get('type', '')}: {str(err.get('message', ''))[:500]}")
        if kind in ("canceled", "expired"):  # не выполнен и не оплачен
            return BatchItem(cid, None, kind)
        return BatchItem(cid, None, f"результат не разобран: {str(kind)[:40]}")

    @staticmethod
    def _batch_path(batch_id: str) -> str:
        if not BATCH_ID.fullmatch(batch_id):  # id идёт в путь запроса
            raise _error(f"неверный id пакета: {batch_id[:80]!r}")
        return f"{BATCHES}/{batch_id}"

    @staticmethod
    def _batch_state(data: dict) -> dict:
        counts = data.get("request_counts")
        counts = {k: v for k, v in counts.items() if isinstance(v, int)} if isinstance(counts, dict) else {}
        def text(k: str) -> str | None:  # в учёт — только короткие строки, а не что угодно из ответа
            v = data.get(k)
            return v[:40] if isinstance(v, str) else None
        status = text("processing_status")
        return {"status": status or "?", "counts": counts, "ended_at": text("ended_at"),
                "expires_at": text("expires_at")}

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

    def _post(self, path: str, body: dict | None, key: str, paid: bool = True) -> dict:
        return self._json("POST", path, body, key, paid)

    def _json(self, method: str, path: str, body: dict | None, key: str, paid: bool) -> dict:
        """Запрос с ответом — JSON-объектом. paid — запрос платный (messages): успешный ответ, который не JSON-объект,
        — PaidError."""
        raw = self._request(method, path, body, key)
        try:
            out = json.loads(raw)
        except ValueError:
            out = None
        if not isinstance(out, dict):
            head = (raw or b"").decode("utf-8", "replace")[:200]
            if paid:  # HTTP-ответ messages успешный — запрос мог быть оплачен (ТЗ WSH-4.4, п. 2.3а.1)
                raise _paid("ответ не JSON-объект", preview=head)
            raise _error(f"ответ не JSON-объект: {head}")  # count_tokens бесплатен — обычный отказ, как в 4.3
        return out

    def _request(self, method: str, path: str, body: dict | None, key: str) -> bytes:
        """Запрос с ключом в заголовке; ответ — сырые байты. 429 и 529 — один повтор после retry-after (не больше
        60 с), остальное — без повторов. Ошибка — ApiError без ключа и заголовков запроса; поднимается вне except
        (без цепочки)."""
        data = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers = {"x-api-key": key, "anthropic-version": API_VERSION}
        if data is not None:
            headers["content-type"] = "application/json"
        for attempt in (1, 2):
            req = urllib.request.Request(self.base_url + path, data=data, method=method, headers=headers)
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
            except (ValueError, http.client.HTTPException) as e:  # текст ValueError заголовка содержал бы ключ
                error = f"запрос не прошёл: {type(e).__name__}"
            except OSError as e:
                error = f"соединение: {type(e).__name__}"
            if error is not None:
                raise _error(error)
            if wait is not None:
                self._sleep(wait)
                continue
            return raw or b""
        raise _error("повтор не удался")  # недостижимо: вторая попытка либо отвечает, либо ошибка


def create(conf) -> AnthropicProvider:
    """conf — секция [api.providers.anthropic]; base_url и timeout_s не заданы — умолчания провайдера."""
    return AnthropicProvider(getattr(conf, "base_url", None) or BASE_URL,
                             getattr(conf, "timeout_s", None) or TIMEOUT_S)

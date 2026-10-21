"""Провайдер anthropic (ТЗ WSH-4.3, п. 3): запросы против локального HTTP-сервера на 127.0.0.1 — сети нет. Тело и
заголовки запроса, разбор ответа, цены, count_tokens, ошибки и повтор 429/529, ключ не утекает; CLI на нём."""
from __future__ import annotations

import asyncio
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from test_api import ApiEnv, set_api

from wshub.llm import keys
from wshub.llm.core import ApiError
from wshub.llm.providers import PROVIDERS
from wshub.llm.types import Request, Usage

MARKER = "sk-ant-api03-WSH43-MARKER-9d2b7e41aa"
HAIKU, SONNET, OPUS = "claude-haiku-5-5", "claude-sonnet-5-5", "claude-opus-5-5"
# usage ответа из PoC WSH-4.0, 3.5.2 (запись кэша на 5 минут)
USAGE_5M = {"input_tokens": 16, "cache_creation_input_tokens": 2018, "cache_read_input_tokens": 0,
            "cache_creation": {"ephemeral_5m_input_tokens": 2018, "ephemeral_1h_input_tokens": 0},
            "output_tokens": 5, "output_tokens_details": {"thinking_tokens": 0}, "service_tier": "standard"}
# таблица ТЗ п. 1 — литералами, а не из модуля: опечатка в таблице провайдера краснит тест
TABLE = {OPUS: (4, 20, 5, 8, 0.20), SONNET: (2, 10, 2.50, 4, 0.10), HAIKU: (0.10, 0.50, 0.125, 0.20, 0.01)}
HAIKU_LONG = (0.50, 2.50, 0.625, 1, 0.05)


def msg(text: str = "Ок", model: str = HAIKU, usage: dict | None = None) -> dict:
    return {"model": model, "id": "msg_1", "type": "message", "role": "assistant", "stop_reason": "end_turn",
            "content": [{"type": "thinking", "thinking": "думаю"}, {"type": "text", "text": text}],
            "usage": usage or USAGE_5M}


def err(etype: str, message: str) -> dict:
    return {"type": "error", "error": {"type": etype, "message": message}}


class Server:
    """HTTP-сервер в потоке: отвечает по очереди сценариев (код, заголовки, тело, задержка), пишет запросы."""

    def __init__(self):
        self.script: list[tuple] = []
        self.seen: list[tuple[str, dict, bytes]] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                raw = self.rfile.read(int(self.headers.get("content-length") or 0))
                outer.seen.append((self.path, {k.lower(): v for k, v in self.headers.items()}, raw))
                status, hdrs, body, delay = outer.script.pop(0) if outer.script else (200, {}, msg(), 0)
                if delay:
                    time.sleep(delay)
                data = body if isinstance(body, bytes) else json.dumps(body).encode()
                try:
                    self.send_response(status)
                    for k, v in hdrs.items():
                        self.send_header(k, v)
                    self.send_header("content-length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                except OSError:  # клиент уже ушёл по таймауту
                    pass

            def log_message(self, *a):
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        self.httpd.block_on_close = False
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def reply(self, status: int = 200, body=None, headers: dict | None = None, delay: float = 0) -> None:
        self.script.append((status, headers or {}, msg() if body is None else body, delay))

    def body(self, i: int = -1) -> dict:
        return json.loads(self.seen[i][2])

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def srv(monkeypatch):
    for k in ("http_proxy", "https_proxy", "all_proxy", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
        monkeypatch.delenv(k, raising=False)  # к 127.0.0.1 — напрямую
    s = Server()
    yield s
    s.close()


def provider(srv, timeout_s: int = 5):
    conf = type("Conf", (), {"base_url": srv.url, "timeout_s": timeout_s})()
    prov = PROVIDERS["anthropic"].create(conf)
    prov.sleeps = []
    prov._sleep = prov.sleeps.append  # повтор без настоящего ожидания
    return prov


def rq(**kw) -> Request:
    base = {"system": "", "messages": ({"role": "user", "text": "ответь: ок"},), "project": "payload",
            "model": HAIKU}
    return Request(**{**base, **kw})


def raised(call) -> ApiError:
    with pytest.raises(ApiError) as ei:
        call()
    return ei.value


# ---------- п. 3.1: тело и заголовки ----------

@pytest.mark.parametrize("cache,control", [("none", None), ("short", {"type": "ephemeral"}),
                                           ("long", {"type": "ephemeral", "ttl": "1h"})])
def test_body_system_cache_control(srv, cache, control):
    provider(srv).run(rq(system="контекст", cache=cache, max_tokens=50), MARKER)
    path, hdrs, raw = srv.seen[-1]
    block = {"type": "text", "text": "контекст", **({"cache_control": control} if control else {})}
    assert path == "/v1/messages"
    assert srv.body() == {"model": HAIKU, "max_tokens": 50, "system": [block],
                          "messages": [{"role": "user", "content": "ответь: ок"}]}
    assert hdrs["x-api-key"] == MARKER and hdrs["anthropic-version"] == "2023-06-01"
    assert hdrs["content-type"] == "application/json"
    assert MARKER.encode() not in raw and MARKER not in path  # ключ — только в заголовке


def test_body_schema_and_effort(srv):
    schema = {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"]}
    srv.reply(body=msg('{"ok": true}'))
    provider(srv).run(rq(schema=schema, effort="low"), MARKER)
    body = srv.body()
    assert body["output_config"] == {"format": {"type": "json_schema", "schema": schema}, "effort": "low"}
    assert "system" not in body  # пустой system не отправляется
    provider(srv).run(rq(), MARKER)
    assert "output_config" not in srv.body()


# ---------- п. 3.2: ответ, usage, цены ----------

def test_response_text_and_usage(srv):
    usage = {**USAGE_5M, "cache_read_input_tokens": 300, "cache_creation_input_tokens": 2027,
             "cache_creation": {"ephemeral_5m_input_tokens": 0, "ephemeral_1h_input_tokens": 2027}}
    srv.reply(body=msg("Ок", model="claude-haiku-5-5-20261001", usage=usage))
    resp = provider(srv).run(rq(), MARKER)
    assert (resp.text, resp.data, resp.provider, resp.raw_ref) == ("Ок", None, "anthropic", None)
    assert resp.model == "claude-haiku-5-5-20261001"  # фактическая модель — из ответа
    assert resp.usage == Usage(input=16, output=5, cache_read=300, cache_write=2027, cache_write_1h=2027)
    srv.reply(body=msg())
    assert provider(srv).run(rq(), MARKER).usage == Usage(input=16, output=5, cache_write=2018)  # без 1 ч


def test_response_schema_data_and_broken_json(srv):
    schema = {"type": "object"}
    srv.reply(body=msg('{"label": "neutral", "confidence": 0.6}'))
    resp = provider(srv).run(rq(schema=schema), MARKER)
    assert resp.data == {"label": "neutral", "confidence": 0.6}
    broken = "не JSON: " + "x" * 300
    srv.reply(body=msg(broken))
    e = raised(lambda: provider(srv).run(rq(schema=schema), MARKER))
    assert "ответ не разобран как JSON по схеме" in str(e) and broken[:200] in str(e) and broken[:201] not in str(e)
    assert e.__context__ is None and e.__cause__ is None
    srv.reply(body=msg(broken))
    assert provider(srv).run(rq(), MARKER).text == broken  # без схемы текст не разбирается


@pytest.mark.parametrize("model", [OPUS, SONNET, HAIKU])
def test_price_table(model):
    prov = PROVIDERS["anthropic"].create(None)
    inp, out, w5m, w1h, read = TABLE[model]
    u = Usage(input=1000, output=2000, cache_read=3000, cache_write=4000, cache_write_1h=1000)
    want = (1000 * inp + 2000 * out + 3000 * read + 3000 * w5m + 1000 * w1h) / 1e6
    assert prov.price(model, u) == pytest.approx(want)
    # без разбивки по TTL вся запись — по цене 5 мин; 100 000 токенов — ещё не порог Haiku
    assert prov.price(model, Usage(cache_write=10**5)) == pytest.approx(w5m / 10)
    assert prov.price(model, Usage(cache_write=10**5, cache_write_1h=10**5)) == pytest.approx(w1h / 10)
    assert prov.price(model, Usage(input=10**5)) == pytest.approx(inp / 10)
    assert prov.price(model, Usage(cache_read=10**5)) == pytest.approx(read / 10)
    assert prov.price(model, Usage(output=10**6)) == pytest.approx(out)


def test_price_haiku_threshold_and_unknown():
    prov = PROVIDERS["anthropic"].create(None)
    # промпт ровно 100 000 и 100 001; часть записи — с TTL 1 ч
    at = Usage(input=40_000, cache_write=30_000, cache_write_1h=10_000, cache_read=30_000, output=1000)
    over = Usage(input=40_001, cache_write=30_000, cache_write_1h=10_000, cache_read=30_000, output=1000)
    for u, (inp, out, w5m, w1h, read) in ((at, TABLE[HAIKU]), (over, HAIKU_LONG)):
        want = (u.input * inp + u.output * out + 20_000 * w5m + 10_000 * w1h + u.cache_read * read) / 1e6
        assert prov.price(HAIKU, u) == pytest.approx(want)
    assert prov.price(SONNET, over) == pytest.approx(prov.price(SONNET, at) + 2 / 1e6)  # порог — только у Haiku
    with pytest.raises(KeyError):
        prov.price("claude-haiku-4-5", Usage(input=1))


def test_create_defaults():
    """Умолчания провайдера: https://api.anthropic.com и таймаут 300 с — и без секции, и при незаданных ключах."""
    from wshub.registry import ApiProvider
    for conf in (None, ApiProvider()):
        prov = PROVIDERS["anthropic"].create(conf)
        assert (prov.base_url, prov.timeout_s) == ("https://api.anthropic.com", 300)
    prov = PROVIDERS["anthropic"].create(ApiProvider(base_url="https://proxy.local/a/", timeout_s=30))
    assert (prov.base_url, prov.timeout_s) == ("https://proxy.local/a", 30)


# ---------- п. 3.3: estimate ----------

def test_estimate_with_key_count_tokens_without_key_rough(srv):
    srv.reply(body={"input_tokens": 2034})
    prov = provider(srv)
    req = rq(system="контекст", cache="short", effort="low", max_tokens=50)
    assert prov.estimate(req, key=MARKER) == Usage(input=2034)
    path, hdrs, _ = srv.seen[-1]
    assert path == "/v1/messages/count_tokens" and hdrs["x-api-key"] == MARKER
    body = srv.body()
    assert "max_tokens" not in body and body["model"] == HAIKU and body["output_config"] == {"effort": "low"}
    assert body["system"][0]["cache_control"] == {"type": "ephemeral"}
    n = len(srv.seen)
    assert prov.estimate(req) == Usage(input=(len("контекст\nответь: ок") + 3) // 4)  # грубо, без запроса
    assert len(srv.seen) == n


# ---------- п. 3.4: ошибки и повтор ----------

def test_error_400_text_and_request_id(srv):
    srv.reply(400, err("invalid_request_error", "max_tokens: 99999999 > 64000"), {"request-id": "req_011CgX"})
    e = raised(lambda: provider(srv).run(rq(), MARKER))
    assert str(e) == "HTTP 400 invalid_request_error: max_tokens: 99999999 > 64000 (request-id req_011CgX)"
    assert len(srv.seen) == 1  # без повтора
    srv.reply(500, b"<html>oops</html>")
    assert str(raised(lambda: provider(srv).run(rq(), MARKER))) == "HTTP 500: <html>oops</html>"
    assert len(srv.seen) == 2


def test_error_401_without_key(srv):
    """Исключение самого провайдера — до scrub ядра: ни ключа, ни заголовков запроса в тексте, args и цепочке."""
    srv.reply(401, err("authentication_error", "invalid x-api-key"), {"request-id": "req_401"})
    e = raised(lambda: provider(srv).run(rq(), MARKER))
    text = f"{e} {e.args!r} {e!r}"
    assert "HTTP 401 authentication_error: invalid x-api-key" in str(e)
    assert MARKER not in text and MARKER[:12] not in text and MARKER[-12:] not in text
    assert "anthropic-version" not in text and "x-api-key:" not in text
    assert e.__context__ is None and e.__cause__ is None


def test_retry_429_once_then_success(srv):
    srv.reply(429, err("rate_limit_error", "slow down"), {"retry-after": "7"})
    srv.reply(body=msg("после повтора"))
    prov = provider(srv)
    assert prov.run(rq(), MARKER).text == "после повтора"
    assert len(srv.seen) == 2 and prov.sleeps == [7.0]


def test_retry_529_twice_fails(srv):
    srv.reply(529, err("overloaded_error", "Overloaded"))
    srv.reply(529, err("overloaded_error", "Overloaded"))
    srv.reply(body=msg("третьего запроса быть не должно"))
    prov = provider(srv)
    e = raised(lambda: prov.run(rq(), MARKER))
    assert str(e).startswith("HTTP 529 overloaded_error: Overloaded")
    assert len(srv.seen) == 2 and prov.sleeps == [1]  # retry-after нет — 1 с


def test_retry_after_capped(srv):
    srv.reply(429, err("rate_limit_error", "x"), {"retry-after": "120"})
    srv.reply(body=msg())
    prov = provider(srv)
    prov.run(rq(), MARKER)
    assert prov.sleeps == [60]
    srv.reply(429, err("rate_limit_error", "x"), {"retry-after": "Wed, 21 Oct 2026 07:28:00 GMT"})  # не секунды
    srv.reply(body=msg())
    prov.run(rq(), MARKER)
    assert prov.sleeps == [60, 1]


def test_other_5xx_not_retried(srv):
    srv.reply(503, err("api_error", "unavailable"))
    srv.reply(body=msg())
    prov = provider(srv)
    assert str(raised(lambda: prov.run(rq(), MARKER))).startswith("HTTP 503 api_error: unavailable")
    assert len(srv.seen) == 1 and prov.sleeps == []


def test_timeout(srv):
    srv.reply(body=msg(), delay=5)
    t0 = time.monotonic()
    e = raised(lambda: provider(srv, timeout_s=1).run(rq(), MARKER))
    assert str(e) == "нет ответа за 1 с" and time.monotonic() - t0 < 4


def test_bad_key_value_not_in_error(srv):
    """Перевод строки внутри ключа: http.client отвергает заголовок ValueError с ключом в тексте — провайдер отдаёт
    ApiError без значения ключа, запрос не уходит."""
    key = "sk-ant-api03-FIRSTPART\ntail7"
    e = raised(lambda: provider(srv).run(rq(), key))
    text = f"{e} {e.args!r} {e!r}"
    assert str(e) == "запрос не прошёл: ValueError" and "FIRSTPART" not in text and "tail7" not in text
    assert e.__context__ is None and srv.seen == []


def test_loopback_bypasses_env_proxy(srv, monkeypatch):
    """http на 127.0.0.1 — мимо прокси из окружения: ключ открытым текстом не уходит постороннему процессу."""
    for k in ("http_proxy", "HTTP_PROXY"):
        monkeypatch.setenv(k, "http://127.0.0.1:9")  # прокси нет — через него запрос бы не прошёл
    assert provider(srv).run(rq(), MARKER).text == "Ок" and len(srv.seen) == 1


def test_redirect_not_followed(srv):
    """Редирект — ошибка, второго запроса (с ключом по новому адресу) нет."""
    srv.reply(307, err("redirect", "moved"), {"location": srv.url + "/elsewhere"})
    e = raised(lambda: provider(srv).run(rq(), MARKER))
    assert str(e).startswith("HTTP 307") and len(srv.seen) == 1


# ---------- ядро и CLI на anthropic ----------

@pytest.fixture
def api(env, capsys, srv) -> ApiEnv:
    a = ApiEnv(env, capsys)
    a.srv = srv
    a.section = f'[api.providers.anthropic]\nbase_url = "{srv.url}"\ntimeout_s = 5\n'
    set_api(env, "[api]\nenabled = true\n\n" + a.section)  # ни потолков, ни included_usd (п. 3.6)
    keys.write(keys.key_path(env.cfg.parent, "anthropic"), MARKER)
    return a


def test_cli_run_without_money_limits(api):
    """П. 3.6: без included_usd, daily_usd и max_usd_per_call запрос проходит, строка в журнале есть."""
    api.srv.reply(body={"input_tokens": 2034})  # count_tokens для оценки — ключ есть
    api.srv.reply(body=msg("Ок"))
    rc, out, err_ = api.run(provider="anthropic", tier="cheap")
    assert rc == 0, err_
    assert [p for p, _h, _b in api.srv.seen] == ["/v1/messages/count_tokens", "/v1/messages"]
    lines = out.splitlines()
    assert lines[0] == "Ок" and lines[1] == "API: claude-haiku-5-5 · вход 2034 (из кэша 0) · выход 5 · ≈$0.00"
    row = api.rows()[-1]
    assert (row["provider"], row["model"], row["zone"], row["ok"]) == ("anthropic", HAIKU, "included", True)
    assert row["cost_usd"] == pytest.approx((16 * 0.10 + 5 * 0.50 + 2018 * 0.125) / 1e6)
    assert row["usage"] == {"input": 16, "output": 5, "cache_read": 0, "cache_write": 2018, "cache_write_1h": 0}
    rc, out, _ = api.cli("stats")
    assert rc == 0 and "(daily_usd не задан)" in out and "anthropic: зоны не проверяются" in out
    assert "по моделям: claude-haiku-5-5" in out


def test_cli_estimate_exact_with_key_rough_without(api):
    api.srv.reply(body={"input_tokens": 2034})
    rc, out, _ = api.run("--estimate", provider="anthropic")
    assert rc == 0 and "вход ≈2034 · " in out and "(грубо)" not in out and "зоны не проверяются" in out
    assert [p for p, _h, _b in api.srv.seen] == ["/v1/messages/count_tokens"]
    keys.key_path(api.env.cfg.parent, "anthropic").unlink()
    rc, out, _ = api.run("--estimate", provider="anthropic")
    assert rc == 0 and "вход ≈4 (грубо)" in out and len(api.srv.seen) == 1


def test_cli_401_key_not_in_output_or_ledger(api):
    api.srv.reply(body={"input_tokens": 10})
    api.srv.reply(401, err("authentication_error", "invalid x-api-key"))
    rc, out, err_ = api.run(provider="anthropic")
    assert rc == 1 and "провайдер anthropic: ApiError: HTTP 401 authentication_error: invalid x-api-key" in err_
    assert MARKER not in out + err_ and MARKER[:12] not in out + err_
    row = api.rows()[-1]
    assert row["ok"] is False and "authentication_error" in row["error"]
    assert MARKER[:12].encode() not in api.ledger_path.read_bytes()


def test_estimate_failure_refused_without_key(api):
    """Сбой count_tokens (ключ отвергнут) — отказ до запроса, без ключа в тексте и без строки журнала."""
    api.srv.reply(401, err("authentication_error", "invalid x-api-key"))
    rc, out, err_ = api.run(provider="anthropic")
    assert rc == 1 and "провайдер anthropic: оценка входа: ApiError: HTTP 401" in err_
    assert MARKER not in out + err_ and len(api.srv.seen) == 1 and api.rows() == []


def test_model_outside_tiers_needs_operator_choice(api):
    """П. 3.5 на anthropic: Haiku убрана из уровней переопределением — явная --model только с --choice operator."""
    set_api(api.env, "[api]\nenabled = true\n\n" + api.section + 'models = { cheap = "claude-sonnet-5-5" }\n')
    rc, _, err_ = api.run("--model", HAIKU, tier=None, provider="anthropic")
    assert rc == 1 and f"модель {HAIKU} не из уровней провайдера anthropic" in err_ and "--choice operator" in err_
    assert "cheap — claude-sonnet-5-5" in err_ and "strong — claude-opus-5-5" in err_
    assert api.srv.seen == []
    api.srv.reply(body={"input_tokens": 10})  # count_tokens
    api.srv.reply(body=msg())
    rc, out, err_ = api.run("--model", HAIKU, "--choice", "operator", tier=None, provider="anthropic")
    assert rc == 0, err_
    assert api.srv.body()["model"] == HAIKU and "API: claude-haiku-5-5" in out
    n = len(api.srv.seen)
    rc, _, err_ = api.run("--model", "claude-haiku-4-5", "--choice", "operator", tier=None, provider="anthropic")
    assert rc == 1 and "цена модели claude-haiku-4-5" in err_ and len(api.srv.seen) == n  # до count_tokens


# ---------- хвосты 4.3 (ТЗ WSH-4.4, п. 2.3а) ----------

def test_broken_json_paid_by_usage(api):
    """Битый JSON при схеме: запрос оплачен — строка журнала по usage ответа, а не $0 (п. 2.3а.1)."""
    (api.env.proj / "s.json").write_text('{"type": "object"}', encoding="utf-8")
    api.srv.reply(body={"input_tokens": 10})
    api.srv.reply(body=msg("не JSON вовсе"))
    rc, _, err_ = api.run("--schema", "s.json", provider="anthropic")
    assert rc == 1 and "ответ не разобран как JSON по схеме: не JSON вовсе" in err_
    row = api.rows()[-1]
    assert row["ok"] is False and row["model"] == HAIKU
    assert row["cost_usd"] == pytest.approx((16 * 0.10 + 5 * 0.50 + 2018 * 0.125) / 1e6)
    assert row["usage"] == {"input": 16, "output": 5, "cache_read": 0, "cache_write": 2018, "cache_write_1h": 0}


def test_non_json_200_paid_by_estimate(api):
    """Успешный HTTP-ответ, но не JSON-объект: usage нет — в журнал оценка до запроса (п. 2.3а.1)."""
    api.srv.reply(body={"input_tokens": 10})
    api.srv.reply(body=b"<html>oops</html>")
    rc, _, err_ = api.run(provider="anthropic")
    assert rc == 1 and "ответ не JSON-объект: <html>oops</html>" in err_
    row = api.rows()[-1]
    assert row["ok"] is False and row["cost_usd"] == pytest.approx((10 * 0.10 + 1024 * 0.50) / 1e6)
    api.srv.reply(body={"input_tokens": 10})
    api.srv.reply(body=msg(usage={"input_tokens": "много", "output_tokens": 5}))  # usage не разобран
    rc, _, err_ = api.run(provider="anthropic")
    assert rc == 1 and "usage ответа не разобран" in err_
    assert api.rows()[-1]["cost_usd"] == pytest.approx((10 * 0.10 + 1024 * 0.50) / 1e6)


def test_estimate_cache_write_prices(api):
    """П. 2.3а.2 на цифрах Haiku: system «s»×400 — 100 токенов по цене записи кэша (5 мин — 0.125, 1 ч — 0.20)."""
    eng = api.engine()

    def plan(cache):
        return eng.plan(Request("s" * 400, ({"role": "user", "text": "привет"},), "payload", tier="cheap",
                                cache=cache), "anthropic")
    api.srv.reply(body={"input_tokens": 500})  # с ключом — точный вход из count_tokens
    p = plan("short")
    assert (p.estimate.input, p.estimate.cache_write, p.estimate.cache_write_1h) == (400, 100, 0)
    assert p.estimate_usd == pytest.approx((400 * 0.10 + 100 * 0.125 + 1024 * 0.50) / 1e6)
    keys.key_path(api.env.cfg.parent, "anthropic").unlink()  # без ключа — грубо: 407 символов ≈ 102 токена
    p = plan("long")
    assert (p.estimate.input, p.estimate.cache_write, p.estimate.cache_write_1h) == (2, 100, 100)
    assert p.estimate_usd == pytest.approx((2 * 0.10 + 100 * 0.20 + 1024 * 0.50) / 1e6)
    p = plan("none")
    assert p.estimate_usd == pytest.approx((102 * 0.10 + 1024 * 0.50) / 1e6)


# ---------- без настроек реестра (ТЗ WSH-4.4, п. 2.3б) ----------

def test_no_registry_settings_reaches_http(api, monkeypatch):
    """Реестр без [api] и [api.providers.*], ключ anthropic: CLI run и api_run проходят до HTTP (п. 2.3б.4).
    Секции с base_url нет — адрес провайдера по умолчанию подменён на локальный сервер."""
    from wshub import api_server
    monkeypatch.setattr(PROVIDERS["anthropic"], "BASE_URL", api.srv.url)
    set_api(api.env, "", ws="")
    keys.key_path(api.env.cfg.parent, "fake").unlink()  # единственный доступный провайдер — anthropic
    api.srv.reply(body={"input_tokens": 12})
    api.srv.reply(body=msg("Ок из CLI"))
    rc, out, err_ = api.run(provider="anthropic")
    assert rc == 0, err_
    assert out.splitlines()[0] == "Ок из CLI" and "цикл" not in out
    assert [p for p, _h, _b in api.srv.seen] == ["/v1/messages/count_tokens", "/v1/messages"]
    assert (api.rows()[-1]["provider"], api.rows()[-1]["zone"]) == ("anthropic", "included")
    mcp = api_server.build_api_server(api.env.hub, now=lambda: api.t)

    def call(name, **args):
        res = asyncio.run(mcp.call_tool(name, args))
        return (res[0] if isinstance(res, tuple) else res)[0].text
    api.srv.reply(body={"input_tokens": 12})
    api.srv.reply(body=msg("Ок из чата"))
    out = call("api_run", project="payload", input_file="q.txt", tier="cheap")  # provider по умолчанию
    assert out.startswith("Ок из чата\nAPI: claude-haiku-5-5"), out
    assert len(api.srv.seen) == 4 and api.srv.seen[-1][1]["x-api-key"] == MARKER
    assert [p["provider"] for p in json.loads(call("api_models"))["providers"]] == ["anthropic"]
    n = len(api.srv.seen)
    set_api(api.env, "", ws="[workspace.payload.api]\nenabled = false\n")  # проект закрыт — отказ до HTTP
    assert "закрыт от отправки в API" in call("api_run", project="payload", input_file="q.txt", tier="cheap")
    assert "закрыт от отправки в API" in api.run(provider="anthropic")[2]
    set_api(api.env, "[api]\nenabled = false\n", ws="")
    assert "API хаба выключен" in api.run(provider="anthropic")[2]
    set_api(api.env, "", ws="")
    keys.key_path(api.env.cfg.parent, "anthropic").unlink()
    rc, _, err_ = api.run(provider="anthropic")
    assert rc == 1 and "нет секции [api.providers.anthropic] в реестре и нет годного ключа" in err_
    assert "провайдеров нет" in call("api_run", project="payload", input_file="q.txt", tier="cheap")
    assert len(api.srv.seen) == n

"""Пакетные запросы (ТЗ WSH-4.5): тело пакета anthropic, раскладка результатов по custom_id, цена со скидкой, журнал
без дублей, out_dir, ключ не утекает; CLI wshub api batch и инструменты api_batch_* сервера wshub-api."""
from __future__ import annotations

import json
from dataclasses import replace

import pytest
from test_api import MARKER, ApiEnv, set_api, use
from test_api_anthropic import HAIKU, Server, msg, provider, rq
from test_api_anthropic import MARKER as AMARKER
from test_api_server import Srv

from wshub.core import BLOCKED
from wshub.llm import batches, cli, keys
from wshub.llm.core import Api, ApiError
from wshub.llm.providers import fake

SUBMIT = ("batch", "submit", "--provider", "fake", "--project", "payload", "--tier", "cheap")
# usage r1 из PoC WSH-4.0, 3.5.6: запись system в кэш на 1 ч, service_tier batch
USAGE_B = {"input_tokens": 31, "cache_creation_input_tokens": 2267, "cache_read_input_tokens": 0,
           "cache_creation": {"ephemeral_5m_input_tokens": 0, "ephemeral_1h_input_tokens": 2267},
           "output_tokens": 18, "service_tier": "batch"}


@pytest.fixture
def srv(monkeypatch):
    for k in ("http_proxy", "https_proxy", "all_proxy", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
        monkeypatch.delenv(k, raising=False)
    s = Server()
    yield s
    s.close()


@pytest.fixture
def api(env, capsys) -> ApiEnv:
    a = ApiEnv(env, capsys)
    (env.proj / "in").mkdir()
    for name, text in (("one", "текст один"), ("two", "текст два"), ("three", "текст три")):
        (env.proj / "in" / f"{name}.txt").write_text(text, encoding="utf-8")
    return a


@pytest.fixture
def aapi(env, capsys, srv) -> ApiEnv:
    """anthropic на локальном сервере, без денежных настроек."""
    a = ApiEnv(env, capsys)
    a.srv = srv
    a.section = f'[api.providers.anthropic]\nbase_url = "{srv.url}"\ntimeout_s = 5\n'
    set_api(env, "[api]\nenabled = true\n\n" + a.section)
    keys.write(keys.key_path(env.cfg.parent, "anthropic"), AMARKER)
    (env.proj / "in").mkdir()
    (env.proj / "in" / "one.txt").write_text("отзыв один", encoding="utf-8")
    (env.proj / "in" / "two.txt").write_text("отзыв два", encoding="utf-8")
    (env.proj / "sys.txt").write_text("Классифицируй отзыв.", encoding="utf-8")
    return a


def created(bid: str = "msgbatch_01Abc", status: str = "in_progress", **counts) -> dict:
    return {"id": bid, "type": "message_batch", "processing_status": status,
            "request_counts": {"processing": 0, "succeeded": 0, "errored": 0, "canceled": 0, "expired": 0, **counts},
            "ended_at": None, "expires_at": "2026-10-19T09:00:00+00:00", "results_url": None}


def batch_rows(api: ApiEnv, bid: str) -> list[dict]:
    return [r for r in api.rows() if r.get("batch") == bid]


# ---------- провайдер anthropic: тело пакета, результаты (тесты 1–2) ----------

def test_batch_body_params_equal_single_run(srv):
    """Тест 1: params каждого запроса — тело одиночного /v1/messages; custom_id; кэш с ttl 1h на system."""
    prov = provider(srv)
    one = rq(system="общий контекст", cache="long", max_tokens=50)
    two = rq(system="общий контекст", cache="long", max_tokens=50, messages=({"role": "user", "text": "второй"},))
    prov.run(one, AMARKER)
    single = srv.body()
    srv.reply(body=created())
    assert prov.batch_submit([("r1", one), ("r2", two)], AMARKER) == "msgbatch_01Abc"
    path, hdrs, raw = srv.seen[-1]
    assert path == "/v1/messages/batches"
    assert srv.body() == {"requests": [
        {"custom_id": "r1", "params": single},
        {"custom_id": "r2", "params": {**single, "messages": [{"role": "user", "content": "второй"}]}}]}
    assert single["system"] == [{"type": "text", "text": "общий контекст",
                                 "cache_control": {"type": "ephemeral", "ttl": "1h"}}]
    assert hdrs["x-api-key"] == AMARKER and AMARKER.encode() not in raw
    srv.reply(body={**created(), "id": "../evil"})  # id идёт в путь и в имя файла — только [A-Za-z0-9_-]
    with pytest.raises(ApiError, match="нет id"):
        prov.batch_submit([("r1", one)], AMARKER)


def test_batch_results_shuffled_by_custom_id(srv):
    """Тест 2 на уровне провайдера: строки JSONL в чужом порядке — каждая со своим custom_id; ошибки — текстом."""
    prov = provider(srv)
    lines = [{"custom_id": "r2", "result": {"type": "errored", "error": {"type": "error", "error": {
                 "type": "invalid_request_error", "message": "плохой запрос"}}}},
             {"custom_id": "r1", "result": {"type": "succeeded", "message": msg("ответ r1", usage=USAGE_B)}},
             {"custom_id": "r3", "result": {"type": "expired"}}]
    srv.reply(body=b"\n".join(json.dumps(x, ensure_ascii=False).encode() for x in lines) + b"\n")
    items = list(prov.batch_results("msgbatch_01Abc", AMARKER))
    assert srv.seen[-1][0] == "/v1/messages/batches/msgbatch_01Abc/results"
    assert [i.custom_id for i in items] == ["r2", "r1", "r3"]
    assert items[0].response is None and items[0].model is None
    assert items[0].error == "errored invalid_request_error: плохой запрос"
    assert items[1].response.text == "ответ r1" and items[1].response.usage.cache_write_1h == 2267
    assert (items[2].response, items[2].error) == (None, "expired")
    srv.reply(body=created(status="ended", succeeded=1, errored=1))
    st = prov.batch_status("msgbatch_01Abc", AMARKER)
    assert srv.seen[-1][0] == "/v1/messages/batches/msgbatch_01Abc"
    assert st["status"] == "ended" and st["counts"]["succeeded"] == 1
    srv.reply(body=created(status="canceling"))
    assert prov.batch_cancel("msgbatch_01Abc", AMARKER)["status"] == "canceling"
    assert srv.seen[-1][0] == "/v1/messages/batches/msgbatch_01Abc/cancel"
    n = len(srv.seen)
    with pytest.raises(ApiError, match="неверный id пакета"):
        prov.batch_status("../x", AMARKER)
    assert len(srv.seen) == n


def test_anthropic_cli_end_to_end(aapi):
    """CLI на anthropic: submit (без денежных настроек — без count_tokens, Р2), status, results до и после ended,
    цена по таблице Haiku со скидкой 50 %, с денежной проверкой — count_tokens на каждый запрос, cancel."""
    srv = aapi.srv
    sub = ("batch", "submit", "--provider", "anthropic", "--project", "payload", "--tier", "cheap",
           "--inputs", "in/one.txt", "in/two.txt", "--system", "sys.txt", "--cache", "long")
    srv.reply(body=created())
    rc, out, err = aapi.cli(*sub)
    assert rc == 0, err
    assert [p for p, _h, _b in srv.seen] == ["/v1/messages/batches"]
    assert "пакет msgbatch_01Abc отправлен: запросов 2 · claude-haiku-5-5" in out and "(вход грубо)" in out
    reqs = srv.body()["requests"]
    assert [r["custom_id"] for r in reqs] == ["one", "two"]
    assert reqs[0]["params"]["system"][0]["cache_control"] == {"type": "ephemeral", "ttl": "1h"}
    assert reqs[1]["params"]["messages"] == [{"role": "user", "content": "отзыв два"}]
    srv.reply(body=created(processing=2))
    rc, out, _ = aapi.cli("batch", "status", "msgbatch_01Abc")
    assert rc == 0 and "msgbatch_01Abc · in_progress (в работе 2)" in out
    srv.reply(body=created(processing=2))
    rc, _, err = aapi.cli("batch", "results", "msgbatch_01Abc")
    assert rc == 1 and "ещё не закончен (in_progress" in err and batch_rows(aapi, "msgbatch_01Abc") == []
    srv.reply(body=created(status="ended", succeeded=1, errored=1))
    lines = [{"custom_id": "two", "result": {"type": "errored", "error": {"type": "error", "error": {
                 "type": "overloaded_error", "message": "перегрузка"}}}},
             {"custom_id": "one", "result": {"type": "succeeded", "message": msg("положительный", usage=USAGE_B)}}]
    srv.reply(body=b"\n".join(json.dumps(x, ensure_ascii=False).encode() for x in lines))
    rc, out, err = aapi.cli("batch", "results", "msgbatch_01Abc")
    assert rc == 0, err
    assert srv.seen[-1][0] == "/v1/messages/batches/msgbatch_01Abc/results"
    assert "успешно 1, с ошибкой 1" in out and "✗ two (in/two.txt): errored overloaded_error: перегрузка" in out
    out_dir = aapi.env.state / "api" / "out" / "msgbatch_01Abc"
    assert (out_dir / "one.md").read_text(encoding="utf-8") == "положительный" and not (out_dir / "two.md").exists()
    rows = {r["custom_id"]: r for r in batch_rows(aapi, "msgbatch_01Abc")}
    # таблица Haiku литералами (ТЗ WSH-4.3): вход 0.10, запись кэша 1 ч 0.20, выход 0.50 — и скидка пакета 50 %
    assert rows["one"]["cost_usd"] == pytest.approx((31 * 0.10 + 2267 * 0.20 + 18 * 0.50) / 1e6 * 0.5)
    assert (rows["one"]["model"], rows["one"]["ok"], rows["one"]["template"]) == (HAIKU, True, None)
    assert (rows["two"]["cost_usd"], rows["two"]["ok"]) == (0.0, False)
    # денежная проверка включена (любая из трёх) — точная оценка: count_tokens на каждый запрос (решение Р2)
    for i, (api_keys, prov_keys) in enumerate((("daily_usd = 10.0\n", ""), ("max_usd_per_call = 1.0\n", ""),
                                               ("", 'included_usd = 5\ncredits_until = "2026-10-31"\n'))):
        set_api(aapi.env, "[api]\nenabled = true\n" + api_keys + "\n" + aapi.section + prov_keys)
        bid = ("msgbatch_02Def", "msgbatch_03Ghi", "msgbatch_04Jkl")[i]
        n = len(srv.seen)
        srv.reply(body={"input_tokens": 10})
        srv.reply(body={"input_tokens": 12})
        srv.reply(body=created(bid))
        rc, out, err = aapi.cli(*sub)
        assert rc == 0, (api_keys, prov_keys, err)
        assert [p for p, _h, _b in srv.seen[n:]] == ["/v1/messages/count_tokens"] * 2 + ["/v1/messages/batches"]
        assert "(вход грубо)" not in out
    srv.reply(body=created("msgbatch_02Def", status="canceling"))
    rc, out, _ = aapi.cli("batch", "cancel", "msgbatch_02Def")
    assert rc == 0 and "отмена запрошена: msgbatch_02Def · canceling" in out
    assert srv.seen[-1][0] == "/v1/messages/batches/msgbatch_02Def/cancel"
    files = [p for p in aapi.env.state.rglob("*") if p.is_file()]
    assert all(AMARKER not in o for o in aapi.outputs)
    assert [str(p) for p in files if AMARKER[:12].encode() in p.read_bytes()] == []


# ---------- CLI и ядро на fake (тесты 2–4) ----------

def test_cli_results_by_custom_id_error_not_written(api, monkeypatch):
    """Тест 2: результаты в обратном порядке раскладываются по custom_id (ответ fake — эхо входа); ошибочный — в
    сводке, файла нет, строка журнала $0."""
    prov = use(monkeypatch, fake.FakeProvider())
    prov.batch_errors = {"two": "errored invalid_request_error: слишком длинно"}
    rc, out, err = api.cli(*SUBMIT, "--inputs", "in/one.txt", "in/two.txt", "in/three.txt", "--out-dir", "out")
    assert rc == 0, err
    assert "пакет fakebatch_1 отправлен: запросов 3" in out and "ответы — в out проекта payload" in out
    assert [cid for cid, _r in prov.batches["fakebatch_1"]] == ["one", "two", "three"]
    rec = batches.load(api.env.state, "fakebatch_1")
    assert rec["inputs"]["two"]["input"] == "in/two.txt" and rec["out_dir"] == "out" and rec["requests"] == 3
    rc, out, err = api.cli("batch", "results", "fakebatch_1")
    assert rc == 0, err
    proj = api.env.proj
    assert (proj / "out" / "one.md").read_text(encoding="utf-8") == "текст один"
    assert (proj / "out" / "three.md").read_text(encoding="utf-8") == "текст три"
    assert not (proj / "out" / "two.md").exists()
    assert "успешно 2, с ошибкой 1" in out and "✗ two (in/two.txt): errored invalid_request_error" in out
    assert "one → out/one.md" in out
    rows = {r["custom_id"]: r for r in batch_rows(api, "fakebatch_1")}
    assert sorted(rows) == ["one", "three", "two"]
    assert (rows["two"]["ok"], rows["two"]["cost_usd"], rows["one"]["ok"]) == (False, 0.0, True)
    assert batches.load(api.env.state, "fakebatch_1")["status"] == "ended"


def test_batch_price_half(api, monkeypatch):
    """Тест 3а: цена результата и оценка пакета — со скидкой 50 % (литералами по таблице fake-cheap: $1 вход,
    $5 выход за Мток). Вход «\\nпривет, модель» — 4 токена, ответ-эхо — 4."""
    use(monkeypatch, fake.FakeProvider())
    assert api.cli(*SUBMIT, "--inputs", "q.txt")[0] == 0
    rec = batches.load(api.env.state, "fakebatch_1")
    assert rec["estimate_usd"] == pytest.approx((4 * 1.0 + 1024 * 5.0) / 1e6 * 0.5)
    rc, out, _ = api.cli("batch", "results", "fakebatch_1")
    assert rc == 0
    [row] = batch_rows(api, "fakebatch_1")
    assert row["cost_usd"] == pytest.approx((4 * 1.0 + 4 * 5.0) / 1e6 * 0.5)
    assert row["usage"]["input"] == 4 and row["usage"]["output"] == 4 and row["zone"] == "included"


def test_batch_results_repeat_no_duplicate(api, monkeypatch):
    """Тест 3б: повторный results не дублирует строки журнала; файлы — «записан ранее», не перезаписаны."""
    use(monkeypatch, fake.FakeProvider())
    assert api.cli(*SUBMIT, "--inputs", "in/one.txt", "in/two.txt", "--out-dir", "out")[0] == 0
    rc, out, _ = api.cli("batch", "results", "fakebatch_1")
    assert rc == 0 and "журнал трат: записано строк 2" in out
    assert len(batch_rows(api, "fakebatch_1")) == 2
    rc, out, _ = api.cli("batch", "results", "fakebatch_1")
    assert rc == 0 and "записано строк 0, уже были 2 — не дублируются" in out
    assert len(batch_rows(api, "fakebatch_1")) == 2
    assert "one → out/one.md (записан ранее)" in out
    # файл в проекте изменили — не перезаписывается, ответ — в каталог состояния
    (api.env.proj / "out" / "one.md").write_text("правка оператора", encoding="utf-8")
    rc, out, _ = api.cli("batch", "results", "fakebatch_1")
    assert rc == 0 and "one → out/one.md не записан: файл уже есть — перезапись запрещена" in out
    assert (api.env.proj / "out" / "one.md").read_text(encoding="utf-8") == "правка оператора"
    saved = api.env.state / "api" / "out" / "fakebatch_1" / "one.md"
    assert saved.read_text(encoding="utf-8") == "текст один" and len(batch_rows(api, "fakebatch_1")) == 2
    # другой пакет с теми же custom_id — свои строки: фильтр дублей — по паре (пакет, custom_id) (ревью, п. 2)
    assert api.cli(*SUBMIT, "--inputs", "in/one.txt", "in/two.txt")[0] == 0
    rc, out, _ = api.cli("batch", "results", "fakebatch_2")
    assert rc == 0 and "журнал трат: записано строк 2" in out and len(batch_rows(api, "fakebatch_2")) == 2


def test_cli_out_dir_refusals(api, monkeypatch):
    """Тест 4 (CLI): ro-проект, существующий файл, ../ — отказ до отправки."""
    prov = use(monkeypatch, fake.FakeProvider())
    (api.env.proj / "out").mkdir()
    (api.env.proj / "out" / "q.md").write_text("было", encoding="utf-8")
    for project, out_dir, err in (("rt", "new", "out_dir: проект rt в реестре только для чтения (ro)"),
                                  ("payload", "out", "out_dir: файл out/q.md уже есть"),
                                  ("payload", "../outside", "out_dir: путь вне проекта"),
                                  ("payload", ".git", "запись внутрь .git"),
                                  ("payload", "q.txt", "out_dir: q.txt — не каталог")):  # ревью, п. 9
        rc, _, e = api.cli("batch", "submit", "--provider", "fake", "--project", project, "--tier", "cheap",
                           "--inputs", "q.txt", "--out-dir", out_dir)
        assert rc == 1 and err in e, (out_dir, e)
    assert prov.batches == {} and not (api.env.outside / "q.md").exists()
    assert (api.env.proj / "out" / "q.md").read_text(encoding="utf-8") == "было"
    assert not batches.batches_dir(api.env.state).exists()


def test_server_out_dir_refusals(api, monkeypatch):
    """Тест 4 (сервер): те же отказы у api_batch_submit."""
    prov = use(monkeypatch, fake.FakeProvider())
    s = Srv(api)
    (api.env.proj / "out").mkdir()
    (api.env.proj / "out" / "q.md").write_text("было", encoding="utf-8")
    for project, out_dir, err in (("rt", "new", "только для чтения (ro)"), ("payload", "out", "out/q.md уже есть"),
                                  ("payload", "../outside", "путь вне проекта")):
        out = s.call("api_batch_submit", project=project, input_files=["q.txt"], tier="cheap", out_dir=out_dir)
        assert out.startswith("отказ: out_dir: ") and err in out, (out_dir, out)
    assert prov.batches == {}


# ---------- ключ не утекает (тест 5) ----------

def test_key_marker_never_leaks_batch(api, monkeypatch, capsys, caplog):
    prov = use(monkeypatch, fake.FakeProvider())
    s = Srv(api)
    prov.batch_errors = {"q": f"errored: invalid x-api-key {MARKER}"}  # ключ в тексте ошибки результата
    assert api.cli(*SUBMIT, "--inputs", "q.txt", "in/one.txt")[0] == 0
    api.cli("batch", "status", "fakebatch_1")
    api.cli("batch", "status")
    rc, out, _ = api.cli("batch", "results", "fakebatch_1")
    assert rc == 0 and "✗ q (q.txt): errored: invalid x-api-key <ключ скрыт>" in out
    [row] = [r for r in batch_rows(api, "fakebatch_1") if r["custom_id"] == "q"]
    assert row["error"] == "errored: invalid x-api-key <ключ скрыт>"
    s.call("api_batch_submit", project="payload", input_files=["q.txt"], tier="cheap")
    s.call("api_batch_status", id="fakebatch_2")
    s.call("api_batch_status")
    s.call("api_batch_results", id="fakebatch_2")
    s.call("api_batch_cancel", id="fakebatch_2")
    api.cli("batch", "cancel", "fakebatch_1")
    for fail in ("401 invalid x-api-key {key} for request", "bad header {key!r}", f"token {MARKER[:12]}… rejected",
                 f"suffix …{MARKER[-9:]}"):
        prov.fail = fail
        outs = [api.cli(*SUBMIT, "--inputs", "q.txt")[2], api.cli("batch", "status", "fakebatch_1")[2],
                api.cli("batch", "status")[1], api.cli("batch", "results", "fakebatch_1")[2],
                api.cli("batch", "cancel", "fakebatch_1")[2],
                s.call("api_batch_submit", project="payload", input_files=["q.txt"], tier="cheap"),
                s.call("api_batch_status", id="fakebatch_1"), s.call("api_batch_status"),
                s.call("api_batch_results", id="fakebatch_1"), s.call("api_batch_cancel", id="fakebatch_1")]
        assert all("<ключ скрыт>" in o for o in outs), outs
    c = capsys.readouterr()
    logs = c.out + c.err + caplog.text
    for part in (MARKER, MARKER[:12], MARKER[-9:]):
        assert all(part not in o for o in api.outputs) and part not in logs
    files = [p for p in api.env.state.rglob("*") if p.is_file()]
    assert api.env.state / "audit.jsonl" in files and batches.path(api.env.state, "fakebatch_1") in files
    assert [str(p) for p in files if MARKER[:8].encode() in p.read_bytes() or MARKER[-8:].encode() in p.read_bytes()] \
        == []
    assert len(api.outputs) >= 49


# ---------- прочие проверки ----------

def test_batch_size_limit(api, monkeypatch):
    prov = use(monkeypatch, fake.FakeProvider())
    rc, _, err = api.cli(*SUBMIT, "--inputs", *[f"f{i}.txt" for i in range(1001)])
    assert rc == 1 and "пакет — до 1000 запросов" in err
    out = Srv(api).call("api_batch_submit", project="payload", inputs=["x"] * 1001, tier="cheap")
    assert out.startswith("отказ: ") and "пакет — до 1000 запросов" in out and prov.batches == {}
    assert Srv(api).call("api_batch_submit", project="payload", inputs=["x"] * 1000, tier="cheap").startswith(
        "пакет fakebatch_1 отправлен: запросов 1000")


def test_custom_ids_and_file_names():
    """Р4: custom_id — ASCII по имени входа, дубли без учёта регистра; файлы — имена как есть."""
    assert batches.custom_ids(["Отчёт", "report", "Report", "a b.c", "x" * 70, "report"]) == \
        ["r1", "report", "Report-2", "a_b_c", "x" * 64, "report-3"]
    assert batches.file_names(["Отчёт", "отчёт", "a"]) == ["Отчёт", "отчёт-2", "a"]


def test_glob_inputs_skip_deny(api, monkeypatch):
    prov = use(monkeypatch, fake.FakeProvider())
    (api.env.proj / "secrets").mkdir()
    (api.env.proj / "secrets" / "k.txt").write_text("секрет", encoding="utf-8")
    (api.env.proj / "link.txt").symlink_to(api.env.outside / "secret.txt")  # симлинк пропускается (ревью, п. 12)
    rc, out, err = api.cli(*SUBMIT, "--inputs-glob", "*.txt")
    assert rc == 0, err
    rec = batches.load(api.env.state, "fakebatch_1")
    assert sorted(v["input"] for v in rec["inputs"].values()) == ["a.txt", "config.txt", "in/one.txt",
                                                                  "in/three.txt", "in/two.txt", "q.txt"]
    assert all("секрет" not in r.messages[0]["text"] for _c, r in prov.batches["fakebatch_1"])
    rc, _, err = api.cli(*SUBMIT, "--inputs-glob", "*.nope")
    assert rc == 1 and "по маске *.nope в проекте нет файлов" in err
    rc, _, err = api.cli(*SUBMIT, "--inputs", "q.txt", "--inputs-glob", "*.txt")
    assert rc == 2 and "что-то одно" in err


def test_bad_batch_id_refused(api, monkeypatch):
    use(monkeypatch, fake.FakeProvider())
    for op in ("status", "results", "cancel"):
        rc, _, err = api.cli("batch", op, "../x")
        assert rc == 1 and "неверный id пакета" in err
    assert "неверный id пакета" in Srv(api).call("api_batch_results", id="../../etc/passwd")
    rc, _, err = api.cli("batch", "results", "fakebatch_9")
    assert rc == 1 and "пакета fakebatch_9 нет в учёте хаба" in err
    bdir = batches.batches_dir(api.env.state)
    bdir.mkdir(parents=True)
    (bdir / "fakebatch_7.json").write_text('{"id": "fakebatch_7", "inputs": {"a": "строка"}}', encoding="utf-8")
    rc, _, err = api.cli("batch", "results", "fakebatch_7")  # отказ, а не трассировка (ревью, п. 15)
    assert rc == 1 and "учёт пакета" in err and "повреждён" in err


def test_results_before_ended_refused(api, monkeypatch):
    prov = use(monkeypatch, fake.FakeProvider())
    assert api.cli(*SUBMIT, "--inputs", "q.txt", "--out-dir", "out")[0] == 0
    prov.batch_state = "in_progress"
    rc, _, err = api.cli("batch", "results", "fakebatch_1")
    assert rc == 1 and "пакет fakebatch_1 ещё не закончен (in_progress" in err
    assert batch_rows(api, "fakebatch_1") == [] and not (api.env.proj / "out").exists()
    out = Srv(api).call("api_batch_results", id="fakebatch_1")
    assert out.startswith("отказ: ") and "ещё не закончен" in out


def test_strong_needs_choice_and_reason(api, monkeypatch):
    prov = use(monkeypatch, fake.FakeProvider())
    s = Srv(api)
    rc, _, err = api.cli("batch", "submit", "--provider", "fake", "--project", "payload", "--tier", "strong",
                         "--inputs", "q.txt")
    assert rc == 1 and "только явным выбором оператора" in err
    planned = []
    orig = Api.plan
    monkeypatch.setattr(Api, "plan", lambda self, *a, **k: planned.append(1) or orig(self, *a, **k))
    out = s.call("api_batch_submit", project="payload", inputs=["x", "y", "z"], tier="strong", choice="operator")
    assert out.startswith("отказ: ") and "только с reason" in out
    assert prov.batches == {} and len(planned) == 1  # reason — по первому плану, до оценки остальных (ревью, п. 10)
    rc, out, err = api.cli("batch", "submit", "--provider", "fake", "--project", "payload", "--tier", "strong",
                           "--inputs", "q.txt", "--choice", "operator")
    assert rc == 0 and "fake-strong" in out, err


def test_money_limits_on_batch_sum(api, monkeypatch):
    """Р1: потолок одного запроса — на каждый (со скидкой), дневной — на сумму пакета."""
    prov = use(monkeypatch, fake.FakeProvider())
    # 3 запроса по ≈$0.00256 (со скидкой): дневной потолок $0.005 пропускает один, но не три
    set_api(api.env, "[api]\nenabled = true\ndaily_usd = 0.005\n\n[api.providers.fake]\n")
    rc, _, err = api.cli(*SUBMIT, "--inputs", "in/one.txt", "in/two.txt", "in/three.txt")
    assert rc == 1 and "пакет: дневной потолок daily_usd" in err and "пакет не отправлен" in err
    assert api.cli(*SUBMIT, "--inputs", "in/one.txt")[0] == 0 and list(prov.batches) == ["fakebatch_1"]
    set_api(api.env, "[api]\nenabled = true\nmax_usd_per_call = 0.003\n\n[api.providers.fake]\n")
    assert api.cli(*SUBMIT, "--inputs", "in/one.txt", "in/two.txt")[0] == 0  # 0.00256 < 0.003 только со скидкой
    rc, _, err = api.cli(*SUBMIT, "--inputs", "in/one.txt", "--max-tokens", "2000")
    assert rc == 1 and "max_usd_per_call" in err and len(prov.batches) == 2


def test_closed_and_blocked_project(api, monkeypatch):
    prov = use(monkeypatch, fake.FakeProvider())
    s = Srv(api)

    def boom(hub, sess, path):
        raise AssertionError(f"файл прочитан: {path}")
    assert api.cli(*SUBMIT, "--inputs", "q.txt")[0] == 0  # fakebatch_1 — до закрытия
    monkeypatch.setattr(cli, "_read", boom)
    set_api(api.env, ws="[workspace.payload.api]\nenabled = false\n")
    assert "закрыт от отправки в API" in api.cli(*SUBMIT, "--inputs", "q.txt")[2]
    assert "закрыт от отправки в API" in s.call("api_batch_submit", project="payload", input_files=["q.txt"],
                                                tier="cheap")
    set_api(api.env)
    api.env.hub.runtime.block("payload")
    out = s.call("api_batch_submit", project="payload", input_files=["q.txt"], tier="cheap")
    assert out == f"отказ: {BLOCKED}"
    out = s.call("api_batch_results", id="fakebatch_1")
    assert out == f"отказ: {BLOCKED}" and batch_rows(api, "fakebatch_1") == []
    assert list(prov.batches) == ["fakebatch_1"]
    api.env.hub.runtime.unblock("payload")
    assert "успешно 1, с ошибкой 0" in s.call("api_batch_results", id="fakebatch_1")


def test_status_list_and_cancel(api, monkeypatch):
    prov = use(monkeypatch, fake.FakeProvider())
    s = Srv(api)
    assert s.call("api_batch_status") == "пакетов нет"
    prov.batch_state = "in_progress"
    assert api.cli(*SUBMIT, "--inputs", "q.txt")[0] == 0
    assert api.cli(*SUBMIT, "--inputs", "in/one.txt", "in/two.txt")[0] == 0
    rc, out, _ = api.cli("batch", "status")
    assert rc == 0 and len(out.splitlines()) == 2 and "fakebatch_1 · in_progress" in out
    assert "fakebatch_2 · in_progress" in out and "запросов 2" in out
    out = s.call("api_batch_cancel", id="fakebatch_2")
    assert out.startswith("отказ: ") is False and "fakebatch_2 · canceling" in out
    assert batches.load(api.env.state, "fakebatch_2")["status"] == "canceling"
    rows = [r for r in json.loads("[" + ",".join((api.env.state / "audit.jsonl").read_text(
        encoding="utf-8").splitlines()) + "]") if str(r.get("tool")).startswith("api_batch")]
    assert [r["tool"] for r in rows] == ["api_batch_status", "api_batch_cancel"]


def test_server_results_texts_only_when_small(api, monkeypatch):
    """П. 2.3: тексты в чат — только если ответов ≤ 5 и каждый ≤ 2 КБ; иначе сводка и пути."""
    use(monkeypatch, fake.FakeProvider())
    s = Srv(api)
    out = s.call("api_batch_submit", project="payload", inputs=["первый ответ", "второй ответ"], tier="cheap")
    assert out.startswith("пакет fakebatch_1 отправлен: запросов 2")
    assert batches.load(api.env.state, "fakebatch_1")["inputs"]["r1"]["input"] == "первый ответ"
    out = s.call("api_batch_results", id="fakebatch_1")
    assert "--- r1 ---\nпервый ответ" in out and "--- r2 ---\nвторой ответ" in out
    assert (api.env.state / "api" / "out" / "fakebatch_1" / "r2.md").read_text(encoding="utf-8") == "второй ответ"
    s.call("api_batch_submit", project="payload", inputs=[f"текст {i}" for i in range(6)], tier="cheap")
    out = s.call("api_batch_results", id="fakebatch_2")
    assert "успешно 6" in out and "---" not in out and "r6: сохранён" in out
    s.call("api_batch_submit", project="payload", inputs=["коротко", "д" * 1100], tier="cheap")  # 2200 байт
    out = s.call("api_batch_results", id="fakebatch_3")
    assert "успешно 2" in out and "---" not in out and "коротко" not in out
    audit = [json.loads(x) for x in (api.env.state / "audit.jsonl").read_text(encoding="utf-8").splitlines()]
    res = [r for r in audit if r["tool"] == "api_batch_results"]
    assert res[0]["params"]["batch"] == "fakebatch_1" and res[0]["params"]["ok"] == 2
    assert "первый ответ" not in (api.env.state / "audit.jsonl").read_text(encoding="utf-8")


def test_schema_answers_json_and_broken_paid(api, monkeypatch):
    """Р7: при схеме — файлы .json; ответ не по JSON — ошибка, файла нет, строка журнала по usage со скидкой."""
    prov = use(monkeypatch, fake.FakeProvider())
    (api.env.proj / "s.json").write_text('{"type": "object", "properties": {"ok": {"type": "boolean"}}}',
                                         encoding="utf-8")
    assert api.cli(*SUBMIT, "--inputs", "in/one.txt", "--schema", "s.json")[0] == 0
    rc, out, _ = api.cli("batch", "results", "fakebatch_1")
    path = api.env.state / "api" / "out" / "fakebatch_1" / "one.json"
    assert rc == 0 and json.loads(path.read_text(encoding="utf-8")) == {"ok": False}
    prov.reply = "не JSON"
    orig = prov._answer
    monkeypatch.setattr(prov, "_answer", lambda r: orig(replace(r, schema=None)))  # ответ без схемы — текст
    assert api.cli(*SUBMIT, "--inputs", "in/two.txt", "--schema", "s.json")[0] == 0
    rc, out, _ = api.cli("batch", "results", "fakebatch_2")
    assert rc == 0 and "✗ two (in/two.txt): ответ не разобран как JSON по схеме" in out
    assert not (api.env.state / "api" / "out" / "fakebatch_2").exists()
    [row] = batch_rows(api, "fakebatch_2")
    assert row["ok"] is False and row["cost_usd"] > 0


# ---------- по ревью субагента ----------

def test_anthropic_results_foreign_id_and_paid_without_usage(aapi):
    """Ревью, п. 1, 3, 4: custom_id не из учёта и повтор — ни файла (имя шло бы в путь), ни строки журнала; оплаченный
    ответ с неразобранным usage и без usage — по оценке запроса из учёта, а не $0; файлов нет."""
    srv = aapi.srv
    srv.reply(body=created())
    assert aapi.cli("batch", "submit", "--provider", "anthropic", "--project", "payload", "--tier", "cheap",
                    "--inputs", "in/one.txt", "in/two.txt")[0] == 0
    rec = batches.load(aapi.env.state, "msgbatch_01Abc")
    no_usage = {k: v for k, v in msg("без usage").items() if k != "usage"}
    lines = [{"custom_id": "one", "result": {"type": "succeeded",
                                             "message": msg("плохой usage", usage={"input_tokens": "x"})}},
             {"custom_id": "two", "result": {"type": "succeeded", "message": no_usage}},
             {"custom_id": "../../evil", "result": {"type": "succeeded", "message": msg("чужой")}},
             {"custom_id": "one", "result": {"type": "succeeded", "message": msg("повтор")}}]
    srv.reply(body=created(status="ended", succeeded=2))
    srv.reply(body=b"\n".join(json.dumps(x, ensure_ascii=False).encode() for x in lines))
    rc, out, err = aapi.cli("batch", "results", "msgbatch_01Abc")
    assert rc == 0, err
    assert "успешно 0, с ошибкой 4" in out and "журнал трат: записано строк 2" in out
    assert "✗ ../../evil: custom_id не из этого пакета или повтор — пропущен" in out
    assert "✗ one (in/one.txt): usage ответа не разобран" in out and "✗ two (in/two.txt): в ответе нет usage" in out
    rows = {r["custom_id"]: r for r in batch_rows(aapi, "msgbatch_01Abc")}
    assert sorted(rows) == ["one", "two"]
    for cid in ("one", "two"):
        assert rows[cid]["ok"] is False and rows[cid]["cost_usd"] == pytest.approx(rec["inputs"][cid]["estimate_usd"])
        assert rows[cid]["cost_usd"] > 0
    assert not (aapi.env.state / "api" / "out").exists()
    assert [p for p in aapi.env.tmp.rglob("evil*")] == []


def test_zones_on_batch_sum_and_row_zone(api, monkeypatch):
    """Ревью, п. 5: зоны — на сумму пакета; зона строки результата — определённая при отправке."""
    prov = use(monkeypatch, fake.FakeProvider())
    # 3 запроса по ≈$0.00256: включённой зоны $0.006 хватает на каждый, но не на сумму
    set_api(api.env, '[api]\nenabled = true\n\n[api.providers.fake]\nincluded_usd = 0.006\n'
                     'credits_until = "2026-10-31"\n')
    rc, _, err = api.cli(*SUBMIT, "--inputs", "in/one.txt", "in/two.txt", "in/three.txt")
    assert rc == 1 and "пакет: включённые кредиты исчерпаны" in err and prov.batches == {}
    assert api.cli("allow", "fake", "--usd", "1", "--until", "31.10")[0] == 0
    rc, out, err = api.cli(*SUBMIT, "--inputs", "in/one.txt", "in/two.txt", "in/three.txt")
    assert rc == 0, err
    assert batches.load(api.env.state, "fakebatch_1")["zone"] == "purchased"
    assert api.cli("batch", "results", "fakebatch_1")[0] == 0
    assert [r["zone"] for r in batch_rows(api, "fakebatch_1")] == ["purchased"] * 3


def test_results_recheck_out_dir_before_write(api, monkeypatch):
    """Ревью, п. 7: перед записью out_dir проверяется заново — проект стал ro, каталог подменён симлинком наружу:
    в проект и наружу не пишется, ответ — в каталог состояния."""
    use(monkeypatch, fake.FakeProvider())
    proj, outside = api.env.proj, api.env.outside
    assert api.cli(*SUBMIT, "--inputs", "in/one.txt", "--out-dir", "out")[0] == 0
    assert api.cli(*SUBMIT, "--inputs", "in/two.txt", "--out-dir", "out2")[0] == 0
    api.env.workspaces["payload"]["mode"] = "ro"
    set_api(api.env)
    rc, out, _ = api.cli("batch", "results", "fakebatch_1")
    assert rc == 0 and "one → out/one.md не записан: проект payload в реестре только для чтения (ro)" in out
    assert not (proj / "out").exists()
    assert (api.env.state / "api" / "out" / "fakebatch_1" / "one.md").read_text(encoding="utf-8") == "текст один"
    api.env.workspaces["payload"]["mode"] = "rw"
    set_api(api.env)
    (proj / "out2").symlink_to(outside, target_is_directory=True)
    rc, out, _ = api.cli("batch", "results", "fakebatch_2")
    assert rc == 0 and "two → out2/two.md не записан: путь вне проекта" in out
    assert sorted(p.name for p in outside.iterdir()) == ["secret.txt"]
    assert (api.env.state / "api" / "out" / "fakebatch_2" / "two.md").exists()


def test_server_blocked_before_write(api, monkeypatch):
    """Ревью, п. 8: проект заблокирован из панели после выборки результатов — в out_dir не пишется, ответ — в каталог
    состояния, текст ответа — не в чат."""
    use(monkeypatch, fake.FakeProvider(reply="ОПЛАЧЕННЫЙ-ОТВЕТ"))
    s = Srv(api)
    assert s.call("api_batch_submit", project="payload", input_files=["q.txt"], tier="cheap", out_dir="out") \
        .startswith("пакет fakebatch_1 отправлен")
    orig = Api.batch_results

    def then_block(self, batch_id):
        res = orig(self, batch_id)
        api.env.hub.runtime.block("payload")
        return res
    monkeypatch.setattr(Api, "batch_results", then_block)
    out = s.call("api_batch_results", id="fakebatch_1")
    assert "q → out/q.md не записан: проект заблокирован пользователем" in out
    assert "ОПЛАЧЕННЫЙ-ОТВЕТ" not in out and not (api.env.proj / "out").exists()
    assert (api.env.state / "api" / "out" / "fakebatch_1" / "q.md").read_text(encoding="utf-8") == "ОПЛАЧЕННЫЙ-ОТВЕТ"

"""MCP-сервер wshub-api (ТЗ WSH-4.4): инструменты, защита (блокировка, out_file, ключ, журнал вызовов), reason,
хвосты 4.3 (оплаченный ответ с ошибкой разбора, оценка с кэшем), doctor, точка входа."""
from __future__ import annotations

import asyncio
import json
import os
import stat
from pathlib import Path

import pytest
from test_api import API, EST, MARKER, ApiEnv, set_api, use

from wshub import api_server, doctor
from wshub.llm import cli, keys
from wshub.llm.core import PaidError
from wshub.llm.providers import fake
from wshub.llm.types import Request, Usage

REASON = "оператор просил сильную модель для сверки итогов"
SCHEMA = {"type": "object", "properties": {"ok": {"type": "boolean"}}}


class Srv:
    """Сервер на окружении ApiEnv: реестр с [api] и fake, ключ-маркер fake, время NOW."""

    def __init__(self, api: ApiEnv):
        self.api, self.env = api, api.env
        self.mcp = api_server.build_api_server(self.env.hub, now=lambda: api.t)

    def call(self, name: str, **args) -> str:
        out = asyncio.run(self.mcp.call_tool(name, args))
        content = out[0] if isinstance(out, tuple) else out
        text = content[0].text
        self.api.outputs.append(text)
        return text

    def run(self, **kw) -> str:
        return self.call("api_run", **{"project": "payload", "tier": "cheap", **kw})

    def audit(self) -> list[dict]:
        p = self.env.state / "audit.jsonl"
        rows = [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines()] if p.exists() else []
        return [r for r in rows if str(r.get("tool", "")).startswith("api_")]


@pytest.fixture
def api(env, capsys) -> ApiEnv:
    return ApiEnv(env, capsys)


@pytest.fixture
def srv(api) -> Srv:
    return Srv(api)


@pytest.fixture
def no_read(monkeypatch):
    """Файлы проекта не читаются: _read сервера падает, если его позвали."""
    calls = []

    def boom(hub, s, path):
        calls.append(path)
        raise AssertionError(f"файл прочитан: {path}")
    monkeypatch.setattr(api_server, "_read", boom)
    return calls


# ---------- инструменты (п. 2.2, тест 1) ----------

def test_registers_exactly_8_tools(srv):
    """4 инструмента 4.4 и 4 пакетных (ТЗ WSH-4.5, п. 2.3)."""
    assert {t.name for t in asyncio.run(srv.mcp.list_tools())} == {
        "api_models", "api_run", "api_estimate", "api_stats",
        "api_batch_submit", "api_batch_status", "api_batch_results", "api_batch_cancel"}


def test_run_text_file_schema(srv, monkeypatch):
    prov = use(monkeypatch, fake.FakeProvider())
    out = srv.run(input="привет из чата")
    lines = out.splitlines()
    assert lines[0] == "привет из чата" and lines[1].startswith("API: fake-cheap · вход ")
    assert srv.run(input_file="q.txt").splitlines()[0] == "привет, модель"
    (srv.env.proj / "s.json").write_text(json.dumps(SCHEMA), encoding="utf-8")
    for kw in ({"schema": SCHEMA}, {"schema_file": "s.json"}):
        out = srv.run(input="оцени", **kw)
        assert out.startswith('{\n  "ok": false\n}\nAPI: fake-cheap'), out
    assert prov.calls[-1].schema == SCHEMA
    srv.run(input="с системой", system_file="q.txt", max_tokens=50, cache="short", effort="low")
    r = prov.calls[-1]
    assert (r.system, r.max_tokens, r.cache, r.effort) == ("привет, модель", 50, "short", "low")
    n = len(prov.calls)
    for kw, err in (({"input": "x", "input_file": "q.txt"}, "ровно одно из двух"), ({}, "ровно одно из двух"),
                    ({"input": "x", "system": "a", "system_file": "q.txt"}, "system и system_file"),
                    ({"input": "x", "schema": SCHEMA, "schema_file": "s.json"}, "schema и schema_file"),
                    ({"input": "x", "cache": "forever"}, "cache: одно из"),
                    ({"input": "x", "choice": "chat"}, 'choice: только "operator"'),
                    ({"input": "x", "max_tokens": 0}, "max_tokens"),
                    ({"input": "x", "provider": "nope"}, "неизвестный провайдер nope")):
        out = srv.run(**kw)
        assert out.startswith("отказ: ") and err in out, (kw, out)
    assert len(prov.calls) == n


def test_long_answer_goes_to_state_file(srv, monkeypatch):
    use(monkeypatch, fake.FakeProvider(reply="я" * 6000))  # 12 000 байт
    out = srv.run(input="длинно")
    head = out.splitlines()[0]
    assert head.startswith("ответ длиннее 8 КБ (12000 байт) — сохранён: ")
    path = Path(head.split("сохранён: ")[1])
    assert path.is_relative_to(srv.env.state / "api" / "out") and path.read_text(encoding="utf-8") == "я" * 6000
    assert out.count("я") <= api_server.HEAD_MAX // 2 and "я" * 1000 in out and "API: fake-cheap" in out


def test_state_save_failure_keeps_call(srv, monkeypatch):
    """Файл состояния не записался — вызов не падает: в чате причина, начало ответа и строка API (ревью, п. 9)."""
    use(monkeypatch, fake.FakeProvider(reply="я" * 6000))
    srv.env.state.mkdir(parents=True, exist_ok=True)
    (srv.env.state / "plain").write_text("не каталог")
    monkeypatch.setattr(api_server, "_out_path", lambda state, now: state / "plain" / "x.txt")
    out = srv.run(input="длинно")
    assert out.splitlines()[0].startswith("ответ длиннее 8 КБ (12000 байт) — не сохранён — ошибка записи в ")
    assert "я" * 1000 in out and "API: fake-cheap" in out and srv.audit()[-1]["status"] == "ok"


def test_estimate_without_request(srv, monkeypatch):
    prov = use(monkeypatch, fake.FakeProvider())
    out = srv.call("api_estimate", project="payload", input_file="q.txt", tier="cheap")
    assert out.splitlines()[0] == "оценка: fake-cheap · вход ≈4 · выход до 1024 · ≈$0.01"
    assert "зона: включённая" in out and prov.calls == [] and srv.api.rows() == []
    out = srv.call("api_estimate", project="payload", input="привет", system="s" * 400, tier="cheap", cache="short")
    assert out.splitlines()[0].startswith("оценка: fake-cheap · вход ≈102 (запись в кэш 100) · ")  # ревью, п. 11


def test_default_provider_only_when_single(srv, monkeypatch):
    """provider пуст: единственный доступный — он; несколько — отказ с перечнем (ревью, п. 13)."""
    prov = use(monkeypatch, fake.FakeProvider())
    assert srv.call("api_run", project="payload", input="x", tier="cheap").startswith("x\nAPI: fake-cheap")
    keys.write(keys.key_path(srv.env.cfg.parent, "anthropic"), "sk-ant-other-key-0000")  # anthropic — по ключу
    out = srv.call("api_run", project="payload", input="x", tier="cheap")
    assert out == "отказ: provider обязателен: доступны fake, anthropic" and len(prov.calls) == 1


def test_out_file_refused_in_hub_code_dir(api, code_dir, monkeypatch):
    """Проект rw с корнем в каталоге кода wshub: читать в API можно (ro), out_file — отказ (служебные каталоги хаба,
    п. 2.3.2; ревью, п. 5)."""
    prov = use(monkeypatch, fake.FakeProvider())
    api.env.workspaces["hubcode"] = {"path": str(code_dir), "mode": "rw"}
    set_api(api.env)
    s = Srv(api)  # Hub из фикстуры code_dir знает каталог кода
    assert s.call("api_run", project="hubcode", input_file="x.txt", tier="cheap").startswith("code\n")
    out = s.call("api_run", project="hubcode", input="x", tier="cheap", out_file="answer.txt")
    assert out.startswith("отказ: out_file: папка пересекается с кодом wshub") and len(prov.calls) == 1
    assert not (code_dir / "answer.txt").exists()


def test_models_and_stats(srv, monkeypatch):
    out = json.loads(srv.call("api_models"))
    assert out["enabled"] is True
    (p,) = [x for x in out["providers"] if x["provider"] == "fake"]
    assert p["tiers"] == {"cheap": "fake-cheap", "standard": "fake-standard", "strong": "fake-strong"}
    assert "schema" in p["caps"] and p["strong_requires_choice"] is True and p["key"] == "есть"
    keys.key_path(srv.env.cfg.parent, "fake").unlink()
    (p,) = [x for x in json.loads(srv.call("api_models"))["providers"] if x["provider"] == "fake"]
    assert p["key"].startswith("ключа нет: ")  # секция есть — провайдер виден и без ключа
    set_api(srv.env, "[api]\nenabled = true\n")
    out = json.loads(srv.call("api_models"))
    assert out["providers"] == [] and "wshub api key set" in out["hint"]
    set_api(srv.env)
    srv.api.spend(0.5)
    assert "по проектам: payload ≈$0.50 (1)" in srv.call("api_stats")
    assert "за 1 дн." in srv.call("api_stats", days=1)
    assert srv.call("api_stats", days=0).startswith("отказ: days")


# ---------- барьеры и блокировка (п. 2.3.1, тест 2) ----------

def test_closed_project_refused_before_reading(srv, monkeypatch, no_read):
    prov = use(monkeypatch, fake.FakeProvider())
    set_api(srv.env, ws="[workspace.payload.api]\nenabled = false\n")
    for name in ("api_run", "api_estimate"):
        out = srv.call(name, project="payload", input_file="q.txt", tier="cheap")
        assert out.startswith("отказ: ") and "закрыт от отправки в API" in out
    set_api(srv.env, API.replace("enabled = true", "enabled = false"))
    assert "API хаба выключен" in srv.run(input_file="q.txt")
    assert no_read == [] and prov.calls == [] and srv.api.rows() == []


def test_blocked_project_refused(srv, monkeypatch, no_read):
    prov = use(monkeypatch, fake.FakeProvider())
    srv.env.hub.runtime.block("payload")
    for name in ("api_run", "api_estimate"):
        for kw in ({"input_file": "q.txt"}, {"input": "без файлов"}):
            out = srv.call(name, project="payload", tier="cheap", **kw)
            assert out.startswith("отказ: ") and "заблокирован пользователем" in out, out
    assert no_read == [] and prov.calls == [] and srv.api.rows() == []
    srv.env.hub.runtime.unblock("payload")
    monkeypatch.undo()
    use(monkeypatch, prov)
    assert srv.run(input_file="q.txt").startswith("привет, модель")


# ---------- out_file (п. 2.3.2, тест 3) ----------

def test_out_file_written_0644(srv, monkeypatch):
    use(monkeypatch, fake.FakeProvider(reply="ответ в файл"))
    out = srv.run(input="x", out_file="out/answer.txt")
    target = srv.env.proj / "out" / "answer.txt"
    assert out.splitlines()[0] == f"ответ записан: out/answer.txt ({len('ответ в файл'.encode())} байт)"
    assert target.read_text(encoding="utf-8") == "ответ в файл" and "ответ в файл" not in out
    assert stat.S_IMODE(target.stat().st_mode) == 0o644
    assert [p.name for p in target.parent.iterdir()] == ["answer.txt"]  # временного файла не осталось
    (srv.env.proj / "s.json").write_text(json.dumps(SCHEMA), encoding="utf-8")
    use(monkeypatch, fake.FakeProvider())
    srv.run(input="x", schema_file="s.json", out_file="out/data.json")
    assert json.loads((srv.env.proj / "out" / "data.json").read_text()) == {"ok": False}


def test_out_file_existing_not_overwritten(srv, monkeypatch):
    prov = use(monkeypatch, fake.FakeProvider(reply="НОВОЕ"))
    before = (srv.env.proj / "a.txt").read_bytes()
    out = srv.run(input="x", out_file="a.txt")
    assert out.startswith("отказ: ") and "файл a.txt уже есть — перезапись запрещена" in out
    assert (srv.env.proj / "a.txt").read_bytes() == before
    assert prov.calls == [] and srv.api.rows() == []  # отказ до запроса: деньги не потрачены


def test_out_file_appears_during_request_not_overwritten(srv, monkeypatch):
    """Файл появился, пока шёл запрос: повторная проверка перед записью отказывает; оплаченный ответ — в файл
    состояния."""
    target = srv.env.proj / "late.txt"

    class Racer(fake.FakeProvider):
        def run(self, request, key):
            target.write_text("чужое")
            return super().run(request, key)
    use(monkeypatch, Racer(reply="ОПЛАЧЕННЫЙ-ОТВЕТ"))
    out = srv.run(input="x", out_file="late.txt")
    assert target.read_text() == "чужое"
    assert "out_file late.txt не записан: файл late.txt уже есть — перезапись запрещена" in out
    saved = Path(out.split("оплаченный ответ сохранён: ")[1].splitlines()[0])
    assert saved.is_relative_to(srv.env.state / "api" / "out") and saved.read_text() == "ОПЛАЧЕННЫЙ-ОТВЕТ"
    assert "ОПЛАЧЕННЫЙ-ОТВЕТ" not in out
    assert srv.audit()[-1]["status"] == "error" and srv.audit()[-1]["params"]["cost_usd"] > 0


def test_out_file_atomic_link_never_overwrites(srv, monkeypatch):
    """Запись — временный файл с полным текстом и правами 0644, затем link: в момент link цели ещё нет; если цель
    появилась уже после всех проверок, link не перезаписывает её (ревью, п. 16)."""
    use(monkeypatch, fake.FakeProvider(reply="полный ответ"))
    real_link, seen = os.link, []

    def link(src, dst):
        seen.append((Path(src).read_text(), stat.S_IMODE(os.stat(src).st_mode), os.path.lexists(dst)))
        return real_link(src, dst)
    monkeypatch.setattr(api_server.os, "link", link)
    srv.run(input="x", out_file="atomic.txt")
    assert seen == [("полный ответ", 0o644, False)]
    assert (srv.env.proj / "atomic.txt").read_text() == "полный ответ"

    def racing_link(src, dst):
        Path(dst).write_text("чужое")  # гонка после повторной проверки
        return real_link(src, dst)
    monkeypatch.setattr(api_server.os, "link", racing_link)
    out = srv.run(input="x", out_file="race.txt")
    assert (srv.env.proj / "race.txt").read_text() == "чужое"
    assert "out_file race.txt не записан: файл появился во время запроса — перезапись запрещена" in out
    assert sorted(p.name for p in srv.env.proj.iterdir() if p.name.endswith(".wshub-tmp")) == []


def test_out_file_rechecked_before_write(srv, monkeypatch):
    """Путь и реестр проверяются заново перед записью (ревью, п. 1): каталог стал симлинком наружу или проект —
    ro, пока шёл запрос, — в проект и наружу не пишется, оплаченный ответ — в файл состояния."""
    env = srv.env

    class Swap(fake.FakeProvider):
        def run(self, request, key):
            (env.proj / "new").symlink_to(env.outside, target_is_directory=True)
            return super().run(request, key)
    use(monkeypatch, Swap(reply="ОТВЕТ"))
    out = srv.run(input="x", out_file="new/x.txt")
    assert "out_file new/x.txt не записан: путь вне проекта" in out and "оплаченный ответ сохранён: " in out
    assert sorted(p.name for p in env.outside.iterdir()) == ["secret.txt"]

    class ToRo(fake.FakeProvider):
        def run(self, request, key):
            env.workspaces["payload"]["mode"] = "ro"
            set_api(env)
            return super().run(request, key)
    use(monkeypatch, ToRo(reply="ОТВЕТ"))
    out = srv.run(input="x", out_file="ro-later.txt")
    assert "out_file ro-later.txt не записан: проект payload в реестре только для чтения (ro)" in out
    assert not (env.proj / "ro-later.txt").exists()


def test_out_file_blocked_during_request(srv, monkeypatch):
    """Проект заблокировали во время запроса: в проект не пишется (Р3), ответ — в файл состояния, в чат — путь."""
    hub = srv.env.hub

    class Blocker(fake.FakeProvider):
        def run(self, request, key):
            hub.runtime.block("payload")
            return super().run(request, key)
    use(monkeypatch, Blocker(reply="СЕКРЕТНЫЙ-ОТВЕТ"))
    out = srv.run(input="x", out_file="out/b.txt")
    assert not (srv.env.proj / "out" / "b.txt").exists()
    assert "out_file out/b.txt не записан: проект заблокирован пользователем" in out
    assert "оплаченный ответ сохранён: " in out and "СЕКРЕТНЫЙ-ОТВЕТ" not in out


def test_out_file_refusals(srv, monkeypatch):
    prov = use(monkeypatch, fake.FakeProvider())
    proj, outside = srv.env.proj, srv.env.outside
    (proj / "linkdir").symlink_to(outside, target_is_directory=True)
    (proj / "lnk.txt").symlink_to(outside / "new.txt")
    (proj / ".git").mkdir()
    (proj / "subdir").mkdir()
    for path, err in (("../outside/x.txt", "путь вне проекта"), (str(outside / "x.txt"), "путь вне проекта"),
                      ("linkdir/x.txt", "путь вне проекта"), ("lnk.txt", "симлинк"),
                      (".git/x.txt", "внутрь .git"), (".git", "внутрь .git"), (".env.new", "политикой deny"),
                      ("secrets/x.txt", "политикой deny"), ("subdir", "это каталог")):
        out = srv.run(input="x", out_file=path)
        assert out.startswith("отказ: ") and err in out, (path, out)
    out = srv.call("api_run", project="rt", input="x", tier="cheap", out_file="ro.txt")
    assert out.startswith("отказ: ") and "только для чтения (ro)" in out
    assert prov.calls == [] and sorted(p.name for p in outside.iterdir()) == ["secret.txt"]
    assert not (proj / "ro.txt").exists() and list((proj / ".git").iterdir()) == []


# ---------- strong и модель вне уровней: reason (тест 4, решение Р1) ----------

def test_strong_needs_reason_and_operator(srv, monkeypatch):
    prov = use(monkeypatch, fake.FakeProvider())
    for kw, err in (({"choice": "operator"}, "сильная модель (fake-strong) — только с reason (≥ 15 символов)"),
                    ({"choice": "operator", "reason": "коротко"}, "только с reason"),
                    ({"reason": REASON}, "только явным выбором оператора")):
        out = srv.run(input="x", tier="strong", **kw)
        assert out.startswith("отказ: ") and err in out, (kw, out)
    assert prov.calls == []
    out = srv.run(input="x", tier="strong", choice="operator", reason=REASON)  # как --choice operator в CLI
    assert "API: fake-strong" in out and len(prov.calls) == 1
    est = srv.call("api_estimate", project="payload", input="x", tier="strong", choice="operator")
    assert est.startswith("отказ: ") and "только с reason" in est


def test_off_tier_model_needs_reason(srv, monkeypatch):
    prov = use(monkeypatch, fake.FakeProvider())
    set_api(srv.env, API + 'models = { cheap = "fake-standard" }\n')  # fake-cheap — вне уровней
    out = srv.run(input="x", tier="", model="fake-cheap", choice="operator")
    assert out.startswith("отказ: ") and "модель вне уровней (fake-cheap) — только с reason" in out
    out = srv.run(input="x", tier="", model="fake-cheap", reason=REASON)
    assert "не из уровней провайдера fake" in out and prov.calls == []
    out = srv.run(input="x", tier="", model="fake-cheap", choice="operator", reason=REASON)
    assert "API: fake-cheap" in out and len(prov.calls) == 1


def test_strong_without_choice_requirement_still_needs_reason(srv, monkeypatch):
    """strong_requires_choice = false: ядро strong пропускает без выбора оператора, а сервер — только с reason
    (ревью, п. 4)."""
    prov = use(monkeypatch, fake.FakeProvider())
    set_api(srv.env, API + "strong_requires_choice = false\n")
    out = srv.run(input="x", tier="strong")
    assert out.startswith("отказ: ") and "сильная модель (fake-strong) — только с reason" in out and prov.calls == []
    assert "API: fake-strong" in srv.run(input="x", tier="strong", reason=REASON) and len(prov.calls) == 1


# ---------- ключ не утекает, журнал вызовов (п. 2.3.3–2.3.4, тест 5) ----------

def test_key_marker_never_leaks(srv, monkeypatch, capsys, caplog):
    prov = use(monkeypatch, fake.FakeProvider())
    srv.call("api_models")
    srv.run(input_file="q.txt")
    srv.run(input="x", schema=SCHEMA)
    srv.run(input="x", out_file="o.txt")
    srv.call("api_estimate", project="payload", input_file="q.txt", tier="cheap")
    srv.call("api_stats")
    for fail in ("401 invalid x-api-key {key} for request", "bad header {key!r}", f"token {MARKER[:12]}… rejected",
                 f"suffix …{MARKER[-9:]}"):
        prov.fail = fail
        out = srv.run(input="x")
        assert out.startswith("отказ: ") and "<ключ скрыт>" in out
    c = capsys.readouterr()
    logs = c.out + c.err + caplog.text
    for part in (MARKER, MARKER[:12], MARKER[-9:]):
        assert all(part not in o for o in srv.api.outputs) and part not in logs
    files = [p for p in srv.env.state.rglob("*") if p.is_file()] + [srv.env.proj / "o.txt"]
    assert srv.env.state / "audit.jsonl" in files and srv.api.ledger_path in files
    assert [str(p) for p in files if MARKER[:8].encode() in p.read_bytes() or MARKER[-8:].encode() in p.read_bytes()] \
        == []
    assert len(srv.api.outputs) >= 10


def test_audit_line_per_call_without_texts(srv, monkeypatch):
    use(monkeypatch, fake.FakeProvider(reply="ТЕКСТ-ОТВЕТА"))
    srv.run(input="ТЕКСТ-ВХОДА", system="СИСТЕМНЫЙ-ТЕКСТ")
    srv.call("api_estimate", project="payload", input="ТЕКСТ-ВХОДА", tier="cheap")
    srv.call("api_stats")
    srv.call("api_models")
    srv.run(input="x", out_file="a.txt")  # отказ
    rows = srv.audit()
    assert [r["tool"] for r in rows] == ["api_run", "api_estimate", "api_stats", "api_models", "api_run"]
    run = rows[0]
    assert (run["ws"], run["status"], run["params"]["provider"], run["params"]["model"]) == \
        ("payload", "ok", "fake", "fake-cheap")
    assert run["params"]["cost_usd"] == pytest.approx(srv.api.rows()[-1]["cost_usd"])
    assert rows[-1]["status"] == "error" and "уже есть" in rows[-1]["error"]
    text = (srv.env.state / "audit.jsonl").read_text(encoding="utf-8")
    assert all(s not in text for s in ("ТЕКСТ-ВХОДА", "ТЕКСТ-ОТВЕТА", "СИСТЕМНЫЙ-ТЕКСТ"))


# ---------- хвосты 4.3 (п. 2.3а) ----------

class Paid(fake.FakeProvider):
    def run(self, request, key):
        self.calls.append(request)
        raise PaidError("ответ не разобран как JSON по схеме", Usage(input=100, output=50), "fake-cheap",
                        preview="СЫРОЙ-ОТВЕТ")


def test_paid_parse_error_logged_with_cost(srv, monkeypatch):
    """Ответ оплачен, но не разобран: строка журнала — по usage, не $0 (п. 2.3а.1); начало ответа — в чат, но не в
    журнал вызовов."""
    use(monkeypatch, Paid())
    out = srv.run(input="x", schema=SCHEMA)
    assert out.startswith("отказ: провайдер fake: ApiError: ответ не разобран как JSON по схеме: СЫРОЙ-ОТВЕТ")
    row = srv.api.rows()[-1]
    assert row["ok"] is False and row["cost_usd"] == pytest.approx((100 * 1 + 50 * 5) / 1e6)
    assert row["usage"]["input"] == 100 and row["model"] == "fake-cheap"
    assert "СЫРОЙ-ОТВЕТ" not in json.dumps(srv.audit(), ensure_ascii=False)
    a = srv.audit()[-1]  # журнал вызовов: модель и сумма оплаченного вызова есть (ревью, п. 8)
    assert a["status"] == "error" and "ответ не разобран" in a["error"]
    assert a["params"] == {"provider": "fake", "model": "fake-cheap", "cost_usd": pytest.approx(row["cost_usd"])}
    rc, _, err = srv.api.run()  # CLI: тот же текст и та же строка журнала
    assert rc == 1 and "СЫРОЙ-ОТВЕТ" in err and srv.api.rows()[-1]["cost_usd"] > 0


def test_paid_error_key_scrubbed(srv, monkeypatch):
    """Ветка PaidError: ключ в тексте и в начале ответа вырезается — в чате, журнале трат и журнале вызовов (ревью,
    п. 6)."""
    class LeakyPaid(fake.FakeProvider):
        def run(self, request, key):
            raise PaidError(f"bad {key}", Usage(input=10), "fake-cheap", preview=f"echo {key} / {key[:12]}")
    use(monkeypatch, LeakyPaid())
    out = srv.run(input="x")
    assert out.startswith("отказ: ") and out.count("<ключ скрыт>") >= 2
    files = [srv.api.ledger_path, srv.env.state / "audit.jsonl"]
    for part in (MARKER, MARKER[:12], MARKER[-9:]):
        assert part not in out and all(part.encode() not in p.read_bytes() for p in files)
    assert srv.api.rows()[-1]["cost_usd"] == pytest.approx(10 / 1e6)


def test_paid_error_without_usage_logs_estimate(srv, monkeypatch):
    class NoUsage(fake.FakeProvider):
        def run(self, request, key):
            raise PaidError("ответ не JSON-объект", preview="<html>")
    use(monkeypatch, NoUsage())
    assert srv.api.run()[0] == 1
    row = srv.api.rows()[-1]
    assert row["ok"] is False and row["cost_usd"] == pytest.approx(EST)


def test_estimate_counts_cache_write(api):
    """П. 2.3а.2 на цифрах (fake: вход $1, запись кэша $1.25, выход $5 за Мток). Вход «s»×400 + «\\n» + «привет» —
    407 символов ≈ 102 токена; системная часть — 100 токенов."""
    eng = api.engine()

    def plan(cache):
        return eng.plan(Request("s" * 400, ({"role": "user", "text": "привет"},), "payload", tier="cheap",
                                cache=cache), "fake")
    p = plan("short")
    assert (p.estimate.input, p.estimate.cache_write, p.estimate.cache_write_1h) == (2, 100, 0)
    assert p.estimate_usd == pytest.approx((2 * 1 + 100 * 1.25 + 1024 * 5) / 1e6)
    p = plan("long")
    assert (p.estimate.input, p.estimate.cache_write, p.estimate.cache_write_1h) == (2, 100, 100)
    p = plan("none")
    assert (p.estimate.input, p.estimate.cache_write) == (102, 0)
    assert p.estimate_usd == pytest.approx((102 * 1 + 1024 * 5) / 1e6)
    p = eng.plan(Request("", ({"role": "user", "text": "привет"},), "payload", tier="cheap", cache="short"), "fake")
    assert p.estimate.cache_write == 0  # пустой system — записи в кэш нет


# ---------- doctor, точка входа ----------

def test_doctor_api_line(api, tmp_path, monkeypatch, capsys):
    """doctor: запись wshub-api и ключи (как api key check, без значения); сервер — опция (ревью, п. 14–15)."""
    home = tmp_path / "home"
    ctx = doctor.Ctx(config=api.env.cfg, state=api.env.state, home=home, win_users=tmp_path / "none")

    def check():
        return doctor.check_api(ctx, api.env.hub.registry.get())
    c = check()
    assert "API хаба: включён" in c["detail"] and "ключ fake: ключ есть, права верны" in c["detail"]
    assert any("wshub setup --api" in x for x in c["detail"]) and c["status"] == "info"  # без записи — опция
    desk = home / ".config/Claude/claude_desktop_config.json"
    desk.parent.mkdir(parents=True)
    desk.write_text(json.dumps({"mcpServers": {"wshub-api": {"command": "wshub", "args": ["api"]}}}))
    c = check()
    assert "запись wshub-api в Desktop: есть" in c["detail"] and c["status"] == "ok"
    kp = keys.key_path(api.env.cfg.parent, "fake")
    os.chmod(kp, 0o644)
    c = check()
    assert c["status"] == "fail" and any(x.startswith("ключ fake: права ключа") and "0644" in x for x in c["detail"])
    assert MARKER not in json.dumps(c, ensure_ascii=False)
    kp.unlink()
    c = check()
    assert any(x.startswith("ключ fake: ключа нет") for x in c["detail"]) and c["status"] == "warn"
    set_api(api.env, "", ws="[workspace.rt.api]\nenabled = false\n")  # без секций: провайдер — по файлу ключа
    keys.write(keys.key_path(api.env.cfg.parent, "anthropic"), "sk-ant-other-key-0000")
    c = check()
    assert "ключ anthropic: ключ есть, права верны" in c["detail"] and not any("fake" in x for x in c["detail"])
    assert any("закрыты от отправки в API" in x and "rt" in x for x in c["detail"]) and c["status"] == "ok"
    assert doctor._is_wshub_cmd(["wshub", "api"]) is False
    assert doctor._is_wshub_cmd(["python", "-m", "wshub", "api"]) is False
    monkeypatch.setattr(doctor, "check_code", lambda ctx, reg, hub: doctor._check("code", "wshub-code", "info", "x"))
    doctor.main(api.env.cfg, api.env.state)  # строка wshub-api — в отчёте wshub doctor
    assert "] wshub-api" in capsys.readouterr().out


def test_stats_without_sections(api, capsys):
    """stats: провайдер без секции — по ключу; нет ни секций, ни ключей — «провайдеров нет» (ревью, п. 14)."""
    set_api(api.env, "", ws="")
    assert "fake: зоны не проверяются" in api.cli("stats")[1]
    keys.key_path(api.env.cfg.parent, "fake").unlink()
    assert "провайдеров нет: ни секций [api.providers.<имя>] в реестре, ни ключей" in api.cli("stats")[1]


def test_api_main_without_args_serves(env, monkeypatch):
    seen = []
    monkeypatch.setattr(api_server, "serve", lambda config, state: seen.append((config, state)) or 0)
    assert cli.api_main([], env.cfg, env.state) == 0 and seen == [(env.cfg, env.state)]
    assert "wshub api  " in cli.USAGE and "MCP-сервер wshub-api" in cli.USAGE

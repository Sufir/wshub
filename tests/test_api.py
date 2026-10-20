"""API хаба по ключу (ТЗ WSH-4.2): ядро, фейковый провайдер, ключи, журнал трат и зоны, реестр, CLI."""
from __future__ import annotations

import json
import os
import re
import stat
import sys
import types
import warnings
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

import wshub
from wshub import core as hub_core
from wshub import registry
from wshub.llm import cli, keys, ledger
from wshub.llm import core as api_core
from wshub.llm.providers import PROVIDERS, fake
from wshub.llm.types import Request, Response, Usage

TZ = timezone(timedelta(hours=3))
NOW = datetime(2026, 10, 18, 12, 0, tzinfo=TZ)
MARKER = "sk-test-MARKER-5f3a9c"
API = """[api]
enabled = true
max_usd_per_call = 1.0
daily_usd = 10.0

[api.providers.fake]
included_usd = 5
credits_until = "2026-10-31"
"""
WS_API = "[workspace.payload.api]\nenabled = true\n"
# оценка запроса по умолчанию: вход «\nпривет, модель» — 4 токена × $1/Мток + max_tokens 1024 × $5/Мток
EST = (4 * 1 + 1024 * 5) / 1e6


def set_api(env, api: str = API, ws: str = WS_API) -> None:
    env.write_registry()
    with env.cfg.open("a", encoding="utf-8") as f:
        f.write("\n" + ws + "\n" + api)
    env._mtime += 1_000_000_000
    os.utime(env.cfg, ns=(env._mtime, env._mtime))


class ApiEnv:
    def __init__(self, env, capsys):
        self.env, self.capsys, self.t = env, capsys, NOW
        self.outputs: list[str] = []
        (env.proj / "q.txt").write_text("привет, модель", encoding="utf-8")
        set_api(env)
        keys.write(keys.key_path(env.cfg.parent, "fake"), MARKER)
        self.ledger_path = ledger.ledger_path(env.state)

    def cli(self, *args) -> tuple[int, str, str]:
        rc = cli.api_main(list(args), self.env.cfg, self.env.state, now=lambda: self.t)
        c = self.capsys.readouterr()
        self.outputs.append(c.out + c.err)
        return rc, c.out, c.err

    def run(self, *extra, tier: str | None = "cheap", provider: str = "fake", project: str = "payload",
            inp: str = "q.txt"):
        return self.cli("run", "--provider", provider, "--project", project, "--input", inp,
                        *(("--tier", tier) if tier else ()), *extra)

    def rows(self, kind: str = "run") -> list[dict]:
        return [r for r in ledger.read(self.ledger_path).rows if r["kind"] == kind]

    def spend(self, cost: float, zone: str = "included", ts: datetime = NOW, provider: str = "fake") -> None:
        ledger.append(self.ledger_path, {"ts": ledger.stamp(ts), "kind": "run", "provider": provider,
                                         "model": "fake-cheap", "tier": "cheap", "project": "payload",
                                         "template": None, "zone": zone, "usage": Usage().as_dict(),
                                         "cost_usd": cost, "ok": True})

    def engine(self) -> api_core.Api:
        return api_core.Api(registry.RegistryFile(self.env.cfg).get(), self.env.cfg.parent, self.env.state,
                            lambda: self.t)


@pytest.fixture
def api(env, capsys) -> ApiEnv:
    return ApiEnv(env, capsys)


def use(monkeypatch, prov, name: str = "fake"):
    monkeypatch.setitem(PROVIDERS, name, types.SimpleNamespace(create=lambda conf: prov))
    return prov


def req(**kw) -> Request:
    return Request("", ({"role": "user", "text": "привет"},), "payload", **{"tier": "cheap", **kw})


# ---------- модульность (ТЗ, п. 4.1) ----------

class Echo2:
    """Второй провайдер — свой класс, не FakeProvider: ядро знает только интерфейс provider.Provider."""
    name = "echo2"
    caps = frozenset()
    PRICES = {"e2-small": 1e-6, "e2-mid": 2e-6, "e2-big": 5e-6}  # USD за токен входа и выхода

    def __init__(self):
        self.calls: list[Request] = []

    def models(self):
        return {"cheap": "e2-small", "standard": "e2-mid", "strong": "e2-big"}

    def price(self, model, usage):
        return self.PRICES[model] * (usage.input + usage.output)

    def estimate(self, request):
        return Usage(input=10)

    def run(self, request, key):
        self.calls.append(request)
        return Response("эхо-2", None, Usage(input=10, output=3), request.model, self.name)


def test_second_provider_from_test_only(api, monkeypatch):
    """Провайдер, объявленный только в тесте (подмена словаря), проходит run, stats и зоны без правки ядра."""
    prov = use(monkeypatch, Echo2(), "echo2")
    set_api(api.env, API + '\n[api.providers.echo2]\nincluded_usd = 1\ncredits_until = "2026-10-31"\n')
    keys.write(keys.key_path(api.env.cfg.parent, "echo2"), MARKER)
    rc, out, _ = api.run(provider="echo2")
    assert rc == 0 and out.startswith("эхо-2\nAPI: e2-small") and len(prov.calls) == 1
    assert api.rows()[-1]["provider"] == "echo2" and api.rows()[-1]["zone"] == "included"
    assert api.rows()[-1]["cost_usd"] == pytest.approx(13e-6)
    rc, out, _ = api.cli("stats")
    assert rc == 0 and "echo2: включённые" in out and "по моделям: e2-small" in out
    assert api.cli("credits", "echo2", "--left", "0", "--until", "31.10")[0] == 0
    rc, _, err = api.run(provider="echo2")
    assert rc == 1 and "wshub api allow echo2" in err and len(prov.calls) == 1
    assert api.cli("allow", "echo2", "--usd", "1", "--until", "31.10")[0] == 0
    rc, out, _ = api.run(provider="echo2")
    assert rc == 0 and api.rows()[-1]["zone"] == "purchased" and "купленная зона" in out
    (api.env.proj / "s.json").write_text('{"type": "object"}')
    rc, _, err = api.run("--schema", "s.json", provider="echo2")  # caps пустые — ядро отказывает и ему
    assert rc == 1 and "провайдер echo2 не поддерживает schema" in err and len(prov.calls) == 2


IMPORT_RE = re.compile(r"^\s*(?:from\s+\S*providers\.\w+\s+import|from\s+\S*providers\s+import\s+(?!PROVIDERS\s*$)"
                       r"|import\s+\S*providers\.\w+|from\s+\S+\s+import\s+.*\bproviders\b)", re.M)


def test_providers_imported_only_through_dict():
    """Ядро и остальной код не импортируют модули провайдеров по имени; загрузки кода по строке в llm нет."""
    for bad_line in ("from .providers import fake", "from wshub.llm.providers.fake import FakeProvider",
                     "from .providers import PROVIDERS, fake", "from . import providers",
                     "import wshub.llm.providers.fake"):
        assert IMPORT_RE.search(bad_line), bad_line  # шаблон ловит все виды импорта
    assert not IMPORT_RE.search("from .providers import PROVIDERS")
    src = Path(wshub.__file__).parent
    allowed = src / "llm" / "providers" / "__init__.py"
    files = [f for f in src.rglob("*.py") if f != allowed]
    assert any(f.name == "core.py" and f.parent.name == "llm" for f in files)
    bad = [str(f) for f in files if IMPORT_RE.search(f.read_text(encoding="utf-8"))]
    assert bad == []
    # ядро и реестр не знают имён конкретных провайдеров: ни класса, ни строки "fake"
    named = [str(f) for f in files if f.parent.name != "providers"
             and re.search(r"FakeProvider|[\"']fake[\"']", f.read_text(encoding="utf-8"))]
    assert named == []
    dynamic = [str(f) for f in (src / "llm").rglob("*.py")
               if re.search(r"import_module|__import__|importlib", f.read_text(encoding="utf-8"))]
    assert dynamic == []
    # провайдеров несколько — isort собирает их в одну строку (ТЗ WSH-4.6, решение 5.1.4)
    assert re.search(r"^from \. import .*\bfake\b", allowed.read_text(encoding="utf-8"), re.M)


# ---------- caps (п. 4.2) ----------

@pytest.mark.parametrize("cap", ["schema", "effort"])
def test_caps_refused_before_request_cli(api, monkeypatch, cap):
    prov = use(monkeypatch, fake.FakeProvider(caps={"batch", "cache", "schema", "effort"} - {cap}))
    (api.env.proj / "s.json").write_text('{"type": "object", "properties": {"ok": {"type": "boolean"}}}')
    extra = ("--schema", "s.json") if cap == "schema" else ("--effort", "low")
    rc, _, err = api.run(*extra)
    assert rc == 1 and f"не поддерживает {cap}" in err
    assert prov.calls == [] and api.rows() == []


def test_cache_cap_refused_before_request(api, monkeypatch):
    prov = use(monkeypatch, fake.FakeProvider(caps={"schema", "effort"}))
    with pytest.raises(api_core.ApiError, match="не поддерживает cache"):
        api.engine().plan(req(cache="short"), "fake")
    assert prov.calls == []
    prov.caps = frozenset({"cache"})
    eng = api.engine()
    resp = eng.run(eng.plan(req(cache="short"), "fake"))
    assert resp.usage.cache_read > 0 and len(prov.calls) == 1


def test_caps_present_schema_answer(api):
    (api.env.proj / "s.json").write_text('{"type": "object", "properties": {"ok": {"type": "boolean"}}}')
    rc, out, _ = api.run("--schema", "s.json", "--effort", "low")
    assert rc == 0 and json.loads(out.split("\nAPI:")[0]) == {"ok": False}


# ---------- уровни (п. 4.3) ----------

@pytest.mark.parametrize("tier,model", [("cheap", "fake-cheap"), ("standard", "fake-standard")])
def test_tiers_map_to_provider_models(api, tier, model):
    rc, out, _ = api.run("--estimate", tier=tier)
    assert rc == 0 and f"оценка: {model} ·" in out


def test_registry_overrides_tier_models(api):
    set_api(api.env, API + 'models = { cheap = "fake-standard" }\n')
    rc, out, _ = api.run("--estimate")
    assert rc == 0 and "оценка: fake-standard ·" in out


def test_strong_requires_operator_choice(api, monkeypatch):
    prov = use(monkeypatch, fake.FakeProvider())
    assert api.run("--choice", "nobody", tier="strong")[0] == 2
    rc, _, err = api.run(tier="strong")
    assert rc == 1 and "--choice operator" in err
    rc, _, err = api.run("--model", "fake-strong", tier=None)  # явная модель уровня strong — тоже выбор
    assert rc == 1 and "--choice operator" in err
    assert prov.calls == []
    rc, out, _ = api.run("--choice", "operator", tier="strong")
    assert rc == 0 and "API: fake-strong" in out
    with pytest.raises(api_core.ApiError, match="--choice operator"):  # ядро само, без отсечки CLI
        api.engine().plan(req(tier="strong"), "fake", choice="nobody")
    set_api(api.env, API + "strong_requires_choice = false\n")
    assert api.run(tier="strong")[0] == 0


@pytest.mark.parametrize("models,args", [
    ('{ strong = "fake-standard" }', ("--model", "fake-strong")),  # своя strong провайдера убрана из уровней
    ('{ cheap = "fake-strong" }', ("--tier", "cheap")),  # strong поставлена на другой уровень
    ('{ strong = "x-big" }', ("--model", "x-big")),  # strong из реестра
])
def test_strong_choice_not_bypassed_by_tier_override(api, monkeypatch, models, args):
    prov = use(monkeypatch, fake.FakeProvider(prices={**fake.PRICES, "x-big": (15.0, 75.0, 0, 0)}))
    set_api(api.env, API + f"models = {models}\n")
    rc, _, err = api.run(*args, tier=None)
    assert rc == 1 and "--choice operator" in err and prov.calls == []
    assert api.run(*args, "--choice", "operator", tier=None)[0] == 0


def test_unknown_model_price_refused(api, monkeypatch):
    prov = use(monkeypatch, fake.FakeProvider())  # модель вне уровней — выбор оператора (WSH-4.3), отказ — по цене
    rc, _, err = api.run("--model", "nope-1", "--choice", "operator", tier=None)
    assert rc == 1 and "цена модели nope-1" in err and prov.calls == []


def test_model_outside_tiers_needs_operator_choice(api, monkeypatch):
    """WSH-4.3, п. 2.2.1 и 3.5: явная --model вне уровней (с учётом переопределений) — только с --choice operator,
    независимо от strong_requires_choice; текст называет уровни и их модели."""
    prov = use(monkeypatch, fake.FakeProvider())
    set_api(api.env, API + 'models = { cheap = "fake-standard" }\nstrong_requires_choice = false\n')
    rc, _, err = api.run("--model", "fake-cheap", tier=None)  # своя cheap провайдера убрана переопределением
    assert rc == 1 and prov.calls == [] and api.rows() == []
    assert ("модель fake-cheap не из уровней провайдера fake (cheap — fake-standard, standard — fake-standard, "
            "strong — fake-strong); другая модель — только явным выбором оператора: --choice operator") in err
    with pytest.raises(api_core.ApiError, match="не из уровней"):  # ядро само, без отсечки CLI
        api.engine().plan(req(tier=None, model="fake-cheap"), "fake")
    rc, out, _ = api.run("--model", "fake-cheap", "--choice", "operator", tier=None)
    assert rc == 0 and "API: fake-cheap" in out and prov.calls[-1].model == "fake-cheap"
    assert api.run("--model", "fake-standard", tier=None)[0] == 0  # модель уровня — без выбора


def test_money_limits_only_when_set(api, monkeypatch):
    """WSH-4.3, п. 2.2.2 и 3.6: без included_usd, daily_usd и max_usd_per_call — ни зон, ни потолков; запрос
    проходит, строка журнала пишется (зона — включённая)."""
    prov = use(monkeypatch, fake.FakeProvider())
    set_api(api.env, "[api]\nenabled = true\n\n[api.providers.fake]\n")
    reg = registry.RegistryFile(api.env.cfg).get()
    assert reg.api.max_usd_per_call is None and reg.api.daily_usd is None
    assert not reg.api.providers["fake"].included_set
    api.spend(1000)  # сегодня «потрачено» много — потолка нет
    rc, out, err = api.run("--max-tokens", "1000000")  # оценка ≈$5 — потолка одного запроса нет
    assert rc == 0, err
    assert "цикл" not in out and len(prov.calls) == 1
    r = api.rows()[-1]
    assert (r["zone"], r["ok"], r["provider"]) == ("included", True, "fake")
    rc, out, _ = api.run("--estimate")
    assert rc == 0 and "зона: включённая" in out and "зоны не проверяются" in out
    rc, out, _ = api.cli("stats")
    assert rc == 0 and "сегодня ≈$1000.00 (daily_usd не задан)" in out
    assert "fake: зоны не проверяются" in out and "по зонам: включённая" in out


def test_credits_command_turns_zones_on(api, monkeypatch):
    """Решение 5.1 (вопрос 2): команда credits — явное действие оператора, она включает зоны и без included_usd."""
    prov = use(monkeypatch, fake.FakeProvider())
    set_api(api.env, "[api]\nenabled = true\n\n[api.providers.fake]\n")
    assert api.cli("credits", "fake", "--left", "0", "--until", "31.10")[0] == 0
    rc, _, err = api.run()
    assert rc == 1 and "включённые кредиты исчерпаны" in err and prov.calls == []
    assert "fake: включённые" in api.cli("stats")[1]
    api.cli("credits", "fake", "--left", "1", "--until", "31.10")
    rc, out, _ = api.run()
    assert rc == 0 and "из $1.00, до 31.10" in out


# ---------- ключи (п. 4.4, 2.2) ----------

def _kp(api) -> Path:
    return keys.key_path(api.env.cfg.parent, "fake")


@pytest.mark.parametrize("case", ["file0644", "dir0755", "owner"])
def test_key_permissions_refused(api, monkeypatch, case):
    kp = _kp(api)
    if case == "file0644":
        os.chmod(kp, 0o644)
        want = f"права ключа {kp} — 0644; исправь: chmod 600 {kp}"
    elif case == "dir0755":
        os.chmod(kp.parent, 0o755)
        want = f"права ключа {kp.parent} — 0755; исправь: chmod 700 {kp.parent}"
    else:
        uid = os.geteuid()
        monkeypatch.setattr(os, "geteuid", lambda: uid + 1)
        want = f"владелец uid {uid}, а не текущий пользователь; исправь: chown {uid + 1}"
    prov = use(monkeypatch, fake.FakeProvider())
    rc, out, _ = api.cli("key", "check", "fake")
    assert rc == 1 and want in out
    rc, _, err = api.run()
    assert rc == 1 and want in err and prov.calls == [] and api.rows() == []


def test_key_symlink_and_missing_refused(api, monkeypatch):
    kp = _kp(api)
    real = api.env.outside / "k"
    real.write_text(MARKER)
    os.chmod(real, 0o600)
    kp.unlink()
    kp.symlink_to(real)
    rc, out, _ = api.cli("key", "check", "fake")
    assert rc == 1 and "симлинк" in out
    with pytest.raises(keys.KeyProblem, match="симлинк"):
        keys.read(kp)
    kp.unlink()
    rc, out, _ = api.cli("key", "check", "fake")
    assert rc == 1 and "ключа нет" in out and "wshub api key set fake" in out
    assert api.cli("key", "check", "../x")[0] == 1  # имя только из словаря провайдеров


def test_key_set_atomic_0600(api, monkeypatch):
    kp = _kp(api)
    kp.unlink()
    os.rmdir(kp.parent)
    seen: dict = {}
    real_open, real_replace = os.open, os.replace

    def spy_open(p, flags, mode=0o777, *a, **k):
        if str(p).endswith(".tmp"):
            seen["flags"], seen["mode"] = flags, mode
        return real_open(p, flags, mode, *a, **k)

    def spy_replace(src, dst):
        seen["tmp_mode"] = stat.S_IMODE(os.lstat(src).st_mode)
        return real_replace(src, dst)

    monkeypatch.setattr(os, "open", spy_open)
    monkeypatch.setattr(os, "replace", spy_replace)
    monkeypatch.setattr(cli.getpass, "getpass", lambda prompt="": MARKER)
    old = os.umask(0)  # права не зависят от umask: файл создаётся сразу с 0600
    try:
        rc, out, _ = api.cli("key", "set", "fake")
    finally:
        os.umask(old)
    assert rc == 0 and MARKER not in out
    assert seen["mode"] == 0o600 and seen["flags"] & os.O_EXCL and seen["flags"] & os.O_CREAT
    assert seen["tmp_mode"] == 0o600
    assert stat.S_IMODE(kp.stat().st_mode) == 0o600 and stat.S_IMODE(kp.parent.stat().st_mode) == 0o700
    assert keys.read(kp) == MARKER and [p.name for p in kp.parent.iterdir()] == ["fake"]
    assert api.cli("key", "check", "fake")[1].startswith("ключ есть, права верны")


def test_key_set_empty_refused(api, monkeypatch):
    kp = _kp(api)
    kp.unlink()
    monkeypatch.setattr(cli.getpass, "getpass", lambda prompt="": "  ")
    rc, _, err = api.cli("key", "set", "fake")
    assert rc == 1 and "пустой ввод" in err and not kp.exists()


def test_key_check_blank_key_file(api):
    _kp(api).write_text("  \n")  # права прежние, 0600
    rc, out, _ = api.cli("key", "check", "fake")
    assert rc == 1 and "пуст" in out


def test_key_set_without_terminal_refused(api, monkeypatch):
    def echoing(prompt=""):  # так getpass ведёт себя без терминала: предупреждает и читает с эхом
        warnings.warn("Can not control echo on the terminal.", cli.getpass.GetPassWarning, stacklevel=1)
        return "new-key-value"

    monkeypatch.setattr(cli.getpass, "getpass", echoing)
    rc, _, err = api.cli("key", "set", "fake")
    assert rc == 1 and "нет терминала" in err and keys.read(_kp(api)) == MARKER


def test_key_set_refuses_open_keys_dir(api, monkeypatch):
    os.chmod(_kp(api).parent, 0o755)
    monkeypatch.setattr(cli.getpass, "getpass", lambda prompt="": "new")
    rc, _, err = api.cli("key", "set", "fake")
    assert rc == 1 and "chmod 700" in err
    os.chmod(_kp(api).parent, 0o700)
    assert keys.read(_kp(api)) == MARKER  # ключ не перезаписан


def test_key_marker_never_leaks(api, monkeypatch):
    """Ключ-маркер не встречается ни в файлах каталога состояния, ни в выводе CLI, ни в исключениях."""
    prov = use(monkeypatch, fake.FakeProvider())
    (api.env.proj / "s.json").write_text('{"type": "object"}')
    api.run()
    api.run("--estimate")
    api.run("--schema", "s.json")
    api.cli("key", "check", "fake")
    api.cli("credits", "fake", "--left", "4", "--until", "30.10")
    api.cli("allow", "fake", "--usd", "1", "--until", "30.10")
    api.cli("stats")
    prov.fail = "401 invalid x-api-key {key} for request"
    rc, _, err = api.run()
    assert rc == 1 and "<ключ скрыт>" in err
    eng = api.engine()
    with pytest.raises(api_core.ApiError) as ei:
        eng.run(eng.plan(req(), "fake"))
    e = ei.value
    assert MARKER not in str(e) and MARKER not in repr(e.args)
    assert e.__context__ is None and e.__cause__ is None
    assert api.rows()[-1]["ok"] is False and "<ключ скрыт>" in api.rows()[-1]["error"]
    for fail in ("bad header {key!r}", f"token {MARKER[:12]}… rejected", f"suffix …{MARKER[-9:]}"):
        prov.fail = fail  # ключ в repr и куски ключа — тоже вырезаются
        rc, _, err = api.run()
        assert rc == 1 and "<ключ скрыт>" in err
    assert all(MARKER not in o and MARKER[:12] not in o and MARKER[-9:] not in o for o in api.outputs)
    assert len(api.outputs) >= 11
    files = [p for p in api.env.state.rglob("*") if p.is_file()]
    assert api.ledger_path in files
    assert [str(p) for p in files if MARKER[:SCRUB].encode() in p.read_bytes()
            or MARKER[-SCRUB:].encode() in p.read_bytes()] == []


SCRUB = api_core.SCRUB_MIN


def test_scrub_parts_of_key():
    key = "abcdefghijklmnop"
    assert api_core.scrub(f"x {key} y", key) == "x <ключ скрыт> y"
    assert api_core.scrub(f"x {key[:8]}…", key) == "x <ключ скрыт>…"
    assert api_core.scrub(f"x {key[:7]}", key) == f"x {key[:7]}"  # короче SCRUB_MIN — не трогаем
    assert api_core.scrub("short k1 here", "k1") == "short <ключ скрыт> here"  # короткий ключ — целиком


def test_keys_inside_closed_dirs(env):
    """keys/ — внутри ~/.config/wshub: закрыт deny_read задач Code и служебными данными файлового сервера."""
    home_keys = keys.key_path(hub_core.DEFAULT_CONFIG.parent, "fake").parent
    closed = [Path(p).expanduser() for p in registry.CODE_DENY_READ]
    assert any(home_keys.is_relative_to(c) for c in closed)
    assert registry.CodeGlobal().deny_read == registry.CODE_DENY_READ
    hub = hub_core.Hub(env.cfg, env.state)
    for k in (home_keys, keys.key_path(env.cfg.parent, "fake").parent):
        assert hub.workspace_conflict(k.resolve(), "ro") is not None


# ---------- зоны и цикл (п. 4.5) ----------

def test_included_zone_spent(api):
    rc, out, _ = api.run()
    assert rc == 0
    r = api.rows()[-1]
    assert r["zone"] == "included" and r["ok"] is True and r["cost_usd"] > 0 and r["project"] == "payload"
    assert set(r) >= {"ts", "provider", "model", "tier", "project", "template", "usage", "cost_usd", "zone", "ok"}
    z = api.engine().zones("fake")
    assert z.included_spent == pytest.approx(r["cost_usd"]) and z.included_left == pytest.approx(5 - r["cost_usd"])


def test_included_exhausted_refuses_with_allow_hint(api, monkeypatch):
    prov = use(monkeypatch, fake.FakeProvider())
    api.spend(5 - EST / 2)
    rc, _, err = api.run()
    assert rc == 1 and prov.calls == []
    assert ("включённые кредиты исчерпаны (≈$0.00); купленные — только по разрешению оператора: "
            "wshub api allow fake") in err
    assert len(api.rows()) == 1


def test_allow_until_date_and_amount(api, monkeypatch):
    prov = use(monkeypatch, fake.FakeProvider())
    api.spend(5)
    assert api.cli("allow", "fake", "--usd", "0.02", "--until", "20.10")[0] == 0
    rc, out, _ = api.run()
    assert rc == 0 and api.rows()[-1]["zone"] == "purchased" and "купленная зона" in out
    api.spend(0.02 - EST / 2, zone="purchased")  # разрешение почти исчерпано
    rc, _, err = api.run()
    assert rc == 1 and "действующее разрешение" in err and len(prov.calls) == 1
    assert api.cli("allow", "fake", "--usd", "1", "--until", "20.10")[0] == 0  # новое разрешение — заново
    assert api.run()[0] == 0
    api.t = datetime(2026, 10, 21, 9, 0, tzinfo=TZ)  # срок разрешения прошёл
    rc, _, err = api.run()
    assert rc == 1 and "wshub api allow fake" in err and len(prov.calls) == 2


def test_purchased_spent_before_allow_not_counted(api):
    api.spend(5)
    api.spend(3, zone="purchased")  # старое разрешение
    api.cli("allow", "fake", "--usd", "0.5", "--until", "20.10")
    z = api.engine().zones("fake")
    assert z.purchased_spent == 0 and z.purchased_left == 0.5


def test_revoke(api):
    api.spend(5)
    api.cli("allow", "fake", "--usd", "1", "--until", "20.10")
    assert api.run()[0] == 0
    assert api.cli("allow", "fake", "--revoke")[0] == 0
    rc, _, err = api.run()
    assert rc == 1 and "исчерпаны" in err
    assert api.engine().zones("fake").allow_usd is None


def test_until_day_itself_counts(api):
    """Срок включается целиком: в сам день credits_until и allow --until ещё можно тратить."""
    api.t = datetime(2026, 10, 31, 23, 30, tzinfo=TZ)
    assert api.run()[0] == 0 and api.rows()[-1]["zone"] == "included"
    api.spend(5)
    api.cli("allow", "fake", "--usd", "1", "--until", "31.10")
    assert api.run()[0] == 0 and api.rows()[-1]["zone"] == "purchased"


def test_run_rechecks_zone_under_lock(api, monkeypatch):
    """План, проверенный раньше, не тратит, если остаток кончился до отправки (параллельный запрос, revoke)."""
    prov = use(monkeypatch, fake.FakeProvider())
    eng = api.engine()
    plan = eng.plan(req(), "fake")
    assert plan.zone == "included"
    api.spend(5)
    with pytest.raises(api_core.ApiError, match="исчерпаны"):
        eng.run(plan)
    api.cli("allow", "fake", "--usd", "1", "--until", "20.10")
    plan = eng.plan(req(), "fake")
    assert plan.zone == "purchased"
    api.cli("allow", "fake", "--revoke")
    with pytest.raises(api_core.ApiError, match="только по разрешению"):
        eng.run(plan)
    assert prov.calls == [] and len(api.rows()) == 1
    assert api.ledger_path.with_suffix(".lock").exists()


def test_included_zero_after_credits_until(api):
    api.t = datetime(2026, 11, 1, 9, 0, tzinfo=TZ)
    rc, _, err = api.run()
    assert rc == 1 and "исчерпаны (≈$0.00)" in err
    assert api.engine().zones("fake").included_left == 0
    assert "остаток ≈$0.00" in api.cli("stats")[1]


def test_credits_left_resets_cycle(api):
    api.spend(4.999)
    assert api.run()[0] == 1
    rc, out, _ = api.cli("credits", "fake", "--left", "3", "--until", "25.10")
    assert rc == 0 and "новый цикл" in out
    assert api.rows("credits")[-1]["left_usd"] == 3 and api.rows("credits")[-1]["until"] == "2026-10-25"
    rc, out, _ = api.run()
    assert rc == 0 and "из $3.00, до 25.10" in out
    z = api.engine().zones("fake")
    assert z.included_total == 3 and z.until == date(2026, 10, 25) and z.included_spent < 0.01
    api.cli("credits", "fake", "--left", "0", "--until", "25.10")
    assert api.run()[0] == 1
    assert api.cli("credits", "fake", "--left", "3", "--until", "01.10.2026")[0] == 1  # дата прошла
    assert api.cli("credits", "fake", "--left", "-1", "--until", "25.10")[0] == 2


def test_credits_beats_registry_after_until(api):
    api.t = datetime(2026, 11, 2, 9, 0, tzinfo=TZ)
    assert api.run()[0] == 1
    api.cli("credits", "fake", "--left", "2", "--until", "30.11")
    assert api.run()[0] == 0


def test_ceiling_per_call(api, monkeypatch):
    prov = use(monkeypatch, fake.FakeProvider())
    set_api(api.env, API.replace("max_usd_per_call = 1.0", "max_usd_per_call = 0.001"))
    rc, _, err = api.run()
    assert rc == 1 and "max_usd_per_call" in err and prov.calls == []
    assert api.run("--max-tokens", "100")[0] == 0  # (4 + 100×5) / 1e6 ≈ 0.0005


def test_ceiling_daily(api, monkeypatch):
    prov = use(monkeypatch, fake.FakeProvider())
    set_api(api.env, API.replace("included_usd = 5", "included_usd = 100"))
    api.spend(9.999, ts=NOW - timedelta(days=1))  # вчера — в дневной потолок не идёт
    assert api.run()[0] == 0
    api.spend(9.999, ts=NOW - timedelta(hours=2))
    rc, _, err = api.run()
    assert rc == 1 and "daily_usd" in err and len(prov.calls) == 1


def test_ceiling_daily_counts_all_providers(api, monkeypatch):
    prov = use(monkeypatch, fake.FakeProvider())
    set_api(api.env, API.replace("included_usd = 5", "included_usd = 100"))
    api.spend(9.999, ts=NOW - timedelta(hours=1), provider="other")
    rc, _, err = api.run()
    assert rc == 1 and "daily_usd" in err and prov.calls == []


def test_estimate_makes_no_request(api, monkeypatch):
    prov = use(monkeypatch, fake.FakeProvider())
    _kp(api).unlink()  # ключ для оценки не нужен
    rc, out, _ = api.run("--estimate")
    assert rc == 0 and "оценка: fake-cheap" in out and "≈$0.01" in out and "зона: включённая" in out
    assert prov.calls == [] and not api.ledger_path.exists()
    api.spend(5)
    rc, out, _ = api.run("--estimate")
    assert rc == 0 and "запрос был бы отклонён: включённые кредиты исчерпаны" in out


# ---------- «сгорит» (п. 4.6) ----------

@pytest.mark.parametrize("included,until,burn", [(100, "2026-10-23", True), (100, "2026-10-25", False),
                                                 (10, "2026-10-23", False), (10.01, "2026-10-23", True),
                                                 (100, "2026-10-18", True)])
def test_burn_line(api, included, until, burn):
    set_api(api.env, API.replace("included_usd = 5", f"included_usd = {included}")
            .replace('"2026-10-31"', f'"{until}"'))
    rc, out, _ = api.run("--estimate")  # без траты: остаток ровно included — граница «больше $10»
    assert rc == 0 and ("⚠ сгорит:" in out) is burn
    rc, out, _ = api.run()
    assert rc == 0 and ("⚠ сгорит:" in out) is burn
    rc, out, _ = api.cli("stats")
    assert rc == 0 and ("⚠ сгорит:" in out) is burn


# ---------- реестр (п. 4.7, 2.4) ----------

def test_api_error_breaks_only_api(api):
    broken = API.replace("max_usd_per_call = 1.0", 'max_usd_per_call = "x"')
    set_api(api.env, broken + "\n[code]\nmax_wait_s = 30\n")
    reg = registry.RegistryFile(api.env.cfg).get()
    assert reg.api.error and "max_usd_per_call" in reg.api.error
    assert reg.code.present and reg.code.error is None and "payload" in reg.workspaces
    tok = api.env.open("payload")  # файловый сервер работает
    assert "hello" in api.env.hub.read(tok, "a.txt")
    for args in (("run", "--provider", "fake", "--project", "payload", "--input", "q.txt", "--tier", "cheap"),
                 ("stats",), ("credits", "fake", "--left", "1", "--until", "30.10")):
        rc, _, err = api.cli(*args)
        assert rc == 1 and "секция [api] в реестре с ошибкой" in err
    set_api(api.env, "[api]\nenabled = 5\n")
    assert registry.RegistryFile(api.env.cfg).get().api.error == "api.enabled: true или false"


@pytest.mark.parametrize("text,err", [
    ('[api.providers.fake]\nincluded_usd = 5\n', "нужен credits_until"),
    ('[api.providers.fake]\ncredits_until = "31.10"\n', "credits_until"),
    ('[api.providers.fake]\nmodels = { best = "x" }\n', "models"),
    ('[api.providers.fake]\nstrong_requires_choice = "yes"\n', "strong_requires_choice"),
    ("[api]\ndaily_usd = 0\n", "daily_usd"),
    ("[api]\nmax_usd_per_call = nan\n", "max_usd_per_call"),  # nan и inf отключили бы потолки
    ("[api]\ndaily_usd = inf\n", "daily_usd"),
    ('[api.providers.fake]\nincluded_usd = inf\ncredits_until = "2026-10-31"\n', "included_usd"),
    ('[api.providers.fake]\ncredits_until = "20261031"\n', "credits_until"),
    ('[api.providers.fake]\nbase_url = "http://api.example.com"\n', "base_url"),  # http — только loopback
    ('[api.providers.fake]\nbase_url = "http://127.0.0.2:8080"\n', "base_url"),
    ('[api.providers.fake]\nbase_url = "https://user@api.example.com"\n', "base_url"),
    ('[api.providers.fake]\nbase_url = "ftp://x"\n', "base_url"),
    ('[api.providers.fake]\nbase_url = 5\n', "base_url"),
    ('[api.providers.fake]\ntimeout_s = 0\n', "timeout_s"),
    ('[api.providers.fake]\ntimeout_s = "30"\n', "timeout_s"),
    ('[api.providers.fake]\ntimeout_s = true\n', "timeout_s"),
])
def test_api_section_errors(text, err):
    reg = registry.parse(text)
    assert reg.api.present and reg.api.error and err in reg.api.error


def test_api_registry_values():
    reg = registry.parse('[api]\nenabled = true\n'
                         '[api.providers.fake]\nincluded_usd = 7\ncredits_until = 2026-10-23\n'
                         '[api.providers.nope]\nx = 1\n[api.future]\n')
    p = reg.api.providers["fake"]
    assert reg.api.enabled and p.included_usd == 7 and p.credits_until == date(2026, 10, 23)
    assert p.strong_requires_choice is True and p.models == {}
    assert "nope" not in reg.api.providers
    assert any("[api.providers.nope]: неизвестный провайдер" in w for w in reg.warnings)
    assert "неизвестный ключ api.future — пропущен" in reg.warnings
    assert registry.parse("").api == registry.Api()
    assert p.base_url is None and p.timeout_s is None
    for url in ("https://api.anthropic.com", "https://proxy.local/anthropic", "http://127.0.0.1:8123",
                "http://localhost:9"):
        p = registry.parse(f'[api.providers.fake]\nbase_url = "{url}"\ntimeout_s = 30\n').api.providers["fake"]
        assert (p.base_url, p.timeout_s) == (url, 30)


def test_broken_provider_import_breaks_only_api(env, monkeypatch):
    monkeypatch.setitem(sys.modules, "wshub.llm.providers", None)  # импорт словаря провайдеров падает
    reg = registry.parse(env.cfg.read_text(encoding="utf-8") + "\n" + API)
    assert reg.api.error and "провайдеры API не загрузились" in reg.api.error
    assert "payload" in reg.workspaces and reg.code.error is None


def test_api_disabled_and_project_without_api(api, monkeypatch):
    prov = use(monkeypatch, fake.FakeProvider())
    set_api(api.env, API.replace("enabled = true", "enabled = false"))
    rc, _, err = api.run(inp="missing.txt")  # отказ до чтения файлов: не «нет файла»
    assert rc == 1 and "API хаба выключен" in err
    set_api(api.env, "")
    assert "API хаба выключен" in api.run(inp="missing.txt")[2]
    set_api(api.env)
    rc, _, err = api.run(project="rt", inp="missing.txt")
    assert rc == 1 and "[workspace.rt.api] enabled = true" in err
    set_api(api.env, ws="[workspace.payload.api]\nenabled = false\n")
    assert "[workspace.payload.api] enabled = true" in api.run(inp="missing.txt")[2]
    set_api(api.env, ws='[workspace.payload.api]\nenabled = "yes"\n')
    reg = registry.RegistryFile(api.env.cfg).get()
    assert "payload" in reg.api.project_errors and reg.api.error is None
    assert "[workspace.payload.api] с ошибкой" in api.run(inp="missing.txt")[2]
    assert prov.calls == [] and api.rows() == []


def test_provider_needs_registry_section(api, monkeypatch):
    set_api(api.env, API.split("[api.providers.fake]")[0])
    rc, _, err = api.run()
    assert rc == 1 and "нет секции [api.providers.fake]" in err
    assert api.run(provider="nope")[0] == 1


# ---------- файлы проекта (п. 4.8) ----------

def test_input_outside_symlink_deny_refused(api, monkeypatch):
    prov = use(monkeypatch, fake.FakeProvider())
    (api.env.proj / "link.txt").symlink_to(api.env.outside / "secret.txt")
    for inp, err in (("../outside/secret.txt", "путь вне проекта"),
                     (str(api.env.outside / "secret.txt"), "путь вне проекта"),
                     ("link.txt", "путь вне проекта"),
                     (".env", "политикой deny")):
        rc, out, e = api.run(inp=inp)
        assert rc == 1 and err in e, inp
        assert "TOPSECRET" not in out + e and "supersecret" not in out + e
    (api.env.proj / "env-link").symlink_to(api.env.proj / ".env")  # симлинк внутри корня на deny-файл
    rc, out, e = api.run(inp="env-link")
    assert rc == 1 and "политикой deny" in e and "supersecret" not in out + e
    rc, _, e = api.run("--system", ".env")
    assert rc == 1 and "политикой deny" in e
    rc, _, e = api.run("--schema", "../outside/secret.txt")
    assert rc == 1 and "путь вне проекта" in e
    assert prov.calls == [] and api.rows() == []


def test_input_absolute_inside_root_ok(api):
    assert api.run(inp=str(api.env.proj / "q.txt"))[0] == 0


# ---------- вывод (п. 2.5) ----------

def test_run_summary_line(api):
    rc, out, _ = api.run()
    lines = out.splitlines()
    assert rc == 0 and lines[0] == "привет, модель"
    assert re.fullmatch(r"API: fake-cheap · вход 4 \(из кэша 0\) · выход 4 · ≈\$0\.00 · цикл ≈\$0\.00 из \$5\.00, "
                        r"до 31\.10", lines[1]), lines[1]


def test_long_answer_goes_to_file(api, monkeypatch):
    use(monkeypatch, fake.FakeProvider(reply="x" * 9000))
    rc, out, _ = api.run()
    first = out.splitlines()[0]
    assert rc == 0 and first.startswith("ответ: ") and "x" * 100 not in out
    p = Path(first.removeprefix("ответ: "))
    assert p.is_relative_to(api.env.state / "api") and p.read_text() == "x" * 9000
    target = api.env.tmp / "ans.txt"
    use(monkeypatch, fake.FakeProvider(reply="коротко"))
    rc, out, _ = api.run("--out", str(target))
    assert rc == 0 and out.splitlines()[0] == f"ответ: {target}" and target.read_text() == "коротко"


# ---------- журнал (п. 6.1) ----------

def test_broken_ledger_line_skipped(api):
    api.spend(0.5)
    with api.ledger_path.open("a") as f:
        f.write('{"ts": "2026-10-18T10:00:00+03:00", "kind": "run", "provi\n')
    rc, out, _ = api.run()
    assert rc == 0 and "журнал: пропущено строк 1" in out
    assert "журнал: пропущено строк 1" in api.cli("stats")[1]
    assert api.engine().zones("fake").included_spent > 0.5


def test_torn_ledger_line_does_not_swallow_next(api, monkeypatch):
    """Обрыв без перевода строки: следующая запись (revoke) не склеивается с обрывком и не теряется."""
    prov = use(monkeypatch, fake.FakeProvider())
    api.spend(5)
    api.cli("allow", "fake", "--usd", "1", "--until", "20.10")
    with api.ledger_path.open("a") as f:
        f.write('{"ts": "2026-10-18T10:00:00+03:00", "kind": "ru')  # оборванная запись, без «\n»
    assert api.cli("allow", "fake", "--revoke")[0] == 0
    rc, _, err = api.run()
    assert rc == 1 and "исчерпаны" in err and prov.calls == []
    led = ledger.read(api.ledger_path)
    assert led.skipped == 1 and led.rows[-1]["kind"] == "revoke"


def test_stats_breakdown_and_days(api):
    api.spend(0.5, ts=NOW - timedelta(days=3))
    api.spend(0.25, zone="purchased")
    rc, out, _ = api.cli("stats")
    assert rc == 0 and "за цикл" in out
    assert "по зонам: включённая ≈$0.50 (1) · купленная ≈$0.25 (1)" in out
    assert "по проектам: payload ≈$0.75 (2)" in out and "по моделям: fake-cheap ≈$0.75 (2)" in out
    rc, out, _ = api.cli("stats", "--days", "1")
    assert rc == 0 and "за 1 дн." in out and "по проектам: payload ≈$0.25 (1)" in out
    assert api.cli("stats", "--days", "0")[0] == 2


def test_unreadable_ledger_fails_closed(api, monkeypatch):
    prov = use(monkeypatch, fake.FakeProvider())
    api.ledger_path.parent.mkdir(parents=True)
    api.ledger_path.write_text("garbage\nnot json\n")
    rc, _, err = api.run()
    assert rc == 1 and "не разбирается ни одна строка" in err and prov.calls == []
    assert api.cli("stats")[0] == 1
    api.ledger_path.unlink()
    api.ledger_path.mkdir()  # файл не читается целиком
    rc, _, err = api.run()
    assert rc == 1 and "не читается" in err and prov.calls == []


# ---------- даты, справка, точка входа ----------

def test_parse_until():
    today = date(2026, 10, 18)
    assert cli.parse_until("23.10", today) == date(2026, 10, 23)
    assert cli.parse_until("18.10", today) == today
    assert cli.parse_until("05.01", today) == date(2027, 1, 5)
    assert cli.parse_until("31.10.2026", today) == date(2026, 10, 31)
    with pytest.raises(api_core.ApiError):
        cli.parse_until("01.10.2026", today)
    for bad in ("31.02", "1/10", "2026-10-31"):
        with pytest.raises(cli._Usage):
            cli.parse_until(bad, today)


def test_server_dispatches_api(env, monkeypatch, capsys):
    from wshub.server import USAGE, main
    set_api(env)
    monkeypatch.setenv("WSHUB_CONFIG", str(env.cfg))
    monkeypatch.setenv("WSHUB_STATE", str(env.state))
    with pytest.raises(SystemExit) as e:
        main(["api", "stats"])
    assert e.value.code == 0 and "fake: включённые" in capsys.readouterr().out
    assert "wshub api run" in USAGE

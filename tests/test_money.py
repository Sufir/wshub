"""WSH-4.8, денежные настройки: по умолчанию ничего не ограничивает и не предупреждает; зоны задач Code после credits,
бюджет claude — остаток зоны, оценка при отмене и таймауте без итога, резерв пакетов, пороги [api.warn], сбой журнала
после оплаченного ответа, нечитаемый журнал без денежных настроек (решение 5.1.3), таймаут создания пакета, doctor
«деньги: …». Настоящий claude и сеть не вызываются."""
from __future__ import annotations

import time
from datetime import datetime

import pytest
from test_api import NOW, ApiEnv, req, set_api, use
from test_api_anthropic import Server
from test_api_batch import created
from test_api_server import Srv
from test_code import SUB_DIRS, _wait_running, add_sub, make_code, wait_terminal
from test_code_key import API as KEY_API
from test_code_key import MARKER as KEY_MARKER
from test_code_key import _key_path, _plan, _raw, _registry, _run
from test_code_limits import NO_LIMITS, _argv
from test_code_plan import _opt

from wshub import claude_cli, doctor, registry
from wshub.jobs import JobError
from wshub.llm import batches, keys, ledger
from wshub.llm import core as api_core
from wshub.llm.cli import api_main
from wshub.llm.core import BATCH_MAYBE, ApiError
from wshub.llm.providers import fake
from wshub.runner import apply_billing

SUBMIT = ("batch", "submit", "--provider", "fake", "--project", "payload", "--tier", "cheap")
FAKE_ZONE = '[api.providers.fake]\nincluded_usd = 5\ncredits_until = "2026-10-31"\n'
NOTE = "оценка, итога нет"


@pytest.fixture
def api(env, capsys) -> ApiEnv:
    """Пустой реестр: ни [api], ни [workspace.*.api]; провайдер fake — по ключу."""
    a = ApiEnv(env, capsys)
    set_api(env, "", ws="")
    (env.proj / "in").mkdir()
    for name in ("one", "two"):
        (env.proj / "in" / f"{name}.txt").write_text(f"текст {name}", encoding="utf-8")
    return a


@pytest.fixture
def code(tmp_path, monkeypatch):
    """Проект wshub-code, ключ anthropic, реестр без [api] и без лимитов."""
    c = make_code(tmp_path, monkeypatch)
    _registry(c, api="", **NO_LIMITS)
    keys.write(_key_path(c), KEY_MARKER)
    monkeypatch.setattr(claude_cli, "wshub_exe", lambda: str(tmp_path / "bin" / "wshub"))
    return c


class Spy(fake.FakeProvider):
    """Считает точные оценки входа (count_tokens — оценка с ключом)."""

    def __init__(self):
        super().__init__()
        self.keyed = 0

    def estimate(self, request, key=None):
        self.keyed += key is not None
        return super().estimate(request, key)


def warned(api: ApiEnv) -> list[str]:
    return [line for out in api.outputs for line in out.splitlines() if "⚠" in line]


def spend_now(path, cost: float, provider: str = "fake") -> None:
    ledger.append(path, {"ts": ledger.stamp(datetime.now().astimezone()), "kind": "run", "provider": provider,
                         "zone": "included", "cost_usd": cost})


def ctx(env, tmp_path) -> doctor.Ctx:
    return doctor.Ctx(config=env.cfg, state=env.state, home=tmp_path / "home", win_users=tmp_path / "none")


def money_line(c: dict) -> str:
    return next(x for x in c["detail"] if x.startswith("деньги: "))


# ---------- п. 3.1: умолчания ----------

def test_defaults_nothing_checks_or_warns(api, monkeypatch, tmp_path):
    """Пустой реестр (мутация М1 — умолчание day_usd не None): ни проверок, ни предупреждений; count_tokens у пакета
    не зовётся (у run остаётся — решение 5.1.2); резерва пакета нет; doctor — «деньги: ограничений нет»."""
    prov = use(monkeypatch, Spy())
    assert registry.parse("").api.warn == registry.ApiWarn(None, None, None)
    reg = registry.RegistryFile(api.env.cfg).get()
    assert (reg.api.max_usd_per_call, reg.api.daily_usd, reg.api.warn) == (None, None, registry.ApiWarn())
    api.spend(1000)  # сегодня «потрачено» много: любой порог по умолчанию дал бы предупреждение или отказ
    plan = api.engine().plan(req(), "fake")
    assert (plan.zone, plan.zones, plan.refusal, plan.warnings, plan.unread) == ("included", None, None, (), None)
    assert api.run("--max-tokens", "1000000")[0] == 0  # оценка ≈$5 — ни потолка, ни порога
    assert api.run("--estimate")[0] == 0 and api.cli("stats")[0] == 0
    keyed = prov.keyed
    assert api.cli(*SUBMIT, "--inputs", "in/one.txt", "in/two.txt", "--max-tokens", "1000000")[0] == 0
    assert prov.keyed == keyed  # пакет без денежных проверок — вход грубо, без count_tokens
    assert [r for r in api.rows() if r.get("reserve")] == []  # резерва пакета нет
    for args in (("batch", "status"), ("batch", "status", "fakebatch_1"), ("batch", "results", "fakebatch_1"),
                 ("batch", "cancel", "fakebatch_1")):
        assert api.cli(*args)[0] == 0
    prov.batch_state = "ended"  # отмена фейка переводит все его пакеты в canceling
    s = Srv(api)
    outs = [s.run(input="привет"), s.call("api_estimate", project="payload", input="привет", tier="cheap"),
            s.call("api_stats"), s.call("api_batch_submit", project="payload", inputs=["а", "б"], tier="cheap"),
            s.call("api_batch_status"), s.call("api_batch_results", id="fakebatch_2"),
            s.call("api_batch_cancel", id="fakebatch_2")]
    assert not any(o.startswith("отказ") for o in outs) and prov.keyed == keyed + 2  # run и estimate — точно
    assert warned(api) == [] and api.outputs and not any("⚠" in o for o in outs)
    c = doctor.check_api(ctx(api.env, tmp_path), registry.RegistryFile(api.env.cfg).get())
    assert money_line(c) == "деньги: ограничений нет" and not any("⚠" in x for x in c["detail"])


def test_defaults_code_key_no_budget_no_reserve(code, monkeypatch):
    """Пустой реестр: у задачи Code на ключе нет --max-budget-usd, резерва нет, строка оплаты без ⚠."""
    jid, d = _run(code, monkeypatch)
    assert d["state"] == "completed" and "--max-budget-usd" not in _argv(code, jid)
    assert not {"cap_usd", "reserve_usd", "zone"} & set(d["billing"])
    assert [r["reserve"] for r in _raw(code) if r.get("job") == jid] == [False]
    spend_now(ledger.ledger_path(code.state), 1000.0)
    line = apply_billing(code.jobs, code.jobs.snapshot(jid))["billing"]
    assert line == "оплата: ключ anthropic · ≈$0.50 за задачу" and not any("⚠" in w for w in d["warnings"])


# ---------- п. 2.1: ограничения ----------

def test_code_zone_after_credits(code, monkeypatch):
    """П. 2.1.1–2.1.2 (мутация М2): included_usd в реестре нет — зону включает команда credits; задача Code её
    видит: без бюджета claude получает --max-budget-usd = остаток (вниз до цента), он же резерв; бюджет выше остатка —
    отказ."""
    assert api_main(["credits", "anthropic", "--left", "7.257", "--until", "31.12.2099"], code.cfg, code.state) == 0
    jid, d = _run(code, monkeypatch)
    assert _opt(_argv(code, jid), "--max-budget-usd") == "7.25"
    assert (d["billing"]["zone"], d["billing"]["cap_usd"], d["budget_usd"]) == ("included", 7.25, None)
    assert [(r["reserve"], r["cost_usd"]) for r in _raw(code) if r.get("job") == jid] == [(True, 7.25), (False, 0.5)]
    assert "зона: включённая · цикл ≈$0.50 из $7.26" in apply_billing(code.jobs, code.jobs.snapshot(jid))["billing"]
    with pytest.raises(JobError, match="включённые кредиты исчерпаны"):  # остаток 6.757 < 10
        code.jobs.start(_plan(code, budget_usd=10))
    assert api_main(["credits", "anthropic", "--left", "0", "--until", "31.12.2099"], code.cfg, code.state) == 0
    with pytest.raises(JobError, match="включённые кредиты исчерпаны"):
        code.jobs.start(_plan(code))


def test_code_cap_included_allow_and_parallel(code, monkeypatch):
    """П. 2.1.2 и решение 5.1.1: зона из included_usd, бюджета нет — флаг = остаток включённой зоны, затем —
    остаток разрешения allow; резерв = весь остаток: вторая задача параллельно не стартует."""
    _registry(code, api=KEY_API.replace("included_usd = 100", "included_usd = 3"), **NO_LIMITS)
    jid, d = _run(code, monkeypatch)
    assert _opt(_argv(code, jid), "--max-budget-usd") == "3" and d["billing"]["reserve_usd"] == 3
    jid, d = _run(code, monkeypatch)
    assert _opt(_argv(code, jid), "--max-budget-usd") == "2.5"
    _registry(code, api=KEY_API.replace("included_usd = 100", "included_usd = 1"), **NO_LIMITS)  # исчерпана
    assert api_main(["allow", "anthropic", "--usd", "4", "--until", "31.12.2099"], code.cfg, code.state) == 0
    jid, d = _run(code, monkeypatch)
    assert _opt(_argv(code, jid), "--max-budget-usd") == "4" and d["billing"]["zone"] == "purchased"
    add_sub(code)  # вторая задача — в другом каталоге: одна живая задача на каталог (ТЗ WSH-3.8)
    _registry(code, api=KEY_API.replace("included_usd = 100", "included_usd = 1"), max_parallel="2", dirs=SUB_DIRS,
              **NO_LIMITS)
    monkeypatch.setenv("FAKE_SLEEP", "3")
    first = code.jobs.start(_plan(code))["id"]
    assert _opt(_argv(code, first), "--max-budget-usd") == "3.5"
    with pytest.raises(JobError, match="исчерпаны"):  # резерв первой — весь остаток разрешения
        code.jobs.start(_plan(code, code.taskfile(dir="sub")))
    assert wait_terminal(code.jobs, first)["state"] == "completed"
    _registry(code, api="[api]\ndaily_usd = 50\n", **NO_LIMITS)  # без зоны — флага нет, как раньше
    jid, _d = _run(code, monkeypatch)
    assert "--max-budget-usd" not in _argv(code, jid)


def _wait_text(path, text: str, timeout: float = 15) -> None:
    end = time.time() + timeout
    while time.time() < end:
        if path.exists() and text in path.read_text(encoding="utf-8"):
            return
        time.sleep(0.1)
    raise AssertionError(f"{text!r} не появилось в {path}")


@pytest.mark.parametrize("how", ["cancel", "timeout"])
@pytest.mark.parametrize("cost", ["0.42", None])
def test_cancel_or_timeout_without_result(code, monkeypatch, how, cost):
    """П. 2.1.3: итога нет (claude убит SIGKILL) — в журнал оценка по последнему total_cost_usd потока (фейк пишет
    0.01, затем 0.42) с пометкой «оценка, итога нет»; суммы в потоке не было — резерв остаётся."""
    over = {**NO_LIMITS, "max_hours": f"{3 / 3600}"} if how == "timeout" else NO_LIMITS
    _registry(code, api=KEY_API, **over)
    for k, v in {"FAKE_SLEEP": "60", "FAKE_SIGINT": "ignore-all", "WSHUB_CODE_CANCEL_SIGTERM_S": "0.5",
                 "WSHUB_CODE_CANCEL_SIGKILL_S": "0.5"}.items():
        monkeypatch.setenv(k, v)
    if cost:
        monkeypatch.setenv("FAKE_RUN_COST", cost)
    jid = code.jobs.start(_plan(code))["id"]
    _wait_running(code.jobs, jid)
    if how == "cancel":
        _wait_text(code.jobs.job_dir(jid) / "events.jsonl", f'"total_cost_usd": {cost}' if cost else '"Read"')
        code.jobs.cancel(jid)
    snap = wait_terminal(code.jobs, jid, timeout=60)
    assert snap["state"] == ("cancelled" if how == "cancel" else "timeout")
    assert code.jobs.read(jid)["result"]["total_cost_usd"] is None  # итога нет
    rows = [(r["reserve"], r["cost_usd"], r.get("note")) for r in _raw(code) if r.get("job") == jid]
    assert rows == ([(True, 100.0, None), (False, 0.42, NOTE)] if cost else [(True, 100.0, None)])


def test_cancel_with_result_has_no_note(code, monkeypatch):
    """Итог есть (claude вышел по SIGINT с итогом) — факт по итогу, без пометки оценки."""
    _registry(code, api=KEY_API, **NO_LIMITS)
    monkeypatch.setenv("FAKE_SLEEP", "30")
    monkeypatch.setenv("FAKE_RUN_COST", "0.42")
    jid = code.jobs.start(_plan(code))["id"]
    _wait_running(code.jobs, jid)
    _wait_text(code.jobs.job_dir(jid) / "events.jsonl", '"total_cost_usd": 0.42')
    code.jobs.cancel(jid)
    assert wait_terminal(code.jobs, jid)["state"] == "cancelled"
    assert [(r["reserve"], r.get("note")) for r in _raw(code) if r.get("job") == jid] == [(True, None), (False, None)]


def test_batch_reserve_replaced_by_results(api, monkeypatch):
    """П. 2.1.4: при денежной проверке submit пишет резерв на оценку пакета — следующий пакет его видит; results
    заменяют резерв фактом, повтор results его не возвращает."""
    prov = use(monkeypatch, fake.FakeProvider())
    set_api(api.env, "[api]\ndaily_usd = 0.006\n", ws="")  # пакет из одного входа ≈$0.00256 со скидкой
    assert api.cli(*SUBMIT, "--inputs", "in/one.txt")[0] == 0
    [res] = [r for r in api.rows() if r.get("batch") == "fakebatch_1"]
    est = batches.load(api.env.state, "fakebatch_1")["estimate_usd"]
    assert (res["reserve"], res["cost_usd"], res["zone"], res["provider"]) == (True, pytest.approx(est), "included",
                                                                               "fake")
    assert api.cli(*SUBMIT, "--inputs", "in/two.txt")[0] == 0  # 2 резерва ≈ $0.0051 ≤ $0.006
    rc, _, err = api.cli(*SUBMIT, "--inputs", "in/one.txt")  # третий не помещается: резервы в потолке дня
    assert rc == 1 and "дневной потолок daily_usd" in err and "пакет не отправлен" in err and len(prov.batches) == 2
    for bid in ("fakebatch_1", "fakebatch_2"):
        assert api.cli("batch", "results", bid)[0] == 0
        rows = [r for r in api.rows() if r.get("batch") == bid]
        assert len(rows) == 1 and rows[0].get("reserve") is not True and rows[0]["cost_usd"] < est / 10
    assert api.cli("batch", "results", "fakebatch_1")[0] == 0  # повтор — резерв не возвращается, дублей нет
    assert [r.get("reserve") for r in api.rows() if r.get("batch") == "fakebatch_1"] == [None]
    assert api.cli(*SUBMIT, "--inputs", "in/one.txt")[0] == 0  # факты меньше резервов — третий пакет проходит
    raw = ledger.ledger_path(api.env.state).read_text(encoding="utf-8")
    assert raw.count('"reserve": true') == 3  # строки резерва остаются в файле — действует факт


# ---------- п. 2.2: [api.warn] ----------

def test_warn_thresholds_show_and_do_not_block(api, monkeypatch, tmp_path):
    """Каждый порог: ниже — предупреждения нет, выше — есть; запрос и пакет не блокируются."""
    prov = use(monkeypatch, fake.FakeProvider())
    set_api(api.env, "[api.warn]\nday_usd = 2\nleft_usd = 3\ncall_usd = 0.006\n\n" + FAKE_ZONE, ws="")
    s = Srv(api)
    assert api.run()[0] == 0 and api.run("--estimate")[0] == 0 and api.cli("stats")[0] == 0  # оценка ≈$0.0051
    assert "⚠" not in s.run(input="привет") and warned(api) == []
    rc, out, _ = api.run("--max-tokens", "2000")  # ≈$0.0100 > 0.006
    assert rc == 0 and "⚠ оценка запроса ≈$0.01 выше порога call_usd = $0.01 — только предупреждение" in out
    assert len(prov.calls) == 3
    assert "⚠ оценка запроса" in api.run("--estimate", "--max-tokens", "2000")[1]
    assert "⚠ оценка запроса" in s.call("api_estimate", project="payload", input="привет", tier="cheap",
                                        max_tokens=2000)
    rc, out, _ = api.cli(*SUBMIT, "--inputs", "in/one.txt", "in/two.txt", "--max-tokens", "2000")
    assert rc == 0 and "⚠ оценка пакета ≈$0.01 выше порога call_usd" in out and len(prov.batches) == 1
    rc, out, _ = api.cli(*SUBMIT, "--inputs", "in/one.txt")  # ≈$0.0026 — ниже
    assert rc == 0 and "⚠" not in out
    api.spend(1.95, provider="other")  # день ≈$1.98 ≤ $2
    assert not any("траты за сутки" in x for x in (api.run()[1], api.cli("stats")[1]))
    api.spend(0.1, provider="other")
    for text in (api.run()[1], api.cli("stats")[1], s.call("api_stats"), api.cli("batch", "status")[1]):
        assert "⚠ траты за сутки ≈$2.0" in text and "выше порога day_usd = $2.00" in text
    assert not any("остаток включённой зоны" in x for x in warned(api))
    api.spend(1.9)  # остаток fake ≈ $3.06 ≥ $3
    assert "остаток включённой зоны" not in api.cli("stats")[1]
    api.spend(0.2)  # остаток ≈ $2.89 (results заменяют резерв фактом — чуть больше)
    for text in (api.run()[1], api.cli("stats")[1], s.run(input="привет"),
                 api.cli("batch", "results", "fakebatch_1")[1]):
        assert "⚠ остаток включённой зоны fake ≈$2." in text and "ниже порога left_usd = $3.00" in text
    assert len(prov.calls) == 7 and len(prov.batches) == 2  # ни один запрос и пакет не остановлен
    c = doctor.check_api(ctx(api.env, tmp_path), registry.RegistryFile(api.env.cfg).get())
    assert any(x.startswith("⚠ остаток включённой зоны fake") for x in c["detail"]) and c["status"] != "fail"
    assert "[api.warn] day_usd $2.00, left_usd $3.00, call_usd $0.01" in money_line(c)


def test_left_warn_only_when_zone_on(api, monkeypatch):
    """left_usd — только при включённой зоне: без included_usd и credits предупреждения нет; credits включает."""
    use(monkeypatch, fake.FakeProvider())
    set_api(api.env, "[api.warn]\nleft_usd = 3\n", ws="")
    assert "⚠" not in api.run()[1]
    assert api.cli("credits", "fake", "--left", "1", "--until", "31.10")[0] == 0
    assert "⚠ остаток включённой зоны fake ≈$1.00 ниже порога left_usd = $3.00" in api.run()[1]


def test_warn_in_code_billing_line(code, monkeypatch):
    """Строка оплаты задачи Code на ключе (code_status, code_result): остаток зоны и траты за сутки."""
    _registry(code, api=KEY_API + "\n[api.warn]\nday_usd = 1\nleft_usd = 99.9\n")
    jid, _d = _run(code, monkeypatch)  # бюджет 10 из реестра, факт 0.5: остаток 99.5, сутки 0.5
    line = apply_billing(code.jobs, code.jobs.snapshot(jid))["billing"]
    assert line.endswith(" · ⚠ остаток включённой зоны anthropic ≈$99.50 ниже порога left_usd = $99.90")
    assert "траты за сутки" not in line
    spend_now(ledger.ledger_path(code.state), 0.6)
    line = apply_billing(code.jobs, code.jobs.result_view(jid), model=True)["billing"]
    assert "⚠ траты за сутки ≈$1.10 выше порога day_usd = $1.00" in line


@pytest.mark.parametrize("text,warn", [
    ("[api]\nwarn = 5\n", "[api.warn]: ожидается таблица — пороги предупреждений пропущены"),
    ("[api.warn]\nday_usd = 0\n", "api.warn.day_usd: число больше 0 — порог пропущен"),
    ("[api.warn]\nleft_usd = -1\n", "api.warn.left_usd: число больше 0 — порог пропущен"),
    ("[api.warn]\ncall_usd = nan\n", "api.warn.call_usd: число больше 0 — порог пропущен"),
    ('[api.warn]\ncall_usd = "1"\n', "api.warn.call_usd: число больше 0 — порог пропущен"),
])
def test_warn_registry_errors_only_warn(text, warn):
    """Ревью, п. 9: неверный порог — предупреждение реестра, порог пропущен; API не ломается (пороги ничего не
    останавливают, и их ошибка тоже)."""
    reg = registry.parse(text)
    assert not reg.api.error and warn in reg.warnings and reg.api.warn == registry.ApiWarn()
    reg = registry.parse("[api.warn]\nday_usd = 0\ncall_usd = 1\n")
    assert not reg.api.error and reg.api.warn == registry.ApiWarn(call_usd=1.0)


def test_warn_registry_values():
    reg = registry.parse("[api.warn]\nday_usd = 5\ncall_usd = 0.5\nfuture = 1\n")
    assert reg.api.warn == registry.ApiWarn(day_usd=5.0, left_usd=None, call_usd=0.5) and not reg.api.error
    assert "неизвестный ключ api.warn.future — пропущен" in reg.warnings
    assert (reg.api.max_usd_per_call, reg.api.daily_usd) == (None, None)  # пороги — не потолки


# ---------- п. 2.3: надёжность учёта ----------

@pytest.mark.parametrize("fail", [ledger.LedgerError("журнал трат X: строка записана не целиком"),
                                  PermissionError(13, "Permission denied")])
def test_ledger_error_after_paid_answer(api, monkeypatch, fail):
    """П. 2.3.1: строка журнала после оплаченного ответа не записалась — ответ отдаётся, ошибка журнала — ⚠."""
    prov = use(monkeypatch, fake.FakeProvider())
    set_api(api.env)  # с денежными настройками: журнал читается, запись падает

    def boom(path, rec):
        raise fail
    monkeypatch.setattr(ledger, "append", boom)
    why = "Permission denied" if isinstance(fail, OSError) else str(fail)
    rc, out, err = api.run()
    assert rc == 0, err
    assert out.splitlines()[0] == "привет, модель"  # оплаченный ответ отдан
    assert f"⚠ журнал трат: строка не записана ({why}) — сумма не учтена" in out and len(prov.calls) == 1
    text = Srv(api).run(input="ответь")
    assert text.splitlines()[0] == "ответь" and f"⚠ журнал трат: строка не записана ({why})" in text


def test_ledger_error_on_batch_results(api, monkeypatch):
    """Строки results не записались — ответы разложены, ⚠; следующая выборка дописывает строки."""
    use(monkeypatch, fake.FakeProvider())
    assert api.cli(*SUBMIT, "--inputs", "in/one.txt")[0] == 0
    orig = ledger.append

    def boom(path, rec):
        raise OSError(28, "No space left on device")
    monkeypatch.setattr(ledger, "append", boom)
    rc, out, _ = api.cli("batch", "results", "fakebatch_1")
    assert rc == 0 and "журнал трат: записано строк 0, не записано 1" in out
    assert "⚠ журнал трат: строки результатов не записаны (No space left on device)" in out
    assert (api.env.state / "api" / "out" / "fakebatch_1" / "one.md").read_text(encoding="utf-8") == "текст one"
    monkeypatch.setattr(ledger, "append", orig)
    rc, out, _ = api.cli("batch", "results", "fakebatch_1")
    assert rc == 0 and "журнал трат: записано строк 1" in out
    assert len([r for r in api.rows() if r.get("batch") == "fakebatch_1"]) == 1


@pytest.mark.parametrize("broken", ["garbage", "dir"])
def test_unreadable_ledger_without_settings_warns(api, monkeypatch, broken):
    """Решение 5.1.3: журнал не читается, денежных настроек в реестре нет — запрос идёт без проверок, ⚠ журнал трат
    не читается; настройка задана — отказ, как раньше."""
    prov = use(monkeypatch, fake.FakeProvider())
    api.ledger_path.parent.mkdir(parents=True, exist_ok=True)
    if broken == "dir":
        api.ledger_path.mkdir()
    else:
        api.ledger_path.write_text("garbage\nnot json\n")
    rc, out, err = api.run("--estimate")
    assert rc == 0 and "⚠ журнал трат не читается: журнал трат" in out and "до тех пор" not in out
    rc, out, err = api.run()
    assert rc == 0, err
    assert "⚠ журнал трат не читается: журнал трат" in out and len(prov.calls) == 1
    if broken == "dir":  # и записать строку нельзя — ответ всё равно отдан
        assert "⚠ журнал трат: строка не записана" in out
    if broken == "garbage":  # запись строки после ответа починила журнал — снова ломаем
        api.ledger_path.write_text("garbage\n")
    assert "⚠ журнал трат не читается" in Srv(api).run(input="привет")
    set_api(api.env, "[api]\ndaily_usd = 10\n", ws="")
    if broken == "garbage":
        api.ledger_path.write_text("garbage\n")
    rc, _, err = api.run()
    assert rc == 1 and "журнал трат" in err and len(prov.calls) == 2
    rc, _, err = api.run("--estimate")  # оценка с настройкой — тоже отказ: проверить её нечем (ревью)
    assert rc == 1 and "журнал трат" in err


def test_unreadable_ledger_code_key(code, monkeypatch, tmp_path):
    """Решение 5.1.3 для задачи Code на ключе: без денежных настроек — старт без проверок и ⚠; с daily_usd — отказ."""
    path = ledger.ledger_path(code.state)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("garbage\n")
    jid, d = _run(code, monkeypatch)
    assert d["state"] == "completed" and any(w.startswith("⚠ журнал трат не читается") for w in d["warnings"])
    assert "reserve_usd" not in d["billing"]
    c = doctor.check_api(ctx(code, tmp_path), code.hub.registry.get())
    assert money_line(c) == "деньги: ограничений нет"
    path.write_text("garbage\n")
    c = doctor.check_api(ctx(code, tmp_path), code.hub.registry.get())
    assert any(x.startswith("⚠ журнал трат не читается") for x in c["detail"]) and c["status"] != "fail"
    _registry(code, api="[api]\ndaily_usd = 50\n", **NO_LIMITS)
    with pytest.raises(JobError, match="не разбирается ни одна строка"):
        code.jobs.start(_plan(code))
    c = doctor.check_api(ctx(code, tmp_path), code.hub.registry.get())
    assert c["status"] == "fail" and any(x.startswith("журнал трат: ") for x in c["detail"])


class NoAnswerBatch(fake.FakeProvider):
    def __init__(self, exc):
        super().__init__()
        self.exc = exc

    def batch_submit(self, requests, key):
        raise self.exc


def _no_answer() -> ApiError:
    e = ApiError("нет ответа за 5 с")
    e.no_answer = True
    return e


@pytest.mark.parametrize("exc,hint", [(_no_answer(), True), (TimeoutError("timed out"), True),
                                      (ApiError("HTTP 500 api_error: x"), False)])
def test_batch_submit_timeout_hint(api, monkeypatch, exc, hint):
    """П. 2.3.2: ответа на создание пакета нет — подсказка проверить batch status, прежде чем повторять."""
    use(monkeypatch, NoAnswerBatch(exc))
    rc, _, err = api.cli(*SUBMIT, "--inputs", "in/one.txt")
    assert rc == 1 and (BATCH_MAYBE in err) is hint
    out = Srv(api).call("api_batch_submit", project="payload", inputs=["а"], tier="cheap")
    assert out.startswith("отказ: ") and (BATCH_MAYBE in out) is hint


@pytest.fixture
def asrv(monkeypatch):
    for k in ("http_proxy", "https_proxy", "all_proxy", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
        monkeypatch.delenv(k, raising=False)
    s = Server()
    yield s
    s.close()


def test_batch_submit_timeout_anthropic(api, asrv):
    """Провайдер anthropic: таймаут создания пакета — тот же текст; HTTP-ошибка — без подсказки."""
    set_api(api.env, f'[api.providers.anthropic]\nbase_url = "{asrv.url}"\ntimeout_s = 1\n', ws="")
    keys.write(keys.key_path(api.env.cfg.parent, "anthropic"), "sk-ant-test-0000-timeout")
    args = ("batch", "submit", "--provider", "anthropic", "--project", "payload", "--tier", "cheap", "--inputs",
            "in/one.txt")
    asrv.reply(body=created(), delay=3)
    rc, _, err = api.cli(*args)
    assert rc == 1 and "нет ответа за 1 с" in err and BATCH_MAYBE in err
    asrv.reply(500, {"type": "error", "error": {"type": "api_error", "message": "x"}})
    rc, _, err = api.cli(*args)
    assert rc == 1 and "HTTP 500" in err and BATCH_MAYBE not in err


# ---------- п. 2.4.3: doctor ----------

def test_doctor_money_line_and_api_error(api, monkeypatch, tmp_path):
    """Включённые денежные настройки — одной строкой «деньги: …»; ошибка [api] — проблема."""
    use(monkeypatch, fake.FakeProvider())
    set_api(api.env, "[api]\nmax_usd_per_call = 1\ndaily_usd = 10\n\n" + FAKE_ZONE, ws="")
    keys.write(keys.key_path(api.env.cfg.parent, "anthropic"), "sk-ant-test-0000-doctor")
    assert api.cli("credits", "anthropic", "--left", "3", "--until", "31.10")[0] == 0
    assert api.cli("allow", "fake", "--usd", "2", "--until", "25.10.2099")[0] == 0  # doctor — по настоящей дате
    c = doctor.check_api(ctx(api.env, tmp_path), registry.RegistryFile(api.env.cfg).get())
    line = money_line(c)
    assert line.startswith("деньги: max_usd_per_call $1.00 · daily_usd $10.00 · зоны: ")
    assert "fake (included_usd)" in line and "anthropic (credits)" in line and "allow: fake $2.00 до 25.10" in line
    assert "anthropic" not in line.split("allow:")[1]  # разрешения у anthropic нет
    assert "⚠" not in " ".join(c["detail"])
    set_api(api.env, "[api]\ndaily_usd = 0\n", ws="")
    c = doctor.check_api(ctx(api.env, tmp_path), registry.RegistryFile(api.env.cfg).get())
    assert c["status"] == "fail" and any(x.startswith("[api]: ") and "daily_usd" in x for x in c["detail"])


def test_doctor_code_budget_in_money_line(code, tmp_path):
    _registry(code, api="")  # max_budget_usd = 10 у проекта proj
    c = doctor.check_api(ctx(code, tmp_path), code.hub.registry.get())
    assert money_line(c) == "деньги: max_budget_usd (Code): proj $10"


# ---------- по ревью субагента ----------

def _row(cost: float) -> dict:
    return {"ts": ledger.stamp(NOW), "kind": "run", "provider": "fake", "zone": "included", "cost_usd": cost}


def test_warn_boundaries_strict(tmp_path):
    """Порог — строго «выше» / «ниже»: ровно на пороге предупреждения нет (мутации > → >=, < → <=)."""
    reg = registry.parse("[api.warn]\nday_usd = 2\nleft_usd = 3\ncall_usd = 0.5\n" + FAKE_ZONE)
    eng = api_core.Api(reg, tmp_path, tmp_path, lambda: NOW)
    provs = {"fake": reg.api.providers["fake"]}
    assert eng.warn_lines(ledger.Ledger((_row(2.0),), 0), provs, 0.5) == []  # сутки ровно 2, остаток ровно 3
    out = eng.warn_lines(ledger.Ledger((_row(2.0), _row(0.01)), 0), provs, 0.51)
    assert [x.split(" ")[1] for x in out] == ["оценка", "траты", "остаток"]


def test_warn_in_all_batch_outputs(api, monkeypatch, tmp_path):
    """⚠ — во всех командах batch и во всех инструментах api_batch_* (ревью: cancel, status ID, сервер); doctor
    считает сутки по настоящей дате."""
    prov = use(monkeypatch, fake.FakeProvider())
    set_api(api.env, "[api.warn]\nday_usd = 1\n", ws="")
    api.spend(1.5, provider="other")
    s = Srv(api)
    outs = [api.cli(*SUBMIT, "--inputs", "in/one.txt")[1], api.cli("batch", "status", "fakebatch_1")[1],
            api.cli("batch", "status")[1], api.cli("batch", "results", "fakebatch_1")[1],
            api.cli("batch", "cancel", "fakebatch_1")[1]]
    prov.batch_state = "ended"
    outs += [s.call("api_batch_submit", project="payload", inputs=["а"], tier="cheap"),
             s.call("api_batch_status", id="fakebatch_2"), s.call("api_batch_status"),
             s.call("api_batch_results", id="fakebatch_2"), s.call("api_batch_cancel", id="fakebatch_2")]
    for o in outs:
        assert "⚠ траты за сутки ≈$1.50 выше порога day_usd = $1.00" in o, o
    spend_now(api.ledger_path, 1.5, provider="other")
    c = doctor.check_api(ctx(api.env, tmp_path), registry.RegistryFile(api.env.cfg).get())
    assert any(x.startswith("⚠ траты за сутки ≈$1.50 выше порога day_usd") for x in c["detail"])


def test_day_warn_shows_reserve_part(code, monkeypatch):
    """Ревью, п. 7: траты за сутки для day_usd — с резервами, как для daily_usd; их доля — в скобках."""
    _registry(code, api=KEY_API + "\n[api.warn]\nday_usd = 5\n", **NO_LIMITS)
    monkeypatch.setenv("FAKE_SLEEP", "3")
    jid = code.jobs.start(_plan(code))["id"]  # резерв = остаток зоны 100
    line = apply_billing(code.jobs, code.jobs.snapshot(jid))["billing"]
    assert "⚠ траты за сутки ≈$100.00 (из них резерв ≈$100.00) выше порога day_usd = $5.00" in line
    wait_terminal(code.jobs, jid)
    assert "⚠" not in apply_billing(code.jobs, code.jobs.snapshot(jid))["billing"]  # факт 0.5 < 5


def test_batch_reserve_write_failure_keeps_batch(api, monkeypatch):
    """Ревью, п. 5: резерв пакета не записался — пакет уже создан, учёт пакета записан, ⚠ в выводе."""
    use(monkeypatch, fake.FakeProvider())
    set_api(api.env, "[api]\ndaily_usd = 1\n", ws="")

    def boom(path, rec):
        raise OSError(28, "No space left on device")
    monkeypatch.setattr(ledger, "append", boom)
    rc, out, err = api.cli(*SUBMIT, "--inputs", "in/one.txt")
    assert rc == 0, err
    assert "пакет fakebatch_1 отправлен" in out
    assert "⚠ журнал трат: резерв пакета не записан (No space left on device) — сумма не учтена" in out
    rec = batches.load(api.env.state, "fakebatch_1")
    assert rec["provider"] == "fake" and "warnings" not in rec  # предупреждение — в ответе, не в учёте


def test_batch_reserve_with_credits_only_zone(api, monkeypatch):
    """Ревью: зона только по команде credits — тоже денежная проверка, резерв есть; у пакета из двух входов results
    оставляют обе строки факта (не «последняя строка пакета»)."""
    use(monkeypatch, fake.FakeProvider())
    assert api.cli("credits", "fake", "--left", "1", "--until", "31.10")[0] == 0
    assert api.cli(*SUBMIT, "--inputs", "in/one.txt", "in/two.txt")[0] == 0
    [res] = [r for r in api.rows() if r.get("reserve")]
    assert (res["batch"], res["zone"]) == ("fakebatch_1", "included")
    assert api.cli("batch", "results", "fakebatch_1")[0] == 0
    rows = [r for r in api.rows() if r.get("batch") == "fakebatch_1"]
    assert sorted(r["custom_id"] for r in rows) == ["one", "two"] and not any(r.get("reserve") for r in rows)


def test_run_refuses_when_ledger_breaks_after_credits_plan(api, monkeypatch):
    """Ревью: зоны включила команда credits, журнал перестал читаться между plan и run — проверить нечем, отказ."""
    prov = use(monkeypatch, fake.FakeProvider())
    assert api.cli("credits", "fake", "--left", "1", "--until", "31.10")[0] == 0
    eng = api.engine()
    plan = eng.plan(req(), "fake")
    assert plan.zones is not None
    api.ledger_path.write_text("garbage\n")
    with pytest.raises(ApiError, match="не разбирается ни одна строка"):
        eng.run(plan)
    assert prov.calls == []


def test_results_with_unreadable_ledger_without_settings(api, monkeypatch):
    """Ревью, п. 6 и решение 5.1.3: журнал не читается, настроек нет — results раскладывает ответы, строки журнала
    откладывает до следующей выборки."""
    use(monkeypatch, fake.FakeProvider())
    assert api.cli(*SUBMIT, "--inputs", "in/one.txt")[0] == 0
    api.ledger_path.parent.mkdir(parents=True, exist_ok=True)
    api.ledger_path.write_text("garbage\n")
    rc, out, err = api.cli("batch", "results", "fakebatch_1")
    assert rc == 0, err
    assert "журнал трат: записано строк 0, не записано 1" in out
    assert out.count("⚠ журнал трат не читается: ") == 1 and "— строки результатов не записаны" in out
    assert (api.env.state / "api" / "out" / "fakebatch_1" / "one.md").exists()
    api.ledger_path.unlink()
    rc, out, _ = api.cli("batch", "results", "fakebatch_1")
    assert rc == 0 and "журнал трат: записано строк 1" in out

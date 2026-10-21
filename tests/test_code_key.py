"""WSH-4.6, задачи wshub-code на API-ключе: режим billing, передача ключа claude через apiKeyHelper, страж оплаты по
режиму, остановка key_rejected, деньги только заданные оператором и журнал трат 4.2 (резерв → факт), показ сумм,
флаги терминала, провайдер-заготовка anthropic (с упрощением 5.2: без key = true и без умолчаний сумм). Настоящий
claude и сеть не вызываются — tests/fake_claude.py, ключ — фейковый с уникальной строкой."""
from __future__ import annotations

import json
import os
import re
import shlex
import threading
import time
from datetime import datetime
from pathlib import Path

import pytest
from test_code import _wait_running, make_code, wait_terminal
from test_code_limits import NO_LIMITS, _argv
from test_code_plan import _call, _opt, _wait_state

from wshub import claude_cli, code_cli, code_server, doctor
from wshub.core import Hub
from wshub.jobs import LIMIT_IGNORED, JobError, Jobs, effective_limits
from wshub.llm import keys, ledger
from wshub.llm.cli import api_main
from wshub.llm.core import Api
from wshub.llm.providers import PROVIDERS
from wshub.llm.types import Request
from wshub.registry import ProjectCode
from wshub.runner import apply_billing

MARKER = "sk-ant-api03-WSH46-MARKER-c0ffee7d1e9b"
REASON = "оператор в чате 20.10: разведка на промокредитах"
API = '[api]\ndaily_usd = 50\n\n[api.providers.anthropic]\nincluded_usd = 100\ncredits_until = "2099-12-31"\n'
NO_REASON = "задача на ключе из чата — только с reason"


def _registry(code, api: str = API, **over) -> None:
    """Реестр проекта и секции [api] / [api.providers.anthropic] (api="" — секций нет)."""
    code.write(**over)
    code.cfg.write_text(code.cfg.read_text(encoding="utf-8") + "\n" + api, encoding="utf-8")
    code.hub = Hub(code.cfg, code.state)
    code.jobs = Jobs(code.hub)


def _key_path(code) -> Path:
    return keys.key_path(code.cfg.parent, "anthropic")


@pytest.fixture
def code(tmp_path, monkeypatch):
    c = make_code(tmp_path, monkeypatch)
    _registry(c)
    keys.write(_key_path(c), MARKER)
    # хаб — исполняемый файл wshub (ТЗ WSH-4.3, п. 2.2.4): иначе задача на ключе не стартует
    monkeypatch.setattr(claude_cli, "wshub_exe", lambda: str(tmp_path / "bin" / "wshub"))
    return c


def _plan(code, task=None, **kw):
    kw.setdefault("channel", "cli")
    return code.jobs.plan("proj", task or code.taskfile(), billing="key", **kw)


def _run(code, monkeypatch, timeout=30, **env) -> tuple[str, dict]:
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    jid = code.jobs.start(_plan(code))["id"]
    wait_terminal(code.jobs, jid, timeout)
    return jid, code.jobs.read(jid)


def _raw(code) -> list[dict]:
    p = ledger.ledger_path(code.state)
    return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines()] if p.exists() else []


def _effective(code, jid) -> list[dict]:
    return [r for r in ledger.read(ledger.ledger_path(code.state)).rows if r.get("job") == jid]


def _job_dirs(code) -> int:
    d = code.state / "jobs"
    return len([p for p in d.iterdir() if p.is_dir()]) if d.is_dir() else 0


def _settings(code, jid) -> dict:
    return json.loads((code.jobs.job_dir(jid) / "settings.json").read_text(encoding="utf-8"))


HELPER = shlex.join([*claude_cli.wshub_cmd(), "code", "key-helper"])


# ---------- п. 3.1: выбор режима ----------

def test_key_without_provider_section_and_limits(code, monkeypatch):
    """Упрощение 5.2: ни разрешения проекта, ни [api], ни max_budget_usd — задача на ключе стартует, флага
    --max-budget-usd нет, резерва нет, а факт итога в журнале есть; строка оплаты — без зоны."""
    _registry(code, api="", **NO_LIMITS)
    text = code.cfg.read_text(encoding="utf-8")
    assert "[api" not in text and not re.search(r"^key\s*=", text, re.M)
    jid, d = _run(code, monkeypatch)
    assert (d["state"], d["billing"]["mode"], d["billing"]["source"]) == ("completed", "key", "apiKeyHelper")
    assert "--max-budget-usd" not in _argv(code, jid) and d["budget_usd"] is None
    assert _settings(code, jid)["apiKeyHelper"] == HELPER
    rows = [r for r in _raw(code) if r.get("job") == jid]
    assert [(r["reserve"], r["cost_usd"], r["zone"], r["template"]) for r in rows] == \
        [(False, 0.5, "included", "code")]  # только факт
    assert "zone" not in d["billing"] and "reserve_usd" not in d["billing"]
    st = apply_billing(code.jobs, code.jobs.snapshot(jid))
    assert st["billing"] == "оплата: ключ anthropic · ≈$0.50 за задачу"
    out = json.loads(_call(code, "code_start", {"project": "proj", "task": code.taskfile(), "billing": "key",
                                                "reason": REASON}))  # из чата — тоже без разрешения проекта
    assert wait_terminal(code.jobs, out["job"])["state"] == "completed"


def test_key_from_chat_needs_reason(code):
    for reason in (None, "коротко", " ​" * 20):
        with pytest.raises(JobError, match=NO_REASON):
            _plan(code, channel="mcp", reason=reason)
    assert _call(code, "code_start", {"project": "proj", "task": code.taskfile(), "billing": "key"}).startswith(
        f"отказ: {NO_REASON}")
    out = json.loads(_call(code, "code_start", {"project": "proj", "task": code.taskfile(), "billing": "key",
                                                "reason": REASON}))
    assert out["params"]["billing"] == "key"
    wait_terminal(code.jobs, out["job"])
    d = code.jobs.read(out["job"])
    assert d["billing"]["mode"] == "key" and d["state"] == "completed"
    assert _settings(code, out["job"])["apiKeyHelper"] == HELPER


def test_key_from_terminal_is_operator_choice(code, capsys):
    """Из терминала режим key — выбор оператора: reason не нужен."""
    assert code_cli.code_main("code", ["start", "proj", code.taskfile(), "--billing", "key"], code.cfg,
                              code.state) == 0
    jid = capsys.readouterr().out.strip().splitlines()[0]
    wait_terminal(code.jobs, jid)
    d = code.jobs.read(jid)
    assert (d["billing"]["mode"], d["billing"]["source"], d["state"]) == ("key", "apiKeyHelper", "completed")
    assert _settings(code, jid)["apiKeyHelper"] == HELPER


def test_subscription_has_no_key_helper(code):
    d = code.jobs.start(code.jobs.plan("proj", code.taskfile()))
    wait_terminal(code.jobs, d["id"])
    assert "apiKeyHelper" not in _settings(code, d["id"])
    job = code.jobs.read(d["id"])
    assert (job["billing"]["mode"], job["state"]) == ("subscription", "completed") and _raw(code) == []


def test_billing_value_checked(code):
    with pytest.raises(JobError, match="billing: subscription или key"):
        code.jobs.plan("proj", code.taskfile(), billing="api")


@pytest.mark.parametrize("case", ["missing", "mode_0644", "dir_0755"])
def test_key_file_problems_refused_before_start(code, case):
    p = _key_path(code)
    if case == "missing":
        p.unlink()
        why = "ключа нет"
    elif case == "mode_0644":
        os.chmod(p, 0o644)
        why = "0644; исправь: chmod 600"
    else:
        os.chmod(p.parent, 0o755)
        why = "0755; исправь: chmod 700"
    with pytest.raises(JobError) as e:
        code.jobs.start(_plan(code))
    assert why in str(e.value) and MARKER not in str(e.value)
    assert _job_dirs(code) == 0 and _raw(code) == []


def test_registry_marks_written_money_keys():
    """Проверки денег на ключе — только по записанным оператором ключам (упрощение 5.2.2)."""
    from wshub.registry import parse
    base = '[workspace.p]\npath = "/x"\n'
    assert parse(base).api.daily_set is False
    assert parse(base + "[api]\nenabled = true\n").api.daily_set is False
    assert parse(base + "[api]\ndaily_usd = 10\n").api.daily_set is True
    reg = parse(base + '[api.providers.anthropic]\ncredits_until = "2099-12-31"\n')
    assert reg.api.providers["anthropic"].included_set is False
    reg = parse(base + '[api.providers.anthropic]\nincluded_usd = 0\n')
    assert reg.api.providers["anthropic"].included_set is True


# ---------- п. 3.2: цепочка ----------

def test_chain_inherits_key_and_refuses_switch(code, monkeypatch):
    """План → replan → approve → resume: все звенья на ключе (job.json и apiKeyHelper); смена режима в resume —
    отказ, в обе стороны."""
    root = code.jobs.start(_plan(code, plan="review", chosen_by="operator"))["id"]
    _wait_state(code.jobs, root, ("plan_ready",))
    rp = code.jobs.replan(root, "добавь шаг проверки")
    _wait_state(code.jobs, rp["id"], ("plan_ready",))
    ex = code.jobs.approve(root)
    wait_terminal(code.jobs, ex["id"])
    with pytest.raises(JobError, match="сменить режим оплаты внутри цепочки нельзя"):
        code.jobs.resume(ex["id"], "cont", note="продолжи", billing="subscription")
    rs = code.jobs.resume(ex["id"], "cont", note="продолжи")
    wait_terminal(code.jobs, rs["id"])
    links = [code.jobs.read(x) for x in (root, rp["id"], ex["id"], rs["id"])]
    assert [x["phase"] for x in links] == ["plan", "plan", "execute", None]
    for x in links:
        assert x["billing"]["mode"] == "key" and x["billing"]["source"] == "apiKeyHelper", x["id"]
        assert _settings(code, x["id"])["apiKeyHelper"] == HELPER
        assert [r["cost_usd"] for r in _effective(code, x["id"])] == [0.5]  # факт звена, а не резерв
    assert code.jobs.read(rs["id"])["state"] == "completed"
    # подписка: перейти на ключ в resume тоже нельзя
    sub = code.jobs.start(code.jobs.plan("proj", code.taskfile()))["id"]
    wait_terminal(code.jobs, sub)
    with pytest.raises(JobError, match="сменить режим оплаты внутри цепочки нельзя"):
        code.jobs.resume(sub, "cont", note="продолжи", billing="key")
    out = _call(code, "code_resume", {"job": sub, "prompt": "cont", "note": "x", "billing": "key"})
    assert out.startswith("отказ: сменить режим оплаты")


# ---------- п. 3.3: страж ----------

def test_guard_key_expects_api_key_helper(code, monkeypatch):
    jid, d = _run(code, monkeypatch, FAKE_SOURCE="none", FAKE_SLEEP="20")
    assert (d["state"], d["reason"], d["result"]["billing_source"]) == ("failed", "billing_mismatch", "none")
    st = apply_billing(code.jobs, code.jobs.snapshot(jid))
    assert st["billing"] == "оплата: НЕ ключ хаба (apiKeySource: none) · задача остановлена"
    assert "apiKeyHelper" in st["reason_hint"]


def test_guard_key_completed_with_api_key_helper(code, monkeypatch):
    _jid, d = _run(code, monkeypatch)
    assert (d["state"], d["reason"], d["billing"]["source"]) == ("completed", None, "apiKeyHelper")


def test_guard_subscription_with_api_key_helper_still_mismatch(code, monkeypatch):
    monkeypatch.setenv("FAKE_SOURCE", "apiKeyHelper")
    d = code.jobs.start(code.jobs.plan("proj", code.taskfile()))
    wait_terminal(code.jobs, d["id"])
    d = code.jobs.read(d["id"])
    assert (d["state"], d["reason"]) == ("failed", "billing_mismatch")


def test_settings_check_in_key_mode(code):
    p = code.root / ".claude" / "settings.local.json"
    p.parent.mkdir()
    p.write_text('{"apiKeyHelper": "/bin/echo other"}', encoding="utf-8")
    with pytest.raises(JobError, match="задача на ключе: .*задаёт apiKeyHelper — ключ задаёт только хаб"):
        code.jobs.start(_plan(code))
    assert _job_dirs(code) == 0 and _raw(code) == []


# ---------- п. 3.4: key_rejected ----------

@pytest.mark.parametrize("status", ["401", "403"])
def test_api_retry_auth_error_stops_in_seconds(code, monkeypatch, status):
    t0 = time.time()
    jid, d = _run(code, monkeypatch, timeout=40, FAKE_API_RETRY=status, FAKE_SLEEP="60")
    assert time.time() - t0 < 20  # фейк «повторял» бы минуту
    assert (d["state"], d["reason"]) == ("failed", "key_rejected")
    assert (d["result"]["error_status"], d["result"]["error"]) == (int(status), "authentication_failed")
    assert [r["cost_usd"] for r in _effective(code, jid)] == [0.0]  # резерв снят фактом $0 (решение 5.1.3)
    st = apply_billing(code.jobs, code.jobs.snapshot(jid))
    assert f"ключ отклонён ({status} authentication_failed) · задача остановлена" in st["billing"]
    assert "wshub api key check anthropic" in st["reason_hint"]


def test_api_retry_429_keeps_running(code, monkeypatch):
    _jid, d = _run(code, monkeypatch, FAKE_API_RETRY="429", FAKE_SLEEP="2")
    assert (d["state"], d["reason"]) == ("completed", None)


def test_result_auth_error_without_retry_is_key_rejected(code, monkeypatch):
    jid, d = _run(code, monkeypatch, FAKE_API_ERROR="401")
    assert (d["state"], d["reason"], d["result"]["error_status"]) == ("failed", "key_rejected", 401)
    assert d["result"]["error"] == "Invalid API key · Fix external API key"
    assert [r["cost_usd"] for r in _effective(code, jid)] == [0.0]


def test_subscription_ignores_api_retry_401(code, monkeypatch):
    """Остановка по ключу — только у задач на ключе: подписка не меняется."""
    monkeypatch.setenv("FAKE_API_RETRY", "401")
    d = code.jobs.start(code.jobs.plan("proj", code.taskfile()))
    wait_terminal(code.jobs, d["id"])
    assert code.jobs.read(d["id"])["state"] == "completed"


# ---------- п. 3.5: ключ не утекает ----------

def test_key_never_leaks(code, monkeypatch, tmp_path):
    out_file, rec = tmp_path / "helper.json", tmp_path / "rec.json"
    jid, d = _run(code, monkeypatch, FAKE_HELPER_OUT=str(out_file), FAKE_REC=str(rec))
    assert d["state"] == "completed"
    got = json.loads(out_file.read_text(encoding="utf-8"))
    assert got == {"code": 0, "stdout": MARKER + "\n"}  # claude получил ключ командой хаба
    answers = [_call(code, n, a) for n, a in (("code_status", {"job": jid}), ("code_result", {"job": jid}),
                                               ("code_jobs", {}), ("code_stats", {}), ("code_cancel", {"job": jid}))]
    answers.append(json.dumps(code_server.card_data(code.jobs, jid, 0, True), ensure_ascii=False))
    for text in answers:
        assert MARKER not in text and MARKER[8:24] not in text
    rec_data = json.loads(rec.read_text(encoding="utf-8"))
    assert MARKER not in json.dumps(rec_data["env"]) and MARKER not in json.dumps(rec_data["argv"])
    assert MARKER not in json.dumps(claude_cli.environment(dict(os.environ), code.hub.registry.get().code.glob
                                                           .pass_env))
    files = [p for p in code.state.rglob("*") if p.is_file()]  # каталог задачи, журналы, ledger
    assert any(p.parent == code.jobs.job_dir(jid) for p in files)
    for p in files:
        assert MARKER.encode() not in p.read_bytes(), p


# ---------- п. 3.6: wshub code key-helper ----------

def test_key_helper_prints_key(code, capsys):
    assert code_cli.code_main("code", ["key-helper"], code.cfg, code.state) == 0
    out = capsys.readouterr()
    assert out.out == MARKER + "\n" and out.err == ""
    assert code_cli.code_main("code", ["key-helper", "x"], code.cfg, code.state) == 2


@pytest.mark.parametrize("case", ["missing", "mode_0644"])
def test_key_helper_problem_exit_1_without_key(code, capsys, case):
    if case == "missing":
        _key_path(code).unlink()
    else:
        os.chmod(_key_path(code), 0o644)
    assert code_cli.code_main("code", ["key-helper"], code.cfg, code.state) == 1
    out = capsys.readouterr()
    assert out.out == "" and MARKER not in out.err
    assert ("ключа нет" if case == "missing" else "0644") in out.err


# ---------- п. 3.7: деньги ----------

def test_budget_only_if_set(code, monkeypatch):
    """Упрощение 5.2.2: умолчания бюджета нет — как на подписке (WSH-4.7); заданный max_budget_usd — потолок."""
    assert effective_limits(ProjectCode(), "key") == effective_limits(ProjectCode(), "subscription")
    assert effective_limits(ProjectCode(), "key").max_budget_usd is None
    assert effective_limits(ProjectCode(max_budget_usd=3), "key").max_budget_usd == 3
    _registry(code, **NO_LIMITS)
    p = _plan(code, code.taskfile(extra="budget_usd: 4\n"), channel="mcp", reason=REASON)
    assert p.budget_usd is None and LIMIT_IGNORED in p.warnings  # из шапки без потолка — не применяется
    jid, d = _run(code, monkeypatch)
    assert "--max-budget-usd" not in _argv(code, jid) and "--max-turns" not in _argv(code, jid)
    assert [(r["reserve"], r["cost_usd"]) for r in _raw(code) if r.get("job") == jid] == [(True, 0.0), (False, 0.5)]
    _registry(code)  # max_budget_usd = 10 в реестре
    jid, d = _run(code, monkeypatch)
    assert _opt(_argv(code, jid), "--max-budget-usd") == "10" and d["budget_usd"] == 10
    with pytest.raises(JobError, match="budget_usd 30 выше потолка 10"):
        _plan(code, code.taskfile(extra="budget_usd: 30\n"))
    assert _plan(code, code.taskfile(extra="budget_usd: 4\n")).budget_usd == 4


def test_no_budget_needs_a_cent(code):
    """Без бюджета звена проверка дня и зон требует хотя бы цента остатка: исчерпанная зона не пропускает задачу."""
    _registry(code, api=API.replace("included_usd = 100", "included_usd = 0"), **NO_LIMITS)
    with pytest.raises(JobError, match="включённые кредиты исчерпаны"):
        code.jobs.start(_plan(code))
    _registry(code, api="[api]\ndaily_usd = 5\n", **NO_LIMITS)  # зон нет, потолок дня есть
    ledger.append(ledger.ledger_path(code.state), {"ts": ledger.stamp(datetime.now().astimezone()), "kind": "run",
                                                   "provider": "fake", "zone": "included", "cost_usd": 5.0})
    with pytest.raises(JobError, match=r"daily_usd = \$5\.00: сегодня уже ≈\$5\.00"):
        code.jobs.start(_plan(code))
    assert _job_dirs(code) == 0


def test_daily_only_reserve_without_zone(code, monkeypatch):
    """Задан только [api] daily_usd: резерв есть (проверка дня), зоны в строке оплаты нет."""
    _registry(code, api="[api]\ndaily_usd = 50\n")
    jid, d = _run(code, monkeypatch)
    assert [(r["reserve"], r["cost_usd"]) for r in _raw(code) if r.get("job") == jid] == [(True, 10.0), (False, 0.5)]
    assert "zone" not in d["billing"] and d["billing"]["reserve_usd"] == 10
    assert apply_billing(code.jobs, code.jobs.snapshot(jid))["billing"] == "оплата: ключ anthropic · ≈$0.50 за задачу"


def test_daily_ceiling_refuses(code):
    _registry(code, api=API.replace("daily_usd = 50", "daily_usd = 15"))
    t = ledger.stamp(datetime.now().astimezone())
    ledger.append(ledger.ledger_path(code.state), {"ts": t, "kind": "run", "provider": "fake", "zone": "included",
                                                   "cost_usd": 6.0, "model": "m", "project": "proj"})
    with pytest.raises(JobError, match=r"daily_usd = \$15\.00: сегодня уже ≈\$6\.00, запрос ≈\$10\.00"):
        code.jobs.start(_plan(code))
    assert _job_dirs(code) == 0 and len(_raw(code)) == 1


def test_included_exhausted_needs_allow(code, monkeypatch):
    _registry(code, api=API.replace("included_usd = 100", "included_usd = 5"))
    with pytest.raises(JobError, match="включённые кредиты исчерпаны .*wshub api allow anthropic"):
        code.jobs.start(_plan(code))
    assert _job_dirs(code) == 0 and _raw(code) == []
    assert api_main(["allow", "anthropic", "--usd", "20", "--until", "31.12.2099"], code.cfg, code.state) == 0
    jid, d = _run(code, monkeypatch)
    assert d["billing"]["zone"] == "purchased" and d["state"] == "completed"
    assert [(r["zone"], r["cost_usd"]) for r in _effective(code, jid)] == [("purchased", 0.5)]
    st = apply_billing(code.jobs, code.jobs.snapshot(jid))
    assert "зона: купленная" in st["billing"]
    assert api_main(["allow", "anthropic", "--revoke"], code.cfg, code.state) == 0
    with pytest.raises(JobError, match="включённые кредиты исчерпаны"):  # без действующего allow — никогда
        code.jobs.start(_plan(code))


def test_reserve_then_fact_and_stats(code, monkeypatch, capsys):
    monkeypatch.setenv("FAKE_SLEEP", "3")
    jid = code.jobs.start(_plan(code))["id"]
    rows = [r for r in _raw(code) if r.get("job") == jid]
    assert len(rows) == 1 and rows[0]["reserve"] is True and rows[0]["cost_usd"] == 10
    assert (rows[0]["kind"], rows[0]["provider"], rows[0]["template"], rows[0]["project"], rows[0]["zone"]) == \
        ("run", "anthropic", "code", "proj", "included")
    assert code.jobs.read(jid)["billing"]["reserve_usd"] == 10
    wait_terminal(code.jobs, jid)
    raw = [r for r in _raw(code) if r.get("job") == jid]
    assert [(r["reserve"], r["cost_usd"]) for r in raw] == [(True, 10.0), (False, 0.5)]
    eff = _effective(code, jid)
    assert len(eff) == 1 and eff[0]["cost_usd"] == 0.5 and eff[0]["ok"] is True
    api = Api(code.hub.registry.get(), code.cfg.parent, code.state)
    assert api.zones("anthropic").included_spent == 0.5  # резерв не сложился с фактом
    assert api_main(["stats"], code.cfg, code.state) == 0
    out = capsys.readouterr().out
    assert "anthropic: включённые ≈$0.50 из $100.00" in out and "по проектам: proj ≈$0.50 (1)" in out
    st = apply_billing(code.jobs, code.jobs.snapshot(jid))
    assert st["billing"] == "оплата: ключ anthropic · ≈$0.50 за задачу · зона: включённая · цикл ≈$0.50 из " \
                            "$100.00, до 31.12"


def test_lost_keeps_reserve(code, monkeypatch):
    monkeypatch.setenv("FAKE_SLEEP", "30")
    jid = code.jobs.start(_plan(code))["id"]
    _wait_running(code.jobs, jid)
    jd = code.jobs.read(jid)
    os.kill(jd["sup_pid"], 9)
    os.killpg(jd["pgid"], 9)
    time.sleep(0.3)
    assert code.jobs.snapshot(jid)["state"] == "lost"
    assert [(r["reserve"], r["cost_usd"]) for r in _effective(code, jid)] == [(True, 10.0)]


def test_two_tasks_do_not_share_one_remainder(code, monkeypatch):
    _registry(code, api=API.replace("included_usd = 100", "included_usd = 15"), max_parallel="2")
    monkeypatch.setenv("FAKE_SLEEP", "5")
    first = code.jobs.start(_plan(code))["id"]
    with pytest.raises(JobError, match="включённые кредиты исчерпаны"):  # 15 − резерв 10 = 5 < 10
        code.jobs.start(_plan(code))
    wait_terminal(code.jobs, first)
    second = code.jobs.start(_plan(code))["id"]  # факт 0.5 вместо резерва — остаток снова 14.5
    wait_terminal(code.jobs, second)
    assert code.jobs.read(second)["state"] == "completed"


def test_reserve_waits_for_ledger_lock(code):
    """Проверка зоны и резерв — под ledger.lock, как у wshub api run: пока блокировку держит другой, старт ждёт."""
    plan = _plan(code)
    done: dict = {}
    path = ledger.ledger_path(code.state)
    with ledger.locked(path):
        th = threading.Thread(target=lambda: done.setdefault("d", code.jobs.start(plan)))
        th.start()
        th.join(1.5)
        assert th.is_alive() and not done and not [r for r in _raw(code) if r.get("job")]
    th.join(15)
    assert done["d"]["id"] and [r["reserve"] for r in _raw(code) if r.get("job") == done["d"]["id"]][0] is True
    wait_terminal(code.jobs, done["d"]["id"])


def test_reserve_lifted_when_start_fails(code, monkeypatch):
    """Звено не стартовало (сбой запуска надзирателя) — резерв снимается фактом 0."""
    def boom(jid):
        raise OSError("нет процесса")
    monkeypatch.setattr(code.jobs, "_spawn_supervisor", boom)
    with pytest.raises(OSError):
        code.jobs.start(_plan(code))
    rows = [r for r in _raw(code) if r.get("job")]
    assert [(r["reserve"], r["cost_usd"]) for r in rows] == [(True, 10.0), (False, 0.0)]


# ---------- п. 3.8: хвосты WSH-4.7 ----------

def _cli_start(code, capsys, *args) -> str:
    assert code_cli.code_main("code", ["start", "proj", code.taskfile(), *args], code.cfg, code.state) == 0
    jid = capsys.readouterr().out.strip().splitlines()[0]
    wait_terminal(code.jobs, jid)
    return jid


def test_terminal_flags_applied_without_registry_ceiling(code, capsys):
    code.write(**NO_LIMITS)
    jid = _cli_start(code, capsys, "--max-turns", "7", "--budget-usd", "2")
    argv = _argv(code, jid)
    assert _opt(argv, "--max-turns") == "7" and _opt(argv, "--max-budget-usd") == "2"
    assert LIMIT_IGNORED not in code.jobs.read(jid)["warnings"]
    code.write(max_turns="50", max_budget_usd="5")  # потолок реестра флаг терминала не ограничивает
    jid = _cli_start(code, capsys, "--max-turns", "80", "--budget-usd", "9")
    assert (_opt(_argv(code, jid), "--max-turns"), _opt(_argv(code, jid), "--max-budget-usd")) == ("80", "9")
    # из чата — как раньше: при незаданном потолке не применяется
    code.write(**NO_LIMITS)
    assert code.jobs.plan("proj", code.taskfile(), max_turns=7).max_turns is None


def test_terminal_budget_on_key_capped(code, capsys):
    """Флаг терминала на ключе: без потолка в реестре применяется как есть; с max_budget_usd — не выше него."""
    _registry(code, **NO_LIMITS)
    jid = _cli_start(code, capsys, "--billing", "key", "--budget-usd", "12", "--max-turns", "500")
    assert (_opt(_argv(code, jid), "--max-budget-usd"), _opt(_argv(code, jid), "--max-turns")) == ("12", "500")
    _registry(code)  # max_budget_usd = 10
    assert code_cli.code_main("code", ["start", "proj", code.taskfile(), "--billing", "key", "--budget-usd", "12"],
                              code.cfg, code.state) == 1
    assert "budget_usd 12 выше потолка 10" in capsys.readouterr().err
    jid = _cli_start(code, capsys, "--billing", "key", "--budget-usd", "3", "--max-turns", "500")
    assert (_opt(_argv(code, jid), "--max-budget-usd"), _opt(_argv(code, jid), "--max-turns")) == ("3", "500")


def test_sums_shown_on_key(code, monkeypatch, capsys):
    jid, _d = _run(code, monkeypatch)
    st = json.loads(_call(code, "code_status", {"job": jid}))
    assert st["result"]["phase_cost_usd"] == 0.5
    res = json.loads(_call(code, "code_result", {"job": jid}))
    assert res["result"]["total_cost_usd"] == 0.5
    assert json.loads(_call(code, "code_jobs", {}))[0]["cost_usd"] == 0.5
    assert json.loads(_call(code, "code_stats", {}))[0]["avg_cost_usd"] == 0.5
    assert json.loads(_call(code, "code_cancel", {"job": jid}))["result"]["total_cost_usd"] == 0.5
    card = code_server.card_data(code.jobs, jid, 0, False)
    assert (card["cost_usd"], card["billing"]) == (0.5, "ключ anthropic")
    assert code_cli.code_main("code", ["status"], code.cfg, code.state) == 0
    assert "≈$0.50" in capsys.readouterr().out
    assert code_cli.code_main("code", ["stats"], code.cfg, code.state) == 0
    assert "ср. $" in capsys.readouterr().out
    # подписка рядом — сумм нет
    sub = code.jobs.start(code.jobs.plan("proj", code.taskfile()))["id"]
    wait_terminal(code.jobs, sub)
    assert "cost" not in _call(code, "code_status", {"job": sub})
    rows = {r["id"]: r for r in json.loads(_call(code, "code_jobs", {}))}
    assert "cost_usd" not in rows[sub] and rows[jid]["cost_usd"] == 0.5
    assert "cost_usd" not in code_server.card_data(code.jobs, sub, 0, False)
    stats = {r["billing"]: r for r in json.loads(_call(code, "code_stats", {}))}
    assert "avg_cost_usd" not in stats["subscription"] and stats["key"]["avg_cost_usd"] == 0.5


def test_sums_shown_on_billing_mismatch(code, monkeypatch):
    monkeypatch.setenv("FAKE_SOURCE", "apiKeyHelper")
    d = code.jobs.start(code.jobs.plan("proj", code.taskfile()))  # подписка, а claude пошёл по ключу
    wait_terminal(code.jobs, d["id"])
    assert code.jobs.read(d["id"])["reason"] == "billing_mismatch"
    assert "total_cost_usd" in json.loads(_call(code, "code_status", {"job": d["id"]}))["result"]


# ---------- п. 2.7.1: песочница закрывает ~/.config/wshub ----------

def test_key_job_sandbox_denies_wshub_config(code, monkeypatch):
    jid, _d = _run(code, monkeypatch)
    s = _settings(code, jid)
    assert "~/.config/wshub" in s["sandbox"]["filesystem"]["denyRead"]
    rule = f"/{Path.home()}/.config/wshub"
    assert f"Read({rule})" in s["permissions"]["deny"] and f"Read({rule}/**)" in s["permissions"]["deny"]
    assert MARKER not in json.dumps(s)


# ---------- п. 2.2, 2.8.3: провайдер-заготовка, doctor ----------

def test_anthropic_provider(code):
    """Провайдер рабочий с WSH-4.3 (запросы — tests/test_api_anthropic.py): caps, уровни, оценка без ключа — грубо."""
    mod = PROVIDERS["anthropic"]
    prov = mod.create(None)
    assert (prov.name, prov.caps) == ("anthropic", frozenset({"batch", "cache", "schema", "count_tokens",
                                                              "effort"}))  # batch — ТЗ WSH-4.5
    assert prov.models() == {"cheap": "claude-haiku-5-5", "standard": "claude-sonnet-5-5",
                             "strong": "claude-opus-5-5"}
    _registry(code, api=API.replace("[api]\n", "[api]\nenabled = true\n") + "[workspace.proj.api]\nenabled = true\n")
    api = Api(code.hub.registry.get(), code.cfg.parent, code.state)
    _key_path(code).unlink()  # без ключа count_tokens не зовётся: сети нет
    plan = api.plan(Request("", ({"role": "user", "text": "x"},), "proj", tier="cheap"), "anthropic")
    assert plan.request.model == "claude-haiku-5-5" and plan.rough and plan.zone == "included"


def test_key_job_refused_under_python_m(code, monkeypatch):
    """ТЗ WSH-4.3, п. 2.2.4: хаб запущен как python -m wshub — задача на ключе не стартует: ни каталога задачи, ни
    строки в журнале трат; текст — как у пуша."""
    monkeypatch.setattr(claude_cli, "wshub_exe", lambda: None)
    with pytest.raises(JobError, match=r"задача на ключе: wshub запущен как «python -m wshub».*uv tool install"):
        code.jobs.start(_plan(code))
    assert _job_dirs(code) == 0 and _raw(code) == []


def test_api_key_check_anthropic(code, capsys):
    assert api_main(["key", "check", "anthropic"], code.cfg, code.state) == 0
    out = capsys.readouterr().out
    assert "ключ есть, права верны" in out and MARKER not in out


def test_doctor_key_line(code, monkeypatch):
    """Строка о ключе — при включённом wshub-code (разрешения проекта нет, 5.2.1): ключа нет — не ошибка, права
    неверны — ошибка; значение ключа не выводится."""
    monkeypatch.setattr(doctor.shutil, "which", lambda name, *a, **k: f"/usr/bin/{name}")  # bwrap/socat — «есть»
    ctx = doctor.Ctx(config=code.cfg, state=code.state, win_users=code.state / "none")
    c = doctor.check_code(ctx, code.hub.registry.get(), code.hub)
    assert "ключ anthropic: ключ есть, права верны" in c["detail"]
    ok_status = c["status"]
    os.chmod(_key_path(code), 0o644)
    c = doctor.check_code(ctx, code.hub.registry.get(), code.hub)
    assert c["status"] == "fail" and any(x.startswith("ключ anthropic: права ключа") and "0644" in x
                                         for x in c["detail"])
    assert MARKER not in json.dumps(c, ensure_ascii=False)
    _key_path(code).unlink()
    c = doctor.check_code(ctx, code.hub.registry.get(), code.hub)
    assert "ключ anthropic: нет — задачи на ключе (billing = key) недоступны" in c["detail"]
    assert c["status"] == ok_status  # нет ключа — режим недоступен, но это не ошибка doctor

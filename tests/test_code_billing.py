"""WSH-4.1, страж подписки: проверка до старта, запрет правки настроек claude, окружение, сверка init и
rate_limit_event в потоке, причины billing_mismatch / overage / subscription_limit, строка оплаты.
Настоящий claude не вызывается — tests/fake_claude.py (FAKE_SOURCE, FAKE_RATE…)."""
from __future__ import annotations

import contextlib
import json
import os
import time
from pathlib import Path

import pytest
from test_code import FAKE_ENV, GOLDEN, _golden_settings, make_code, registry_text, wait_terminal
from test_code_plan import _call

from wshub import claude_cli, code_cli
from wshub.core import Hub
from wshub.jobs import JobError, Jobs
from wshub.registry import CodeGlobal, ProjectCode, parse
from wshub.runner import Supervisor, _Stream, billing_line

# rate_limit_info с подписки (PoC WSH-4.0, факт 3): 1792059600 = 15.10 10:20Z, 1792522800 = 20.10 19:00Z
RATE = {"status": "allowed", "resetsAt": 1792059600, "rateLimitType": "five_hour", "overageStatus": "rejected",
        "overageDisabledReason": "org_level_disabled", "isUsingOverage": False,
        "unifiedWindows": {"five_hour": {"utilization": 0.11, "resetsAt": 1792059600},
                           "seven_day": {"utilization": 0.23, "resetsAt": 1792522800}}}
LINE = ("оплата: подписка · 5 ч — 11 %, сброс 15.10 10:20 · неделя — 23 %, сброс 20.10 19:00 · "
        "overage: выкл (org_level_disabled)")
NO_DATA = "оплата: подписка (данных о лимитах ещё нет)"
UNKNOWN = "оплата: источник ещё не известен"


@pytest.fixture
def code(tmp_path, monkeypatch):
    return make_code(tmp_path, monkeypatch)


@contextlib.contextmanager
def _tz(name: str):
    old = os.environ.get("TZ")
    os.environ["TZ"] = name
    time.tzset()
    try:
        yield
    finally:
        if old is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = old
        time.tzset()


@pytest.fixture
def utc():
    """Часы хоста — UTC: строка оплаты сверяется с примером ТЗ буквально. Надзиратель наследует TZ."""
    with _tz("UTC"):
        yield


def _job_dirs(code) -> int:
    d = code.state / "jobs"
    return len([p for p in d.iterdir() if p.is_dir()]) if d.is_dir() else 0


def _settings(code, name: str, content) -> Path:
    p = code.root / ".claude" / name
    p.parent.mkdir(exist_ok=True)
    if isinstance(content, bytes):
        p.write_bytes(content)
    else:
        p.write_text(content if isinstance(content, str) else json.dumps(content), encoding="utf-8")
    return p


def _run(code, monkeypatch, timeout=30, **env) -> tuple[str, dict]:
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    jid = code.jobs.start(code.jobs.plan("proj", code.taskfile()))["id"]
    wait_terminal(code.jobs, jid, timeout)
    return jid, code.jobs.read(jid)


def _finish_params(code, jid) -> dict:
    lines = [json.loads(x) for x in (code.state / "audit.jsonl").read_text().splitlines()]
    return next(r for r in lines if r["tool"] == "code_finish" and r["job"] == jid)["params"]


def _rewrite(code, text: str) -> None:
    code.cfg.write_text(text, encoding="utf-8")
    code.hub = Hub(code.cfg, code.state)
    code.jobs = Jobs(code.hub)


# ---------- п. 2.1: проверка до старта ----------

@pytest.mark.parametrize("name,content,key", [
    ("settings.json", {"apiKeyHelper": "/bin/echo k"}, "apiKeyHelper"),
    ("settings.local.json", {"apiKeyHelper": "/bin/echo k"}, "apiKeyHelper"),
    ("settings.json", {"env": {"ANTHROPIC_API_KEY": "k"}}, "env.ANTHROPIC_API_KEY"),
    ("settings.local.json", {"env": {"ANTHROPIC_BASE_URL": "http://x"}}, "env.ANTHROPIC_BASE_URL"),
    ("settings.json", {"env": {"CLAUDE_CODE_USE_BEDROCK": "1"}}, "env.CLAUDE_CODE_USE_BEDROCK"),
    ("settings.local.json", {"env": {"CLAUDE_CODE_OAUTH_TOKEN": "t"}}, "env.CLAUDE_CODE_OAUTH_TOKEN"),
    ("settings.json", {"env": {"AWS_BEARER_TOKEN_BEDROCK": "t"}}, "env.AWS_BEARER_TOKEN_BEDROCK"),
    ("settings.local.json", {"env": {"ANTHROPIC_VERTEX_PROJECT_ID": "p"}}, "env.ANTHROPIC_VERTEX_PROJECT_ID"),
])
def test_prestart_refuses_billing_keys(code, name, content, key):
    _settings(code, name, content)
    plan = code.jobs.plan("proj", code.taskfile())
    with pytest.raises(JobError) as e:
        code.jobs.start(plan)
    assert str(e.value) == f"задача пошла бы не по подписке: {plan.dir / '.claude' / name} задаёт {key}"
    assert _job_dirs(code) == 0  # каталог задачи не создан


@pytest.mark.parametrize("kind,why", [
    ("broken", "не разбирается как JSON"),
    ("array", "не JSON-объект"),
    ("null", "не JSON-объект"),
    ("bad_utf8", "не разбирается как JSON"),
    ("big", "больше 1024 КБ"),
    ("dir", "не обычный файл"),
    ("fifo", "не обычный файл"),
    ("device_link", "не обычный файл"),
    ("dir_link", "не обычный файл"),
])
def test_prestart_refuses_unverifiable(code, kind, why):
    """7.1: проверка после resolve(); не обычный файл (в том числе цель симлинка), > 1 МБ, не JSON-объект — отказ.
    FIFO не вешает старт."""
    p = code.root / ".claude" / "settings.local.json"
    p.parent.mkdir()
    if kind == "broken":
        p.write_text('{"apiKeyHelper": ', encoding="utf-8")
    elif kind == "array":
        p.write_text('[{"apiKeyHelper": "x"}]', encoding="utf-8")
    elif kind == "null":
        p.write_text("null", encoding="utf-8")
    elif kind == "bad_utf8":
        p.write_bytes(b'{"env": {"\xff": 1}}')
    elif kind == "big":
        p.write_text('{"x": "' + "a" * (1024 * 1024) + '"}', encoding="utf-8")
    elif kind == "dir":
        p.mkdir()
    elif kind == "fifo":
        os.mkfifo(p)
    elif kind == "device_link":
        p.symlink_to("/dev/null")
    elif kind == "dir_link":
        (code.root / "elsewhere").mkdir()
        p.symlink_to(code.root / "elsewhere")
    plan = code.jobs.plan("proj", code.taskfile())
    t0 = time.time()
    with pytest.raises(JobError, match=why) as e:
        code.jobs.start(plan)
    msg = str(e.value)
    assert str(plan.dir / ".claude" / "settings.local.json") in msg and "хаб не может проверить" in msg
    assert time.time() - t0 < 10 and _job_dirs(code) == 0


@pytest.mark.parametrize("content", [
    {"permissions": {"allow": []}, "env": {"FOO": "1", "MY_ANTHROPIC_API_KEY": "x", "anthropic_api_key": "x"}},
    "",
    "  \n\t\n",
    "link",
])
def test_prestart_passes_clean_and_empty(code, content):
    """Чистые настройки, env с посторонними именами (префикс — с начала имени), пустой файл (точка монтирования
    песочницы) и симлинк на чистый файл — старт проходит."""
    if content == "link":
        target = code.root / "clean.json"
        target.write_text('{"model": "sonnet"}', encoding="utf-8")
        (code.root / ".claude").mkdir()
        (code.root / ".claude" / "settings.json").symlink_to(target)
    else:
        _settings(code, "settings.json", content)
    _settings(code, "settings.local.json", "")
    jid = code.jobs.start(code.jobs.plan("proj", code.taskfile()))["id"]
    assert wait_terminal(code.jobs, jid)["state"] == "completed"


def test_prestart_on_resume(code):
    jid = code.jobs.start(code.jobs.plan("proj", code.taskfile()))["id"]
    wait_terminal(code.jobs, jid)
    _settings(code, "settings.local.json", {"apiKeyHelper": "x"})
    n = _job_dirs(code)
    with pytest.raises(JobError, match="задаёт apiKeyHelper"):
        code.jobs.resume(jid, "task")
    out = _call(code, "code_resume", {"job": jid, "prompt": "task"})
    assert out.startswith("отказ: задача пошла бы не по подписке")
    assert _job_dirs(code) == n


def test_prestart_on_approve_and_replan(code):
    root = code.jobs.start(code.jobs.plan("proj", code.taskfile(size="L", risk="low")))["id"]
    assert wait_terminal(code.jobs, root)["state"] == "plan_ready"
    _settings(code, "settings.json", {"env": {"ANTHROPIC_BASE_URL": "http://proxy"}})
    n = _job_dirs(code)
    with pytest.raises(JobError, match="задаёт env.ANTHROPIC_BASE_URL"):
        code.jobs.approve(root)
    with pytest.raises(JobError, match="задаёт env.ANTHROPIC_BASE_URL"):
        code.jobs.replan(root, "переделай")
    assert _job_dirs(code) == n and code.jobs.read(root)["state"] == "plan_ready"


# ---------- п. 2.2: запрет правки настроек claude ----------

@pytest.mark.parametrize("profile", ["recon", "task", "review"])
@pytest.mark.parametrize("phase", [None, "plan"])
def test_deny_edit_claude_settings(profile, phase):
    s = claude_cli.settings(profile, "T.md", CodeGlobal(), ProjectCode(), [], [], ["/w"], Path("/home/u"),
                            Path("/d"), phase=phase)
    deny = s["permissions"]["deny"]
    assert "Edit(./.claude/settings.json)" in deny and "Edit(./.claude/settings.local.json)" in deny
    # эталон целиком (test_settings_golden, test_plan_settings_golden) — с теми же двумя правилами
    gold = _golden_settings(profile) if phase is None else GOLDEN["plan"][profile]
    assert {"Edit(./.claude/settings.json)", "Edit(./.claude/settings.local.json)"} <= set(gold["permissions"]["deny"])


# ---------- п. 2.3: окружение ----------

BILLING_VARS = {"ANTHROPIC_BASE_URL": "http://x", "ANTHROPIC_PROFILE": "p", "ANTHROPIC_BEDROCK_BASE_URL": "u",
                "CLAUDE_CODE_USE_BEDROCK": "1", "CLAUDE_CODE_USE_VERTEX": "1", "CLAUDE_CODE_OAUTH_REFRESH_TOKEN": "r",
                "AWS_BEARER_TOKEN_BEDROCK": "t"}


def test_environment_drops_billing_names():
    out = claude_cli.environment({**BILLING_VARS, "HOME": "/h", "WSHUB_TESTVAR": "v", "MY_ANTHROPIC_X": "m"},
                                 (*BILLING_VARS, "WSHUB_TESTVAR", "MY_ANTHROPIC_X"))
    assert out["HOME"] == "/h" and out["WSHUB_TESTVAR"] == "v" and out["MY_ANTHROPIC_X"] == "m"
    for k in BILLING_VARS:
        assert k not in out


def test_pass_env_billing_names_warn_and_do_not_reach_task(code, monkeypatch):
    names = ["ANTHROPIC_BASE_URL", "ANTHROPIC_PROFILE", "CLAUDE_CODE_USE_BEDROCK"]
    text = code.cfg.read_text().replace(
        f'pass_env = {json.dumps(FAKE_ENV + ["WSHUB_TESTVAR"])}',
        f'pass_env = {json.dumps(FAKE_ENV + ["WSHUB_TESTVAR", *names])}')
    reg = parse(text)
    assert not reg.code.error  # предупреждение, а не ошибка
    for n in names:
        assert any(n in w and "источник оплаты" in w for w in reg.warnings), reg.warnings
    _rewrite(code, text)
    for k, v in BILLING_VARS.items():
        monkeypatch.setenv(k, v)
    _jid, d = _run(code, monkeypatch, FAKE_REC=str(code.state / "rec.json"))
    assert d["state"] == "completed"
    env = json.loads((code.state / "rec.json").read_text())["env"]
    assert env.get("WSHUB_TESTVAR") == "сквозь-белый-список"
    for k in BILLING_VARS:
        assert k not in env


def test_allow_overage_registry(code):
    base = registry_text(code.root)
    assert parse(base).code.glob.allow_overage is False
    assert parse(base.replace("[code]\n", "[code]\nallow_overage = true\n")).code.glob.allow_overage is True
    bad = parse(base.replace("[code]\n", '[code]\nallow_overage = "yes"\n'))
    assert bad.code.error and "allow_overage" in bad.code.error


# ---------- п. 2.4: billing_mismatch ----------

@pytest.mark.parametrize("source", ["apiKeyHelper", "ANTHROPIC_API_KEY", "-"])
def test_billing_mismatch_stops(code, monkeypatch, source):
    mark = code.state.parent / "sigmark"
    t0 = time.time()
    jid, d = _run(code, monkeypatch, FAKE_SOURCE=source, FAKE_SLEEP="20", FAKE_SIGMARK=str(mark))
    assert time.time() - t0 < 15  # остановлен, а не доработал 20 с
    assert mark.read_text() == "INT"  # тот же путь, что code_cancel: SIGINT процессу claude
    assert (d["state"], d["reason"]) == ("failed", "billing_mismatch")
    real = None if source == "-" else source
    assert d["result"]["billing_source"] == real and d["billing"]["source"] == real
    st = json.loads(_call(code, "code_status", {"job": jid}))
    assert st["billing"].startswith("оплата: НЕ подписка") and st["billing"].endswith("задача остановлена")
    assert "не «none»" in st["reason_hint"]
    res = json.loads(_call(code, "code_result", {"job": jid}))
    assert res["reason"] == "billing_mismatch" and res["result"]["billing_source"] == real
    assert _finish_params(code, jid)["billing_source"] == real


def test_billing_mismatch_after_claude_exit(code, monkeypatch):
    """claude сам успешно вышел до остановки (фейк без FAKE_SLEEP): итог — billing_mismatch, а не completed."""
    _jid, d = _run(code, monkeypatch, FAKE_SOURCE="apiKeyHelper")
    assert (d["state"], d["reason"], d["result"]["billing_source"]) == ("failed", "billing_mismatch", "apiKeyHelper")


def test_subscription_completes_and_records_model(code, monkeypatch):
    jid, d = _run(code, monkeypatch, FAKE_INIT_MODEL="claude-sonnet-5-5")
    assert d["state"] == "completed"
    assert d["billing"]["source"] == "none" and d["billing"]["model"] == "claude-sonnet-5-5"
    res = json.loads(_call(code, "code_result", {"job": jid}))
    assert res["model_line"] == f"модель: {d['model']} → claude-sonnet-5-5"
    assert res["billing"] == NO_DATA and "reason_hint" not in res


# ---------- п. 2.4: overage ----------

def test_overage_stops(code, monkeypatch):
    rate = {**RATE, "isUsingOverage": True, "overageStatus": "allowed"}
    t0 = time.time()
    jid, d = _run(code, monkeypatch, FAKE_RATE=json.dumps(rate), FAKE_RATE_AT="1", FAKE_SLEEP="20")
    assert time.time() - t0 < 15
    assert (d["state"], d["reason"]) == ("failed", "overage")
    st = json.loads(_call(code, "code_status", {"job": jid}))
    assert "overage: используется" in st["billing"] and st["billing"].endswith("задача остановлена: overage")
    assert "allow_overage = true" in st["reason_hint"]


def test_overage_allowed_by_registry(code, monkeypatch):
    _rewrite(code, code.cfg.read_text().replace("[code]\n", "[code]\nallow_overage = true\n"))
    rate = {**RATE, "isUsingOverage": True, "overageStatus": "allowed"}
    _jid, d = _run(code, monkeypatch, FAKE_RATE=json.dumps(rate), FAKE_RATE_AT="1", FAKE_SLEEP="2")
    assert (d["state"], d["reason"]) == ("completed", None)
    assert d["billing"]["rate"]["isUsingOverage"] is True


# ---------- п. 2.5: subscription_limit ----------

def test_subscription_limit(code, monkeypatch, utc):
    rate = {**RATE, "status": "rejected"}
    jid, d = _run(code, monkeypatch, FAKE_RATE=json.dumps(rate), FAKE_LIMIT_ERROR="1")
    assert (d["state"], d["reason"]) == ("failed", "subscription_limit")
    assert d["result"]["resets_at"] == "2026-10-15T10:20:00+00:00"
    for tool in ("code_status", "code_result"):
        out = json.loads(_call(code, tool, {"job": jid}))
        assert out["reason_hint"] == "лимит подписки исчерпан; после 15.10 10:20 продолжи: code_resume"
        assert out["billing"].endswith("· лимит подписки исчерпан, сброс 15.10 10:20")


@pytest.mark.parametrize("status", ["allowed", "allowed_warning"])
def test_error_with_allowed_keeps_old_reason(code, monkeypatch, status):
    _jid, d = _run(code, monkeypatch, FAKE_RATE=json.dumps({**RATE, "status": status}), FAKE_LIMIT_ERROR="1",
                   FAKE_SUBTYPE="error_during_execution")
    assert (d["state"], d["reason"]) == ("failed", "error_during_execution")


def test_rejected_without_error_is_not_limit(code, monkeypatch):
    _jid, d = _run(code, monkeypatch, FAKE_RATE=json.dumps({**RATE, "status": "rejected"}))
    assert (d["state"], d["reason"]) == ("completed", None)


# ---------- п. 2.6–2.8: строка оплаты, code_projects, журнал ----------

def test_billing_line_format(utc):
    assert billing_line({"source": "none", "rate": RATE}) == LINE
    warn = {**RATE, "status": "allowed_warning"}
    assert billing_line({"source": "none", "rate": warn}) == "близко к лимиту · " + LINE
    assert billing_line({"source": "none"}) == NO_DATA
    assert billing_line(None) == UNKNOWN and billing_line({}) == UNKNOWN  # до первого init — не «подписка»
    assert billing_line({"source": None}, "billing_mismatch") == \
        "оплата: НЕ подписка (apiKeySource: поля нет) · задача остановлена"


def test_billing_line_everywhere(code, monkeypatch, capsys, utc):
    assert all(r["rate"] is None for r in json.loads(_call(code, "code_projects", {})))  # задач ещё нет
    jid, d = _run(code, monkeypatch, FAKE_RATE=json.dumps(RATE), FAKE_RATE_COUNT="3",
                  FAKE_INIT_MODEL="claude-sonnet-5-5")
    assert d["state"] == "completed"
    b = d["billing"]
    assert b["rate"] == RATE and b["rate_events"] == 3 and b["rate_at"]
    assert json.loads(_call(code, "code_status", {"job": jid}))["billing"] == LINE
    assert json.loads(_call(code, "code_wait", {"job": jid, "seconds": 1}))["billing"] == LINE
    res = json.loads(_call(code, "code_result", {"job": jid}))
    assert res["billing"] == LINE and res["model_line"] == f"модель: {d['model']} → claude-sonnet-5-5"
    assert code_cli.code_main("code", ["status", jid], code.cfg, code.state) == 0
    assert json.loads(capsys.readouterr().out)["billing"] == LINE
    assert code_cli.code_main("code", ["result", jid], code.cfg, code.state) == 0
    out = capsys.readouterr().out
    assert LINE in out and "claude-sonnet-5-5" in out
    rows = json.loads(_call(code, "code_projects", {}))
    assert rows[0]["rate"] == {"job": jid, "project": "proj", "at": b["rate_at"], "line": LINE}
    p = _finish_params(code, jid)
    assert (p["billing_source"], p["billing_model"], p["rate_events"]) == ("none", "claude-sonnet-5-5", 3)
    assert p["rate"] == {"window": "five_hour", "utilization": 0.11, "status": "allowed", "isUsingOverage": False}
    # вторая задача свежее — code_projects показывает её снимок
    newer = {**RATE, "unifiedWindows": {"five_hour": {"utilization": 0.5, "resetsAt": 1792059600}}}
    jid2, _ = _run(code, monkeypatch, FAKE_RATE=json.dumps(newer), FAKE_RATE_COUNT="1")
    rows = json.loads(_call(code, "code_projects", {}))
    assert rows[0]["rate"]["job"] == jid2 and "5 ч — 50 %" in rows[0]["rate"]["line"]


def test_billing_line_host_clock():
    """Время сброса — по часам хоста, а не в UTC (TZ без tzdata: MSK-3 — UTC+3)."""
    with _tz("MSK-3"):
        line = billing_line({"source": "none", "rate": RATE})
    assert "5 ч — 11 %, сброс 15.10 13:20" in line and "неделя — 23 %, сброс 20.10 22:00" in line


def test_billing_line_bad_numbers(utc):
    """Infinity и NaN из json.loads не роняют строку оплаты — окно пропускается."""
    rate = {**RATE, "unifiedWindows": {"five_hour": {"utilization": float("inf")},
                                       "seven_day": {"utilization": 0.23, "resetsAt": 1792522800}}}
    assert billing_line({"source": "none", "rate": rate}) == ("оплата: подписка · неделя — 23 %, сброс 20.10 19:00 · "
                                                              "overage: выкл (org_level_disabled)")


def test_billing_written_while_running(code, monkeypatch, utc):
    """billing пишется в job.json по ходу работы (не только в _finish): живая строка оплаты в code_status."""
    for k, v in {"FAKE_RATE": json.dumps(RATE), "FAKE_RATE_AT": "1", "FAKE_SLEEP": "20",
                 "FAKE_INIT_MODEL": "claude-sonnet-5-5"}.items():
        monkeypatch.setenv(k, v)
    jid = code.jobs.start(code.jobs.plan("proj", code.taskfile()))["id"]
    end = time.time() + 15
    while time.time() < end and not (code.jobs.read(jid).get("billing") or {}).get("rate"):
        time.sleep(0.1)
    d = code.jobs.read(jid)
    assert d["state"] == "running" and d["billing"]["source"] == "none"
    assert d["billing"]["model"] == "claude-sonnet-5-5" and d["billing"]["rate_events"] == 1
    assert json.loads(_call(code, "code_status", {"job": jid}))["billing"] == LINE
    code.jobs.cancel(jid)
    wait_terminal(code.jobs, jid, timeout=60)


def test_watch_unit(code, monkeypatch, tmp_path):
    """Сверка _watch напрямую: второй init с "none" не затирает несовпадение, первая причина остаётся, строка
    с глубокой вложенностью не роняет разбор; реестр не читается — overage не разрешён (даже при allow_overage)."""
    _rewrite(code, code.cfg.read_text().replace("[code]\n", "[code]\nallow_overage = true\n"))
    jid, _ = _run(code, monkeypatch)
    over = {"type": "rate_limit_event", "rate_limit_info": {**RATE, "isUsingOverage": True}}
    p = tmp_path / "ev.jsonl"
    p.write_text("[" * 100000 + "\n" + "\n".join(json.dumps(e) for e in (
        {"type": "system", "subtype": "init", "apiKeySource": "apiKeyHelper", "model": "m1"},
        {"type": "system", "subtype": "init", "apiKeySource": "none", "model": "m2"}, over)) + "\n")
    sup = Supervisor(code.hub, jid)
    sup._watch(_Stream(p))
    assert sup.stop == "billing_mismatch"  # overage после него причину не меняет
    b = code.jobs.read(jid)["billing"]  # _watch пишет job.json сам
    assert (b["source"], b["model"], b["rate_events"]) == ("apiKeyHelper", "m2", 1)

    sup = Supervisor(code.hub, jid)
    sup._watch(_Stream(tmp_path / "none.jsonl"))  # файла нет — пусто, без ошибки
    assert sup.stop is None

    def broken():
        raise RuntimeError("реестр не читается")

    q = tmp_path / "over.jsonl"
    q.write_text(json.dumps(over) + "\n")
    sup = Supervisor(code.hub, jid)
    sup._watch(_Stream(q))
    assert sup.stop is None  # allow_overage = true
    monkeypatch.setattr(sup.hub.registry, "get", broken)
    sup._watch(_Stream(q))
    assert sup.stop == "overage"


def test_billing_line_chain_root_uses_active_link(code, monkeypatch, utc):
    """У корня цепочки (план → исполнение) строка оплаты и модель — по активному звену, а не по звену плана."""
    root = code.jobs.start(code.jobs.plan("proj", code.taskfile(size="L", risk="low")))["id"]
    assert wait_terminal(code.jobs, root)["state"] == "plan_ready"
    assert json.loads(_call(code, "code_status", {"job": root}))["billing"] == NO_DATA
    monkeypatch.setenv("FAKE_RATE", json.dumps(RATE))
    monkeypatch.setenv("FAKE_INIT_MODEL", "claude-opus-5-5")
    code.jobs.approve(root)
    end = time.time() + 30
    while time.time() < end and json.loads(_call(code, "code_status", {"job": root}))["state"] != "completed":
        time.sleep(0.2)
    st = json.loads(_call(code, "code_status", {"job": root}))
    assert st["state"] == "completed" and st["billing"] == LINE
    res = json.loads(_call(code, "code_result", {"job": root}))
    assert res["billing"] == LINE and res["model_line"].endswith("→ claude-opus-5-5")


def test_prestart_on_auto_execute(code):
    """plan = auto: исполнение запускает сам надзиратель (_auto_execute → approve → start) — проверка до старта
    срабатывает и там; план остаётся plan_ready с chain_error. Файл кладётся между планом и исполнением: в живой фазе
    плана его поймала бы уже повторная проверка надзирателя (test_settings_written_mid_plan_stops)."""
    root = code.jobs.start(code.jobs.plan("proj", code.taskfile(size="L", risk="low")))["id"]
    assert wait_terminal(code.jobs, root)["state"] == "plan_ready"
    d = code.jobs.read(root)
    d["plan_mode"] = "auto"
    code.jobs.write(root, d)
    _settings(code, "settings.local.json", {"apiKeyHelper": "x"})
    Supervisor(code.hub, root)._auto_execute()
    d = code.jobs.read(root)
    assert d["state"] == "plan_ready" and "задаёт apiKeyHelper" in d["chain_error"]
    assert len(code.jobs.chain(root)) == 1


def test_settings_written_mid_plan_stops(code, monkeypatch):
    """Фаза плана (plan = auto): файл с apiKeyHelper появился посреди плана — план остановлен billing_mismatch,
    до plan_ready и исполнения не доходит."""
    monkeypatch.setenv("FAKE_SLEEP_PLAN", "3")
    root = code.jobs.start(code.jobs.plan("proj", code.taskfile(size="M", risk="low")))["id"]
    assert code.jobs.read(root)["plan_mode"] == "auto"
    _settings(code, "settings.local.json", {"apiKeyHelper": "x"})  # проверка до старта плана уже пройдена
    wait_terminal(code.jobs, root)
    d = code.jobs.read(root)
    assert (d["state"], d["reason"]) == ("failed", "billing_mismatch")
    assert d["result"]["billing_source"].endswith("settings.local.json задаёт apiKeyHelper")
    assert len(code.jobs.chain(root)) == 1


# ---------- ревью круга 1, п. 1: настройки, изменённые посреди звена ----------

def test_settings_written_mid_run_stops(code, monkeypatch):
    """Задача посреди звена (после первой проверки надзирателя) пишет .claude/settings.local.json мимо deny —
    надзиратель замечает смену файла, проверяет его той же функцией, что до старта, и останавливает задачу."""
    mark = code.state.parent / "sigmark"
    t0 = time.time()
    jid, d = _run(code, monkeypatch, FAKE_WRITE_SETTINGS='{"apiKeyHelper": "/bin/echo k"}', FAKE_SLEEP="10",
                  FAKE_SIGMARK=str(mark))  # файл появится на 5-й секунде — после первой проверки надзирателя
    assert time.time() - t0 < 9  # остановлен, не доработал свои 10 с
    assert mark.read_text() == "INT"
    src = f"settings: {Path(d['dir']) / '.claude' / 'settings.local.json'} задаёт apiKeyHelper"
    assert (d["state"], d["reason"], d["result"]["billing_source"]) == ("failed", "billing_mismatch", src)
    assert d["billing"]["source"] == "none"  # init был по подписке — поймала именно повторная проверка
    st = json.loads(_call(code, "code_status", {"job": jid}))
    assert st["billing"] == f"оплата: НЕ подписка ({src}) · задача остановлена"
    assert "изменился посреди звена" in st["reason_hint"]
    assert _finish_params(code, jid)["billing_source"] == "none"


def test_settings_written_mid_run_unverifiable_stops(code, monkeypatch):
    jid, d = _run(code, monkeypatch, FAKE_WRITE_SETTINGS='{"env": ', FAKE_SLEEP="6")
    assert (d["state"], d["reason"]) == ("failed", "billing_mismatch")
    assert d["result"]["billing_source"].startswith("settings: ")
    assert "не разбирается как JSON" in d["result"]["billing_source"]


def test_settings_written_mid_run_harmless(code, monkeypatch):
    """Посторонний env в тех же файлах посреди звена — задача доходит до completed."""
    _jid, d = _run(code, monkeypatch, FAKE_WRITE_SETTINGS='{"env": {"FOO": "1"}}', FAKE_SLEEP="3")
    assert (code.root / ".claude" / "settings.local.json").read_text() == '{"env": {"FOO": "1"}}'  # фейк писал
    assert (d["state"], d["reason"]) == ("completed", None)


# ---------- ревью круга 1, п. 2: нет init — fail closed ----------

def test_no_init_fails_closed(code, monkeypatch):
    """result есть, а init не было — failed/billing_mismatch, billing_source = null, «НЕ подтверждена». Пока init
    нет, у живой задачи строка — «источник ещё не известен», а не «подписка»."""
    for k, v in {"FAKE_NO_INIT": "1", "FAKE_SLEEP": "4"}.items():
        monkeypatch.setenv(k, v)
    jid = code.jobs.start(code.jobs.plan("proj", code.taskfile()))["id"]
    end = time.time() + 10
    while time.time() < end and code.jobs.read(jid)["state"] != "running":
        time.sleep(0.1)
    st = json.loads(_call(code, "code_status", {"job": jid}))
    assert st["state"] == "running" and st["billing"] == UNKNOWN
    wait_terminal(code.jobs, jid)
    d = code.jobs.read(jid)
    assert (d["state"], d["reason"]) == ("failed", "billing_mismatch")
    assert "billing_source" in d["result"] and d["result"]["billing_source"] is None
    res = json.loads(_call(code, "code_result", {"job": jid}))
    assert res["billing"] == "оплата: НЕ подтверждена (init не было) · задача остановлена"
    assert "init" in res["reason_hint"]


def test_no_init_no_result_keeps_reason(code, monkeypatch):
    """Нет ни init, ни result (claude убит SIGKILL при отмене) — прежняя причина."""
    for k, v in {"FAKE_NO_INIT": "1", "FAKE_SLEEP": "20", "FAKE_SIGINT": "ignore-all",
                 "WSHUB_CODE_CANCEL_SIGTERM_S": "1", "WSHUB_CODE_CANCEL_SIGKILL_S": "1"}.items():
        monkeypatch.setenv(k, v)
    jid = code.jobs.start(code.jobs.plan("proj", code.taskfile()))["id"]
    end = time.time() + 10
    while time.time() < end and code.jobs.read(jid)["state"] != "running":
        time.sleep(0.1)
    code.jobs.cancel(jid)
    wait_terminal(code.jobs, jid, timeout=60)
    d = code.jobs.read(jid)
    assert (d["state"], d["reason"]) == ("cancelled", "cancel") and "billing_source" not in d["result"]


def test_no_rate_data_line(code, monkeypatch):
    jid, d = _run(code, monkeypatch)
    assert d["billing"].get("rate_events") is None
    assert json.loads(_call(code, "code_status", {"job": jid}))["billing"] == NO_DATA
    assert _finish_params(code, jid)["rate_events"] == 0 and _finish_params(code, jid)["rate"] is None

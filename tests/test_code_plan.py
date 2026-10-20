"""wshub-code, ТЗ WSH-3: политика выбора модели/effort/плана, источники выбора, шапка size/risk, фазы плана
и исполнения одной сессии, plan_ready/approve/replan, стоимость звеньев, code_stats, имена сессий,
временный каталог claude (п. 5, решения 5–8). Настоящий claude не вызывается — tests/fake_claude.py."""
from __future__ import annotations

import asyncio
import json
import os
import re
import time
from pathlib import Path

import pytest
from test_code import _assert_no_fake, _wait_running, make_code, wait_terminal

from wshub import claude_cli, code_cli, code_policy, code_server, taskfile
from wshub.jobs import JobError
from wshub.registry import CodeGlobal, PolicyRow, ProjectCode, parse
from wshub.runner import _plan_source

GOLDEN = json.loads((Path(__file__).parent / "golden_settings.json").read_text(encoding="utf-8"))


@pytest.fixture
def code(tmp_path, monkeypatch):
    return make_code(tmp_path, monkeypatch)


def _call(code, name, args) -> str:
    mcp = code_server.build_code_server(code.hub)
    out = asyncio.run(mcp.call_tool(name, args))
    content = out[0] if isinstance(out, tuple) else out
    return content[0].text


def _reclog(code) -> list[dict]:
    p = code.state / "reclog.jsonl"
    return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []


def _opt(argv: list[str], flag: str) -> str | None:
    return argv[argv.index(flag) + 1] if flag in argv else None


def _with_policy(code, rows: str, **over):
    """Реестр с дополнительными строками [[code.policy]] / [[workspace.proj.code.policy]]."""
    code.write(**over)
    code.cfg.write_text(code.cfg.read_text() + rows, encoding="utf-8")
    from wshub.core import Hub
    from wshub.jobs import Jobs
    code.hub = Hub(code.cfg, code.state)
    code.jobs = Jobs(code.hub)


def _wait_state(jobs, jid, states, timeout=30) -> dict:
    end = time.time() + timeout
    while time.time() < end:
        snap = jobs.snapshot(jid)
        if snap["state"] in states:
            return snap
        time.sleep(0.1)
    raise AssertionError(f"{jid}: нет {states}, сейчас {jobs.snapshot(jid)['state']}")


# ---------- 1. политика ----------

def test_builtin_most_specific_row():
    g, p = CodeGlobal(), ProjectCode()
    assert code_policy.match(g, p, "task", "M", "low").key == "task/M/low"
    assert code_policy.match(g, p, "recon", "L", "high").key == "recon/L/*"  # «*» подходит к любому risk
    assert code_policy.match(g, p, "review", None, None).key == "review/*/*"
    assert code_policy.match(g, p, "task", None, None) is None  # у task нет строк со «*»


def test_star_weaker_than_value_within_layer():
    rows = (PolicyRow("task", "*", "*", "opus", "low", "no", origin="project"),
            PolicyRow("task", "M", "*", "haiku", "low", "no", origin="project"),
            PolicyRow("task", "*", "low", "sonnet", "low", "no", origin="project"))
    p = ProjectCode(policy=rows)
    assert code_policy.match(CodeGlobal(), p, "task", "M", "low").key == "task/M/*"  # size весомее risk
    assert code_policy.match(CodeGlobal(), p, "task", "S", "low").key == "task/*/low"
    assert code_policy.match(CodeGlobal(), p, "task", "L", "high").key == "task/*/*"


def test_project_row_beats_global_beats_builtin(code):
    """Кейс 1 (мутация «строка проекта не сильнее глобальной»)."""
    _with_policy(code, '\n[[code.policy]]\nprofile = "task"\nsize = "S"\nrisk = "low"\nmodel = "opus"\n'
                       'effort = "low"\nplan = "no"\n')
    p = code.jobs.plan("proj", code.taskfile())
    assert (p.model, p.effort) == ("opus", "low") and "глобальная" in p.choice.row  # глобальная сильнее встроенной
    _with_policy(code, '\n[[code.policy]]\nprofile = "task"\nsize = "S"\nrisk = "low"\nmodel = "opus"\n'
                       'effort = "low"\nplan = "no"\n'
                       '\n[[workspace.proj.code.policy]]\nprofile = "task"\nsize = "*"\nrisk = "*"\n'
                       'model = "haiku"\neffort = "low"\nplan = "no"\n')
    p = code.jobs.plan("proj", code.taskfile())
    assert (p.model, p.choice.source) == ("haiku", "policy") and "проекта" in p.choice.row


def test_no_row_refused():
    with pytest.raises(code_policy.PolicyError, match="нет строки политики для task/\\*/\\*"):
        code_policy.choose(CodeGlobal(), ProjectCode(), "task", None, None, {}, None)


@pytest.mark.parametrize("row,msg", [
    ('profile = "task"\nsize = "S"\nrisk = "low"\nmodel = "gpt"\neffort = "low"\nplan = "no"', "model"),
    ('profile = "task"\nsize = "S"\nrisk = "low"\nmodel = "opus"\neffort = "low"\nplan = "auto"', "plan_model"),
    ('profile = "task"\nsize = "XL"\nrisk = "low"\nmodel = "opus"\neffort = "low"\nplan = "no"', "size"),
    ('profile = "task"\nsize = "S"\nrisk = "low"\nmodel = "opus"\neffort = "low"\nplan = "no"\nx = 1', "неизвестные"),
])
def test_policy_row_errors_scoped(code, row, msg):
    text = code.cfg.read_text() + f"\n[[workspace.proj.code.policy]]\n{row}\n"
    reg = parse(text)
    assert msg in reg.code.project_errors["proj"] and reg.workspaces["proj"]  # ломает только wshub-code


def test_policy_above_ceiling_refused(code):
    """Строка политики подчиняется потолкам реестра: строка проекта с xhigh при effort_max=high — отказ."""
    row = ('\n[[workspace.proj.code.policy]]\nprofile = "task"\nsize = "L"\nrisk = "high"\nmodel = "opus"\n'
           'effort = "xhigh"\nplan = "review"\nplan_model = "opus"\nplan_effort = "high"\n')
    _with_policy(code, row)
    t = code.taskfile(size="L", risk="high")
    with pytest.raises(JobError, match="строка политики task/L/high \\(проекта\\).*effort xhigh выше effort_max"):
        code.jobs.plan("proj", t)
    _with_policy(code, row, effort_max='"xhigh"')
    assert code.jobs.plan("proj", t).choice.effort == "xhigh"


def test_builtin_rows_within_default_bounds():
    """Решение 11: каждая встроенная строка проходит пол и потолок ProjectCode() по умолчанию."""
    p = ProjectCode()
    for r in code_policy.BUILTIN:
        code_policy._check_bounds(p, r.model, r.effort, r.key)
        if r.plan != "no":
            code_policy._check_bounds(p, r.plan_model, r.plan_effort, f"{r.key} (план)")
    assert code_policy.match(CodeGlobal(), p, "task", "L", "high").effort == "high"


# ---------- 2. источники выбора ----------

def test_operator_choice_ignores_floor_and_ceiling_all_three_ways(code, capsys):
    """Кейс 2: выбор оператора за пределами пола и потолка применяется — аргументы, шапка, флаги CLI
    (мутация «выбор оператора проверяется по потолку»)."""
    code.write(model_max='"sonnet"', model_min='"sonnet"', effort_max='"medium"')
    p = code.jobs.plan("proj", code.taskfile(), model="opus", effort="max", chosen_by="operator")
    assert (p.model, p.effort, p.choice.source) == ("opus", "max", "operator")
    t = code.taskfile("H.md", extra="model: haiku\neffort: high\nchoice: operator\n")
    p = code.jobs.plan("proj", t)
    assert (p.model, p.effort, p.choice.source) == ("haiku", "high", "operator")
    rc = code_cli.code_main("code", ["start", "proj", code.taskfile("C.md"), "--model", "opus", "--effort", "max"],
                            code.cfg, code.state)
    assert rc == 0
    jid = capsys.readouterr().out.strip().splitlines()[0]
    wait_terminal(code.jobs, jid)
    d = code.jobs.read(jid)
    assert (d["model"], d["effort"], d["choice"]["source"]) == ("opus", "max", "operator")


def test_operator_choice_on_plan_row_ignores_ceiling(code):
    """Ревью, находка 3 (критерий 6.3): выбор оператора выше model_max применяется и на строке с планом."""
    code.write(model_max='"sonnet"')
    p = code.jobs.plan("proj", code.taskfile(size="M", risk="low"), model="opus", chosen_by="operator")
    assert (p.choice.model, p.choice.plan, p.choice.plan_model, p.choice.source) == ("opus", "auto", "opus", "operator")
    with pytest.raises(JobError, match="фаза плана"):  # без оператора строка с планом opus — за потолком
        code.jobs.plan("proj", code.taskfile("P.md", size="M", risk="low"))


def test_operator_budget_still_enforced(code):
    code.write(max_budget_usd="5")
    with pytest.raises(JobError, match="budget"):
        code.jobs.plan("proj", code.taskfile(), model="opus", chosen_by="operator", budget_usd=9)


def test_chat_choice_needs_reason_and_bounds(code):
    """Кейс 2: чат за пределами — отказ; без причины и с короткой — отказ; с причиной — применяется
    (мутация «причина отступления не проверяется»)."""
    code.write(model_max='"opus"', effort_max='"high"')
    t = code.taskfile()
    with pytest.raises(JobError, match="выше effort_max"):
        code.jobs.plan("proj", t, effort="max", reason="очень нужно, задача сложная")
    with pytest.raises(JobError, match="укажи причину отступления"):
        code.jobs.plan("proj", t, model="opus")
    with pytest.raises(JobError, match="укажи причину отступления"):
        code.jobs.plan("proj", t, model="opus", reason="надо")
    with pytest.raises(JobError, match="укажи причину отступления"):  # шапка без choice: operator — тоже чат
        code.jobs.plan("proj", code.taskfile("O.md", extra="model: opus\n"))
    p = code.jobs.plan("proj", t, model="opus", reason="сложная логика расчёта комиссий")
    assert (p.model, p.choice.source, p.choice.reason) == ("opus", "chat", "сложная логика расчёта комиссий")
    p = code.jobs.plan("proj", code.taskfile("R.md", extra="plan: auto\nchoice_reason: хочу сначала план\n"))
    assert p.choice.plan == "auto" and p.choice.source == "chat"


@pytest.mark.parametrize("reason,ok", [("а" * 14, False), ("а" * 15, True), (" " * 20, False),
                                       ("\u200b" * 15, False), ("  " + "б" * 14 + "  ", False)])
def test_reason_length_boundary(code, reason, ok):
    """Ревью, находка 9: порог 15 символов после удаления непечатаемых и краевых пробелов."""
    if ok:
        assert code.jobs.plan("proj", code.taskfile(), model="opus", reason=reason).choice.source == "chat"
    else:
        with pytest.raises(JobError, match="укажи причину"):
            code.jobs.plan("proj", code.taskfile(), model="opus", reason=reason)


def test_chat_deviation_in_answer_and_journal(code):
    out = _call(code, "code_start", {"project": "proj", "task": code.taskfile(), "model": "opus",
                                     "reason": "сложная логика расчёта комиссий"})
    assert "источник: chat" in out and "чат отступил от политики: сложная логика расчёта комиссий" in out
    jid = json.loads(out)["job"]
    wait_terminal(code.jobs, jid)
    rec = [json.loads(x) for x in (code.state / "audit.jsonl").read_text().splitlines()
           if '"code_start"' in x and jid in x][0]
    assert rec["params"]["model_source"] == "chat" and rec["params"]["reason"] == "сложная логика расчёта комиссий"
    assert rec["params"]["policy_row"] == "task/S/low (встроенная)"
    out = _call(code, "code_start", {"project": "proj", "task": code.taskfile(), "model": "opus"})
    assert out.startswith("отказ:") and "укажи причину" in out


# ---------- решение 10: чат не понижает план ----------

def test_chat_cannot_lower_plan(code, capsys):
    """Решение 10 (мутация «чат понижает план»): no < auto < review; понижает только оператор."""
    big = code.taskfile("L.md", size="L", risk="high")  # строка review
    for kw in ({}, {"reason": "план тут не нужен, правка тривиальная"}):
        with pytest.raises(JobError, match=r"понизить план ниже политики \(строка task/L/high \(встроенная\): "
                                           r"plan review\) может только оператор"):
            code.jobs.plan("proj", big, plan="no", **kw)
    with pytest.raises(JobError, match="может только оператор"):  # шапкой без choice: operator — тоже чат
        code.jobs.plan("proj", code.taskfile("LH.md", size="L", risk="high",
                                             extra="plan: no\nchoice_reason: план тут не нужен, правка простая\n"))
    with pytest.raises(JobError, match="plan auto\\) может только оператор"):  # auto → no (ревью круга, находка 3)
        code.jobs.plan("proj", code.taskfile("ML.md", size="M", risk="low"), plan="no",
                       reason="план тут не нужен, правка тривиальная")
    with pytest.raises(JobError, match="plan review\\) может только оператор"):  # review → auto — тоже понижение
        code.jobs.plan("proj", code.taskfile("MH.md", size="M", risk="high"), plan="auto",
                       reason="хватит плана без остановки")
    p = code.jobs.plan("proj", code.taskfile(), plan="review", reason="затронут расчёт комиссий, нужен план")
    assert (p.choice.plan, p.choice.source) == ("review", "chat")  # поднять — можно, с причиной
    p = code.jobs.plan("proj", big, plan="no", chosen_by="operator")
    assert (p.choice.plan, p.choice.source, p.phase) == ("no", "operator", None)
    p = code.jobs.plan("proj", code.taskfile("LO.md", size="L", risk="high", extra="plan: no\nchoice: operator\n"))
    assert (p.choice.plan, p.choice.source) == ("no", "operator")
    rc = code_cli.code_main("code", ["start", "proj", code.taskfile("LC.md", size="L", risk="high"), "--plan", "no"],
                            code.cfg, code.state)
    assert rc == 0
    jid = capsys.readouterr().out.strip().splitlines()[0]
    wait_terminal(code.jobs, jid)
    assert code.jobs.read(jid)["choice"]["plan"] == "no"
    out = _call(code, "code_start", {"project": "proj", "task": big, "plan": "no",
                                     "reason": "план тут не нужен, правка тривиальная"})
    assert out.startswith("отказ:") and "может только оператор" in out
    # строки политики нет (чат задал все три поля) — понижать не от чего
    c = code_policy.choose(CodeGlobal(), ProjectCode(), "task", None, None,
                           {"model": ("sonnet", "chat"), "effort": ("low", "chat"), "plan": ("no", "chat")},
                           "строки нет, выбираю всё сам")
    assert (c.plan, c.source, c.row) == ("no", "chat", None)
    # строка политики проекта — правило то же
    _with_policy(code, '\n[[workspace.proj.code.policy]]\nprofile = "task"\nsize = "S"\nrisk = "low"\n'
                       'model = "sonnet"\neffort = "low"\nplan = "auto"\nplan_model = "sonnet"\nplan_effort = "low"\n')
    with pytest.raises(JobError, match=r"строка task/S/low \(проекта\): plan auto\) может только оператор"):
        code.jobs.plan("proj", code.taskfile("PS.md"), plan="no", reason="план тут не нужен, правка тривиальная")


def test_resume_refuses_changed_size_risk(code):
    """Ревью круга, находка 1: resume не обходит план правкой size/risk в шапке."""
    t = code.taskfile()
    d = code.jobs.start(code.jobs.plan("proj", t))
    wait_terminal(code.jobs, d["id"])
    code.taskfile(size="L", risk="high")  # тот же файл ТЗ, разметка крупной задачи
    with pytest.raises(JobError, match="size/risk"):
        code.jobs.resume(d["id"], "task")


# ---------- решение 12: канал выбора ----------

def _audit_start(code, jid) -> dict:
    return [json.loads(x) for x in (code.state / "audit.jsonl").read_text().splitlines()
            if '"code_start"' in x and jid in x][0]


def test_choice_channel_operator_and_chat(code, capsys):
    """Решение 12 (мутация «channel не пишется»): канал в job.json, журнале и ответе code_start."""
    def check(jid, source, channel):
        wait_terminal(code.jobs, jid)
        c = code.jobs.read(jid)["choice"]
        rec = _audit_start(code, jid)["params"]
        if channel is None:  # у источника policy поля нет
            assert c["source"] == rec["model_source"] == source and "channel" not in c and "channel" not in rec
        else:
            assert (c["source"], c["channel"]) == (source, channel)
            assert (rec["model_source"], rec["channel"]) == (source, channel)

    out = json.loads(_call(code, "code_start", {"project": "proj", "task": code.taskfile("M1.md"),
                                                "model": "opus", "chosen_by": "operator"}))
    assert "источник: operator (mcp)" in out["choice"]
    check(out["job"], "operator", "mcp")
    assert code_cli.code_main("code", ["start", "proj", code.taskfile("C1.md"), "--model", "opus"],
                              code.cfg, code.state) == 0
    cap = capsys.readouterr()
    assert "источник: operator (cli)" in cap.err
    check(cap.out.strip().splitlines()[0], "operator", "cli")
    out = json.loads(_call(code, "code_start", {"project": "proj", "task": code.taskfile(
        "H1.md", extra="model: opus\nchoice: operator\n")}))
    assert "источник: operator (header)" in out["choice"]
    check(out["job"], "operator", "header")
    out = json.loads(_call(code, "code_start", {"project": "proj", "task": code.taskfile(
        "H2.md", extra="effort: low\nchoice: operator\n"), "model": "opus", "chosen_by": "operator"}))
    check(out["job"], "operator", "mcp+header")
    assert code_cli.code_main("code", ["start", "proj", code.taskfile("C2.md", extra="effort: low\nchoice: operator\n"),
                                       "--model", "opus"], code.cfg, code.state) == 0
    check(capsys.readouterr().out.strip().splitlines()[0], "operator", "cli+header")

    why = "сложная логика расчёта комиссий"
    out = json.loads(_call(code, "code_start", {"project": "proj", "task": code.taskfile("M2.md"),
                                                "model": "opus", "reason": why}))
    assert "источник: chat (mcp)" in out["choice"]
    check(out["job"], "chat", "mcp")
    out = json.loads(_call(code, "code_start", {"project": "proj", "task": code.taskfile(
        "H3.md", extra=f"model: opus\nchoice_reason: {why}\n")}))
    check(out["job"], "chat", "header")
    # флаги терминала — всегда оператор (2.2.1): чат в канале cli невозможен; без флагов из CLI —
    # чат только шапкой; канал — объединение каналов всех полей не из политики (уточнение решения 12)
    t = code.taskfile("C3.md", extra=f"model: opus\nchoice_reason: {why}\n")
    assert code_cli.code_main("code", ["start", "proj", t, "--effort", "low"], code.cfg, code.state) == 0
    check(capsys.readouterr().out.strip().splitlines()[0], "chat", "cli+header")
    out = json.loads(_call(code, "code_start", {"project": "proj", "task": code.taskfile(
        "H4.md", extra="effort: low\n"), "model": "opus", "reason": why}))
    check(out["job"], "chat", "mcp+header")
    out = json.loads(_call(code, "code_start", {"project": "proj", "task": code.taskfile("P1.md")}))
    assert "источник: policy," in out["choice"]
    check(out["job"], "policy", None)
    assert code.jobs.read(out["job"])["choice"]["choice_fields"] == {}
    assert _audit_start(code, out["job"])["params"]["choice_fields"] == {}


def test_mixed_choice_channel_and_fields(code):
    """Уточнение решения 12: оператор понижает план шапкой, чат задаёт модель аргументами — источник chat,
    канал — каналы всех полей не из политики, choice_fields — источник и канал каждого поля."""
    t = code.taskfile("MX.md", size="L", risk="high", extra="plan: no\nchoice: operator\n")
    out = json.loads(_call(code, "code_start", {"project": "proj", "task": t, "model": "opus",
                                                "reason": "нужна модель сильнее для разбора"}))
    assert "источник: chat (mcp+header)" in out["choice"]
    jid = out["job"]
    wait_terminal(code.jobs, jid)
    c = code.jobs.read(jid)["choice"]
    want = {"model": "chat:mcp", "plan": "operator:header"}
    assert (c["source"], c["channel"], c["plan"], c["choice_fields"]) == ("chat", "mcp+header", "no", want)
    rec = _audit_start(code, jid)["params"]
    assert (rec["model_source"], rec["channel"], rec["choice_fields"]) == ("chat", "mcp+header", want)
    # зеркально: аргументы оператора понижают план, поле шапки без choice — чат
    p = code.jobs.plan("proj", code.taskfile("MY.md", size="L", risk="high",
                                             extra="effort: medium\nchoice_reason: дешевле для разбора логов\n"),
                       plan="no", chosen_by="operator")
    assert (p.choice.source, p.choice.channel, p.choice.choice_fields) == \
        ("chat", "mcp+header", {"plan": "operator:mcp", "effort": "chat:header"})


def test_resume_channel(code):
    d = code.jobs.start(code.jobs.plan("proj", code.taskfile(), model="opus", chosen_by="operator", channel="cli"))
    wait_terminal(code.jobs, d["id"])
    r = code.jobs.resume(d["id"], "task")  # без смены — выбор и канал родителя
    assert (r["choice"]["source"], r["choice"]["channel"]) == ("operator", "cli")
    wait_terminal(code.jobs, r["id"])
    assert r["choice"]["choice_fields"] == {"model": "operator:cli"}
    r = code.jobs.resume(d["id"], "task", effort="low", reason="нужно дешевле для мелкой правки")
    assert (r["choice"]["source"], r["choice"]["channel"]) == ("chat", "cli+mcp")
    assert r["choice"]["choice_fields"] == {"model": "operator:cli", "effort": "chat:mcp"}
    wait_terminal(code.jobs, r["id"])
    with pytest.raises(JobError, match="канал выбора"):
        code.jobs.plan("proj", code.taskfile(), channel="header")


def test_policy_answer_line(code):
    out = json.loads(_call(code, "code_start", {"project": "proj", "task": code.taskfile()}))
    assert out["choice"] == ("модель/effort/план: sonnet/medium/no, источник: policy, "
                             "строка политики: task/S/low (встроенная)")
    wait_terminal(code.jobs, out["job"])


# ---------- 3. шапка ----------

def test_header_size_risk_required(code):
    with pytest.raises(JobError, match="нет size, risk.*политика выбирает.*task/M/low"):
        code.jobs.plan("proj", code.taskfile(size=None, risk=None))
    with pytest.raises(JobError, match="нет risk"):
        code.jobs.plan("proj", code.taskfile(risk=None))
    with pytest.raises(JobError, match="нет size"):
        code.jobs.plan("proj", code.taskfile(profile="recon", size=None, risk=None))
    assert code.jobs.plan("proj", code.taskfile(profile="recon", risk=None)).choice.row.startswith("recon/S/*")
    assert code.jobs.plan("proj", code.taskfile(profile="review", size=None, risk=None)).model == "opus"


@pytest.mark.parametrize("line,key", [("size: XL", "size"), ("risk: medium", "risk"), ("plan: maybe", "plan"),
                                      ("choice: boss", "choice")])
def test_header_bad_values(line, key):
    with pytest.raises(taskfile.TaskError, match=key):
        taskfile.parse_header(f"---\nticket: P\ndir: .\nprofile: task\n{line}\n---\n")


# ---------- 4. план auto ----------

def test_plan_auto_chain(code, monkeypatch):
    """Кейс 4/7/11: одна code_start → фаза плана (plan, opus, доля бюджета) → plan.md → дочерняя фаза
    исполнения (--resume, dontAsk, модель исполнения, остаток бюджета и ходов); цепочка в снимке корня."""
    monkeypatch.setenv("FAKE_RECLOG", str(code.state / "reclog.jsonl"))
    monkeypatch.setenv("FAKE_COST_PLAN", "1.2")
    monkeypatch.setenv("FAKE_COST", "0.5")
    code.write(plan_max_turns="40")  # лимит фазы плана по умолчанию не задан (WSH-4.7) — здесь он явный
    p = code.jobs.plan("proj", code.taskfile(size="M", risk="low"))
    assert (p.phase, p.plan_mode, p.model, p.budget_usd, p.max_turns) == ("plan", "auto", "opus", 3.0, 40)
    root = code.jobs.start(p)["id"]
    snap = _wait_state(code.jobs, root, ("completed", "failed", "plan_ready"))
    assert snap["state"] == "completed", snap
    chain = snap["chain"]
    assert [(x["phase"], x["state"]) for x in chain] == [("plan", "completed"), ("execute", "completed")]
    plan_d, exe_d = code.jobs.read(chain[0]["id"]), code.jobs.read(chain[1]["id"])
    assert plan_d["reason"] == "auto" and plan_d["next"] == exe_d["id"] and exe_d["parent"] == plan_d["id"]
    assert (code.jobs.job_dir(root) / "plan.md").read_text().startswith("# План")
    runs = _reclog(code)
    a, b = runs[0]["argv"], runs[1]["argv"]
    assert _opt(a, "--permission-mode") == "plan" and _opt(a, "--model") == "opus" and "--resume" not in a
    assert _opt(a, "--max-budget-usd") == "3" and _opt(a, "--name") == "proj/PAY-1/plan"
    sid = _opt(a, "--session-id")
    assert _opt(b, "--resume") == sid and "--session-id" not in b
    assert _opt(b, "--permission-mode") == "dontAsk" and (_opt(b, "--model"), _opt(b, "--effort")) == ("sonnet", "high")
    assert _opt(b, "--max-budget-usd") == "8.8" and _opt(b, "--max-turns") == "98"  # остаток: 10−1.2, 100−2
    assert _opt(b, "--name") == "proj/PAY-1"
    # стоимость звеньев — разностью накопительного total_cost_usd (решение 6)
    assert exe_d["result"]["total_cost_usd"] == pytest.approx(1.7)
    assert exe_d["result"]["phase_cost_usd"] == pytest.approx(0.5)
    assert snap["cost_usd"] == pytest.approx(1.7)
    res = json.loads(_call(code, "code_result", {"job": root}))
    assert res["state"] == "completed" and len(res["chain"]) == 2 and "cost_usd" not in res  # на подписке без сумм
    assert res["open_in_terminal"] == f"cd {code.root} && claude --resume {sid}"


def test_plan_and_execute_settings_files(code):
    """Кейс 8: settings фазы плана — вариант plan, исполнения — эталон профиля."""
    # review + approve из этого процесса: оба звена собраны одним Hub (у надзирателя тестов другой HOME)
    root = code.jobs.start(code.jobs.plan("proj", code.taskfile(size="L", risk="low")))["id"]
    _wait_state(code.jobs, root, ("plan_ready",))
    code.jobs.approve(root)
    snap = _wait_state(code.jobs, root, ("completed", "failed"))
    sp = json.loads((code.jobs.job_dir(snap["chain"][0]["id"]) / "settings.json").read_text())
    se = json.loads((code.jobs.job_dir(snap["chain"][1]["id"]) / "settings.json").read_text())
    assert sp["useAutoModeDuringPlan"] is False and sp["permissions"]["disableAutoMode"] == "disable"
    assert sp["sandbox"]["filesystem"]["denyWrite"] == [str(code.root / g) for g in claude_cli.GIT_PROTECTED]
    assert not any(a.startswith("Edit") for a in sp["permissions"]["allow"])
    assert "useAutoModeDuringPlan" not in se and "Edit(./**)" in se["permissions"]["allow"]
    # хук тот же, профиль у фазы плана — plan (ТЗ WSH-3.4, А4.3)
    assert json.dumps(sp["hooks"]).replace("--profile plan", "--profile task") == json.dumps(se["hooks"])
    assert sp["permissions"]["deny"] == se["permissions"]["deny"]
    # argv обеих фаз из задачи: план — новая сессия в plan, исполнение — --resume той же сессии в dontAsk
    ap = json.loads((code.jobs.job_dir(snap["chain"][0]["id"]) / "argv.json").read_text())
    ae = json.loads((code.jobs.job_dir(snap["chain"][1]["id"]) / "argv.json").read_text())
    assert _opt(ap, "--permission-mode") == "plan" and _opt(ap, "--session-id") and "--resume" not in ap
    assert _opt(ae, "--permission-mode") == "dontAsk" and _opt(ae, "--resume") == _opt(ap, "--session-id")
    assert "--session-id" not in ae


# ---------- 5. план review ----------

def test_plan_review_stops_then_approve(code, monkeypatch):
    """Кейс 5: остановка в plan_ready; без code_approve исполнения нет; code_plan; code_approve доводит."""
    monkeypatch.setenv("FAKE_PLAN", "# План ревью\n1. шаг\n")
    root = code.jobs.start(code.jobs.plan("proj", code.taskfile(size="L", risk="low")))["id"]
    snap = _wait_state(code.jobs, root, ("plan_ready", "failed", "completed"))
    assert snap["state"] == "plan_ready" and snap["phase"] == "plan"
    time.sleep(1.5)  # надзиратель плана завершился — исполнение само не стартовало
    assert len(code.jobs.chain(root)) == 1 and code.jobs.snapshot(root)["state"] == "plan_ready"
    out = _call(code, "code_plan", {"job": root})
    assert "# План ревью" in out and "plan.md" in out
    with pytest.raises(JobError, match="фаза плана"):
        code.jobs.resume(root, "task")  # resume плана — не путь к исполнению
    out = json.loads(_call(code, "code_approve", {"job": root}))
    assert out["phase"] == "execute" and out["model"] == "opus"
    snap = _wait_state(code.jobs, root, ("completed", "failed"))
    assert snap["state"] == "completed" and [x["reason"] for x in snap["chain"]][0] == "approved"
    assert _call(code, "code_approve", {"job": root}).startswith("отказ:")  # второй раз — нет


def test_code_wait_returns_on_plan_ready(code, monkeypatch):
    monkeypatch.setenv("FAKE_SLEEP_PLAN", "1")
    root = code.jobs.start(code.jobs.plan("proj", code.taskfile(size="L", risk="low")))["id"]
    t0 = time.time()
    for _ in range(6):
        snap = json.loads(_call(code, "code_wait", {"job": root, "seconds": 30}))
        if snap["state"] == "plan_ready":
            break
    assert snap["state"] == "plan_ready" and time.time() - t0 < 20


def test_replan(code, monkeypatch):
    monkeypatch.setenv("FAKE_RECLOG", str(code.state / "reclog.jsonl"))
    root = code.jobs.start(code.jobs.plan("proj", code.taskfile(size="L", risk="low")))["id"]
    _wait_state(code.jobs, root, ("plan_ready",))
    assert _call(code, "code_replan", {"job": root, "note": ""}).startswith("отказ:")
    out = json.loads(_call(code, "code_replan", {"job": root, "note": "раздели миграцию на две"}))
    assert out["phase"] == "plan"
    snap = _wait_state(code.jobs, root, ("plan_ready",))
    assert snap["active"] == out["job"] and [x["reason"] for x in snap["chain"]][0] == "replanned"
    a = _reclog(code)[-1]["argv"]
    assert _opt(a, "--permission-mode") == "plan" and _opt(a, "--resume") == code.jobs.read(root)["session_id"]
    assert "раздели миграцию на две" in _opt(a, "-p")
    assert code.jobs.plan_text(root)[1].parent.name == out["job"]  # code_plan по корню — последний план
    # звено переделки — с settings фазы плана, а не исполнения (ревью, находка 5)
    sr = json.loads((code.jobs.job_dir(out["job"]) / "settings.json").read_text())
    assert sr["useAutoModeDuringPlan"] is False and sr["sandbox"]["filesystem"]["denyWrite"] == \
        [str(code.root / g) for g in claude_cli.GIT_PROTECTED]
    assert "Edit(./**)" not in sr["permissions"]["allow"]
    assert _call(code, "code_replan", {"job": root, "note": "   "}).startswith("отказ:")  # одни пробелы
    child = code.jobs.approve(root)  # по корню — активное звено (ревью, находка 8)
    assert child["parent"] == out["job"] and child["phase"] == "execute"
    assert _wait_state(code.jobs, root, ("completed", "failed"))["state"] == "completed"


# ---------- 6. нет плана, нет шаблона ----------

def test_no_plan_fails_without_execution(code, monkeypatch):
    monkeypatch.setenv("FAKE_NO_PLAN", "1")
    root = code.jobs.start(code.jobs.plan("proj", code.taskfile(size="M", risk="low")))["id"]
    snap = _wait_state(code.jobs, root, ("failed", "completed", "plan_ready"))
    assert (snap["state"], code.jobs.read(root)["reason"]) == ("failed", "no_plan")
    hint = "план ищется только в ~/.claude/plans/: настройка plansDirectory в .claude/settings*.json проекта " \
           "не поддерживается"
    assert snap["reason_hint"] == hint and code.jobs.result_view(root)["reason_hint"] == hint  # решение 13
    time.sleep(0.5)
    assert len(code.jobs.chain(root)) == 1


def test_no_execute_template_refused_before_start(code):
    (code.prompts / "execute.md").unlink()
    with pytest.raises(JobError, match="шаблон промпта исполнения не найден"):
        code.jobs.plan("proj", code.taskfile(size="M", risk="low"))
    assert code.jobs.plan("proj", code.taskfile()).phase is None  # без плана шаблон не нужен
    (code.prompts / "execute.md").write_text("Исполни {ticket}: {note}\n")  # auto: {note} некому передать
    with pytest.raises(taskfile.TaskError, match="note"):
        code.jobs.plan("proj", code.taskfile(size="M", risk="low"))


def test_plan_source_only_inside_plans_dir(tmp_path):
    plans = tmp_path / ".claude" / "plans"
    plans.mkdir(parents=True)
    good = plans / "ok.md"
    good.write_text("план")
    (tmp_path / "evil.md").write_text("чужое")
    (plans / "link.md").symlink_to(tmp_path / "evil.md")

    def events(*uses):
        """uses: (путь, инструмент, is_error) — tool_use с id и парный tool_result, как в потоке claude."""
        f = tmp_path / "events.jsonl"
        lines = []
        for i, (p, tool, err) in enumerate(uses):
            lines.append(json.dumps({"type": "assistant", "message": {"content": [
                {"type": "tool_use", "id": f"t{i}", "name": tool, "input": {"file_path": str(p), "content": "x"}}]}}))
            lines.append(json.dumps({"type": "user", "message": {"content": [
                {"type": "tool_result", "tool_use_id": f"t{i}", "is_error": err}]}}))
        f.write_text("\n".join(lines))
        return f
    assert _plan_source(events((good, "Write", None)), plans) == good.resolve()
    assert _plan_source(events((good, "Edit", False)), plans) == good.resolve()  # переделка тем же файлом
    for bad in (tmp_path / "evil.md", plans / ".." / ".." / "evil.md", plans / "link.md", "rel/plan.md"):
        assert _plan_source(events((bad, "Write", None)), plans) is None, bad
    assert _plan_source(events((good, "Write", None), (tmp_path / "evil.md", "Write", None)), plans) == good.resolve()
    assert _plan_source(events((good, "Write", True)), plans) is None  # вызов с ошибкой — не план
    assert _plan_source(events((good, "Read", None)), plans) is None
    f = events((good, "Write", None))
    os.utime(good, (time.time() - 3600, time.time() - 3600))
    assert _plan_source(f, plans, since=time.time()) is None  # файл старше звена — прошлый или чужой план
    no_result = tmp_path / "e2.jsonl"
    no_result.write_text(json.dumps({"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": "z", "name": "Write", "input": {"file_path": str(good)}}]}}))
    assert _plan_source(no_result, plans) is None  # без парного tool_result


def test_cancel_after_plan_exit_is_not_lost(code, monkeypatch):
    """Ревью, находка 1: отмена между выходом claude и итогом фазы плана не теряется — ни plan_ready,
    ни авто-исполнения."""
    monkeypatch.setenv("FAKE_SELF_CANCEL", "1")
    root = code.jobs.start(code.jobs.plan("proj", code.taskfile(size="M", risk="low")))["id"]
    snap = _wait_state(code.jobs, root, ("cancelled", "plan_ready", "completed", "failed"))
    assert snap["state"] == "cancelled"
    time.sleep(1)
    assert len(code.jobs.chain(root)) == 1


def test_approve_refused_when_cancel_marked(code):
    root = code.jobs.start(code.jobs.plan("proj", code.taskfile(size="L", risk="low")))["id"]
    _wait_state(code.jobs, root, ("plan_ready",))
    (code.jobs.job_dir(root) / "cancel").write_text("1")
    with pytest.raises(JobError, match="отменён"):
        code.jobs.approve(root)


def test_replan_template_checks(code):
    (code.prompts / "replan.md").unlink()
    with pytest.raises(JobError, match="переделки плана не найден"):
        code.jobs.plan("proj", code.taskfile(size="L", risk="low"))  # review — шаблон нужен на старте
    assert code.jobs.plan("proj", code.taskfile(size="M", risk="low")).plan_mode == "auto"  # auto — не нужен
    (code.prompts / "replan.md").write_text("Переделай план {ticket}\n")
    with pytest.raises(JobError, match="нет \\{note\\}"):
        code.jobs.plan("proj", code.taskfile(size="L", risk="low"))


# ---------- 7. бюджет ----------

def test_daily_budget_counts_each_phase_once(code, monkeypatch):
    """Решение 6: дневной бюджет и code_stats считают каждую фазу один раз. Цепочка стоит 1.0
    (план 0.5 + исполнение 0.5, total_cost_usd исполнения — накопительный 1.0)."""
    code.write(max_parallel="3", max_budget_usd="2", daily_budget_usd="3.2")
    monkeypatch.setenv("FAKE_COST_PLAN", "0.5")
    monkeypatch.setenv("FAKE_COST", "0.5")
    root = code.jobs.start(code.jobs.plan("proj", code.taskfile("A.md", size="M", risk="low"), budget_usd=2))["id"]
    assert _wait_state(code.jobs, root, ("completed", "failed"))["state"] == "completed"
    # по фазам: 1.0 + 2 = 3.0 ≤ 3.2; двойной счёт дал бы 0.5 + 1.0 + 2 = 3.5 > 3.2
    d = code.jobs.start(code.jobs.plan("proj", code.taskfile("B.md"), budget_usd=2))
    wait_terminal(code.jobs, d["id"])
    row = [r for r in code.jobs.stats("proj") if r["row"].startswith("task/M/low")][0]
    assert row["tasks"] == 1 and row["avg_cost_usd"] == pytest.approx(1.0)


def test_execute_budget_is_remainder_and_daily_checked_on_approve(code, monkeypatch):
    code.write(max_parallel="3", max_budget_usd="4", daily_budget_usd="4.5")
    monkeypatch.setenv("FAKE_COST_PLAN", "1")
    root = code.jobs.start(code.jobs.plan("proj", code.taskfile(size="L", risk="low"), budget_usd=4))["id"]
    _wait_state(code.jobs, root, ("plan_ready",))
    # день: план 1 + исполнение 3 = 4 ≤ 4.5; если бы исполнение получало полный бюджет 4 — 5 > 4.5, отказ
    child = code.jobs.approve(root)
    assert child["budget_usd"] == 3
    wait_terminal(code.jobs, child["id"])


def test_daily_budget_refuses_execute_on_approve(code, monkeypatch):
    """Ревью, находка 6: при переходе к исполнению дневной бюджет проверяется (план съел часть дня)."""
    code.write(max_parallel="3", max_budget_usd="4", daily_budget_usd="3.5")
    monkeypatch.setenv("FAKE_COST_PLAN", "1")
    root = code.jobs.start(code.jobs.plan("proj", code.taskfile(size="L", risk="low"), budget_usd=3))["id"]
    _wait_state(code.jobs, root, ("plan_ready",))
    code.write(max_parallel="3", max_budget_usd="4", daily_budget_usd="2.5")  # план 1 + остаток 2 = 3 > 2.5
    with pytest.raises(JobError, match="дневной бюджет"):
        code.jobs.approve(root)
    code.write(max_parallel="3", max_budget_usd="4", daily_budget_usd="3.5", profiles='["recon", "review"]')
    with pytest.raises(JobError, match="профиль task больше не разрешён"):
        code.jobs.approve(root)


def test_max_parallel_checked_on_auto_transition(code, monkeypatch):
    """Кейс 7: max_parallel проверяется и при автоматическом переходе; не вышло — plan_ready с причиной."""
    code.write(max_parallel="2")
    monkeypatch.setenv("FAKE_SLEEP_PLAN", "2")
    root = code.jobs.start(code.jobs.plan("proj", code.taskfile("A.md", size="M", risk="low")))["id"]
    monkeypatch.setenv("FAKE_SLEEP", "30")
    other = code.jobs.start(code.jobs.plan("proj", code.taskfile("B.md")))["id"]
    code.write(max_parallel="1")  # к переходу живая — только B, а лимит 1
    snap = _wait_state(code.jobs, root, ("plan_ready", "completed", "failed"))
    assert snap["state"] == "plan_ready" and "max_parallel" in (code.jobs.read(root).get("chain_error") or "")
    assert len(code.jobs.chain(root)) == 1
    code.jobs.cancel(other)
    wait_terminal(code.jobs, other, timeout=60)
    monkeypatch.setenv("FAKE_SLEEP", "0")
    code.jobs.approve(root)  # человек доводит
    assert _wait_state(code.jobs, root, ("completed", "failed"))["state"] == "completed"
    _assert_no_fake()


# ---------- 8. эталон settings и argv ----------

@pytest.mark.parametrize("profile", ["recon", "task", "review"])
def test_plan_settings_golden(profile):
    """Кейс 8 и решение 5: settings фазы плана — эталон целиком (мутации «denyWrite — весь рабочий каталог»
    (ТЗ WSH-3.1) и «нет useAutoModeDuringPlan: false» краснят)."""
    from test_code import _golden_settings
    s = claude_cli.settings(profile, "org/TASK.md", CodeGlobal(),
                            ProjectCode(enabled=True, dirs=(".",), prompts="p", deny_edit=("docker-compose*.yml",)),
                            [Path("/home/u/.config/wshub"), Path("/home/u/.local/state/wshub")],
                            [Path("/home/u/.config/wshub"), Path("/home/u/.local/state/wshub")],
                            ["/py", "-m", "wshub.hook"], Path("/home/u"), Path("/home/u/proj"), phase="plan")
    assert s == GOLDEN["plan"][profile]
    # отличие от эталона профиля — ровно четыре пункта решения 5
    base = _golden_settings(profile)
    assert {k: v for k, v in s.items() if k != "useAutoModeDuringPlan"}.keys() == base.keys()
    assert s["useAutoModeDuringPlan"] is False
    assert {k: v for k, v in s["permissions"].items() if k not in ("allow", "disableAutoMode")} == \
        {k: v for k, v in base["permissions"].items() if k != "allow"}
    assert {k: v for k, v in s["sandbox"].items() if k not in ("filesystem", "excludedCommands")} == \
        {k: v for k, v in base["sandbox"].items() if k not in ("filesystem", "excludedCommands")}
    assert {k: v for k, v in s["sandbox"]["filesystem"].items() if k != "denyWrite"} == \
        {k: v for k, v in base["sandbox"]["filesystem"].items() if k != "denyWrite"}
    # хук — тот же, профиль фазы плана — plan (ТЗ WSH-3.4, А4.3)
    assert json.dumps(s["hooks"]).replace("--profile plan", f"--profile {profile}") == json.dumps(base["hooks"])


def test_plan_settings_clear_project_bash_and_exclusions():
    proj = ProjectCode(bash_allow=("docker compose *",), sandbox_exclude=("docker",))
    s = claude_cli.settings("task", "T.md", CodeGlobal(), proj, [], [], ["/w"], Path("/h"), Path("/d"), phase="plan")
    assert "Bash(docker compose *)" not in s["permissions"]["allow"] and s["sandbox"]["excludedCommands"] == []
    full = claude_cli.settings("task", "T.md", CodeGlobal(), proj, [], [], ["/w"], Path("/h"), Path("/d"))
    assert "Bash(docker compose *)" in full["permissions"]["allow"]  # исполнение — как в WSH-2


@pytest.mark.parametrize("profile", ["recon", "review"])
def test_reading_profiles_clear_project_exclusions(profile):
    """Решение 9.2 (правка 2026-10-14): у recon и review excludedCommands пуст и при sandbox_exclude проекта;
    у task — как в WSH-2 (мутация «recon с excludedCommands проекта»). denyWrite у всех — исполняемая часть git,
    а не весь каталог: иначе bwrap не монтирует защищённый <dir>/.gitconfig (ТЗ WSH-3.1, мутация «recon
    с denyWrite: [<dir>]»)."""
    proj = ProjectCode(sandbox_exclude=("docker",))
    git_protected = ["/d/.git/config", "/d/.git/hooks", "/d/.git/info", "/d/.git/modules"]
    s = claude_cli.settings(profile, "T.md", CodeGlobal(), proj, [], [], ["/w"], Path("/h"), Path("/d"))
    assert s["sandbox"]["excludedCommands"] == [] and s["sandbox"]["filesystem"]["denyWrite"] == git_protected
    sp = claude_cli.settings(profile, "T.md", CodeGlobal(), proj, [], [], ["/w"], Path("/h"), Path("/d"),
                             phase="plan")
    assert sp["sandbox"]["excludedCommands"] == [] and sp["sandbox"]["filesystem"]["denyWrite"] == git_protected
    t = claude_cli.settings("task", "T.md", CodeGlobal(), proj, [], [], ["/w"], Path("/h"), Path("/d"))
    assert t["sandbox"]["excludedCommands"] == ["docker"] and t["sandbox"]["filesystem"]["denyWrite"] == git_protected


def test_argv_plan_execute_golden():
    flags = {"permission_prompts": True, "setting_sources": True}
    a = claude_cli.argv("/claude", "PROMPT", job_dir=Path("/j"), model="opus", effort="high", max_turns=40,
                        budget_usd=1.5, flags=flags, session_id="SID", permission_mode="plan", name="proj/PAY-1/plan")
    b = claude_cli.argv("/claude", "PROMPT", job_dir=Path("/j"), model="sonnet", effort="high", max_turns=60,
                        budget_usd=3.5, flags=flags, resume="SID", name="proj/PAY-1")
    assert a == GOLDEN["argv_plan"] and b == GOLDEN["argv_execute"]
    with pytest.raises(claude_cli.CliError):
        claude_cli.argv("/c", "p", job_dir=Path("/j"), model="m", effort="e", max_turns=1, budget_usd=1, flags={},
                        session_id="S", permission_mode="bypassPermissions")


# ---------- 9. отмена цепочки ----------

def test_cancel_root_stops_active_execute(code, monkeypatch):
    monkeypatch.setenv("FAKE_SLEEP", "30")
    monkeypatch.setenv("FAKE_SLEEP_PLAN", "0")
    monkeypatch.setenv("FAKE_SIGINT", "exit")
    root = code.jobs.start(code.jobs.plan("proj", code.taskfile(size="M", risk="low")))["id"]
    end = time.time() + 20
    while time.time() < end and not (code.jobs.snapshot(root).get("phase") == "execute"
                                     and code.jobs.snapshot(root)["state"] == "running"):
        time.sleep(0.1)
    code.jobs.cancel(root)
    snap = _wait_state(code.jobs, root, ("cancelled",), timeout=60)
    assert [x["state"] for x in snap["chain"]] == ["completed", "cancelled"]
    _assert_no_fake()


def test_cancel_during_plan_ready_cancels_chain(code):
    root = code.jobs.start(code.jobs.plan("proj", code.taskfile(size="L", risk="low")))["id"]
    _wait_state(code.jobs, root, ("plan_ready",))
    assert code.jobs.cancel(root)["state"] == "cancelled"
    with pytest.raises(JobError, match="plan_ready"):
        code.jobs.approve(root)


def test_cancel_root_during_plan_phase(code, monkeypatch):
    monkeypatch.setenv("FAKE_SLEEP_PLAN", "30")
    monkeypatch.setenv("FAKE_SIGINT", "exit")
    root = code.jobs.start(code.jobs.plan("proj", code.taskfile(size="M", risk="low")))["id"]
    _wait_running(code.jobs, root)
    code.jobs.cancel(root)
    assert _wait_state(code.jobs, root, ("cancelled",), timeout=60)["state"] == "cancelled"
    time.sleep(0.5)
    assert len(code.jobs.chain(root)) == 1
    _assert_no_fake()


# ---------- 10. code_stats ----------

def _job(code, jid, *, root=None, phase=None, state="completed", reason=None, choice=None, cost=None, turns=0,
         ts=None):
    d = {"id": jid, "project": "proj", "state": state, "reason": reason, "phase": phase, "chain_root": root or jid,
         "created_ts": ts or time.time(), "choice": choice,
         "result": {"total_cost_usd": cost, "phase_cost_usd": cost, "num_turns": turns}}
    jd = code.jobs.job_dir(jid)
    jd.mkdir(parents=True)
    (jd / "job.json").write_text(json.dumps(d))


def test_code_stats_aggregates(code, capsys):
    pol = {"row": "task/M/low (встроенная)", "source": "policy"}
    _job(code, "a1", phase="plan", reason="auto", choice=pol, cost=1.0, turns=3)
    _job(code, "a2", root="a1", phase="execute", cost=2.0, turns=7, ts=time.time() + 1)
    _job(code, "b1", phase="plan", reason="replanned", choice=pol, cost=1.0, turns=2)
    _job(code, "b2", root="b1", phase="plan", state="cancelled", reason="cancel", cost=0.5, turns=2,
         ts=time.time() + 1)
    _job(code, "c1", choice={"row": "task/S/low (встроенная)", "source": "chat"}, cost=0.4, turns=4)
    _job(code, "old", choice=pol, cost=9.0, ts=time.time() - 40 * 86400)  # вне окна
    rows = {(r["row"], r["source"]): r for r in code.jobs.stats("proj", 30)}
    m = rows[("task/M/low (встроенная)", "policy")]
    assert (m["tasks"], m["avg_cost_usd"], m["avg_turns"], m["completed_share"], m["replan_share"]) == \
        (2, 2.25, 7.0, 0.5, pytest.approx(1 / 3, abs=1e-3))
    assert rows[("task/S/low (встроенная)", "chat")]["replan_share"] is None
    out = json.loads(_call(code, "code_stats", {"project": "proj"}))
    assert len(out) == 2 and all("avg_cost_usd" not in r for r in out)  # средняя цена на подписке не показывается
    assert code_cli.code_main("code", ["stats", "proj", "--days", "30"], code.cfg, code.state) == 0
    text = capsys.readouterr().out
    assert "task/M/low (встроенная)" in text and "policy" in text
    assert re.search(r"\s7\.0\s", text)  # средние ходы остались
    assert "2.25" not in text and "$" not in text


def test_code_stats_by_channel(code, capsys):
    """Решение 12: operator/cli и operator/mcp — отдельные строки code_stats."""
    row = "task/S/low (встроенная)"
    _job(code, "o1", choice={"row": row, "source": "operator", "channel": "cli"}, cost=1.0, turns=1)
    _job(code, "o2", choice={"row": row, "source": "operator", "channel": "mcp"}, cost=3.0, turns=1)
    _job(code, "o3", choice={"row": row, "source": "operator", "channel": "mcp"}, cost=5.0, turns=1)
    _job(code, "p1", choice={"row": row, "source": "policy"}, cost=2.0, turns=1)
    rows = {(r["source"], r["channel"]): r for r in code.jobs.stats("proj", 30)}
    assert set(rows) == {("operator", "cli"), ("operator", "mcp"), ("policy", "—")}
    assert (rows[("operator", "cli")]["tasks"], rows[("operator", "mcp")]["tasks"]) == (1, 2)
    assert rows[("operator", "mcp")]["avg_cost_usd"] == 4.0
    assert code_cli.code_main("code", ["stats", "proj"], code.cfg, code.state) == 0
    lines = capsys.readouterr().out.splitlines()
    assert "канал" in lines[0]
    assert any("operator" in x and " cli " in x for x in lines) and any("operator" in x and " mcp " in x for x in lines)


def test_phase_cost_skips_parent_without_result(code):
    """Ревью, находка 4: звено без итога (lost/cancelled) не ломает разность — берётся ближайший предок той
    же сессии с числовым total_cost_usd."""
    from wshub.runner import Supervisor
    _job(code, "p1", phase="plan", cost=1.0)
    for jid, parent in (("x1", "p1"), ("r1", "x1")):
        _job(code, jid, root="p1", phase="execute", state="lost", cost=None)
        d = json.loads((code.jobs.job_dir(jid) / "job.json").read_text())
        d.update(parent=parent, resume="S", session_id="S", result=None)
        (code.jobs.job_dir(jid) / "job.json").write_text(json.dumps(d))
    d = json.loads((code.jobs.job_dir("p1") / "job.json").read_text())
    d["session_id"] = "S"
    (code.jobs.job_dir("p1") / "job.json").write_text(json.dumps(d))
    assert Supervisor(code.hub, "r1")._phase_cost(1.7) == pytest.approx(0.7)


def test_dead_auto_supervisor_shows_plan_ready(code):
    _job(code, "q1", phase="plan", state="plan_ready")
    d = json.loads((code.jobs.job_dir("q1") / "job.json").read_text())
    d.update(plan_mode="auto", sup_pid=2 ** 22 + 7, sup_start="x")
    (code.jobs.job_dir("q1") / "job.json").write_text(json.dumps(d))
    assert code.jobs.snapshot("q1")["state"] == "plan_ready"  # не вечный «starting»


def test_cli_result_has_open_line(code, capsys):
    d = code.jobs.start(code.jobs.plan("proj", code.taskfile()))
    wait_terminal(code.jobs, d["id"])
    assert code_cli.code_main("code", ["result", d["id"]], code.cfg, code.state) == 0
    assert f"открыть сессию: cd {code.root} && claude --resume {d['session_id']}" in capsys.readouterr().out


# ---------- прочее: projects, resume, временный каталог ----------

def test_code_projects_shows_policy(code):
    out = json.loads(_call(code, "code_projects", {}))[0]
    assert any(r["profile"] == "task" and r["size"] == "M" and r["plan"] == "auto" for r in out["policy"])
    assert out["plan_budget_share"] == 0.3 and out["execute_prompt"] == "execute"


def test_resume_model_change_follows_source_rules(code):
    d = code.jobs.start(code.jobs.plan("proj", code.taskfile()))
    wait_terminal(code.jobs, d["id"])
    with pytest.raises(JobError, match="укажи причину"):
        code.jobs.resume(d["id"], "task", model="opus")
    r = code.jobs.resume(d["id"], "task")  # без смены — выбор родителя
    assert r["model"] == "sonnet"
    wait_terminal(code.jobs, r["id"])
    code.write(model_max='"sonnet"')
    with pytest.raises(JobError, match="выше model_max"):  # чат с причиной — всё равно в пределах потолка
        code.jobs.resume(d["id"], "task", model="opus", reason="нужна модель сильнее для отладки")
    r = code.jobs.resume(d["id"], "task", model="opus", chosen_by="operator")
    assert r["model"] == "opus" and r["choice"]["source"] == "operator"
    wait_terminal(code.jobs, r["id"])


def test_registry_plan_keys(code):
    code.write(plan_budget_share="0.5", plan_max_turns="10", execute_prompt='"run_plan"')
    pc = parse(code.cfg.read_text()).code.projects["proj"]
    assert (pc.plan_budget_share, pc.plan_max_turns, pc.execute_prompt) == (0.5, 10, "run_plan")
    for k, v in (("plan_budget_share", "1.5"), ("plan_max_turns", "0"), ("execute_prompt", '"../x"')):
        code.write(**{k: v})
        assert "proj" in parse(code.cfg.read_text()).code.project_errors, k
    with pytest.raises(JobError, match="execute_prompt"):  # ошибка в [workspace.proj.code] — отказ wshub-code
        code.jobs._project("proj")


def test_tmpdir_problem(tmp_path, monkeypatch):
    monkeypatch.setattr(claude_cli, "_uid", lambda: 4242)
    assert claude_cli.tmpdir_problem({"CLAUDE_CODE_TMPDIR": str(tmp_path)}) is None  # каталога нет
    (tmp_path / "claude-4242").mkdir()
    msg = claude_cli.tmpdir_problem({"CLAUDE_CODE_TMPDIR": str(tmp_path)})
    assert msg and "claude-4242" in msg and "sudo rm -rf" in msg  # принадлежит не 4242
    monkeypatch.setattr(claude_cli, "_uid", os.getuid)
    (tmp_path / f"claude-{os.getuid()}").mkdir()
    assert claude_cli.tmpdir_problem({"CLAUDE_CODE_TMPDIR": str(tmp_path)}) is None  # наш


def test_start_and_doctor_refuse_foreign_tmpdir(code, tmp_path, monkeypatch):
    from wshub import doctor
    monkeypatch.setattr(claude_cli, "TMP_DEFAULT", str(tmp_path))
    monkeypatch.setattr(claude_cli, "_uid", lambda: 4242)
    (tmp_path / "claude-4242").mkdir()
    with pytest.raises(JobError, match="временный каталог claude"):
        code.jobs.start(code.jobs.plan("proj", code.taskfile()))
    ctx = doctor.Ctx(config=code.cfg, state=code.state, win_users=code.state / "none")
    c = doctor.check_code(ctx, code.hub.registry.get(), code.hub)
    assert c["status"] == "fail" and any("временный каталог claude" in x for x in c["detail"])

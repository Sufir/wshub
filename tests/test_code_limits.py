"""wshub-code, ТЗ WSH-4.7: на подписке лимитов по умолчанию нет — они действуют, только если оператор записал их
в реестр; суммы в долларах не показываются. Настоящий claude не вызывается — tests/fake_claude.py."""
from __future__ import annotations

import json
import os
import time
from importlib import resources

import pytest
from test_code import _wait_running, make_code, wait_terminal
from test_code_plan import _call, _opt, _reclog, _wait_state

from wshub import code_cli, code_server
from wshub.jobs import LIMIT_IGNORED, JobError, Jobs, Limits, effective_limits
from wshub.registry import ProjectCode, parse
from wshub.runner import hide_cost, over_time

NO_LIMITS = dict(max_turns=None, max_budget_usd=None, daily_budget_usd=None, max_hours=None)
REASON = "затронут расчёт комиссий, нужен план"


@pytest.fixture
def code(tmp_path, monkeypatch):
    return make_code(tmp_path, monkeypatch)


def _argv(code, jid) -> list[str]:
    return json.loads((code.jobs.job_dir(jid) / "argv.json").read_text(encoding="utf-8"))


# ---------- 2.2 реестр ----------

def test_registry_limits_default_to_none(code):
    pc = ProjectCode()
    assert (pc.max_turns, pc.max_budget_usd, pc.daily_budget_usd, pc.max_hours, pc.plan_max_turns) == (None,) * 5
    assert pc.plan_budget_share == 0.3 and pc.max_parallel == 1  # не лимиты расхода — не тронуты
    code.write(**NO_LIMITS)
    pc = parse(code.cfg.read_text()).code.projects["proj"]
    assert (pc.max_turns, pc.max_budget_usd, pc.daily_budget_usd, pc.max_hours, pc.plan_max_turns) == (None,) * 5


def test_registry_explicit_limits_kept_and_validated(code):
    code.write(plan_max_turns="9")
    pc = parse(code.cfg.read_text()).code.projects["proj"]
    assert (pc.max_turns, pc.max_budget_usd, pc.daily_budget_usd, pc.max_hours, pc.plan_max_turns) == \
        (100, 10, 30, 4, 9)
    code.write(**{**NO_LIMITS, "max_budget_usd": "-1"})
    assert "proj" in parse(code.cfg.read_text()).code.project_errors


def test_code_projects_shows_unset_limits_as_no(code):
    code.write(**NO_LIMITS)
    out = json.loads(_call(code, "code_projects", {}))[0]
    assert (out["max_turns"], out["max_budget_usd"], out["daily_budget_usd"], out["plan_max_turns"]) == ("нет",) * 4
    assert out["max_parallel"] == 1 and out["plan_budget_share"] == 0.3
    code.write(plan_max_turns="9")
    out = json.loads(_call(code, "code_projects", {}))[0]
    assert (out["max_turns"], out["max_budget_usd"], out["daily_budget_usd"], out["plan_max_turns"]) == \
        (100, 10, 30, 9)


# ---------- 2.1 effective_limits ----------

def test_effective_limits_subscription_takes_registry_only():
    proj = ProjectCode(max_turns=7, max_budget_usd=2.5)
    assert effective_limits(proj, "subscription") == Limits(7, 2.5, None, None, None)
    assert effective_limits(ProjectCode(), "subscription") == Limits()


@pytest.mark.parametrize("mode", ["api", "", "Subscription"])
def test_effective_limits_other_billing_refused(mode):
    with pytest.raises(JobError, match="не поддерживается: subscription или key"):
        effective_limits(ProjectCode(max_turns=7), mode)


# ---------- 2.3 запуск ----------

def test_no_limits_no_flags_and_task_limits_ignored(code):
    """Проект без лимитов: argv без --max-turns/--max-budget-usd; шапка с budget_usd 30 и max_turns 50
    стартует, лимиты не применены, предупреждение одно."""
    code.write(**NO_LIMITS)
    p = code.jobs.plan("proj", code.taskfile(extra="max_turns: 50\nbudget_usd: 30\n"))
    assert (p.max_turns, p.budget_usd, p.budget_total, p.max_turns_total) == (None,) * 4
    assert p.warnings.count(LIMIT_IGNORED) == 1
    d = code.jobs.start(p)
    wait_terminal(code.jobs, d["id"])
    argv = _argv(code, d["id"])
    assert "--max-turns" not in argv and "--max-budget-usd" not in argv
    job = code.jobs.read(d["id"])
    assert (job["max_turns"], job["budget_usd"], job["max_hours"]) == (None, None, None)
    assert LIMIT_IGNORED in job["warnings"]


def test_no_limits_params_ignored_with_one_warning(code):
    """Лимиты из параметров code_start/code_resume — как из шапки: не применяются, одно предупреждение."""
    code.write(**NO_LIMITS)
    for kw in ({"max_turns": 5}, {"budget_usd": 1.0}, {"max_turns": 5, "budget_usd": 1.0}):
        p = code.jobs.plan("proj", code.taskfile(), **kw)
        assert (p.max_turns, p.budget_usd) == (None, None) and p.warnings.count(LIMIT_IGNORED) == 1, kw
    assert LIMIT_IGNORED not in code.jobs.plan("proj", code.taskfile()).warnings  # ничего не просили — тишина


def test_each_flag_follows_its_own_limit(code):
    """Задан только бюджет: --max-budget-usd есть, --max-turns нет (и наоборот)."""
    code.write(**{**NO_LIMITS, "max_budget_usd": "5"})
    d = code.jobs.start(code.jobs.plan("proj", code.taskfile(extra="max_turns: 50\n")))
    wait_terminal(code.jobs, d["id"])
    argv = _argv(code, d["id"])
    assert _opt(argv, "--max-budget-usd") == "5" and "--max-turns" not in argv
    assert code.jobs.read(d["id"])["warnings"].count(LIMIT_IGNORED) == 1  # ходы из шапки при незаданном потолке
    code.write(**{**NO_LIMITS, "max_turns": "30"})
    d = code.jobs.start(code.jobs.plan("proj", code.taskfile()))
    wait_terminal(code.jobs, d["id"])
    argv = _argv(code, d["id"])
    assert _opt(argv, "--max-turns") == "30" and "--max-budget-usd" not in argv


def test_explicit_limits_work_as_before(code):
    code.write(max_turns="50", max_budget_usd="5")
    p = code.jobs.plan("proj", code.taskfile(extra="max_turns: 20\nbudget_usd: 3\n"))
    assert (p.max_turns, p.budget_usd) == (20, 3) and LIMIT_IGNORED not in p.warnings
    d = code.jobs.start(p)
    wait_terminal(code.jobs, d["id"])
    argv = _argv(code, d["id"])
    assert _opt(argv, "--max-turns") == "20" and _opt(argv, "--max-budget-usd") == "3"
    p = code.jobs.plan("proj", code.taskfile())  # в шапке нет — потолок оператора
    assert (p.max_turns, p.budget_usd) == (50, 5)
    with pytest.raises(JobError, match="выше потолка"):
        code.jobs.plan("proj", code.taskfile(extra="max_turns: 51\n"))
    with pytest.raises(JobError, match="выше потолка"):
        code.jobs.plan("proj", code.taskfile(extra="budget_usd: 6\n"))


def test_daily_budget_not_checked_when_unset(code, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("_check_daily_budget вызван без daily_budget_usd")
    monkeypatch.setattr(Jobs, "_check_daily_budget", boom)
    code.write(**NO_LIMITS)
    d = code.jobs.start(code.jobs.plan("proj", code.taskfile()))
    wait_terminal(code.jobs, d["id"])
    code.write(max_budget_usd="5", daily_budget_usd="7")  # задан — проверка идёт
    with pytest.raises(AssertionError, match="_check_daily_budget"):
        code.jobs.start(code.jobs.plan("proj", code.taskfile("B.md")))


def test_over_time_without_max_hours_never(code):
    assert over_time({"max_hours": None, "started": 0}, 10 ** 9) is False
    assert over_time({"started": 0}, 10 ** 9) is False
    assert over_time({"max_hours": 1, "started": 0}, 3601) is True
    assert over_time({"max_hours": 1, "started": 0}, 3599) is False


def test_no_max_hours_job_runs_and_lost_still_detected(code, monkeypatch):
    code.write(**NO_LIMITS)
    monkeypatch.setenv("FAKE_SLEEP", "3")  # несколько опросов надзирателя (POLL = 1 с): остановки по времени нет
    d = code.jobs.start(code.jobs.plan("proj", code.taskfile()))
    assert code.jobs.read(d["id"])["max_hours"] is None
    snap = wait_terminal(code.jobs, d["id"])
    assert (snap["state"], snap["reason"]) == ("completed", None)
    monkeypatch.setenv("FAKE_SLEEP", "30")
    d = code.jobs.start(code.jobs.plan("proj", code.taskfile("B.md")))
    _wait_running(code.jobs, d["id"])
    jd = code.jobs.read(d["id"])
    os.kill(jd["sup_pid"], 9)
    os.killpg(jd["pgid"], 9)
    time.sleep(0.3)
    assert code.jobs.snapshot(d["id"])["state"] == "lost"  # определение lost не менялось


def test_plan_phase_without_limits(code, monkeypatch):
    """Фаза плана без лимитов: ни потолка ходов, ни доли бюджета; обе фазы идут без флагов."""
    monkeypatch.setenv("FAKE_RECLOG", str(code.state / "reclog.jsonl"))
    code.write(**NO_LIMITS)
    p = code.jobs.plan("proj", code.taskfile(size="M", risk="low"))
    assert (p.phase, p.max_turns, p.budget_usd) == ("plan", None, None)
    root = code.jobs.start(p)["id"]
    snap = _wait_state(code.jobs, root, ("completed", "failed", "plan_ready"))
    assert snap["state"] == "completed", snap
    runs = _reclog(code)
    assert len(runs) == 2
    for r in runs:
        assert "--max-turns" not in r["argv"] and "--max-budget-usd" not in r["argv"]


def test_plan_phase_limits_only_when_set(code):
    code.write(**{**NO_LIMITS, "plan_max_turns": "7"})
    p = code.jobs.plan("proj", code.taskfile(size="M", risk="low"))
    assert (p.phase, p.max_turns, p.budget_usd) == ("plan", 7, None)  # доля бюджета без бюджета не считается
    code.write(**{**NO_LIMITS, "max_budget_usd": "10", "max_turns": "20", "plan_max_turns": "7"})
    p = code.jobs.plan("proj", code.taskfile(size="M", risk="low"))
    assert (p.max_turns, p.budget_usd) == (7, 3.0)
    code.write(**{**NO_LIMITS, "max_turns": "5", "plan_max_turns": "7"})
    assert code.jobs.plan("proj", code.taskfile(size="M", risk="low")).max_turns == 5  # меньшее из двух


def test_chain_remaining_only_with_limit(code):
    """Ходы цепочки: «ходы задачи исчерпаны» — только при заданном max_turns; остаток бюджета — только при бюджете."""
    code.write(**{**NO_LIMITS, "max_turns": "2"})  # фейк делает 2 хода — после плана ходов не остаётся
    p = code.jobs.plan("proj", code.taskfile(), plan="review", reason=REASON)
    root = code.jobs.start(p)["id"]
    _wait_state(code.jobs, root, ("plan_ready", "failed", "completed"))
    with pytest.raises(JobError, match="ходы задачи исчерпаны"):
        code.jobs.approve(root)
    code.write(**NO_LIMITS)
    p = code.jobs.plan("proj", code.taskfile("B.md"), plan="review", reason=REASON)
    root = code.jobs.start(p)["id"]
    _wait_state(code.jobs, root, ("plan_ready", "failed", "completed"))
    nxt = code.jobs.approve(root)  # лимитов нет — исчерпать нечего
    assert nxt["max_turns"] is None and nxt["budget_usd"] is None
    wait_terminal(code.jobs, nxt["id"])


def test_replan_without_limits(code):
    """replan (_link_plan фазы плана) при незаданных лимитах: ни потолка ходов, ни доли бюджета, без TypeError."""
    code.write(**NO_LIMITS)
    root = code.jobs.start(code.jobs.plan("proj", code.taskfile(), plan="review", reason=REASON))["id"]
    _wait_state(code.jobs, root, ("plan_ready", "failed", "completed"))
    r = code.jobs.replan(root, "поправь")
    assert (r["phase"], r["max_turns"], r["budget_usd"]) == ("plan", None, None)
    assert "--max-turns" not in _argv(code, r["id"]) and "--max-budget-usd" not in _argv(code, r["id"])
    _wait_state(code.jobs, root, ("plan_ready", "failed", "completed"))


def test_plan_limit_alone_does_not_leak_into_execute(code):
    """Задан только plan_max_turns: фаза плана и replan получают его, а исполнение (approve) — без потолка ходов;
    потолок фазы плана не становится лимитом всей задачи (остаток ходов цепочки — только при max_turns)."""
    code.write(**{**NO_LIMITS, "plan_max_turns": "7"})
    root = code.jobs.start(code.jobs.plan("proj", code.taskfile(), plan="review", reason=REASON))["id"]
    _wait_state(code.jobs, root, ("plan_ready", "failed", "completed"))
    r = code.jobs.replan(root, "поправь")
    assert (r["max_turns"], r["budget_usd"]) == (7, None)
    assert _opt(_argv(code, r["id"]), "--max-turns") == "7"
    _wait_state(code.jobs, root, ("plan_ready", "failed", "completed"))
    nxt = code.jobs.approve(root)  # фейк потратил 2 хода фазы плана; от «7 − ходы» исполнение остаться не должно
    assert (nxt["phase"], nxt["max_turns"], nxt["budget_usd"]) == ("execute", None, None)
    wait_terminal(code.jobs, nxt["id"])
    assert "--max-turns" not in _argv(code, nxt["id"]) and "--max-budget-usd" not in _argv(code, nxt["id"])


def test_daily_budget_without_task_budget(code, monkeypatch):
    """Дневной бюджет задан, бюджета задачи нет (решение 5.1.3): звено входит в сумму как 0, завершённые звенья
    сегодня — по их стоимости."""
    code.write(**{**NO_LIMITS, "daily_budget_usd": "0.6", "max_parallel": "5"})
    monkeypatch.setenv("FAKE_SLEEP", "10")
    a = code.jobs.start(code.jobs.plan("proj", code.taskfile("A.md")))
    _wait_running(code.jobs, a["id"])
    b = code.jobs.start(code.jobs.plan("proj", code.taskfile("B.md")))  # идущая задача без бюджета — 0, отказа нет
    for x in (a, b):
        code.jobs.cancel(x["id"])
        wait_terminal(code.jobs, x["id"], timeout=60)
    monkeypatch.setenv("FAKE_SLEEP", "0")
    code.write(**{**NO_LIMITS, "daily_budget_usd": "0.4", "max_parallel": "5"})
    c = code.jobs.start(code.jobs.plan("proj", code.taskfile("C.md")))
    wait_terminal(code.jobs, c["id"])
    assert code.jobs.read(c["id"])["result"]["total_cost_usd"] == pytest.approx(0.5)
    with pytest.raises(JobError, match="дневной бюджет"):  # завершённое звено 0.5 + 0 > 0.4
        code.jobs.start(code.jobs.plan("proj", code.taskfile("D.md")))
    code.write(**{**NO_LIMITS, "daily_budget_usd": "0.6", "max_parallel": "5"})
    wait_terminal(code.jobs, code.jobs.start(code.jobs.plan("proj", code.taskfile("E.md")))["id"])  # 0.5 ≤ 0.6


def test_resume_inherited_limit_silent_explicit_warns(code):
    """Лимиты, унаследованные от родителя, молча не применяются; предупреждение — на переданный в этом вызове."""
    d = code.jobs.start(code.jobs.plan("proj", code.taskfile()))  # потолки оператора заданы — у родителя 100 и 10
    wait_terminal(code.jobs, d["id"])
    assert code.jobs.read(d["id"])["max_turns"] == 100
    code.write(**NO_LIMITS)
    r = code.jobs.resume(d["id"], "task")
    assert LIMIT_IGNORED not in r["warnings"] and r["max_turns"] is None and r["budget_usd"] is None
    wait_terminal(code.jobs, r["id"])
    assert "--max-turns" not in _argv(code, r["id"]) and "--max-budget-usd" not in _argv(code, r["id"])
    r2 = code.jobs.resume(r["id"], "task", max_turns=5)
    assert r2["warnings"].count(LIMIT_IGNORED) == 1 and r2["max_turns"] is None
    wait_terminal(code.jobs, r2["id"])


def test_resume_inherits_limit_when_cap_set(code):
    d = code.jobs.start(code.jobs.plan("proj", code.taskfile(), max_turns=30, budget_usd=4))
    wait_terminal(code.jobs, d["id"])
    r = code.jobs.resume(d["id"], "task")
    wait_terminal(code.jobs, r["id"])
    argv = _argv(code, r["id"])
    assert _opt(argv, "--max-turns") == "30" and _opt(argv, "--max-budget-usd") == "4"


# ---------- 2.4 показ сумм ----------

def _finished(code) -> str:
    d = code.jobs.start(code.jobs.plan("proj", code.taskfile()))
    wait_terminal(code.jobs, d["id"])
    return d["id"]


def test_hide_cost_removes_cost_keys_at_any_depth():
    view = {"cost_usd": 1, "x": [{"phase_cost_usd": 2, "ok": 1}], "result": {"total_cost_usd": 3, "turns": 4},
            "avg_cost_usd": 5, "session_cost_usd": 6}
    assert hide_cost(view) == {"x": [{"ok": 1}], "result": {"turns": 4}}
    assert view["cost_usd"] == 1  # копия — исходный ответ не меняется


def test_no_dollars_in_mcp_answers(code):
    jid = _finished(code)
    assert code.jobs.read(jid)["result"]["total_cost_usd"] > 0  # в job.json хранится как раньше
    for name, args in (("code_status", {"job": jid}), ("code_result", {"job": jid}), ("code_jobs", {}),
                       ("code_stats", {}), ("code_wait", {"job": jid, "seconds": 1}),
                       ("code_cancel", {"job": jid})):  # завершённая задача: отмена отдаёт снимок с итогом
        text = _call(code, name, args)
        assert "cost" not in text and "$" not in text, (name, text)
    assert json.loads(_call(code, "code_jobs", {}))[0]["state"] == "completed"  # остальное осталось
    stats = json.loads(_call(code, "code_stats", {}))[0]
    assert stats["tasks"] == 1 and "avg_cost_usd" not in stats and stats["avg_turns"] == 2
    res = json.loads(_call(code, "code_result", {"job": jid}))
    assert res["result"]["num_turns"] == 2 and res["billing"].startswith("оплата:")


def test_no_dollars_in_card(code):
    jid = _finished(code)
    data = json.dumps(code_server.card_data(code.jobs, jid, 0, False), ensure_ascii=False)
    assert "cost" not in data and "$" not in data
    assert json.loads(data)["billing"] == "подписка" and json.loads(data)["state"] == "completed"
    html = (resources.files("wshub") / "code_card.html").read_text(encoding="utf-8")
    # сумму карточка выводит только из cost_usd, а его card_data отдаёт лишь на ключе (ТЗ WSH-4.6, п. 2.6.2)
    assert "стоимость" not in html and '"$"' not in html
    assert html.count("cost_usd") == 4 and html.count("toFixed") == 1  # одно место показа — рядом с оплатой


def test_no_dollars_in_cli(code, capsys):
    jid = _finished(code)
    for args in (["status"], ["status", jid], ["result", jid], ["stats"]):
        assert code_cli.code_main("code", args, code.cfg, code.state) == 0
        out = capsys.readouterr().out
        assert "cost" not in out and "$" not in out, (args, out)
    assert code_cli.code_main("code", ["status"], code.cfg, code.state) == 0
    assert jid in capsys.readouterr().out

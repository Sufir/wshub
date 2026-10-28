"""wshub-code, ТЗ WSH-3.7, часть Б: план с разделом «Вопросы оператору» при plan = auto не уходит в исполнение сам —
стоит в plan_ready и ждёт code_approve с ответом в note (или code_replan). Настоящий claude не вызывается."""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import pytest
from test_code import make_code, wait_terminal
from test_code_card import _notify, _Srv
from test_code_hub_block import _auto_plan_job, _block, _call
from test_code_plan import _wait_state

from wshub import claude_cli, code_server
from wshub import jobs as jobs_mod
from wshub.jobs import PLAN_QUESTIONS_NEXT, PLAN_QUESTIONS_NOTE, JobError, plan_questions
from wshub.registry import Gate

ASK = "# План\n1. Сделать.\n\n## Вопросы оператору\n1. Мигрировать данные сейчас или отдельной задачей?\n"
LINE = jobs_mod.HUB_PLAN_QUESTIONS + "\n"


@pytest.fixture
def code(tmp_path, monkeypatch):
    return make_code(tmp_path, monkeypatch)


@pytest.fixture(autouse=True)
def hub_wshub(tmp_path, monkeypatch):
    exe = tmp_path / "hubbin" / "wshub"
    exe.parent.mkdir()
    exe.write_text("#!/bin/sh\nexit 0\n")
    exe.chmod(0o755)
    monkeypatch.setattr(claude_cli, "wshub_exe", lambda: str(exe))


def _audit(code) -> list[dict]:
    try:
        return [json.loads(x) for x in code.hub.audit_path.read_text().splitlines()]
    except FileNotFoundError:
        return []


def _skipped(code, jid) -> list[dict]:
    return [x for x in _audit(code) if x["tool"] == "code_approve" and x.get("job") == jid
            and x.get("status") == "skipped"]


def _wait_skipped(code, jid, timeout=30) -> dict:
    """Надзиратель пишет строку skipped после plan_ready в job.json — дальше он ничего не делает."""
    end = time.time() + timeout
    while time.time() < end:
        got = _skipped(code, jid)
        if got:
            return got[-1]
        time.sleep(0.1)
    raise AssertionError(f"{jid}: нет строки skipped в журнале хаба")


def _asking(code, monkeypatch, plan=ASK, size="M") -> str:
    """Задача с планом (M/low — plan = auto, L — review) и текстом плана plan; ждём plan_ready или итог."""
    monkeypatch.setenv("FAKE_PLAN", plan)
    root = code.jobs.start(code.jobs.plan("proj", code.taskfile(size=size, risk="low")))["id"]
    _wait_state(code.jobs, root, ("plan_ready", "completed", "failed"))
    return root


# ---------- Б1. признак ----------

@pytest.mark.parametrize("text,want", [
    (ASK, True),
    ("  ## Вопросы оператору  \nвопрос\n", True),
    ("```\n## Вопросы оператору\n```\n", False),
    ("~~~\n## Вопросы оператору\n~~~\n", False),
    ("```md\n## Вопросы оператору\n```\n## Вопросы оператору\n", True),  # после закрытого блока — считается
    ("````\n```\n## Вопросы оператору\n````\n", False),  # ``` внутри ```` блок не закрывает
    ("### Вопросы оператору\n", False),
    ("## Вопросы оператору:\n", False),
    ("## вопросы оператору\n", False),
    ("Текст ## Вопросы оператору\n", False),
])
def test_marker(text, want):
    assert plan_questions(text) is want


# ---------- Б2. надзиратель ----------

def test_auto_plan_with_questions_waits(code, monkeypatch):
    """Тест 1: план auto с заголовком — plan_ready, plan_questions в job.json, исполнения нет, skipped в журнале."""
    root = _asking(code, monkeypatch)
    rec = _wait_skipped(code, root)
    assert rec["params"] == {"auto": True, "reason": "plan_questions"} and rec["ws"] == "proj"
    d = code.jobs.read(root)
    assert d["state"] == "plan_ready" and d["plan_questions"] is True and not d.get("next")
    assert [x["id"] for x in code.jobs.chain(root)] == [root]  # звена исполнения нет
    assert not [x for x in _audit(code) if x["tool"] == "code_approve" and x.get("status") == "ok"]
    code.jobs.cancel(root)


def test_auto_plan_without_questions_runs(code, monkeypatch):
    """Тест 2: без заголовка — автопереход, как раньше."""
    root = _asking(code, monkeypatch, plan="# План\n1. Сделать.\n")
    snap = _wait_state(code.jobs, root, ("completed", "failed"))
    assert snap["state"] == "completed" and code.jobs.read(root).get("next")
    assert "plan_questions" not in code.jobs.read(root) and "plan_questions" not in snap
    assert not _skipped(code, root)


@pytest.mark.parametrize("plan", [
    "# План\n```\n## Вопросы оператору\n```\n",
    "# План\n### Вопросы оператору\n- вопрос\n",
    "# План\n## Вопросы оператору:\n- вопрос\n",
])
def test_marker_lookalikes_run(code, monkeypatch, plan):
    """Тест 3: заголовок в блоке кода или не ровно строка Б1.1 — автопереход."""
    root = _asking(code, monkeypatch, plan=plan)
    assert _wait_state(code.jobs, root, ("completed", "failed"))["state"] == "completed"
    assert code.jobs.read(root).get("next") and not _skipped(code, root)


# ---------- Б3. чат ----------

def test_approve_needs_note(code, monkeypatch):
    """Тест 4: code_approve без note и с note из пробелов — отказ дословно; с note — исполнение, ответ в промпте."""
    root = _asking(code, monkeypatch)
    _wait_skipped(code, root)
    for note in ("", "   "):
        assert _call(code, "code_approve", {"job": root, "note": note}) == f"отказ: {PLAN_QUESTIONS_NOTE}"
        with pytest.raises(JobError) as e:
            code.jobs.approve(root, note or None)
        assert str(e.value) == PLAN_QUESTIONS_NOTE
    assert [x["id"] for x in code.jobs.chain(root)] == [root]
    out = json.loads(_call(code, "code_approve", {"job": root, "note": "мигрировать сейчас"}))
    assert out["phase"] == "execute" and out["parent"] == root
    prompt = (code.jobs.job_dir(out["job"]) / "prompt.txt").read_text()
    assert "- Замечание оператора: мигрировать сейчас\n" in prompt  # execute.md без {note} — строкой блока
    assert jobs_mod.HUB_PLAN_QUESTIONS not in prompt  # у фазы исполнения строки Б4 нет
    assert _wait_state(code.jobs, root, ("completed", "failed"))["state"] == "completed"


def test_replan_works(code, monkeypatch):
    """Тест 5: code_replan с note — новая фаза плана (отказа «переделать план нельзя» нет), со строкой Б4."""
    root = _asking(code, monkeypatch)
    _wait_skipped(code, root)
    out = json.loads(_call(code, "code_replan", {"job": root, "note": "раздели миграцию"}))
    assert out["phase"] == "plan" and out["parent"] == root
    prompt = (code.jobs.job_dir(out["job"]) / "prompt.txt").read_text()
    assert "Переделай план PAY-1: раздели миграцию" in prompt and LINE in _block(prompt)
    _wait_skipped(code, out["job"])  # план снова с вопросом — снова ждёт
    snap = code.jobs.snapshot(root)
    assert snap["state"] == "plan_ready" and snap["active"] == out["job"] and snap["plan_questions"] is True
    code.jobs.cancel(root)


def test_snapshot_fields(code, monkeypatch):
    """Тест 6: снимок — plan_ready, plan_questions и next_step (Б3.5); next — id звена или null, не текст."""
    root = _asking(code, monkeypatch)
    _wait_skipped(code, root)
    hint = PLAN_QUESTIONS_NEXT.format(job=root)
    assert hint == (f"план задаёт вопросы оператору: code_plan({root}), затем code_approve({root}, note=<ответ>) или "
                    f"code_replan({root}, note)")
    snap = code.jobs.snapshot(root)
    assert snap["state"] == "plan_ready" and snap["plan_questions"] is True and snap["next_step"] == hint
    assert snap["next"] is None
    for tool, args in (("code_status", {"job": root}), ("code_wait", {"job": root, "seconds": 0}),
                       ("code_result", {"job": root})):
        got = json.loads(_call(code, tool, args))
        assert got["state"] == "plan_ready" and got["plan_questions"] is True and got["next_step"] == hint, tool
        assert got.get("next") is None, tool
    child = code.jobs.approve(root, "сейчас")
    _wait_state(code.jobs, root, ("completed", "failed"))
    snap = code.jobs.snapshot(root)
    assert "plan_questions" not in snap and "next_step" not in snap and snap["next"] == child["id"]


def test_chain_state_in_transition_window(code):
    """Тест 6: живой надзиратель, план с вопросом — plan_ready, не starting; отказов П4 нет (Б3.1, Б3.2)."""
    jid = _auto_plan_job(code, plan_questions=True)
    assert code.jobs._chain_state(code.jobs.read(jid)) == "plan_ready"
    assert code.jobs.snapshot(jid)["state"] == "plan_ready"
    assert code.jobs._plan_ready(jid)["id"] == jid
    assert code.jobs._plan_ready(jid, "replan")["id"] == jid
    with pytest.raises(JobError) as e:  # без note — отказ Б3.3, а не «утверждается автоматически»
        code.jobs.approve(jid)
    assert str(e.value) == PLAN_QUESTIONS_NOTE
    other = _auto_plan_job(code, "20261018-120000-bbbbbb")  # без вопроса — как раньше
    assert code.jobs._chain_state(code.jobs.read(other)) == "starting"


def test_review_unchanged(code, monkeypatch):
    """Тест 7: plan = review с тем же планом — прежнее поведение, признак не ищется."""
    root = _asking(code, monkeypatch, size="L")
    d = code.jobs.read(root)
    assert d["plan_mode"] == "review" and d["state"] == "plan_ready" and "plan_questions" not in d
    snap = code.jobs.snapshot(root)
    assert "plan_questions" not in snap and "next_step" not in snap
    assert LINE not in _block((code.jobs.job_dir(root) / "prompt.txt").read_text())
    child = code.jobs.approve(root)  # без note — как раньше
    assert child["phase"] == "execute"
    wait_terminal(code.jobs, child["id"])
    assert not _skipped(code, root)


# ---------- Б4. строка блока «От хаба» ----------

@pytest.mark.parametrize("mode", ['"sandbox"', '"auto"'])
def test_block_line_plan_auto(code, mode):
    """Тест 8: строка у фазы плана с plan = auto в проекте sandbox и auto; нет у review и plan = no."""
    code.write(mode=mode)
    p = code.jobs.plan("proj", code.taskfile(size="M"))
    assert p.plan_mode == "auto" and LINE in _block(p.prompt)
    review = code.jobs.plan("proj", code.taskfile(size="L"))
    assert review.plan_mode == "review" and jobs_mod.HUB_PLAN_QUESTIONS not in review.prompt
    no = code.jobs.plan("proj", code.taskfile())
    assert no.plan_mode == "no" and jobs_mod.HUB_PLAN_QUESTIONS not in no.prompt


def test_block_line_link_phases():
    """Тест 8: hub_block — строка только с plan_auto; при переполнении блока она целиком, сокращаются списки."""
    assert jobs_mod.HUB_PLAN_QUESTIONS not in jobs_mod.hub_block("x", (), "plan", [], "p\n")
    assert jobs_mod.HUB_PLAN_QUESTIONS not in jobs_mod.hub_block("x", (), None, [], "p\n")
    gates = tuple(Gate(f"g{i}-" + "x" * 35, ("cmd-" + "y" * 200,), 3600) for i in range(10))
    hidden = [f"dir-{i}/" + "z" * 120 for i in range(25)]
    block = jobs_mod.hub_block("н" * 300, gates, "plan", hidden, "p\n", auto_plan=True, sandbox=True,
                               plan_auto=True)
    assert len(block.encode("utf-8")) <= jobs_mod.HUB_BLOCK_MAX and "и ещё" in block
    assert LINE in block and jobs_mod.HUB_AUTO_PLAN + "\n" in block


# ---------- Б3.6. карточка ----------

def test_card_like_review(code, monkeypatch):
    """Тест 9: notify = card — данные карточки с plan_questions, отметка plan_ready, сообщение — ветка review."""
    _notify(code, "card")
    srv = _Srv(code)
    monkeypatch.setenv("FAKE_PLAN", ASK)
    root = srv.obj("code_start", {"project": "proj", "task": code.taskfile(size="M")})["job"]
    _wait_skipped(code, root)
    data = srv.obj("code_card_data", {"job": root})
    assert data["state"] == "plan_ready" and data["plan_mode"] == "auto" and data["plan_questions"] is True
    assert srv.obj("code_card_ack", {"job": root, "state": "plan_ready", "key": data["key"]}) == {"first": True}
    html = (Path(code_server.__file__).parent / "code_card.html").read_text()
    branch = 'if (d.state === "plan_ready" && (d.plan_mode === "review" || d.plan_questions)) {\n'
    assert branch in html
    assert html.split(branch, 1)[1].lstrip().startswith(
        'text = "wshub-code: план задачи " + job + " готов и ждёт решения.')
    code.jobs.cancel(root)
    review = srv.obj("code_start", {"project": "proj", "task": code.taskfile("R.md", size="L")})["job"]
    _wait_state(code.jobs, review, ("plan_ready",))
    assert "plan_questions" not in srv.obj("code_card_data", {"job": review})
    code.jobs.cancel(review)


def test_tool_descriptions(code):
    """Б3.7: описания code_approve и code_replan — с оговоркой о «Вопросах оператору»."""
    tools = {t.name: t for t in asyncio.run(code_server.build_code_server(code.hub).list_tools())}
    for name in ("code_approve", "code_replan"):
        assert ("при plan = auto не нужен — хаб утверждает сам — кроме плана с разделом «Вопросы оператору»"
                in " ".join(tools[name].description.split()))

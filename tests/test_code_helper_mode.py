"""wshub-code, ТЗ WSH-3.17: помощник хаба отказывает в `wshub gate` и `wshub switch` звену в режиме auto — по `mode` в
job.json звена (не по реестру проекта), при любых аргументах и до любых действий; в sandbox, у звеньев без `mode`,
у `push` и `forge` — всё как раньше."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from test_code import git, make_code
from test_code_gates import _gate_job
from test_code_push import _external, _helper, _token_file
from test_code_switch import _branch, _diverged, _head, _out, _spy

from wshub import helper

GATE_REASON = "в режиме auto гейты не выдаются: запусти проверку проекта сам обычной командой"
SWITCH_REASON = "в режиме auto wshub switch не выдаётся: смени ветку сам — git switch"
REASON = {"gate": GATE_REASON, "switch": SWITCH_REASON}
ABSENT = object()  # ключа mode в job.json нет


@pytest.fixture
def code(tmp_path, monkeypatch):
    return make_code(tmp_path, monkeypatch)


def _registry(code, tmp_path, mode: str | None = '"auto"', **over) -> Path:
    """Реестр с гейтом ok (создаёт маркер ran) и режимом проекта; ответ — путь маркера."""
    marker = tmp_path / "ran"
    gate = json.dumps(["sh", "-c", f"touch {marker}"])
    code.write(gates="{ ok = " + gate + " }", **({"mode": mode} if mode else {}), **over)
    return marker


def _job(code, mode, profile="task", phase=None) -> str:
    """Живое звено; mode ABSENT — ключа в job.json нет (звено до WSH-3.15), иначе значение пишется как есть."""
    jid = _gate_job(code, profile=profile, phase=phase)
    d = code.jobs.read(jid)
    d.pop("mode", None)
    if mode is not ABSENT:
        d["mode"] = mode
    code.jobs.write(jid, d)
    return jid


def _no_gate_traces(code, jid, marker) -> None:
    """Гейт не запускался: ни маркера, ни логов, ни gate.pid, ни лока."""
    jdir = code.jobs.job_dir(jid)
    assert not marker.exists()
    assert not (jdir / "gates").exists() or not list((jdir / "gates").glob("*.log"))
    assert not (jdir / "gate.pid").exists()
    assert not helper.gate_lock_paths(code.state, "proj", code.root)[0].exists()


# ---------- отказ в auto ----------

def test_auto_gate_refused(code, tmp_path, capsys):
    marker = _registry(code, tmp_path)
    jid = _job(code, "auto")
    rc, out = _helper(code, "gate", ["ok"], jid, capsys=capsys)
    assert rc == 1 and out.err == f"wshub gate: отказ: {GATE_REASON}\n" and out.out == ""
    row = _external(code, jid)[-1]
    assert (row["cmd"], row["args"], row["exit"], row["error"]) == ("gate", ["ok"], 1, GATE_REASON)
    _no_gate_traces(code, jid, marker)
    audit = [json.loads(x) for x in code.hub.audit_path.read_text().splitlines()]
    assert any(a.get("tool") == "wshub_gate" and a.get("status") == "error" and a.get("error") == GATE_REASON
               for a in audit)


@pytest.mark.parametrize("args", [["feat"], ["-c", "n1", "feat"], ["--detach", "feat"]])
def test_auto_switch_refused(code, tmp_path, capsys, monkeypatch, args):
    _registry(code, tmp_path)
    main = _diverged(code)
    head = _head(code.root)
    calls = _spy(monkeypatch)
    jid = _job(code, "auto")
    rc, out = _helper(code, "switch", args, jid, capsys=capsys)
    assert rc == 1 and out.err == f"wshub switch: отказ: {SWITCH_REASON}\n" and out.out == ""
    row = _external(code, jid)[-1]
    assert (row["cmd"], row["exit"], row["error"]) == ("switch", 1, SWITCH_REASON)
    assert _branch(code.root) == main and _head(code.root) == head
    audit = [json.loads(x) for x in code.hub.audit_path.read_text().splitlines()]
    assert any(a.get("tool") == "wshub_switch" and a.get("status") == "error" and a.get("error") == SWITCH_REASON
               for a in audit)
    assert _out(code.root, "rev-parse", "--verify", "--quiet", "refs/heads/n1").returncode != 0
    assert calls == []  # ни одного вызова git — отказ до prepare и run


@pytest.mark.parametrize("cmd,args", [("gate", []), ("gate", ["nope"]), ("gate", ["ok", "--bad"]),
                                      ("gate", ["--out", "x"]), ("gate", ["ok", "--out", "../x"]),
                                      ("switch", []), ("switch", ["--force", "x"]), ("switch", ["a", "b"]),
                                      ("switch", ["-c", "a..b", "feat"])])
def test_auto_bad_args_refused_with_auto_reason(code, tmp_path, capsys, cmd, args):
    marker = _registry(code, tmp_path)
    jid = _job(code, "auto")
    rc, out = _helper(code, cmd, args, jid, capsys=capsys)
    assert rc == 1 and out.err == f"wshub {cmd}: отказ: {REASON[cmd]}\n"
    assert _external(code, jid)[-1]["error"] == REASON[cmd]
    _no_gate_traces(code, jid, marker)


@pytest.mark.parametrize("cmd,args", [("gate", ["ok"]), ("switch", ["feat"])])
def test_auto_link_in_sandbox_project_refused(code, tmp_path, capsys, cmd, args):
    """Режим — из job.json звена: проект уже вернули в sandbox, а звено ещё в auto — отказ остаётся."""
    marker = _registry(code, tmp_path, mode=None)
    main = _diverged(code)
    jid = _job(code, "auto")
    rc, out = _helper(code, cmd, args, jid, capsys=capsys)
    assert rc == 1 and out.err == f"wshub {cmd}: отказ: {REASON[cmd]}\n"
    assert _branch(code.root) == main
    _no_gate_traces(code, jid, marker)


@pytest.mark.parametrize("cmd,args", [("gate", ["ok"]), ("switch", ["feat"])])
def test_auto_python_m_refused(code, tmp_path, cmd, args):
    """Отдельный процесс: python -m wshub идёт тем же путём в helper.main, что wshub из PATH и полный путь."""
    marker = _registry(code, tmp_path)
    main = _diverged(code)
    jid = _job(code, "auto")
    env = {**os.environ, "WSHUB_JOB": jid, "WSHUB_CONFIG": str(code.cfg), "WSHUB_STATE": str(code.state)}
    r = subprocess.run([sys.executable, "-m", "wshub", cmd, *args], cwd=code.root, env=env, capture_output=True,
                       text=True, timeout=60)
    assert r.returncode == 1 and r.stderr == f"wshub {cmd}: отказ: {REASON[cmd]}\n" and r.stdout == ""
    assert _external(code, jid)[-1]["error"] == REASON[cmd]
    assert _branch(code.root) == main
    _no_gate_traces(code, jid, marker)


# ---------- прежнее поведение ----------

@pytest.mark.parametrize("mode", ["sandbox", ABSENT, None, "Auto", "AUTO", "auto ", "", ["auto"], True])
def test_not_auto_switch_and_gate_work(code, tmp_path, capsys, mode):
    marker = _registry(code, tmp_path, mode=None)
    _diverged(code)
    jid = _job(code, mode)
    rc, out = _helper(code, "switch", ["feat"], jid, capsys=capsys)
    assert rc == 0 and _branch(code.root) == "feat" and "режиме auto" not in out.err
    rc, out = _helper(code, "gate", ["ok"], jid, capsys=capsys)
    assert rc == 0 and marker.exists() and "режиме auto" not in out.err
    assert (code.jobs.job_dir(jid) / "gates" / "01-ok.log").exists()
    assert all("error" not in row for row in _external(code, jid))


def test_project_auto_review_link_switches(code, tmp_path, capsys):
    """Проект в auto, но звено review — sandbox (так его пишет хаб): режим берётся из job.json звена, не из реестра."""
    _registry(code, tmp_path)
    _diverged(code)
    jid = _job(code, "sandbox", profile="review")
    rc, out = _helper(code, "switch", ["--detach", "feat"], jid, capsys=capsys)
    assert rc == 0 and _head(code.root) == _head(code.root, "feat"), out.err
    assert "error" not in _external(code, jid)[-1]


def test_project_auto_plan_phase_keeps_phase_refusals(code, tmp_path, capsys):
    marker = _registry(code, tmp_path)
    _diverged(code)
    jid = _job(code, "sandbox", phase="plan")
    rc, out = _helper(code, "switch", ["feat"], jid, capsys=capsys)
    assert rc == 1 and "смена ветки — только задаче профиля task или review в фазе исполнения" in out.err
    assert "режиме auto" not in out.err
    rc, out = _helper(code, "gate", ["ok"], jid, capsys=capsys)
    assert rc == 1 and "гейт — только задаче профиля task в фазе исполнения" in out.err
    assert "режиме auto" not in out.err
    _no_gate_traces(code, jid, marker)


@pytest.mark.parametrize("over,push_text,forge_text", [
    ({}, "пуш закрыт реестром (push = no)", "API хостинга закрыт реестром"),
    ({"push": '"task"', "forge": '"github"', "forge_access": '"task"'},
     "нет разрешения на пуш", "нет разрешения на API хостинга"),
])
def test_auto_push_forge_keep_their_own_refusals(code, tmp_path, capsys, over, push_text, forge_text):
    """push и forge в auto решают регламент проекта и разрешения задачи, а не режим."""
    if over:
        over = {**over, "forge_token": f'"{_token_file(tmp_path)}"'}
    _registry(code, tmp_path, **over)
    git(code.root, "remote", "add", "origin", str(tmp_path / "remote.git"))
    jid = _job(code, "auto")
    rc, out = _helper(code, "forge", ["GET", "/x"], jid, capsys=capsys)
    assert rc == 1 and forge_text in out.err and "режиме auto" not in out.err
    assert "режиме auto" not in _external(code, jid)[-1]["error"]
    rc, out = _helper(code, "push", ["origin", "feat-x"], jid, capsys=capsys)
    assert rc == 1 and push_text in out.err and "режиме auto" not in out.err
    assert "режиме auto" not in _external(code, jid)[-1]["error"]

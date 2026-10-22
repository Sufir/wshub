"""wshub-code, ТЗ WSH-3.8, часть В: смена ветки вне песочницы — разбор формы, ключ switch: шапки (надзиратель до
claude, снимок git после него), помощник `wshub switch`, argv git, settings, строка блока «От хаба», видимость в
итоге, CLI и карточке. Настоящий claude не вызывается; отказ «Device or resource busy» здесь не воспроизводится —
песочницы нет (стенд С1)."""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from test_code import git, make_code, wait_terminal
from test_code_plan import _wait_state

from wshub import claude_cli, code_cli, code_server, gitsafe, helper, runner
from wshub import jobs as jobs_mod
from wshub.jobs import JobError, Switch, parse_switch
from wshub.registry import CodeGlobal, Gate, ProjectCode
from wshub.runtime import proc_start


@pytest.fixture
def code(tmp_path, monkeypatch):
    return make_code(tmp_path, monkeypatch)


@pytest.fixture(autouse=True)
def hub_wshub(tmp_path, monkeypatch):
    """wshub хаба — исполняемый файл: иначе правил wshub switch нет (В4.3)."""
    exe = tmp_path / "hubbin" / "wshub"
    exe.parent.mkdir()
    exe.write_text("#!/bin/sh\nexit 0\n")
    exe.chmod(0o755)
    monkeypatch.setattr(claude_cli, "wshub_exe", lambda: str(exe))
    return exe


def _out(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)


def _branch(repo: Path) -> str:
    return _out(repo, "symbolic-ref", "--short", "HEAD").stdout.strip()


def _head(repo: Path, ref="HEAD") -> str:
    return _out(repo, "rev-parse", ref).stdout.strip()


def _diverged(code) -> str:
    """Ветка feat от начального коммита со своим коммитом, на основной ветке — свой: ветки расходятся. Ответ — имя
    основной ветки (HEAD на ней)."""
    main = _branch(code.root)
    git(code.root, "switch", "-q", "-c", "feat")
    (code.root / "f.txt").write_text("feat\n")
    git(code.root, "add", "f.txt")
    git(code.root, "commit", "-qm", "feat")
    git(code.root, "switch", "-q", main)
    (code.root / "m.txt").write_text("main\n")
    git(code.root, "add", "m.txt")
    git(code.root, "commit", "-qm", "main")
    return main


def _start(code, spec: str, profile="task", **kw) -> dict:
    return code.jobs.start(code.jobs.plan("proj", code.taskfile(profile=profile, extra=f"switch: {spec}\n", **kw)))


def _call(code, name, args) -> str:
    out = asyncio.run(code_server.build_code_server(code.hub).call_tool(name, args))
    content = out[0] if isinstance(out, tuple) else out
    return content[0].text


# ---------- В1. разбор формы ----------

def test_parse_three_forms_and_argv():
    assert parse_switch(["feat-x"], "review") == Switch("branch", "feat-x")
    assert parse_switch(["-c", "WSH-1", "origin/main"], "task") == Switch("create", "WSH-1", "origin/main")
    assert parse_switch(["--detach", "origin/feat-x"], "recon") == Switch("detach", "origin/feat-x")
    assert Switch("branch", "feat-x").argv() == ["switch", "--no-guess", "feat-x"]
    assert Switch("create", "WSH-1", "origin/main").argv() == ["switch", "--no-track", "-c", "WSH-1", "origin/main"]
    assert Switch("detach", "v1~2").argv() == ["switch", "--detach", "v1~2"]
    assert [Switch("create", "a", "b").spec, Switch("detach", "r").spec] == ["-c a b", "--detach r"]


@pytest.mark.parametrize("args", [[], ["a", "b"], ["--force", "a"], ["-f", "a"], ["-c", "a"], ["-c", "a", "b", "c"],
                                  ["--detach"], ["--detach", "-x"], ["-c", "-x", "b"], ["-c", "a", "--track"],
                                  ["--orphan", "a"], ["--merge", "a"], ["--discard-changes", "a"], ["-"], [""],
                                  ["-C", "a", "b"], ["--track", "a"], ["a", "--force"]])
def test_parse_refusals(args):
    with pytest.raises(JobError, match="нужно <ветка> \\| -c <ветка> <база> \\| --detach <ссылка>"):
        parse_switch(args, "task")


@pytest.mark.parametrize("profile", ["review", "recon"])
def test_parse_create_only_task(profile):
    with pytest.raises(JobError, match="switch -c — только профилю task"):
        parse_switch(["-c", "a", "b"], profile)


@pytest.mark.parametrize("profile", ["review", "recon"])
def test_header_create_refused_for_non_task(code, profile):
    with pytest.raises(JobError, match="switch -c — только профилю task"):
        code.jobs.plan("proj", code.taskfile(profile=profile, extra="switch: -c x main\n"))


@pytest.mark.parametrize("value,msg", [("--force feat", "других опций нет"), ("feat extra", "других опций нет"),
                                       ("-c master main", "push_protect: master")])
def test_header_form_checked_in_plan(code, value, msg):
    with pytest.raises(JobError, match=msg):
        code.jobs.plan("proj", code.taskfile(extra=f"switch: {value}\n"))
    assert not code.jobs.root.exists() or not any(code.jobs.root.iterdir())


def test_header_any_profile_branch_and_detach(code):
    for profile in ("recon", "review", "task"):
        for spec in ("feat", "--detach feat"):
            assert code.jobs.plan("proj", code.taskfile(profile=profile, extra=f"switch: {spec}\n")).switch == spec


# ---------- В2. ключ switch: шапки ----------

def test_header_switch_existing_branch(code):
    main = _diverged(code)
    d = _start(code, "feat")
    assert code.jobs.read(d["id"])["switch"] == {"spec": "feat"}
    assert wait_terminal(code.jobs, d["id"])["state"] == "completed"
    assert _branch(code.root) == "feat"
    assert code.jobs.read(d["id"])["switch"] == {"spec": "feat", "from": main, "to": "feat", "done": True}


def test_header_switch_already_in_place(code):
    main = _branch(code.root)
    head = _head(code.root)
    d = _start(code, main)
    wait_terminal(code.jobs, d["id"])
    assert code.jobs.read(d["id"])["switch"] == {"spec": main, "from": main, "to": main, "done": False}
    git(code.root, "switch", "-q", "--detach", "HEAD")
    d = _start(code, f"--detach {head}")
    wait_terminal(code.jobs, d["id"])
    assert code.jobs.read(d["id"])["switch"]["done"] is False


def test_header_create_without_upstream(code):
    main = _branch(code.root)
    d = _start(code, f"-c WSH-1 {main}")
    assert wait_terminal(code.jobs, d["id"])["state"] == "completed"
    assert _branch(code.root) == "WSH-1" and _head(code.root, "WSH-1") == _head(code.root, main)
    r = _out(code.root, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "WSH-1@{u}")
    assert r.returncode != 0 and "no upstream" in r.stderr  # --track дал бы upstream на локальную базу
    assert code.jobs.read(d["id"])["switch"]["done"] is True


def test_header_create_existing_branch_elsewhere_fails(code):
    main = _branch(code.root)
    git(code.root, "branch", "WSH-1")
    d = _start(code, f"-c WSH-1 {main}")
    snap = wait_terminal(code.jobs, d["id"])
    jd = code.jobs.read(d["id"])
    assert (snap["state"], jd["reason"]) == ("failed", "switch")
    assert jd["error"] == f"ветка WSH-1 уже есть, HEAD на {main} — поправь switch в шапке"
    assert not (code.jobs.job_dir(d["id"]) / "events.jsonl").exists()  # claude не запускался
    assert _branch(code.root) == main


def test_header_create_existing_branch_in_place_is_noop(code):
    git(code.root, "switch", "-q", "-c", "WSH-1")
    d = _start(code, "-c WSH-1 HEAD")
    wait_terminal(code.jobs, d["id"])
    assert code.jobs.read(d["id"])["switch"]["done"] is False


def test_header_detach(code):
    _diverged(code)
    d = _start(code, "--detach feat")
    wait_terminal(code.jobs, d["id"])
    assert _out(code.root, "symbolic-ref", "-q", "HEAD").returncode != 0
    assert _head(code.root) == _head(code.root, "feat")
    assert code.jobs.read(d["id"])["switch"]["to"] == _head(code.root, "feat")[:9]


@pytest.mark.parametrize("spec,text", [("--detach nope", "ссылка nope не ведёт к коммиту"),
                                       ("nope", "invalid reference: nope"),
                                       ("a..b", "имя ветки a..b недопустимо")])
def test_header_bad_ref_fails_with_git_text(code, spec, text):
    main = _branch(code.root)
    d = _start(code, spec)
    wait_terminal(code.jobs, d["id"])
    jd = code.jobs.read(d["id"])
    assert (jd["state"], jd["reason"]) == ("failed", "switch") and text in jd["error"], jd["error"]
    assert not (code.jobs.job_dir(d["id"]) / "events.jsonl").exists() and _branch(code.root) == main


def test_header_unsafe_config_fails_without_git(code):
    _diverged(code)
    git(code.root, "config", "core.fsmonitor", "true")
    d = _start(code, "feat")
    wait_terminal(code.jobs, d["id"])
    jd = code.jobs.read(d["id"])
    assert (jd["state"], jd["reason"]) == ("failed", "switch") and "git_config_unsafe" in jd["error"]
    assert _branch(code.root) != "feat"


def test_snapshot_after_switch_counts_task_commit(code, monkeypatch):
    """Снимок на старте — после switch:, иначе коммит задачи на расходящейся ветке дал бы «HEAD ушёл» без коммитов."""
    _diverged(code)
    monkeypatch.setenv("FAKE_COMMIT", "1")
    d = _start(code, "feat")
    wait_terminal(code.jobs, d["id"])
    start = json.loads((code.jobs.job_dir(d["id"]) / jobs_mod.GIT_START).read_text())
    assert start["branch"] == "feat"
    oc = code.jobs.read(d["id"])["result"]["outside_code"]
    assert oc["commits_count"] == 1 and "head_moved" not in oc and oc["branch_start"] == "feat", oc


def test_chain_execute_link_does_not_switch_again(code):
    _diverged(code)
    root = _start(code, "feat", size="L")["id"]
    _wait_state(code.jobs, root, ("plan_ready",))
    assert code.jobs.read(root)["switch"]["done"] is True
    child = code.jobs.approve(root)
    assert child["switch"] == {"spec": "feat"}
    wait_terminal(code.jobs, child["id"])
    assert code.jobs.read(child["id"])["switch"] == {"spec": "feat", "from": "feat", "to": "feat", "done": False}


# ---------- argv git: надзиратель и помощник ----------

def _spy(monkeypatch) -> list[list[str]]:
    calls: list[list[str]] = []
    real = gitsafe.run

    def run(git_, cwd, args, environ, timeout, **kw):
        calls.append(list(args))
        return real(git_, cwd, args, environ, timeout, **kw)

    monkeypatch.setattr(gitsafe, "run", run)
    return calls


def _sup_job(code, spec: str, profile="task") -> runner.Supervisor:
    jid = jobs_mod.new_id()
    code.jobs.job_dir(jid).mkdir(parents=True)
    code.jobs.write(jid, {"id": jid, "project": "proj", "dir": str(code.root), "profile": profile, "state": "starting",
                          "switch": {"spec": spec}})
    return runner.Supervisor(code.hub, jid)


@pytest.mark.parametrize("spec,argv", [("feat", ["switch", "--no-guess", "feat"]),
                                       ("-c n1 feat", ["switch", "--no-track", "-c", "n1", "feat"]),
                                       ("--detach feat", ["switch", "--detach", "feat"])])
def test_supervisor_argv_exact(code, monkeypatch, spec, argv):
    _diverged(code)
    calls = _spy(monkeypatch)
    sup = _sup_job(code, spec)
    assert sup._switch("git", code.root, dict(os.environ)) is True
    assert [c for c in calls if c[0] == "switch"] == [argv]
    assert code.jobs.read(sup.jid)["switch"]["done"] is True


def _job(code, profile="task", phase=None, grants=None) -> str:
    jid = jobs_mod.new_id()
    code.jobs.job_dir(jid).mkdir(parents=True)
    code.jobs.write(jid, {"id": jid, "project": "proj", "dir": str(code.root), "profile": profile, "phase": phase,
                          "state": "running", "sup_pid": os.getpid(), "sup_start": proc_start(os.getpid()),
                          "grants": grants or {"push": False, "forge": False, "reason": None}})
    return jid


def _helper(code, args, jid, cwd=None, capsys=None):
    rc = helper.main("switch", args, code.cfg, code.state, environ={**os.environ, "WSHUB_JOB": jid},
                     cwd=str(cwd or code.root))
    return rc, capsys.readouterr() if capsys else None


def _external(code, jid) -> list[dict]:
    p = code.jobs.job_dir(jid) / helper.EXTERNAL
    return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []


@pytest.mark.parametrize("args,argv", [(["feat"], ["switch", "--no-guess", "feat"]),
                                       (["-c", "n1", "feat"], ["switch", "--no-track", "-c", "n1", "feat"]),
                                       (["--detach", "feat"], ["switch", "--detach", "feat"])])
def test_helper_argv_exact(code, monkeypatch, capsys, args, argv):
    _diverged(code)
    calls = _spy(monkeypatch)
    rc, _ = _helper(code, args, _job(code), capsys=capsys)
    assert rc == 0 and [c for c in calls if c[0] == "switch"] == [argv]


# ---------- В3. помощник ----------

def test_helper_without_grants_switches(code, capsys):
    """Раздел 5, п. 6: у задачи нет ни push, ни forge — wshub switch работает (своя ветка check, не forge)."""
    main = _diverged(code)
    jid = _job(code)
    rc, out = _helper(code, ["feat"], jid, capsys=capsys)
    assert rc == 0 and "Switched to branch 'feat'" in out.out and _branch(code.root) == "feat"
    row = _external(code, jid)[-1]
    assert (row["cmd"], row["args"], row["exit"], row["from"], row["to"]) == ("switch", ["feat"], 0, main, "feat")
    assert "error" not in row
    audit = [json.loads(x) for x in code.hub.audit_path.read_text().splitlines()]
    assert any(a.get("tool") == "wshub_switch" and a.get("status") == "ok" for a in audit)


def test_helper_review_detach_and_create(code, capsys):
    _diverged(code)
    jid = _job(code, profile="review")
    rc, _ = _helper(code, ["--detach", "feat"], jid, capsys=capsys)
    assert rc == 0 and _head(code.root) == _head(code.root, "feat")
    assert _external(code, jid)[-1]["to"] == _head(code.root, "feat")[:9]
    jid = _job(code)
    rc, _ = _helper(code, ["-c", "n1", "feat"], jid, capsys=capsys)
    assert rc == 0 and _branch(code.root) == "n1"
    assert _out(code.root, "rev-parse", "--abbrev-ref", "n1@{u}").returncode != 0


def test_helper_git_failure_returns_git_code(code, capsys):
    """Незакоммиченная правка, которую switch перезаписал бы, — отказ git как есть, код git."""
    main = _diverged(code)
    git(code.root, "switch", "-q", "feat")
    (code.root / "f.txt").write_text("правка\n")
    jid = _job(code)
    rc, out = _helper(code, [main], jid, capsys=capsys)
    row = _external(code, jid)[-1]
    assert rc == row["exit"] and rc != 0 and "f.txt" in out.out and row["error"].startswith("git switch вернул")
    assert _branch(code.root) == "feat"


@pytest.mark.parametrize("setup,args,text", [
    ({"profile": "recon"}, ["feat"], "только задаче профиля task или review в фазе исполнения"),
    ({"phase": "plan"}, ["feat"], "только задаче профиля task или review в фазе исполнения"),
    ({"profile": "review"}, ["-c", "x", "feat"], "switch -c — только профилю task"),
    ({}, ["--force", "feat"], "других опций нет"),
    ({}, [], "других опций нет"),
    ({}, ["-c", "master", "feat"], "push_protect: master"),
    ({}, ["-c", "feat", "HEAD"], "ветка feat уже есть — переключись на неё: wshub switch feat"),
    ({}, ["--detach", "nope"], "ссылка nope не ведёт к коммиту"),
    ({}, ["-c", "x", "nope"], "ссылка nope не ведёт к коммиту"),
    ({}, ["a..b"], "имя ветки a..b недопустимо"),
])
def test_helper_refusals_recorded(code, capsys, setup, args, text):
    _diverged(code)
    before = _head(code.root)
    jid = _job(code, **setup)
    rc, out = _helper(code, args, jid, capsys=capsys)
    assert rc == 1 and text in out.err, out.err
    row = _external(code, jid)[-1]
    assert row["cmd"] == "switch" and row["exit"] == 1 and text in row["error"]
    assert _head(code.root) == before


def test_helper_cwd_outside_and_not_running(code, capsys, tmp_path):
    jid = _job(code)
    rc, out = _helper(code, ["feat"], jid, cwd=tmp_path, capsys=capsys)
    assert rc == 1 and "вне каталога задачи" in out.err and _external(code, jid)[-1]["exit"] == 1
    d = code.jobs.read(jid)
    d["state"], d["sup_pid"] = "completed", None
    code.jobs.write(jid, d)
    rc, out = _helper(code, ["feat"], jid, capsys=capsys)
    assert rc == 1 and "не идёт" in out.err and _external(code, jid)[-1]["exit"] == 1


def test_helper_unsafe_config_refused_without_git(code, capsys, monkeypatch):
    _diverged(code)
    git(code.root, "config", "core.fsmonitor", "true")
    calls = _spy(monkeypatch)
    jid = _job(code)
    rc, out = _helper(code, ["feat"], jid, capsys=capsys)
    assert rc == 1 and "плохой ключ" in out.err and _branch(code.root) != "feat"
    assert not [c for c in calls if c[0] == "switch"]


def test_helper_help_and_usage(capsys, tmp_path):
    assert helper.main("switch", ["--help"], tmp_path / "c.toml", tmp_path / "s") == 0
    assert capsys.readouterr().out.strip() == \
        "wshub switch <ветка> | wshub switch -c <ветка> <база> | wshub switch --detach <ссылка>"
    from wshub import server
    assert "wshub switch <ветка> | -c <ветка> <база> | --detach <ссылка>" in server.USAGE


def test_cli_dispatch_switch(code):
    """wshub switch — подкоманда сервера, как gate: вне задачи — отказ помощника, а не «неизвестная команда»."""
    env = {k: v for k, v in os.environ.items() if k != "WSHUB_JOB"}
    env.update(WSHUB_CONFIG=str(code.cfg), WSHUB_STATE=str(code.state))
    r = subprocess.run([sys.executable, "-m", "wshub", "switch", "feat"], cwd=code.root, capture_output=True,
                       text=True, env=env, timeout=30)
    assert r.returncode == 1 and r.stderr.startswith("wshub switch: отказ: нет WSHUB_JOB")


# ---------- В4. settings ----------

@pytest.mark.parametrize("profile,phase,has", [("task", None, True), ("review", None, True), ("recon", None, False),
                                               ("task", "plan", False), ("review", "plan", False)])
def test_settings_switch_rules(profile, phase, has):
    s = claude_cli.settings(profile, "T.md", CodeGlobal(), ProjectCode(), [], [], ["/w"], Path("/h"), Path("/d"),
                            phase=phase, switch=True)
    assert ("Bash(wshub switch *)" in s["permissions"]["allow"]) is has
    assert ("wshub switch *" in s["sandbox"]["excludedCommands"]) is has


def test_job_settings_task_review_recon(code, hub_wshub):
    for profile, has in (("task", True), ("review", True), ("recon", False)):
        p = code.jobs.plan("proj", code.taskfile(profile=profile))
        assert jobs_mod.SWITCH_PYTHON_M not in p.warnings
        jid = code.jobs.start(p)["id"]
        wait_terminal(code.jobs, jid)
        s = json.loads((code.jobs.job_dir(jid) / "settings.json").read_text())
        assert ("Bash(wshub switch *)" in s["permissions"]["allow"]) is has, profile
        assert ("wshub switch *" in s["sandbox"]["excludedCommands"]) is has, profile
        env = json.loads((code.jobs.job_dir(jid) / "env.json").read_text())
        if has:
            assert env["PATH"].split(os.pathsep)[0] == str(hub_wshub.parent)


def test_job_plan_phase_without_rules_execute_with(code):
    root = code.jobs.start(code.jobs.plan("proj", code.taskfile(size="L")))["id"]
    _wait_state(code.jobs, root, ("plan_ready",))
    s = json.loads((code.jobs.job_dir(root) / "settings.json").read_text())
    assert "Bash(wshub switch *)" not in s["permissions"]["allow"] and s["sandbox"]["excludedCommands"] == []
    assert "wshub switch" not in (code.jobs.job_dir(root) / "prompt.txt").read_text()
    child = code.jobs.approve(root)["id"]
    s = json.loads((code.jobs.job_dir(child) / "settings.json").read_text())
    assert "Bash(wshub switch *)" in s["permissions"]["allow"]
    assert jobs_mod.HUB_SWITCH["task"] in (code.jobs.job_dir(child) / "prompt.txt").read_text()
    wait_terminal(code.jobs, child)


def test_python_m_warns_no_rules(code, monkeypatch):
    monkeypatch.setattr(claude_cli, "wshub_exe", lambda: None)
    for profile in ("task", "review"):
        p = code.jobs.plan("proj", code.taskfile(profile=profile))
        assert jobs_mod.SWITCH_PYTHON_M in p.warnings and not p.switch_helper and "wshub switch" not in p.prompt
    assert jobs_mod.SWITCH_PYTHON_M == "смена ветки помощником недоступна: хаб запущен как python -m wshub"
    jid = code.jobs.start(code.jobs.plan("proj", code.taskfile()))["id"]
    wait_terminal(code.jobs, jid)
    s = json.loads((code.jobs.job_dir(jid) / "settings.json").read_text())
    assert "Bash(wshub switch *)" not in s["permissions"]["allow"]
    assert jobs_mod.SWITCH_PYTHON_M in code.jobs.read(jid)["warnings"]
    assert jobs_mod.SWITCH_PYTHON_M not in code.jobs.plan("proj", code.taskfile(profile="recon")).warnings


# ---------- В5. блок «От хаба» ----------

def test_hub_block_switch_line(code):
    task = code.jobs.plan("proj", code.taskfile()).prompt
    assert task.endswith(jobs_mod.HUB_SWITCH["task"] + "\n")
    assert jobs_mod.HUB_SWITCH["task"] == (
        "- Ветку меняй командой `wshub switch <ветка>` | `wshub switch -c <ветка> <база>` | `wshub switch --detach "
        "<ссылка>` целиком: `git switch` в песочнице падает на файлах, которые она монтирует («Device or resource "
        "busy»).")
    review = code.jobs.plan("proj", code.taskfile(profile="review")).prompt
    assert jobs_mod.HUB_SWITCH["review"] in review and "-c <ветка>" not in review
    assert "wshub switch" not in code.jobs.plan("proj", code.taskfile(profile="recon")).prompt
    assert "wshub switch" not in code.jobs.plan("proj", code.taskfile(size="L")).prompt  # фаза плана


def test_hub_block_switch_line_not_shortened():
    gates = tuple(Gate(f"g{i}", ("x" * 90,), 600) for i in range(10))
    block = jobs_mod.hub_block("н" * 300, gates, None, [f"d/{'p' * 150}{i}" for i in range(20)], "p\n",
                               switch="task")
    assert len(block.encode()) <= jobs_mod.HUB_BLOCK_MAX and jobs_mod.HUB_SWITCH["task"] in block


# ---------- видимость ----------

def _finished_with_switch(code) -> tuple[str, str]:
    """Завершённая задача с switch: feat в шапке и двумя вызовами wshub switch в учёте. Ответ — (job, основная
    ветка)."""
    main = _diverged(code)
    jid = _start(code, "feat")["id"]
    wait_terminal(code.jobs, jid)
    rows = [{"ts": "t", "cmd": "switch", "args": [main], "exit": 0, "from": "feat", "to": main},
            {"ts": "t", "cmd": "switch", "args": ["nope"], "exit": 1, "error": "x", "from": main, "to": main}]
    (code.jobs.job_dir(jid) / helper.EXTERNAL).write_text("".join(json.dumps(r) + "\n" for r in rows))
    return jid, main


def test_switch_in_result_cli_and_card(code, capsys):
    jid, main = _finished_with_switch(code)
    line = f"ветка: switch feat — {main} → feat"
    view = code.jobs.result_view(jid)
    assert view["switch"] == [line]
    assert view["external"]["switch_count"] == 2
    assert view["external"]["switch"][0] == {"ts": "t", "args": [main], "exit": 0, "from": "feat", "to": main}
    out = json.loads(_call(code, "code_result", {"job": jid}))
    assert out["switch"] == [line] and out["external"]["switch_count"] == 2
    assert code_cli.code_main("code", ["result", jid], code.cfg, code.state) == 0
    cli = capsys.readouterr().out
    assert line in cli and '"switch_count": 2' in cli
    end = code_server.card_data(code.jobs, jid, 0)["end"]
    assert end["switch"] == [line] and end["external"]["switch_count"] == 2
    assert end["external"]["switch"][1]["exit"] == 1


def test_switch_line_forms():
    assert jobs_mod.switch_line({"spec": "a", "from": "m", "to": "m", "done": False}) == \
        "ветка: switch a — уже на месте (m)"
    assert jobs_mod.switch_line({"spec": "a"}) == "ветка: switch a — не выполнен"


def test_card_html_renders_switch():
    html = (Path(code_server.__file__).parent / "code_card.html").read_text()
    assert "end.switch" in html and "ex.switch_count" in html and "смены ветки: " in html

"""wshub-code, ТЗ WSH-3.5, часть Г: гейты проекта — реестр, settings задачи, помощник `wshub gate` (запуск вне
песочницы, окружение, таймаут, сигналы, лок, лог, --out), гашение группы гейта при остановке задачи, видимость
в итоге, CLI и карточке. Настоящий claude не вызывается."""
from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest
from test_code import make_code, make_repo, registry_text, wait_terminal
from test_code_push import _external, _helper, _running_job

from wshub import claude_cli, code_cli, code_server, helper, hook, registry, runner
from wshub import jobs as jobs_mod
from wshub.registry import CodeGlobal, Gate, ProjectCode, parse
from wshub.runtime import proc_start, read_json


@pytest.fixture
def code(tmp_path, monkeypatch):
    return make_code(tmp_path, monkeypatch)


@pytest.fixture(autouse=True)
def hub_wshub(tmp_path, monkeypatch):
    """wshub хаба — исполняемый файл: иначе гейтов нет (Г2.3)."""
    exe = tmp_path / "hubbin" / "wshub"
    exe.parent.mkdir()
    exe.write_text("#!/bin/sh\nexit 0\n")
    exe.chmod(0o755)
    monkeypatch.setattr(claude_cli, "wshub_exe", lambda: str(exe))
    return exe


def _gates(code, **gates):
    """[workspace.proj.code] gates = { имя = значение-TOML, … }."""
    code.write(gates="{ " + ", ".join(f'{k.replace("_", "-")} = {v}' for k, v in gates.items()) + " }")


def _gate_job(code, profile="task", phase=None, env=None) -> str:
    """Живая задача и её env.json: белый список, который хаб дал бы claude, плюс WSHUB_*."""
    jid = _running_job(code, {}, profile=profile, phase=phase)
    env = env if env is not None else {"PATH": os.environ["PATH"], "HOME": os.environ["HOME"], "LANG": "C.UTF-8",
                                       "TERM": "dumb", "WSHUB_JOB": jid, "WSHUB_CONFIG": str(code.cfg),
                                       "WSHUB_STATE": str(code.state)}
    (code.jobs.job_dir(jid) / "env.json").write_text(json.dumps(env))
    return jid


def _group_live(pgid: int) -> list[int]:
    """Живые (не зомби) процессы группы — по /proc."""
    out = []
    for p in Path("/proc").iterdir():
        if not p.name.isdigit():
            continue
        try:
            f = (p / "stat").read_text().rsplit(")", 1)[-1].split()
        except OSError:
            continue
        if f[2] == str(pgid) and f[0] != "Z":
            out.append(int(p.name))
    return out


def _wait(cond, timeout=10.0):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.05)
    return False


def _call(code, name, args) -> str:
    out = asyncio.run(code_server.build_code_server(code.hub).call_tool(name, args))
    content = out[0] if isinstance(out, tuple) else out
    return content[0].text


# ---------- Г1. реестр ----------

def test_registry_three_forms(code):
    code.write(gates='{ test-docker = ".venv/bin/python -m pytest -m docker", '
                     'check = { cmd = ["scripts/check.sh"], timeout_s = 1500 }, '
                     'lint = { cmd = "ruff check src" } }')
    pc = parse(code.cfg.read_text()).code.projects["proj"]
    assert pc.gates == (Gate("test-docker", (".venv/bin/python", "-m", "pytest", "-m", "docker"), 600),
                        Gate("check", ("scripts/check.sh",), 1500), Gate("lint", ("ruff", "check", "src"), 600))


def test_registry_empty_and_absent(code):
    assert parse(code.cfg.read_text()).code.projects["proj"].gates == ()
    code.write(gates="{}")
    assert parse(code.cfg.read_text()).code.projects["proj"].gates == ()


def test_gate_name_re_same_as_hook():
    assert registry.GATE_NAME_RE.pattern == hook.GATE_NAME_RE.pattern


@pytest.mark.parametrize("value,msg", [
    ('{ Bad = "ls" }', "имя гейта"),
    ('{ _x = "ls" }', "имя гейта"),
    ('{ a = "" }', "пустая команда"),
    ('{ a = [] }', "пустая команда"),
    ('{ a = ["ls", ""] }', "список непустых строк"),
    ('{ a = { timeout_s = 5 } }', "нужен cmd"),
    ('{ a = 5 }', "строка или список"),
    ('{ a = "pytest | tail -5" }', "без оболочки"),
    ('{ a = "pytest|tail" }', "без оболочки"),
    ('{ a = "make && make test" }', "без оболочки"),
    ('{ a = "a || b" }', "без оболочки"),
    ('{ a = "a ; b" }', "без оболочки"),
    ('{ a = "sleep 1 &" }', "без оболочки"),
    ('{ a = "pytest > out.log" }', "без оболочки"),
    ('{ a = "pytest >> out.log" }', "без оболочки"),
    ('{ a = "pytest < in" }', "без оболочки"),
    ('{ a = "pytest 2>&1" }', "без оболочки"),
    ('{ a = "echo $HOME" }', "без оболочки"),
    ('{ a = "echo `id`" }', "без оболочки"),
    ('{ a = "echo \'unterminated" }', "не разбирается"),
    ('{ a = "../bin/check" }', "без «..»"),
    ('{ a = ["scripts/../../x"] }', "без «..»"),
    # круг 2, Р5: ~ и $ в cmd[0] не раскрываются — иначе «~/x» стал бы путём от рабочего каталога
    ('{ a = "~/bin/check" }', "cmd[0]: ~ и $ не раскрываются — гейт идёт без оболочки; укажи абсолютный путь"),
    ('{ a = ["~/bin/check"] }', "cmd[0]: ~ и $ не раскрываются"),
    ('{ a = ["~"] }', "cmd[0]: ~ и $ не раскрываются"),
    ('{ a = "$HOME/bin/check" }', "cmd[0]: ~ и $ не раскрываются"),
    ('{ a = ["$HOME/bin/check", "x"] }', "cmd[0]: ~ и $ не раскрываются"),
    ('{ a = { cmd = ["${X}"] } }', "cmd[0]: ~ и $ не раскрываются"),
    ('{ a = { cmd = "ls", timeout_s = 0 } }', "timeout_s"),
    ('{ a = { cmd = "ls", timeout_s = 3601 } }', "timeout_s"),
    ('{ a = { cmd = "ls", timeout_s = true } }', "timeout_s"),
    ('{ a = { cmd = "ls", timeout_s = 1.5 } }', "timeout_s"),
    ('"ls"', "ожидается таблица"),
])
def test_registry_errors(code, value, msg):
    code.write(gates=value)
    reg = parse(code.cfg.read_text())
    assert msg in reg.code.project_errors["proj"], reg.code.project_errors
    assert reg.workspaces["proj"] and not reg.code.error  # ошибка — только у проекта


def test_registry_quoted_operator_is_argument(code):
    """В кавычках «|» — аргумент, а не конвейер; абсолютный путь и голое имя — можно."""
    code.write(gates='{ a = "grep -E \'a|b\' x", b = "/usr/bin/env true", c = ["sh", "-c", "a | b"] }')
    pc = parse(code.cfg.read_text()).code.projects["proj"]
    assert [g.cmd for g in pc.gates] == [("grep", "-E", "a|b", "x"), ("/usr/bin/env", "true"), ("sh", "-c", "a | b")]


def test_registry_unknown_gate_key_warns(code):
    code.write(gates='{ a = { cmd = "ls", shell = true } }')
    reg = parse(code.cfg.read_text())
    assert reg.code.projects["proj"].gates[0].cmd == ("ls",)
    assert "неизвестный ключ workspace.proj.code.gates.a.shell — пропущен" in reg.warnings


def test_registry_gate_error_only_own_project(code, tmp_path):
    other = tmp_path / "other"
    other.mkdir()
    text = registry_text(code.root, gates='{ a = "x | y" }') + \
        f'\n[workspace.other]\npath = "{other}"\nmode = "rw"\n\n[workspace.other.code]\nenabled = false\n'
    reg = parse(text)
    assert "proj" in reg.code.project_errors and "other" in reg.code.projects


def test_code_projects_shows_gates(code):
    code.write(gates='{ check = { cmd = ["scripts/check.sh", "--python", "3.10"], timeout_s = 1500 }, '
                     'unit = "pytest -q \'tests/a b.py\'" }')
    out = json.loads(_call(code, "code_projects", {}))[0]
    assert out["gates"] == [{"name": "check", "cmd": "scripts/check.sh --python 3.10", "timeout_s": 1500},
                            {"name": "unit", "cmd": "pytest -q 'tests/a b.py'", "timeout_s": 600}]


# ---------- Г2. settings задачи ----------

def _settings(profile, gates=(), phase=None, grants=None):
    proj = ProjectCode(enabled=True, dirs=(".",), prompts="p", gates=gates)
    return claude_cli.settings(profile, "T.md", CodeGlobal(), proj, [], [], ["/py", "-m", "wshub.hook"],
                               Path("/home/u"), Path("/home/u/proj"), phase=phase, grants=grants, gates=gates)


GATES = (Gate("a", ("true",), 600), Gate("b", ("true",), 1500))


def test_settings_task_with_gates():
    s = _settings("task", GATES)
    assert "Bash(wshub gate *)" in s["permissions"]["allow"]
    assert "wshub gate *" in s["sandbox"]["excludedCommands"]
    assert s["env"] == {"BASH_MAX_TIMEOUT_MS": str((1500 + 60) * 1000)}
    small = _settings("task", (Gate("a", ("true",), 30),))
    assert small["env"] == {"BASH_MAX_TIMEOUT_MS": "600000"}  # не ниже потолка по умолчанию


@pytest.mark.parametrize("profile,gates,phase", [("recon", GATES, None), ("review", GATES, None),
                                                 ("task", GATES, "plan"), ("task", (), None)])
def test_settings_no_gate_rules(profile, gates, phase):
    s = _settings(profile, gates, phase)
    assert not [a for a in s["permissions"]["allow"] if "gate" in a]
    assert not [c for c in s["sandbox"]["excludedCommands"] if "gate" in c]
    assert "env" not in s


def test_settings_gate_with_grants_order():
    s = _settings("task", GATES, grants={"push": True, "forge": True})
    assert [c for c in s["sandbox"]["excludedCommands"] if "wshub" in c] == ["wshub push *", "wshub forge *",
                                                                            "wshub gate *"]


def test_start_task_with_gates(code, hub_wshub):
    _gates(code, check='"true"')
    jid = code.jobs.start(code.jobs.plan("proj", code.taskfile()))["id"]
    s = json.loads((code.jobs.job_dir(jid) / "settings.json").read_text())
    env = json.loads((code.jobs.job_dir(jid) / "env.json").read_text())
    assert "Bash(wshub gate *)" in s["permissions"]["allow"] and s["env"]["BASH_MAX_TIMEOUT_MS"] == "660000"
    assert env["PATH"].split(os.pathsep)[0] == str(hub_wshub.parent)  # helper_env
    wait_terminal(code.jobs, jid)


@pytest.mark.parametrize("profile", ["recon", "review"])
def test_start_reading_profiles_no_gates(code, profile):
    _gates(code, check='"true"')
    jid = code.jobs.start(code.jobs.plan("proj", code.taskfile(profile=profile)))["id"]
    s = json.loads((code.jobs.job_dir(jid) / "settings.json").read_text())
    env = json.loads((code.jobs.job_dir(jid) / "env.json").read_text())
    assert "Bash(wshub gate *)" not in s["permissions"]["allow"] and "env" not in s
    assert env["PATH"] == os.environ["PATH"]  # helper_env — только при гейтах, push или forge
    wait_terminal(code.jobs, jid)


def test_start_without_gates_no_helper_env(code):
    jid = code.jobs.start(code.jobs.plan("proj", code.taskfile()))["id"]
    env = json.loads((code.jobs.job_dir(jid) / "env.json").read_text())
    assert env["PATH"] == os.environ["PATH"]
    wait_terminal(code.jobs, jid)


def test_plan_phase_no_gates_execute_has(code):
    _gates(code, check='"true"')
    root = code.jobs.start(code.jobs.plan("proj", code.taskfile(size="L")))["id"]
    from test_code_plan import _wait_state
    _wait_state(code.jobs, root, ("plan_ready",))
    p = json.loads((code.jobs.job_dir(root) / "settings.json").read_text())
    assert not [a for a in p["permissions"]["allow"] if "gate" in a] and p["sandbox"]["excludedCommands"] == []
    assert "env" not in p
    child = code.jobs.approve(root)
    s = json.loads((code.jobs.job_dir(child["id"]) / "settings.json").read_text())
    assert "Bash(wshub gate *)" in s["permissions"]["allow"] and "wshub gate *" in s["sandbox"]["excludedCommands"]
    wait_terminal(code.jobs, child["id"])


def test_python_m_no_gates_start_ok(code, monkeypatch):
    monkeypatch.setattr(claude_cli, "wshub_exe", lambda: None)
    _gates(code, check='"true"')
    p = code.jobs.plan("proj", code.taskfile())
    assert jobs_mod.GATES_PYTHON_M in p.warnings and p.gates == ()
    d = code.jobs.start(p)
    s = json.loads((code.jobs.job_dir(d["id"]) / "settings.json").read_text())
    assert not [a for a in s["permissions"]["allow"] if "gate" in a] and "env" not in s
    assert jobs_mod.GATES_PYTHON_M in d["warnings"]
    wait_terminal(code.jobs, d["id"])


# ---------- Г3. помощник: отказы ----------

def test_gate_refusals_recorded(code, capsys, tmp_path):
    _gates(code, ok='"true"')
    jid = _gate_job(code)
    rc, out = _helper(code, "gate", ["nope"], jid, capsys=capsys)
    assert rc == 1 and "гейта nope нет в реестре проекта (есть: ok)" in out.err
    rc, out = _helper(code, "gate", ["ok"], jid, cwd=tmp_path, capsys=capsys)
    assert rc == 1 and "вне каталога задачи" in out.err
    rc, out = _helper(code, "gate", ["ok", "extra"], jid, capsys=capsys)
    assert rc == 1 and "только --out" in out.err
    rows = _external(code, jid)
    assert [r["cmd"] for r in rows] == ["gate"] * 3 and all(r["exit"] == 1 and r["error"] for r in rows)
    assert rows[0]["args"] == ["nope"]


def test_gate_removed_from_registry_on_the_fly(code, capsys):
    _gates(code, ok='"true"', other='"true"')
    jid = _gate_job(code)
    assert _helper(code, "gate", ["ok"], jid, capsys=capsys)[0] == 0
    _gates(code, other='"true"')
    rc, out = _helper(code, "gate", ["ok"], jid, capsys=capsys)
    assert rc == 1 and "гейта ok нет в реестре проекта (есть: other)" in out.err
    assert _external(code, jid)[-1]["exit"] == 1


def test_gate_job_not_running(code, capsys):
    _gates(code, ok='"true"')
    jid = _gate_job(code)
    d = code.jobs.read(jid)
    d.update(state="completed", sup_pid=None)
    code.jobs.write(jid, d)
    rc, out = _helper(code, "gate", ["ok"], jid, capsys=capsys)
    assert rc == 1 and "не идёт" in out.err and _external(code, jid)[-1]["exit"] == 1


@pytest.mark.parametrize("profile,phase", [("recon", None), ("review", None), ("task", "plan")])
def test_gate_profile_and_phase_refused(code, capsys, profile, phase):
    marker = code.root / "ran"
    _gates(code, ok=json.dumps(["touch", str(marker)]))
    jid = _gate_job(code, profile=profile, phase=phase)
    rc, out = _helper(code, "gate", ["ok"], jid, capsys=capsys)
    assert rc == 1 and "только задаче профиля task в фазе исполнения" in out.err
    assert not marker.exists() and _external(code, jid)[-1]["exit"] == 1


def test_gate_without_job(code, capsys):
    rc, out = _helper(code, "gate", ["ok"], None, capsys=capsys)
    assert rc == 1 and "нет WSHUB_JOB" in out.err


# ---------- Г3. помощник: запуск ----------

def test_gate_success_and_failure(code, capsys):
    _gates(code, ok=json.dumps(["sh", "-c", "echo out; echo err >&2"]),
           fail=json.dumps(["sh", "-c", "echo boom; exit 3"]))
    jid = _gate_job(code)
    rc, out = _helper(code, "gate", ["ok"], jid, capsys=capsys)
    assert rc == 0, out.err
    lines = out.out.splitlines()
    assert lines[:2] == ["out", "err"]
    assert lines[-1].startswith("wshub gate ok: код 0 · ") and lines[-1].endswith(" с · полный вывод: --out <файл>")
    rc, out = _helper(code, "gate", ["fail"], jid, capsys=capsys)
    assert rc == 3 and out.out.startswith("boom\n") and "wshub gate fail: код 3 · " in out.out
    rows = _external(code, jid)
    assert rows[0]["args"] == ["ok"] and rows[0]["exit"] == 0 and "error" not in rows[0]
    assert rows[0]["log"] == "gates/01-ok.log" and rows[0]["timeout"] is False
    assert isinstance(rows[0]["duration_s"], int)
    assert rows[1]["exit"] == 3 and rows[1]["log"] == "gates/02-fail.log" and rows[1]["error"]
    jdir = code.jobs.job_dir(jid)
    assert (jdir / "gates" / "01-ok.log").read_text() == "out\nerr\n"
    assert (jdir / "gates" / "02-fail.log").read_text() == "boom\n"
    assert not (jdir / jobs_mod.GATE_PID).exists()
    audit = [json.loads(x) for x in code.hub.audit_path.read_text().splitlines()]
    assert [x["status"] for x in audit if x.get("tool") == "wshub_gate"] == ["ok", "error"]


def test_gate_runs_in_workdir_relative_cmd(code, capsys):
    (code.root / "scripts").mkdir()
    s = code.root / "scripts" / "where.sh"
    s.write_text("#!/bin/sh\npwd\n")
    s.chmod(0o755)
    (code.root / "sub").mkdir()
    _gates(code, where='"scripts/where.sh"')
    jid = _gate_job(code)
    rc, out = _helper(code, "gate", ["where"], jid, cwd=code.root / "sub", capsys=capsys)
    assert rc == 0 and out.out.splitlines()[0] == str(code.root)


def test_gate_tail_8000(code, capsys):
    _gates(code, big=json.dumps([sys.executable, "-c", "import sys; sys.stdout.write('A' * 50000 + 'B' * 7000)"]))
    jid = _gate_job(code)
    rc, out = _helper(code, "gate", ["big"], jid, capsys=capsys)
    body, summary = out.out.rsplit("\n", 2)[0], out.out.splitlines()[-1]
    assert rc == 0 and len(body) == helper.GATE_TAIL and body == "A" * 1000 + "B" * 7000
    assert summary.startswith("wshub gate big: код 0")
    assert (code.jobs.job_dir(jid) / "gates" / "01-big.log").stat().st_size == 57000


def test_gate_env_is_env_json_without_wshub(code, capsys, monkeypatch):
    """Окружение гейта — env.json без WSHUB_*, а не окружение помощника: переменная, которую добавил «claude»
    (есть у помощника, нет в env.json), в гейт не попадает (круг 2, Т5)."""
    monkeypatch.setenv("GATE_HELPER_ONLY", "из-окружения-помощника")
    _gates(code, names='"env"')
    jid = _gate_job(code)
    rc, out = _helper(code, "gate", ["names"], jid, capsys=capsys)
    names = {line.split("=", 1)[0] for line in out.out.splitlines()[:-1] if "=" in line}
    env = json.loads((code.jobs.job_dir(jid) / "env.json").read_text())
    assert rc == 0 and names == {k for k in env if not k.startswith("WSHUB_")}
    assert "WSHUB_JOB" in env and "WSHUB_JOB" not in names
    assert os.environ["GATE_HELPER_ONLY"] and "GATE_HELPER_ONLY" not in names  # _helper отдаёт os.environ помощнику


def test_gate_bare_name_from_env_json_path(code, capsys, tmp_path):
    """Голое имя — из PATH окружения гейта, а не помощника."""
    bin_dir = tmp_path / "gbin"
    bin_dir.mkdir()
    tool = bin_dir / "mytool"
    tool.write_text("#!/bin/sh\necho from-gbin\n")
    tool.chmod(0o755)
    _gates(code, t='"mytool"')
    jid = _gate_job(code, env={"PATH": f"{bin_dir}:/usr/bin:/bin"})
    rc, out = _helper(code, "gate", ["t"], jid, capsys=capsys)
    assert rc == 0 and out.out.startswith("from-gbin\n")
    jid = _gate_job(code, env={"PATH": "/usr/bin:/bin"})
    rc, out = _helper(code, "gate", ["t"], jid, capsys=capsys)
    assert rc == 1 and "mytool нет в PATH окружения задачи" in out.err


def test_gate_timeout_kills_group(code, capsys):
    pidfile = code.root / "bg.pid"
    _gates(code, slow=f'{{ cmd = {json.dumps(["sh", "-c", f"sleep 100 & echo $! > {pidfile}; sleep 100"])}, '
                      'timeout_s = 1 }')
    jid = _gate_job(code)
    t0 = time.time()
    rc, out = _helper(code, "gate", ["slow"], jid, capsys=capsys)
    assert rc == 124 and time.time() - t0 < 10
    assert "wshub gate slow: код 124 · прерван: timeout_s = 1 · " in out.out
    bg = int(pidfile.read_text())
    assert _wait(lambda: not os.path.exists(f"/proc/{bg}") or
                 (Path(f"/proc/{bg}/stat").read_text().rsplit(")", 1)[-1].split()[0] == "Z"))  # внук мёртв
    row = _external(code, jid)[-1]
    assert row["timeout"] is True and row["exit"] == 124 and "timeout_s = 1" in row["error"]
    assert not (code.jobs.job_dir(jid) / jobs_mod.GATE_PID).exists()


def test_gate_background_child_killed_after_leader_exit(code, capsys):
    """Лидер вышел, фоновый потомок держит канал — гасится, помощник не висит."""
    pidfile = code.root / "bg.pid"
    _gates(code, bg=json.dumps(["sh", "-c", f"sleep 100 & echo $! > {pidfile}; echo done"]))
    jid = _gate_job(code)
    t0 = time.time()
    rc, out = _helper(code, "gate", ["bg"], jid, capsys=capsys)
    assert rc == 0 and out.out.startswith("done\n") and time.time() - t0 < 10
    bg = int(pidfile.read_text())
    assert _wait(lambda: not os.path.exists(f"/proc/{bg}") or
                 Path(f"/proc/{bg}/stat").read_text().rsplit(")", 1)[-1].split()[0] == "Z")


def test_gate_sigterm_to_helper(code):
    """SIGTERM помощнику: группа гейта гаснет, gate.pid удалён, код 130 — отдельный процесс wshub gate."""
    _gates(code, slow='{ cmd = ["sh", "-c", "sleep 100 & sleep 100"], timeout_s = 60 }')
    jid = _gate_job(code)
    env = {**os.environ, "WSHUB_JOB": jid, "WSHUB_CONFIG": str(code.cfg), "WSHUB_STATE": str(code.state)}
    proc = subprocess.Popen([sys.executable, "-m", "wshub", "gate", "slow"], cwd=code.root, env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    pidf = code.jobs.job_dir(jid) / jobs_mod.GATE_PID
    assert _wait(lambda: pidf.exists() and _group_live(json.loads(pidf.read_text())["pgid"]))
    pgid = json.loads(pidf.read_text())["pgid"]
    time.sleep(0.3)
    proc.send_signal(signal.SIGTERM)
    out, err = proc.communicate(timeout=20)
    assert proc.returncode == 130, err
    assert "прерван сигналом" in out
    assert _wait(lambda: not _group_live(pgid)) and not pidf.exists()
    assert _external(code, jid)[-1]["exit"] == 130


def test_gate_one_per_project(code, tmp_path):
    _gates(code, slow='{ cmd = ["sleep", "30"], timeout_s = 60 }', ok='"true"')
    # второй проект со своим гейтом: лок — на проект, а не общий (круг 2, Т2)
    other = make_repo(tmp_path / "other")
    code.cfg.write_text(code.cfg.read_text() + f'\n[workspace.other]\npath = "{other}"\nmode = "rw"\n\n'
                        '[workspace.other.code]\nenabled = true\ndirs = ["."]\nprompts = "prompts"\n'
                        'gates = { ok = "true" }\n', encoding="utf-8")
    jid = _gate_job(code)
    jid_b = _gate_job(code)
    d = code.jobs.read(jid_b)
    d.update(project="other", dir=str(other))
    code.jobs.write(jid_b, d)
    env = {**os.environ, "WSHUB_JOB": jid, "WSHUB_CONFIG": str(code.cfg), "WSHUB_STATE": str(code.state)}
    proc = subprocess.Popen([sys.executable, "-m", "wshub", "gate", "slow"], cwd=code.root, env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        pidf = code.jobs.job_dir(jid) / jobs_mod.GATE_PID
        assert _wait(pidf.exists)
        jid2 = _gate_job(code)
        r = subprocess.run([sys.executable, "-m", "wshub", "gate", "ok"], cwd=code.root, capture_output=True,
                           text=True, env={**env, "WSHUB_JOB": jid2}, timeout=30)
        assert r.returncode == 1 and f"у проекта уже идёт гейт slow задачи {jid}" in r.stderr
        assert _external(code, jid2)[-1]["exit"] == 1
        r = subprocess.run([sys.executable, "-m", "wshub", "gate", "ok"], cwd=other, capture_output=True,
                           text=True, env={**env, "WSHUB_JOB": jid_b}, timeout=30)
        assert r.returncode == 0, r.stderr  # другой проект — свой лок
        assert proc.poll() is None  # гейт проекта proc всё ещё идёт
    finally:
        proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=20)
    # лок отпущен — следующий гейт идёт
    r = subprocess.run([sys.executable, "-m", "wshub", "gate", "ok"], cwd=code.root, capture_output=True, text=True,
                       env=env, timeout=30)
    assert r.returncode == 0, r.stderr


def test_gate_out_written_and_symlink_refused(code, capsys):
    _gates(code, ok=json.dumps(["sh", "-c", "echo full; echo log"]))
    jid = _gate_job(code)
    (code.root / "logs").mkdir()
    rc, out = _helper(code, "gate", ["ok", "--out", "logs/ok.log"], jid, capsys=capsys)
    assert rc == 0 and (code.root / "logs" / "ok.log").read_text() == "full\nlog\n"
    assert out.out.splitlines()[-1].endswith("полный вывод: logs/ok.log")
    assert _external(code, jid)[-1]["args"] == ["ok", "--out", "logs/ok.log"]
    rc, out = _helper(code, "gate", ["ok", "--out=logs/eq.log"], jid, capsys=capsys)
    assert rc == 0 and (code.root / "logs" / "eq.log").exists()
    # симлинк в родителе — внутрь рабочего каталога, _work_file пропускает, open_out — нет
    (code.root / "real").mkdir()
    (code.root / "lnk").symlink_to(code.root / "real")
    rc, out = _helper(code, "gate", ["ok", "--out", "lnk/x.log"], jid, capsys=capsys)
    assert rc == 1 and "не открывается" in out.err and not (code.root / "real" / "x.log").exists()
    rc, out = _helper(code, "gate", ["ok", "--out", "../x.log"], jid, capsys=capsys)
    assert rc == 1 and "только внутри рабочего каталога" in out.err
    rc, out = _helper(code, "gate", ["ok", "--out", ".git/x.log"], jid, capsys=capsys)
    assert rc == 1 and "в .git" in out.err


def test_gate_log_limit(code, capsys, monkeypatch):
    monkeypatch.setattr(helper, "GATE_LOG_MAX", 1000)
    _gates(code, big=json.dumps([sys.executable, "-c", "import sys; sys.stdout.write('x' * 5000)"]))
    jid = _gate_job(code)
    rc, out = _helper(code, "gate", ["big", "--out", "big.log"], jid, capsys=capsys)
    assert rc == 0
    for p in (code.jobs.job_dir(jid) / "gates" / "01-big.log", code.root / "big.log"):
        text = p.read_text()
        assert text.startswith("x" * 1000) and text.endswith("\n… обрезано: 4000 байт\n") and text.count("x") == 1000


def test_gate_killed_by_signal_code(code, capsys):
    _gates(code, k=json.dumps(["sh", "-c", "kill -9 $$"]))
    jid = _gate_job(code)
    rc, _ = _helper(code, "gate", ["k"], jid, capsys=capsys)
    assert rc == 128 + 9


# ---------- круг 2: помощник ----------

def _holder(code) -> Path:
    return code.state / "code" / "gates" / "proj.holder"


def test_gate_orphan_killed(code, capsys):
    """Р1: прежний помощник умер (лок свободен), а его гейт идёт — новый помощник гасит сироту, потом запускает свой."""
    _gates(code, ok='"true"')
    jid = _gate_job(code)
    orphan = _spawn_group()
    try:
        assert _wait(lambda: len(_group_live(orphan.pid)) >= 2)
        _holder(code).parent.mkdir(parents=True, exist_ok=True)
        _holder(code).write_text(json.dumps({"job": "20261019-000000-aaaaaa", "gate": "slow", "helper": 999999,
                                             "pid": orphan.pid, "pgid": orphan.pid,
                                             "proc_start": proc_start(orphan.pid)}))
        rc, out = _helper(code, "gate", ["ok"], jid, capsys=capsys)
        assert rc == 0, out.err
        assert out.out.startswith("погашен осиротевший гейт slow задачи 20261019-000000-aaaaaa\n")
        orphan.wait(timeout=15)
        assert _wait(lambda: not _group_live(orphan.pid))
        row = _external(code, jid)[-1]
        assert row["exit"] == 0 and row["orphan_killed"] == {"job": "20261019-000000-aaaaaa", "gate": "slow"}
        h = json.loads(_holder(code).read_text())
        assert h["job"] == jid and "pgid" not in h  # гейт кончился — группы в .holder нет
    finally:
        if orphan.poll() is None:
            os.killpg(orphan.pid, signal.SIGKILL)


def test_gate_holder_foreign_pid_untouched(code, capsys):
    """Р1: в .holder — pid с другим proc_start (переиспользован) — не трогаем, сироты нет."""
    _gates(code, ok='"true"')
    jid = _gate_job(code)
    other = _spawn_group("sleep 100")
    try:
        _holder(code).parent.mkdir(parents=True, exist_ok=True)
        _holder(code).write_text(json.dumps({"job": "j", "gate": "g", "pid": other.pid, "pgid": other.pid,
                                             "proc_start": "1"}))
        rc, out = _helper(code, "gate", ["ok"], jid, capsys=capsys)
        assert rc == 0 and "осиротевший" not in out.out and "orphan_killed" not in _external(code, jid)[-1]
        assert other.poll() is None
    finally:
        os.killpg(other.pid, signal.SIGKILL)
        other.wait()


def test_gate_holder_has_group_while_running(code):
    """Р1: пока гейт идёт, в .holder — группа гейта (её и найдёт следующий помощник, если этот умрёт)."""
    _gates(code, slow='{ cmd = ["sleep", "30"], timeout_s = 60 }')
    jid = _gate_job(code)
    env = {**os.environ, "WSHUB_JOB": jid, "WSHUB_CONFIG": str(code.cfg), "WSHUB_STATE": str(code.state)}
    proc = subprocess.Popen([sys.executable, "-m", "wshub", "gate", "slow"], cwd=code.root, env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        pidf = code.jobs.job_dir(jid) / jobs_mod.GATE_PID
        assert _wait(lambda: pidf.exists() and "pgid" in read_json(_holder(code)))
        h, g = json.loads(_holder(code).read_text()), json.loads(pidf.read_text())
        assert {k: h[k] for k in ("pid", "pgid", "proc_start")} == g and h["helper"] == proc.pid and h["job"] == jid
    finally:
        proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=20)


class _ClosedStdout:
    """stdout помощника после отмены: канал закрыт."""

    def write(self, _s):
        raise BrokenPipeError(32, "Broken pipe")

    def flush(self):
        raise BrokenPipeError(32, "Broken pipe")

    def fileno(self):
        raise ValueError("нет fd")


def test_gate_record_before_output(code, monkeypatch):
    """Р2: канал stdout закрыт — строка учёта всё равно есть, помощник возвращает код гейта."""
    _gates(code, fail=json.dumps(["sh", "-c", "echo boom; exit 3"]))
    jid = _gate_job(code)
    monkeypatch.setattr(sys, "stdout", _ClosedStdout())
    rc = helper.main("gate", ["fail"], code.cfg, code.state, environ={**os.environ, "WSHUB_JOB": jid},
                     cwd=str(code.root))
    assert rc == 3
    row = _external(code, jid)[-1]
    assert row["args"] == ["fail"] and row["exit"] == 3 and row["log"] == "gates/01-fail.log"


def test_gate_closed_stdout_exit_code_kept(code):
    """Р2: отдельный процесс с закрытым stdout — код гейта, а не 120 от сброса буфера при выходе."""
    _gates(code, fail=json.dumps(["sh", "-c", "echo boom; exit 3"]))
    jid = _gate_job(code)
    env = {**os.environ, "WSHUB_JOB": jid, "WSHUB_CONFIG": str(code.cfg), "WSHUB_STATE": str(code.state)}
    r_fd, w_fd = os.pipe()
    os.close(r_fd)  # читающий конец закрыт — запись в stdout даёт EPIPE
    try:
        r = subprocess.run([sys.executable, "-m", "wshub", "gate", "fail"], cwd=code.root, env=env, stdout=w_fd,
                           stderr=subprocess.PIPE, text=True, timeout=30)
    finally:
        os.close(w_fd)
    assert r.returncode == 3, r.stderr
    assert _external(code, jid)[-1]["exit"] == 3


def test_gate_sink_no_write_after_close(tmp_path):
    """Р4: после close читатель только дочитывает канал — номер fd, отданный другому файлу, не задет."""
    import io
    first = os.open(tmp_path / "log", os.O_WRONLY | os.O_CREAT, 0o600)
    sink = helper._GateSink([first])
    sink.pump(io.BytesIO(b"before\n"))
    sink.close()
    other = os.open(tmp_path / "other", os.O_WRONLY | os.O_CREAT, 0o600)  # скорее всего, тот же номер fd
    try:
        sink.pump(io.BytesIO(b"after\n"))
    finally:
        os.close(other)
    assert (tmp_path / "log").read_bytes() == b"before\n" and (tmp_path / "other").read_bytes() == b""
    assert sink.tail_text() == "before\n"


def test_gate_pid_write_failure_kills_gate(code, capsys, monkeypatch):
    """Р6: gate.pid не записался — гейт погашен, отказ со строкой учёта."""
    marker = code.root / "gate.pid.of"
    _gates(code, slow=f'{{ cmd = {json.dumps(["sh", "-c", f"echo $$ > {marker}; sleep 100"])}, timeout_s = 60 }}')
    jid = _gate_job(code)
    real = helper.atomic_write_text

    def write(path, text):
        if Path(path).name == jobs_mod.GATE_PID:
            raise OSError(28, "No space left on device")
        return real(path, text)

    monkeypatch.setattr(helper, "atomic_write_text", write)
    t0 = time.time()
    rc, out = _helper(code, "gate", ["slow"], jid, capsys=capsys)
    assert rc == 1 and "gate.pid не записан" in out.err and "гейт погашен" in out.err and time.time() - t0 < 15
    row = _external(code, jid)[-1]
    assert row["exit"] == 1 and "gate.pid не записан" in row["error"]
    text = marker.read_text().strip() if marker.exists() else ""  # sh мог не успеть записать pid до гашения
    if text:
        pid = int(text)
        assert _wait(lambda: not os.path.exists(f"/proc/{pid}") or
                     Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[-1].split()[0] == "Z")


def test_gate_cancel_before_start_kills_gate(code, capsys, monkeypatch):
    """Р6: к записи gate.pid задача уже не running (отмена между check() и запуском) — гейт погашен, код 130."""
    _gates(code, slow='{ cmd = ["sleep", "100"], timeout_s = 60 }')
    jid = _gate_job(code)
    real = jobs_mod.Jobs.effective_state
    calls = []

    def state(self, d):
        calls.append(1)
        return real(self, d) if len(calls) == 1 else "cancelling"  # check() — running, повторное чтение — нет

    monkeypatch.setattr(jobs_mod.Jobs, "effective_state", state)
    t0 = time.time()
    rc, out = _helper(code, "gate", ["slow"], jid, capsys=capsys)
    assert rc == 130 and "прерван сигналом" in out.out and time.time() - t0 < 15
    assert _external(code, jid)[-1]["exit"] == 130


def test_gate_signal_during_timeout_ladder_is_timeout(code, capsys, monkeypatch):
    """Р10: сигнал помощнику во время лестницы таймаута итог не меняет — 124. Гейт игнорирует SIGTERM, лестница
    ждёт GATE_KILL_AFTER (2 с), сигнал приходит посреди неё."""
    import threading
    monkeypatch.setattr(jobs_mod, "GATE_KILL_AFTER", 2)
    ignore = "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); print('ready', flush=True); " \
             "time.sleep(100)"
    _gates(code, stubborn=f'{{ cmd = {json.dumps([sys.executable, "-c", ignore])}, timeout_s = 1 }}')
    jid = _gate_job(code)
    timer = threading.Timer(2.0, os.kill, (os.getpid(), signal.SIGTERM))  # таймаут ≈ 1 с, лестница до ≈ 3 с
    timer.start()
    try:
        rc, out = _helper(code, "gate", ["stubborn"], jid, capsys=capsys)
    finally:
        timer.cancel()
    assert rc == 124 and "прерван: timeout_s = 1" in out.out
    row = _external(code, jid)[-1]
    assert row["exit"] == 124 and row["timeout"] is True


def test_gate_signal_after_exit_keeps_code(code, capsys, monkeypatch):
    """Р10: сигнал пришёл после выхода гейта — итог не меняется, код гейта. Сигнал шлём из проверки остатка группы:
    она идёт ровно после выхода лидера."""
    _gates(code, fail=json.dumps(["sh", "-c", "exit 3"]))
    jid = _gate_job(code)

    def alive(_pgid):
        os.kill(os.getpid(), signal.SIGTERM)  # обработчик помощника ещё стоит — сигнал попадёт в stop
        return False

    monkeypatch.setattr(helper, "gate_group_alive", alive)
    rc, out = _helper(code, "gate", ["fail"], jid, capsys=capsys)
    assert rc == 3 and "прерван" not in out.out
    assert _external(code, jid)[-1]["exit"] == 3


# ---------- Г3.5. гашение группы гейта при остановке задачи ----------

def _spawn_group(script="sleep 100 & sleep 100") -> subprocess.Popen:
    return subprocess.Popen(["sh", "-c", script], start_new_session=True, stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _write_gate_pid(jdir: Path, p: subprocess.Popen, started=None):
    (jdir / jobs_mod.GATE_PID).write_text(json.dumps({"pid": p.pid, "pgid": p.pid,
                                                      "proc_start": started or proc_start(p.pid)}))


def test_kill_group_kills_gate_even_if_code_dead(code):
    jid = _gate_job(code)
    g = _spawn_group()
    try:
        assert _wait(lambda: len(_group_live(g.pid)) >= 2)
        _write_gate_pid(code.jobs.job_dir(jid), g)
        d = code.jobs.read(jid)
        d.update(pid=999999, pgid=999999, proc_start="1")  # Code уже мёртв
        code.jobs._kill_group(d)
        g.wait(timeout=15)
        assert _wait(lambda: not _group_live(g.pid))
        assert not (code.jobs.job_dir(jid) / jobs_mod.GATE_PID).exists()
    finally:
        if g.poll() is None:
            os.killpg(g.pid, signal.SIGKILL)


def test_escalate_kills_gate(code, monkeypatch):
    monkeypatch.setenv("WSHUB_CODE_CANCEL_SIGTERM_S", "0.2")
    monkeypatch.setenv("WSHUB_CODE_CANCEL_SIGKILL_S", "0.2")
    jid = _gate_job(code)
    claude = _spawn_group("sleep 100")
    g = _spawn_group()
    try:
        _write_gate_pid(code.jobs.job_dir(jid), g)
        d = code.jobs.read(jid)
        d.update(pid=claude.pid, pgid=claude.pid, proc_start=proc_start(claude.pid))
        code.jobs.write(jid, d)
        sup = runner.Supervisor(code.hub, jid)
        sup._escalate(claude)
        claude.wait(timeout=15)
        g.wait(timeout=15)
        assert _wait(lambda: not _group_live(g.pid) and not _group_live(claude.pid))
        assert not (code.jobs.job_dir(jid) / jobs_mod.GATE_PID).exists()
    finally:
        for p in (claude, g):
            if p.poll() is None:
                os.killpg(p.pid, signal.SIGKILL)


def test_kill_gate_foreign_pid_untouched(code):
    """pid из gate.pid жив, но proc_start другой — переиспользован: группу не трогаем."""
    jid = _gate_job(code)
    g = _spawn_group("sleep 100")
    try:
        _write_gate_pid(code.jobs.job_dir(jid), g, started="1")
        jobs_mod.kill_gate(code.jobs.job_dir(jid))
        time.sleep(0.3)
        assert g.poll() is None and _group_live(g.pid)
    finally:
        os.killpg(g.pid, signal.SIGKILL)
        g.wait()


def test_cancel_running_job_kills_gate(code, monkeypatch):
    """code_cancel посреди гейта: надзиратель гасит и группу из gate.pid (фейковый claude, гейт — свой сеанс)."""
    monkeypatch.setenv("WSHUB_CODE_CANCEL_SIGTERM_S", "0.5")
    monkeypatch.setenv("WSHUB_CODE_CANCEL_SIGKILL_S", "0.5")
    monkeypatch.setitem(os.environ, "FAKE_SLEEP", "30")
    jid = code.jobs.start(code.jobs.plan("proj", code.taskfile()))["id"]
    assert _wait(lambda: code.jobs.read(jid).get("state") == "running")
    g = _spawn_group()
    try:
        _write_gate_pid(code.jobs.job_dir(jid), g)
        code.jobs.cancel(jid)
        snap = wait_terminal(code.jobs, jid, timeout=60)
        assert snap["state"] == "cancelled"
        g.wait(timeout=15)
        assert _wait(lambda: not _group_live(g.pid))
    finally:
        if g.poll() is None:
            os.killpg(g.pid, signal.SIGKILL)


def test_normal_claude_exit_kills_background_gate(code, monkeypatch):
    """Т1: claude вышел сам (без отмены), а гейт ещё идёт в фоне — надзиратель гасит его после proc.wait()."""
    monkeypatch.setitem(os.environ, "FAKE_SLEEP", "3")
    jid = code.jobs.start(code.jobs.plan("proj", code.taskfile()))["id"]
    assert _wait(lambda: code.jobs.read(jid).get("state") == "running")
    g = _spawn_group()
    try:
        _write_gate_pid(code.jobs.job_dir(jid), g)
        snap = wait_terminal(code.jobs, jid, timeout=60)
        assert snap["state"] == "completed"  # обычный выход, не отмена
        g.wait(timeout=20)
        assert _wait(lambda: not _group_live(g.pid))
        assert not (code.jobs.job_dir(jid) / jobs_mod.GATE_PID).exists()
    finally:
        if g.poll() is None:
            os.killpg(g.pid, signal.SIGKILL)


def test_kill_gate_dead_leader_live_children(code):
    """Т4: лидер гейта уже вышел (и снят), а его потомки в группе живы — kill_gate гасит их."""
    jid = _gate_job(code)
    g = _spawn_group("sleep 100 & sleep 100 & exit 0")
    started = proc_start(g.pid)
    g.wait(timeout=10)  # лидер вышел и снят: proc_start(pid) — None
    try:
        assert _group_live(g.pid) and proc_start(g.pid) is None
        _write_gate_pid(code.jobs.job_dir(jid), g, started=started)
        jobs_mod.kill_gate(code.jobs.job_dir(jid))
        assert _wait(lambda: not _group_live(g.pid))
        assert not (code.jobs.job_dir(jid) / jobs_mod.GATE_PID).exists()
    finally:
        if _group_live(g.pid):
            os.killpg(g.pid, signal.SIGKILL)


@pytest.mark.parametrize("raw_state", ["completed", "running"])  # running без живых процессов — lost
def test_cancel_terminal_job_kills_gate(code, raw_state):
    """Р7: code_cancel у конечного звена (в том числе lost) с gate.pid гасит гейт в фоне и отвечает сразу;
    состояние звена не меняется."""
    jid = _gate_job(code)
    d = code.jobs.read(jid)
    d.update(state=raw_state, sup_pid=None, pid=None, result={"subtype": "success"} if raw_state == "completed"
             else None)
    code.jobs.write(jid, d)
    want = "completed" if raw_state == "completed" else "lost"
    assert code.jobs.effective_state(code.jobs.read(jid)) == want
    g = _spawn_group()
    try:
        _write_gate_pid(code.jobs.job_dir(jid), g)
        t0 = time.time()
        snap = code.jobs.cancel(jid)
        assert time.time() - t0 < 2 and snap["state"] == want  # ответ сразу, гасит процесс code-kill --gate
        g.wait(timeout=30)
        assert _wait(lambda: not _group_live(g.pid), timeout=30)
        assert code.jobs.read(jid)["state"] == raw_state and code.jobs.effective_state(code.jobs.read(jid)) == want
    finally:
        if g.poll() is None:
            os.killpg(g.pid, signal.SIGKILL)


def test_code_kill_gate_cli_checks_id(code, capsys, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)  # code-kill переходит в $HOME (home_cwd) — после теста каталог вернётся
    assert code_cli.code_main("code-kill", ["../x", "--gate"], code.cfg, code.state) == 2
    assert code_cli.code_main("code-kill", ["j", "--bad"], code.cfg, code.state) == 2


# ---------- Г4. видимость ----------

def _finished_with_gates(code) -> str:
    jid = code.jobs.start(code.jobs.plan("proj", code.taskfile()))["id"]
    wait_terminal(code.jobs, jid)
    rows = [{"ts": "t", "cmd": "gate", "args": [f"g{i}"], "exit": 1 if i % 4 == 0 else 0, "duration_s": i,
             "timeout": i == 4, "log": f"gates/{i:02d}-g{i}.log"} for i in range(12)]
    rows.append({"ts": "t", "cmd": "push", "args": ["origin", "x"], "exit": 0})
    (code.jobs.job_dir(jid) / helper.EXTERNAL).write_text("".join(json.dumps(r) + "\n" for r in rows))
    return jid


def test_external_view_gates(code):
    jid = _finished_with_gates(code)
    ex = code.jobs.external_view([code.jobs.read(jid)])
    assert ex["gate_count"] == 12 and ex["gate_failed"] == 3 and len(ex["gate"]) == jobs_mod.EXTERNAL_MAX
    assert ex["gate"][-1] == {"ts": "t", "args": ["g11"], "exit": 0, "duration_s": 11, "timeout": False}
    assert ex["gate"][2]["timeout"] is True and ex["push_count"] == 1


def test_gates_in_result_cli_and_card(code, capsys):
    jid = _finished_with_gates(code)
    out = json.loads(_call(code, "code_result", {"job": jid}))
    assert out["external"]["gate_count"] == 12 and out["external"]["gate_failed"] == 3
    assert code_cli.code_main("code", ["result", jid], code.cfg, code.state) == 0
    cli = capsys.readouterr().out
    assert '"gate_count": 12' in cli and '"gate_failed": 3' in cli
    card = code_server.card_data(code.jobs, jid, 0)
    ex = card["end"]["external"]
    assert ex["gate_count"] == 12 and ex["gate_failed"] == 3 and ex["gate"][-1]["args"] == ["g11"]


def test_card_html_renders_gates():
    html = (Path(code_server.__file__).parent / "code_card.html").read_text()
    assert "гейты: " in html and "ex.gate_failed" in html


def test_usage_lines():
    from wshub import server
    assert "wshub gate <имя>" in server.USAGE and "гейт проекта вне песочницы" in server.USAGE
    assert helper.USAGE["gate"] == "wshub gate <имя> [--out <файл>]"


def test_server_dispatches_gate(code, monkeypatch):
    env = {k: v for k, v in os.environ.items() if k != "WSHUB_JOB"}
    r = subprocess.run([sys.executable, "-m", "wshub", "gate", "x"], capture_output=True, text=True, env=env,
                       cwd=code.root, timeout=30)
    assert r.returncode == 1 and "wshub gate: отказ: нет WSHUB_JOB" in r.stderr


def test_python_m_warning_text():
    assert jobs_mod.GATES_PYTHON_M == "гейты недоступны: хаб запущен как python -m wshub"

"""wshub-code: реестр, шапка, потолки, argv/settings, окружение, жизненный цикл, отмена, таймаут,
параллелизм, orphaned/lost, resume, MCP, setup/doctor (ТЗ WSH-2, п. 5). Настоящий claude не вызывается —
вместо него tests/fake_claude.py."""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

import pytest

from wshub import claude_cli, taskfile
from wshub.core import Hub
from wshub.jobs import JobError, Jobs
from wshub.registry import RegistryError, parse

FAKE = Path(__file__).parent / "fake_claude.py"
FAKE_ENV = ["FAKE_REC", "FAKE_SLEEP", "FAKE_SIGINT", "FAKE_TOUCH", "FAKE_COST", "FAKE_SUBTYPE", "FAKE_DENIALS"]
TERMINAL = ("completed", "failed", "max_turns", "max_budget", "timeout", "cancelled", "lost")


def git(repo: Path, *args: str):
    import subprocess
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True,
                   env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@e",
                        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@e"})


def make_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    git(path, "init", "-q")
    (path / "README.md").write_text("# repo\n")
    git(path, "add", "-A")
    git(path, "commit", "-qm", "init")
    return path


def registry_text(root: Path, prompts="prompts", claude=None, **code_over) -> str:
    code = {"enabled": "true", "dirs": '["."]', "prompts": f'"{prompts}"',
            "profiles": '["recon", "task", "review"]', "model_min": '"haiku"', "model_max": '"opus"',
            "effort_max": '"high"', "max_turns": "100", "max_budget_usd": "10", "daily_budget_usd": "30",
            "max_parallel": "1", "max_hours": "4"}
    code.update(code_over)
    gcode = [f'pass_env = {json.dumps(FAKE_ENV + ["WSHUB_TESTVAR"])}']
    if claude is not None:
        gcode.append(f'claude = {claude}')
    lines = ["[defaults]", 'deny = [".env", "**/.git/config"]', "max_read_kb = 512", "ttl_hours = 8", "",
             "[code]", *gcode, "",
             '[workspace.proj]', f'path = "{root}"', 'mode = "rw"', "",
             "[workspace.proj.code]"]
    lines += [f"{k} = {v}" for k, v in code.items()]
    return "\n".join(lines) + "\n"


@pytest.fixture
def code(tmp_path, monkeypatch):
    """Проект-репозиторий, реестр с [code], шаблоны промптов, фейковый claude, Hub на временных путях."""
    root = make_repo(tmp_path / "proj")
    prompts = root / "prompts"
    prompts.mkdir()
    (prompts / "task.md").write_text("Тикет {ticket}. Файл ТЗ: {task}.\n")
    (prompts / "recon.md").write_text("Разведка {ticket}: {task}\n")
    (prompts / "review.md").write_text("Ревью {ticket}: {task}\n")
    (prompts / "cont.md").write_text("Продолжи {ticket}: {task}. {note}\n")
    cfg = tmp_path / "cfg" / "workspaces.toml"
    cfg.parent.mkdir(parents=True)
    state = tmp_path / "state"
    monkeypatch.setenv("WSHUB_CONFIG", str(cfg))
    monkeypatch.setenv("WSHUB_STATE", str(state))
    monkeypatch.setenv("WSHUB_TESTVAR", "сквозь-белый-список")
    fake = tmp_path / "fake_claude.py"
    import shutil
    shutil.copy(FAKE, fake)
    os.chmod(fake, 0o755)
    monkeypatch.setitem(os.environ, "FAKE_SLEEP", "0")

    class C:
        def __init__(self):
            self.root, self.prompts, self.cfg, self.state = root, prompts, cfg, state
            self.write()

        def write(self, **code_over):
            cl = code_over.pop("claude", f'"{fake}"')
            cfg.write_text(registry_text(root, claude=cl, **code_over), encoding="utf-8")
            self.hub = Hub(cfg, state)
            self.jobs = Jobs(self.hub)

        def taskfile(self, rel="TASK.md", profile="task", ticket="PAY-1", prompt=None, extra=""):
            p = root / rel
            hdr = f"---\nticket: {ticket}\ndir: .\nprofile: {profile}\n"
            if prompt:
                hdr += f"prompt: {prompt}\n"
            hdr += extra + "---\n# Задача\n"
            p.write_text(hdr, encoding="utf-8")
            return rel

    return C()


def wait_terminal(jobs: Jobs, jid: str, timeout=30) -> dict:
    end = time.time() + timeout
    while time.time() < end:
        snap = jobs.snapshot(jid)
        if snap["state"] in TERMINAL:
            return snap
        time.sleep(0.1)
    raise AssertionError(f"задача {jid} не завершилась за {timeout}s: {jobs.snapshot(jid)['state']}")


# ---------- 1. реестр ----------

def test_registry_code_valid(code):
    reg = parse(code.cfg.read_text())
    assert reg.code.present and not reg.code.error
    pc = reg.code.projects["proj"]
    assert pc.enabled and pc.dirs == (".",) and pc.model_max == "opus"


def test_registry_code_errors_are_scoped(code):
    code.cfg.write_text(registry_text(code.root, max_turns="0"), encoding="utf-8")
    reg = parse(code.cfg.read_text())
    assert "proj" in reg.code.project_errors and reg.code.projects.get("proj") is None
    # файловый сервер при этом работает
    assert reg.workspaces["proj"].path == code.root


def test_registry_code_global_bad_type(code):
    code.cfg.write_text(registry_text(code.root).replace("[code]\n", "[code]\nmax_wait_s = -1\n"), encoding="utf-8")
    reg = parse(code.cfg.read_text())
    assert reg.code.error and reg.workspaces  # [code] с ошибкой не роняет реестр


def test_registry_pass_env_secret_rejected(code):
    code.cfg.write_text(registry_text(code.root).replace(
        f'pass_env = {json.dumps(FAKE_ENV + ["WSHUB_TESTVAR"])}', 'pass_env = ["GIT_TOKEN"]'), encoding="utf-8")
    reg = parse(code.cfg.read_text())
    assert reg.code.error and "TOKEN" in reg.code.error


def test_dirs_outside_root_is_registry_error(code, tmp_path):
    code.write(dirs='["../outside"]')
    with pytest.raises(RegistryError):
        code.jobs._dirs(code.root, parse(code.cfg.read_text()).code.projects["proj"])


def test_dirs_overlapping_wshub_is_error(code):
    # состояние wshub внутри корня проекта → dir="." пересекается со служебным каталогом
    hub = Hub(code.cfg, code.root / "state_inside")
    jobs = Jobs(hub)
    proj = parse(code.cfg.read_text()).code.projects["proj"]  # dirs = ["."]
    with pytest.raises(RegistryError, match="служебным каталог"):
        jobs._dirs(code.root, proj)


# ---------- 2. шапка ----------

def test_header_valid():
    h = taskfile.parse_header("---\nticket: PAY-1\ndir: org\nprofile: task\nmodel: sonnet\n---\nтело")
    assert h == {"ticket": "PAY-1", "dir": "org", "profile": "task", "model": "sonnet"}


@pytest.mark.parametrize("text,msg", [
    ("тело без шапки", "нет шапки"),
    ("---\ndir: org\nprofile: task\n---\n", "обязательных"),
    ("---\nticket: P\ndir: o\nprofile: task\nxx: 1\n---\n", "неизвестный ключ"),
    ("---\nticket: P\ndir: o\nprofile: task\n  nested: 1\n---\n", "вложенность"),
    ("---\nticket: P\ndir: o\nprofile: task\nmodel: [a, b]\n---\n", "списки"),
    ("---\nticket: P\ndir: o\nprofile: task\n", "не закрыта"),
])
def test_header_errors(text, msg):
    with pytest.raises(taskfile.TaskError) as e:
        taskfile.parse_header(text)
    assert msg in str(e.value)


def test_render_unknown_placeholder_and_note():
    with pytest.raises(taskfile.TaskError):
        taskfile.render("{ticket} {oops}", ticket="P", task="t", note=None)
    with pytest.raises(taskfile.TaskError):
        taskfile.render("{note}", ticket="P", task="t", note=None)
    out, warn = taskfile.render("{ticket}:{task}", ticket="P", task="t.md", note=None)
    assert out == "P:t.md" and warn == []


def test_plan_refused_when_not_enabled(code):
    code.write(enabled="false")
    with pytest.raises(JobError, match="не включён"):
        code.jobs.plan("proj", code.taskfile())


def test_plan_refused_dir_not_in_code_dirs(code):
    sub = code.root / "sub"
    sub.mkdir()
    t = sub / "T.md"
    t.write_text("---\nticket: P\ndir: sub\nprofile: task\n---\n#\n")  # dir=sub, а code.dirs=["."]
    with pytest.raises(JobError, match="не в code.dirs"):
        code.jobs.plan("proj", "sub/T.md")


def test_plan_taskfile_outside_dir(code, tmp_path):
    # ТЗ-симлинк на файл вне dir: resolve() уводит наружу → отказ
    outside = tmp_path / "outside.md"
    outside.write_text("---\nticket: P\ndir: .\nprofile: task\n---\n")
    link = code.root / "link.md"
    link.symlink_to(outside)
    with pytest.raises(JobError):
        code.jobs.plan("proj", "link.md")


# ---------- 3. потолки и пол ----------

def test_ceilings(code):
    code.write(model_max='"sonnet"', model_min='"sonnet"', effort_max='"medium"', max_turns="50",
               max_budget_usd="5")
    t = code.taskfile()
    with pytest.raises(JobError, match="выше model_max"):
        code.jobs.plan("proj", t, model="opus")
    with pytest.raises(JobError, match="ниже model_min"):
        code.jobs.plan("proj", t, model="haiku")
    with pytest.raises(JobError, match="effort"):
        code.jobs.plan("proj", t, effort="high")
    with pytest.raises(JobError, match="max_turns"):
        code.jobs.plan("proj", t, max_turns=200)
    with pytest.raises(JobError, match="budget"):
        code.jobs.plan("proj", t, budget_usd=99)
    p = code.jobs.plan("proj", t, model="sonnet", effort="medium", max_turns=10, budget_usd=1)
    assert p.model == "sonnet" and p.max_turns == 10


# ---------- 4. argv и settings ----------

def test_settings_profiles(code):
    from wshub.registry import CodeGlobal, ProjectCode
    glob = CodeGlobal()
    proj = ProjectCode(enabled=True, dirs=(".",), prompts="p", deny_edit=("docker-compose*.yml",))
    protected = [Path("/home/u/.config/wshub"), Path("/home/u/.local/state/wshub")]
    for profile, must in (("recon", ["Read(./**)", "Edit(./TASK.md)"]),
                          ("task", ["Edit(./**)", "Task", "Bash(git commit *)"]),
                          ("review", ["Edit(./.agents/**)", "Task"])):
        s = claude_cli.settings(profile, "TASK.md", glob, proj, protected, protected, ["/w"], Path("/home/u"),
                                Path("/home/u/proj"))
        allow = s["permissions"]["allow"]
        assert all(m in allow for m in must), (profile, allow)
        assert not any(t in allow for t in ("Read", "Edit", "Write", "Grep", "Glob", "NotebookEdit")), profile
        deny = s["permissions"]["deny"]
        assert "Bash(git push *)" in deny and "WebFetch" in deny
        assert "Edit(docker-compose*.yml)" in deny and "Edit(./.git/config)" in deny
        assert s["sandbox"]["autoAllowBashIfSandboxed"] is False
        assert "/home/u/proj" in s["sandbox"]["filesystem"]["allowRead"]
        assert "~/" in s["sandbox"]["filesystem"]["denyRead"]
        for p in protected:
            assert f"Read(/{p}/**)" in deny and f"Edit(/{p}/**)" in deny
    # recon правит только файл ТЗ
    s = claude_cli.settings("recon", "TASK.md", glob, proj, protected, protected, ["/w"], Path("/h"), Path("/h/p"))
    assert "Edit(./**)" not in s["permissions"]["allow"]


def test_settings_no_fileless_rules_and_git_protected(code):
    """К кейсу 4 (правка архитектора): нет allow без пути; для каждого служебного каталога — deny Read+Edit."""
    from wshub.registry import CodeGlobal, ProjectCode
    protected = [Path("/srv/a"), Path("/srv/b")]
    s = claude_cli.settings("task", "T.md", CodeGlobal(deny_read=("~/.ssh",)), ProjectCode(), protected,
                            protected, ["/w"], Path("/home/u"), Path("/d"))
    allow, deny = s["permissions"]["allow"], s["permissions"]["deny"]
    assert not ({"Read", "Edit", "Write", "Grep", "Glob", "NotebookEdit"} & set(allow))
    for p in protected:
        assert f"Read(/{p}/**)" in deny and f"Edit(/{p}/**)" in deny
    assert "Read(//home/u/.ssh/**)" in deny


def test_argv_session_and_resume(code):
    flags = {"permission_prompts": True, "setting_sources": True}
    a = claude_cli.argv("claude", "p", job_dir=Path("/j"), model="sonnet", effort="high", max_turns=5,
                        budget_usd=2, flags=flags, session_id="SID")
    assert "--session-id" in a and a[a.index("--session-id") + 1] == "SID"
    assert "--permission-mode" in a and a[a.index("--permission-mode") + 1] == "dontAsk"
    assert "--setting-sources" in a and "--strict-mcp-config" in a
    r = claude_cli.argv("claude", "p", job_dir=Path("/j"), model="sonnet", effort="high", max_turns=5,
                        budget_usd=2, flags=flags, resume="OLD")
    assert "--resume" in r and "--session-id" not in r


# ---------- 5. окружение ----------

def test_environment_whitelist():
    env = {"HOME": "/h", "PATH": "/bin", "LC_ALL": "C", "SSH_AUTH_SOCK": "/s", "ANTHROPIC_API_KEY": "k",
           "GITLAB_TOKEN": "t", "X_SECRET": "s", "MY_PASSWORD": "p", "WSHUB_TESTVAR": "v", "OTHER": "o"}
    out = claude_cli.environment(env, ("WSHUB_TESTVAR",))
    assert out["HOME"] == "/h" and out["WSHUB_TESTVAR"] == "v" and out["TERM"] == "dumb"
    for k in ("SSH_AUTH_SOCK", "ANTHROPIC_API_KEY", "GITLAB_TOKEN", "X_SECRET", "MY_PASSWORD", "OTHER"):
        assert k not in out


def test_environment_reaches_fake_without_secrets(code, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    monkeypatch.setenv("GITLAB_TOKEN", "t")
    monkeypatch.setenv("X_SECRET", "s")
    monkeypatch.setenv("FAKE_REC", str(code.state / "rec.json"))
    t = code.taskfile()
    d = code.jobs.start(code.jobs.plan("proj", t))
    wait_terminal(code.jobs, d["id"])
    rec = json.loads((code.state / "rec.json").read_text())
    assert rec["env"].get("WSHUB_TESTVAR") == "сквозь-белый-список"
    for k in ("ANTHROPIC_API_KEY", "GITLAB_TOKEN", "X_SECRET", "SSH_AUTH_SOCK"):
        assert k not in rec["env"]


# ---------- 6. жизненный цикл ----------

def test_lifecycle_completed(code, monkeypatch):
    monkeypatch.setenv("FAKE_TOUCH", "made.txt")
    monkeypatch.setenv("FAKE_COST", "1.25")
    t = code.taskfile()
    d = code.jobs.start(code.jobs.plan("proj", t))
    assert d["state"] == "starting" and d["session_id"]
    snap = wait_terminal(code.jobs, d["id"])
    assert snap["state"] == "completed"
    res = code.jobs.read(d["id"])["result"]
    assert res["total_cost_usd"] == 1.25 and res["num_turns"] == 2
    assert res["session_id"] == d["session_id"]
    assert "made.txt" in res["git"]["status"]
    # строка code_finish в журнале, без текста промпта
    audit = (code.state / "audit.jsonl").read_text()
    assert "code_finish" in audit and "prompt_sha256" in audit and "Тикет PAY-1" not in audit


def test_permission_denials_surface_in_result(code, monkeypatch):
    """Прокидывание отказов инструментов в итог (наблюдаемое критерия 4)."""
    monkeypatch.setenv("FAKE_DENIALS", "Bash,WebFetch")
    d = code.jobs.start(code.jobs.plan("proj", code.taskfile()))
    wait_terminal(code.jobs, d["id"])
    res = code.jobs.read(d["id"])["result"]
    assert res["permission_denials_count"] == 2
    assert {x["tool_name"] for x in res["permission_denials"]} == {"Bash", "WebFetch"}


def test_terminal_cli_parity(code, monkeypatch, capsys):
    """Критерий 6: wshub code start из терминала даёт ту же задачу с теми же настройками, что code_start."""
    from wshub import code_cli
    t = code.taskfile(profile="task")
    rc = code_cli.code_main("code", ["start", "proj", t, "--budget-usd", "3", "--model", "sonnet"],
                            code.cfg, code.state)
    assert rc == 0
    jid = capsys.readouterr().out.strip().splitlines()[0]
    wait_terminal(code.jobs, jid)
    d = code.jobs.read(jid)
    assert d["budget_usd"] == 3 and d["model"] == "sonnet" and d["profile"] == "task"
    # settings.json — как у code_start (та же генерация claude_cli.settings)
    settings = json.loads((code.jobs.job_dir(jid) / "settings.json").read_text())
    assert "Edit(./**)" in settings["permissions"]["allow"]
    assert "Bash(git push *)" in settings["permissions"]["deny"]
    assert settings["sandbox"]["autoAllowBashIfSandboxed"] is False


# ---------- 7. отмена ----------

def test_cancel_sigint(code, monkeypatch):
    monkeypatch.setenv("FAKE_SLEEP", "30")
    monkeypatch.setenv("FAKE_SIGINT", "exit")
    t = code.taskfile()
    before = (code.root / t).read_bytes()
    d = code.jobs.start(code.jobs.plan("proj", t))
    _wait_running(code.jobs, d["id"])
    code.jobs.cancel(d["id"])
    snap = wait_terminal(code.jobs, d["id"])
    assert snap["state"] == "cancelled"
    assert (code.root / t).read_bytes() == before  # файл ТЗ цел (критерий 2)
    _assert_no_fake()


def test_cancel_escalates_to_kill(code, monkeypatch):
    monkeypatch.setenv("FAKE_SLEEP", "60")
    monkeypatch.setenv("FAKE_SIGINT", "ignore")
    d = code.jobs.start(code.jobs.plan("proj", code.taskfile()))
    _wait_running(code.jobs, d["id"])
    code.jobs.cancel(d["id"])
    snap = wait_terminal(code.jobs, d["id"], timeout=60)
    assert snap["state"] == "cancelled"
    _assert_no_fake()


# ---------- 8. таймаут ----------

def test_timeout(code, monkeypatch):
    monkeypatch.setenv("FAKE_SLEEP", "60")
    monkeypatch.setenv("FAKE_SIGINT", "exit")
    code.write(max_hours=f"{2/3600}")  # 2 секунды
    d = code.jobs.start(code.jobs.plan("proj", code.taskfile()))
    snap = wait_terminal(code.jobs, d["id"], timeout=60)
    assert snap["state"] == "timeout"
    _assert_no_fake()


# ---------- 9. параллелизм и дневной бюджет ----------

def test_parallel_barrier(code, monkeypatch):
    monkeypatch.setenv("FAKE_SLEEP", "10")
    code.write(max_parallel="1")
    results, barrier = [], threading.Barrier(2)

    def go():
        barrier.wait()
        try:
            results.append(("ok", code.jobs.start(code.jobs.plan("proj", code.taskfile()))["id"]))
        except JobError as e:
            results.append(("err", str(e)))

    ts = [threading.Thread(target=go) for _ in range(2)]
    for x in ts:
        x.start()
    for x in ts:
        x.join()
    kinds = sorted(k for k, _ in results)
    assert kinds == ["err", "ok"], results
    for k, v in results:
        if k == "ok":
            code.jobs.cancel(v)
            wait_terminal(code.jobs, v, timeout=60)
    _assert_no_fake()


def test_daily_budget(code, monkeypatch):
    monkeypatch.setenv("FAKE_SLEEP", "10")
    code.write(max_parallel="5", daily_budget_usd="7", max_budget_usd="5")
    d1 = code.jobs.start(code.jobs.plan("proj", code.taskfile("A.md"), budget_usd=5))
    _wait_running(code.jobs, d1["id"])
    with pytest.raises(JobError, match="дневной бюджет"):
        code.jobs.start(code.jobs.plan("proj", code.taskfile("B.md"), budget_usd=5))
    code.jobs.cancel(d1["id"])
    wait_terminal(code.jobs, d1["id"], timeout=60)


# ---------- 10. orphaned / lost ----------

def test_orphaned_then_cancel(code, monkeypatch):
    monkeypatch.setenv("FAKE_SLEEP", "30")
    monkeypatch.setenv("FAKE_SIGINT", "exit")
    d = code.jobs.start(code.jobs.plan("proj", code.taskfile()))
    _wait_running(code.jobs, d["id"])
    jd = code.jobs.read(d["id"])
    os.kill(jd["sup_pid"], 9)  # убиваем надзирателя, Code жив
    time.sleep(0.3)
    assert code.jobs.snapshot(d["id"])["state"] == "orphaned"
    code.jobs.cancel(d["id"])  # code_cancel сам гасит группу Code
    end = time.time() + 40
    while time.time() < end and _fake_running():
        time.sleep(0.2)
    _assert_no_fake()


def test_lost(code, monkeypatch):
    monkeypatch.setenv("FAKE_SLEEP", "30")
    d = code.jobs.start(code.jobs.plan("proj", code.taskfile()))
    _wait_running(code.jobs, d["id"])
    jd = code.jobs.read(d["id"])
    os.kill(jd["sup_pid"], 9)
    os.killpg(jd["pgid"], 9)
    time.sleep(0.3)
    assert code.jobs.snapshot(d["id"])["state"] == "lost"


# ---------- 11. resume ----------

def test_resume(code, monkeypatch):
    t = code.taskfile()
    d = code.jobs.start(code.jobs.plan("proj", t))
    snap = wait_terminal(code.jobs, d["id"])
    sid = snap["session_id"]
    monkeypatch.setenv("FAKE_REC", str(code.state / "resume-rec.json"))
    r = code.jobs.resume(d["id"], "продолжай")
    assert r["parent"] == d["id"] and r["session_id"] == sid
    wait_terminal(code.jobs, r["id"])
    rec = json.loads((code.state / "resume-rec.json").read_text())
    assert "--resume" in rec["argv"] and sid in rec["argv"]
    assert "--session-id" not in rec["argv"]


def test_resume_parent_alive_refused(code, monkeypatch):
    monkeypatch.setenv("FAKE_SLEEP", "20")
    d = code.jobs.start(code.jobs.plan("proj", code.taskfile()))
    _wait_running(code.jobs, d["id"])
    with pytest.raises(JobError, match="ещё жив"):
        code.jobs.resume(d["id"], "продолжай")
    code.jobs.cancel(d["id"])
    wait_terminal(code.jobs, d["id"], timeout=60)


# ---------- 14. doctor ----------

def test_doctor_code_info_when_unused(code):
    from wshub import doctor
    code.cfg.write_text("[defaults]\ndeny = []\nmax_read_kb = 512\nttl_hours = 8\n", encoding="utf-8")
    hub = Hub(code.cfg, code.state)
    ctx = doctor.Ctx(config=code.cfg, state=code.state, win_users=code.state / "none")
    c = doctor.check_code(ctx, hub.registry.get(), hub)
    assert c["status"] == "info" and "не настроен" in c["fix"]


def test_doctor_code_fail_when_enabled_no_claude(code):
    from wshub import doctor
    code.write(claude='"/nonexistent/claude"')
    ctx = doctor.Ctx(config=code.cfg, state=code.state, win_users=code.state / "none")
    c = doctor.check_code(ctx, code.hub.registry.get(), code.hub)
    assert c["status"] == "fail" and any("claude не найден" in d for d in c["detail"])


# ---------- хук (заглушка; полный разбор команд пишет архитектор, п. 2.6 / кейс 12) ----------

def test_hook_stub_fail_closed():
    import io

    from wshub import hook
    bash = io.StringIO(json.dumps({"tool_name": "Bash", "tool_input": {"command": "git status"}}))
    err = io.StringIO()
    assert hook.main([], stdin=bash, stderr=err) == 2  # заглушка отклоняет любой Bash
    other = io.StringIO(json.dumps({"tool_name": "Read", "tool_input": {"file_path": "x"}}))
    assert hook.main([], stdin=other, stderr=io.StringIO()) == 0
    broken = io.StringIO("не json")
    assert hook.main([], stdin=broken, stderr=io.StringIO()) == 2  # ошибка разбора — тоже отказ


# ---------- вспомогательное ----------

def _wait_running(jobs: Jobs, jid: str, timeout=15):
    end = time.time() + timeout
    while time.time() < end:
        if jobs.read(jid).get("pid") and jobs.snapshot(jid)["state"] in ("running", "orphaned"):
            return
        time.sleep(0.05)
    raise AssertionError(f"задача {jid} не вышла в running")


def _fake_running() -> bool:
    proc = Path("/proc")
    for p in proc.iterdir() if proc.is_dir() else []:
        if not p.name.isdigit():
            continue
        try:
            cmd = (p / "cmdline").read_bytes()
            stat = (p / "stat").read_text()
        except OSError:
            continue
        state = stat.rsplit(")", 1)[-1].split()
        if b"fake_claude.py" in cmd and state and state[0] != "Z":
            return True
    return False


def _assert_no_fake():
    end = time.time() + 5
    while time.time() < end and _fake_running():
        time.sleep(0.2)
    assert not _fake_running(), "процессы fake_claude остались"

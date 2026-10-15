"""wshub-code: реестр, шапка, потолки, argv/settings, окружение, жизненный цикл, отмена, таймаут,
параллелизм, orphaned/lost, resume, MCP, setup/doctor (ТЗ WSH-2, п. 5). Настоящий claude не вызывается —
вместо него tests/fake_claude.py."""
from __future__ import annotations

import json
import os
import sys
import threading
import time
from pathlib import Path

import pytest

from wshub import claude_cli, taskfile
from wshub.core import Hub
from wshub.jobs import JobError, Jobs
from wshub.registry import RegistryError, parse

FAKE = Path(__file__).parent / "fake_claude.py"
FAKE_ENV = ["FAKE_REC", "FAKE_SLEEP", "FAKE_SIGINT", "FAKE_TOUCH", "FAKE_COST", "FAKE_SUBTYPE",
            "FAKE_DENIALS", "FAKE_SIGMARK", "FAKE_RECLOG", "FAKE_NO_PLAN", "FAKE_PLAN", "FAKE_COST_PLAN",
            "FAKE_SLEEP_PLAN", "FAKE_SELF_CANCEL", "FAKE_DOCKER", "FAKE_GIT_META", "FAKE_COMMIT"]
TERMINAL = ("completed", "failed", "max_turns", "max_budget", "timeout", "cancelled", "lost", "plan_ready")


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
    return make_code(tmp_path, monkeypatch)


def make_code(tmp_path, monkeypatch):
    """Проект-репозиторий, реестр с [code], шаблоны промптов, фейковый claude, Hub на временных путях."""
    root = make_repo(tmp_path / "proj")
    prompts = root / "prompts"
    prompts.mkdir()
    (prompts / "task.md").write_text("Тикет {ticket}. Файл ТЗ: {task}.\n")
    (prompts / "recon.md").write_text("Разведка {ticket}: {task}\n")
    (prompts / "review.md").write_text("Ревью {ticket}: {task}\n")
    (prompts / "cont.md").write_text("Продолжи {ticket}: {task}. {note}\n")
    (prompts / "execute.md").write_text("Исполни утверждённый план {ticket} по {task}.\n")
    (prompts / "replan.md").write_text("Переделай план {ticket}: {note}\n")
    # фейк пишет планы в $HOME/.claude/plans и журнал стоимости сессий — только во временный HOME
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
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

        def taskfile(self, rel="TASK.md", profile="task", ticket="PAY-1", prompt=None, extra="", size="S",
                     risk="low"):
            """size/risk по умолчанию S/low — строка политики без плана (ТЗ WSH-3); None — ключа нет."""
            p = root / rel
            hdr = f"---\nticket: {ticket}\ndir: .\nprofile: {profile}\n"
            hdr += f"size: {size}\n" if size else ""
            hdr += f"risk: {risk}\n" if risk else ""
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
    # model/effort без признака оператора — отступление чата: в пределах потолков и с причиной (WSH-3, 2.2.3)
    p = code.jobs.plan("proj", t, model="sonnet", effort="medium", max_turns=10, budget_usd=1,
                       reason="проверка потолков реестра")
    assert p.model == "sonnet" and p.max_turns == 10


# ---------- 4. argv и settings ----------

GOLDEN = json.loads((Path(__file__).parent / "golden_settings.json").read_text(encoding="utf-8"))


def _golden_settings(profile):
    from wshub.registry import CodeGlobal, ProjectCode
    glob = CodeGlobal()
    proj = ProjectCode(enabled=True, dirs=(".",), prompts="p", deny_edit=("docker-compose*.yml",))
    protected = [Path("/home/u/.config/wshub"), Path("/home/u/.local/state/wshub")]
    return claude_cli.settings(profile, "org/TASK.md", glob, proj, protected, protected,
                               ["/py", "-m", "wshub.hook"], Path("/home/u"), Path("/home/u/proj"))


@pytest.mark.parametrize("profile", ["recon", "task", "review"])
def test_settings_golden(profile):
    """Кейс 4 (замечание 2): весь settings.json каждого профиля сверяется с эталоном целиком.
    Мутации (убрать --settings/правило/denyRead /mnt/, failIfUnavailable=false, сменить matcher) краснят."""
    assert _golden_settings(profile) == GOLDEN[profile]


def test_argv_golden():
    """Полный argv для старта и для resume — целиком против эталона (замечание 2)."""
    flags = {"permission_prompts": True, "setting_sources": True}
    start = claude_cli.argv("/claude", "PROMPT", job_dir=Path("/j"), model="sonnet", effort="high",
                            max_turns=80, budget_usd=5, flags=flags, session_id="SID")
    resume = claude_cli.argv("/claude", "PROMPT", job_dir=Path("/j"), model="sonnet", effort="high",
                             max_turns=80, budget_usd=5, flags=flags, resume="OLD")
    assert start == GOLDEN["argv_start"]
    assert resume == GOLDEN["argv_resume"]


def test_no_fileless_file_rules(code):
    """Ни в одном профиле нет allow-правила файлового инструмента без пути (кейс 4)."""
    for profile in ("recon", "task", "review"):
        allow = _golden_settings(profile)["permissions"]["allow"]
        assert not ({"Read", "Edit", "Write", "Grep", "Glob", "NotebookEdit"} & set(allow)), (profile, allow)


def test_hook_command_denies_push(code, tmp_path):
    """Строку хука берём из сгенерированного settings.json, запускаем с событием git push → код 2 (замечание 2)."""
    import shlex
    import subprocess
    cmd = _golden_settings("task")["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
    parts = shlex.split(cmd)
    # заменяем питон эталона на реальный и прогоняем модуль хука со служебными каталогами из строки
    argv = [sys.executable, *parts[1:]]
    event = json.dumps({"tool_name": "Bash", "tool_input": {"command": "git push origin main"}, "cwd": "/home/u/proj"})
    r = subprocess.run(argv, input=event, capture_output=True, text=True,
                       env={**os.environ, "PYTHONPATH": str(Path(__file__).parent.parent / "src")})
    assert r.returncode == 2, (r.returncode, r.stderr)
    ok = json.dumps({"tool_name": "Bash", "tool_input": {"command": "git status"}, "cwd": "/home/u/proj"})
    r2 = subprocess.run(argv, input=ok, capture_output=True, text=True,
                        env={**os.environ, "PYTHONPATH": str(Path(__file__).parent.parent / "src")})
    assert r2.returncode == 0, (r2.returncode, r2.stderr)


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


def test_environment_second_filter_blocks_secret_in_pass_env():
    """Второй фильтр _secret_name (замечание 12): даже если секрет просочился в pass_env, в Code он не идёт."""
    out = claude_cli.environment({"API_KEY": "x", "HOME": "/h"}, ("API_KEY",))
    assert "API_KEY" not in out


def test_environment_reaches_fake_without_secrets(code, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    monkeypatch.setenv("GITLAB_TOKEN", "t")
    monkeypatch.setenv("X_SECRET", "s")
    monkeypatch.setenv("SSH_AUTH_SOCK", "/tmp/agent.sock")  # явно задан — проверка не пустая (замечание 12)
    monkeypatch.setenv("FAKE_REC", str(code.state / "rec.json"))
    t = code.taskfile()
    d = code.jobs.start(code.jobs.plan("proj", t))
    wait_terminal(code.jobs, d["id"])
    rec = json.loads((code.state / "rec.json").read_text())
    assert rec["env"].get("WSHUB_TESTVAR") == "сквозь-белый-список"
    assert os.environ["SSH_AUTH_SOCK"] == "/tmp/agent.sock"  # в сервере был
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


def test_real_job_settings_wiring(code):
    """jobs.start передаёт в settings.json правильную строку хука и deny служебных каталогов (не только
    чистая функция settings() из golden): замечание второго ревью."""
    d = code.jobs.start(code.jobs.plan("proj", code.taskfile()))
    wait_terminal(code.jobs, d["id"])
    s = json.loads((code.jobs.job_dir(d["id"]) / "settings.json").read_text())
    hook = s["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
    assert "-m wshub.hook" in hook  # лёгкий запуск (замечание 9)
    assert str(code.state) in hook  # служебный каталог состояния передан хуку аргументом
    deny = s["permissions"]["deny"]
    # реальные служебные каталоги wshub (из Hub.protected) закрыты для файловых инструментов
    assert any(str(code.state) in r and r.startswith("Read(//") for r in deny)
    assert s["sandbox"]["failIfUnavailable"] is True and s["sandbox"]["autoAllowBashIfSandboxed"] is False


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


def test_cancel_escalates_to_sigterm(code, monkeypatch):
    """Фейк игнорирует SIGINT, умирает по SIGTERM: эскалация дошла до SIGTERM (мутация «без SIGTERM» краснит)."""
    mark = code.state / "sig.txt"
    monkeypatch.setenv("FAKE_SLEEP", "60")
    monkeypatch.setenv("FAKE_SIGINT", "ignore")
    monkeypatch.setenv("FAKE_SIGMARK", str(mark))
    monkeypatch.setenv("WSHUB_CODE_CANCEL_SIGTERM_S", "1")
    monkeypatch.setenv("WSHUB_CODE_CANCEL_SIGKILL_S", "10")
    d = code.jobs.start(code.jobs.plan("proj", code.taskfile()))
    _wait_running(code.jobs, d["id"])
    code.jobs.cancel(d["id"])
    snap = wait_terminal(code.jobs, d["id"], timeout=30)
    assert snap["state"] == "cancelled"
    assert mark.exists() and mark.read_text() == "TERM"  # дошло именно до SIGTERM, не до SIGINT
    _assert_no_fake()


def test_cancel_escalates_to_sigkill(code, monkeypatch):
    """Фейк игнорирует SIGINT и SIGTERM: остановит только SIGKILL, в пределах времени (мутация «без SIGKILL»)."""
    mark = code.state / "sig2.txt"
    monkeypatch.setenv("FAKE_SLEEP", "60")
    monkeypatch.setenv("FAKE_SIGINT", "ignore-all")
    monkeypatch.setenv("FAKE_SIGMARK", str(mark))
    monkeypatch.setenv("WSHUB_CODE_CANCEL_SIGTERM_S", "1")
    monkeypatch.setenv("WSHUB_CODE_CANCEL_SIGKILL_S", "1")
    d = code.jobs.start(code.jobs.plan("proj", code.taskfile()))
    _wait_running(code.jobs, d["id"])
    t0 = time.time()
    code.jobs.cancel(d["id"])
    snap = wait_terminal(code.jobs, d["id"], timeout=30)
    assert snap["state"] == "cancelled"
    assert time.time() - t0 < 30  # предел ≤ 30 с (критерий 2)
    assert not mark.exists()  # ни SIGINT, ни SIGTERM не привели к выходу — только SIGKILL
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


def test_prefetch_fail_under_sigchld_ign(code, monkeypatch):
    """Замечание 1: даже если сервер игнорировал SIGCHLD (наследуется надзирателем через exec), prefetch
    с битым remote заканчивается failed/prefetch — надзиратель сбрасывает SIGCHLD в SIG_DFL."""
    import signal
    git(code.root, "remote", "add", "origin", str(code.root / "no-such-remote.git"))
    code.write(prefetch="true")
    old = signal.getsignal(signal.SIGCHLD)
    signal.signal(signal.SIGCHLD, signal.SIG_IGN)  # как в сервере — надзиратель не должен это унаследовать
    try:
        d = code.jobs.start(code.jobs.plan("proj", code.taskfile()))
        snap = wait_terminal(code.jobs, d["id"])
    finally:
        signal.signal(signal.SIGCHLD, old)
    assert snap["state"] == "failed" and code.jobs.read(d["id"])["reason"] == "prefetch"


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
    code.jobs.cancel(d["id"])  # code_cancel спавнит отдельный процесс wshub code-kill (не fork сервера)
    # ждём, пока killer погасит Code и запишет именно cancelled (между смертью Code и записью — миг lost)
    end = time.time() + 40
    while time.time() < end and code.jobs.snapshot(d["id"])["state"] != "cancelled":
        time.sleep(0.2)
    assert code.jobs.snapshot(d["id"])["state"] == "cancelled"  # не «lost» (мёртвая ветка исправлена)
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
    r = code.jobs.resume(d["id"], "task", note="продолжай после BLOCKED")  # prompt — ИМЯ шаблона (замечание 6)
    assert r["parent"] == d["id"] and r["session_id"] == sid
    wait_terminal(code.jobs, r["id"])
    rec = json.loads((code.state / "resume-rec.json").read_text())
    assert "--resume" in rec["argv"] and sid in rec["argv"]
    assert "--session-id" not in rec["argv"]


def test_resume_bad_template_name_refused(code):
    d = code.jobs.start(code.jobs.plan("proj", code.taskfile()))
    wait_terminal(code.jobs, d["id"])
    with pytest.raises((JobError, taskfile.TaskError)):
        code.jobs.resume(d["id"], "../etc/passwd")  # недопустимое имя шаблона
    with pytest.raises(JobError, match="шаблон промпта не найден"):
        code.jobs.resume(d["id"], "nosuchtemplate")


def test_resume_parent_alive_refused(code, monkeypatch):
    monkeypatch.setenv("FAKE_SLEEP", "20")
    d = code.jobs.start(code.jobs.plan("proj", code.taskfile()))
    _wait_running(code.jobs, d["id"])
    with pytest.raises(JobError, match="состоянии"):
        code.jobs.resume(d["id"], "task")
    code.jobs.cancel(d["id"])
    wait_terminal(code.jobs, d["id"], timeout=60)


def test_resume_refused_when_orphaned(code, monkeypatch):
    """Замечание 5: родитель orphaned (надзиратель мёртв, Code жив) — resume отказан."""
    monkeypatch.setenv("FAKE_SLEEP", "30")
    monkeypatch.setenv("FAKE_SIGINT", "exit")
    d = code.jobs.start(code.jobs.plan("proj", code.taskfile()))
    _wait_running(code.jobs, d["id"])
    jd = code.jobs.read(d["id"])
    os.kill(jd["sup_pid"], 9)
    time.sleep(0.3)
    assert code.jobs.snapshot(d["id"])["state"] == "orphaned"
    with pytest.raises(JobError, match="состоянии orphaned"):
        code.jobs.resume(d["id"], "task")
    code.jobs.cancel(d["id"])
    _assert_no_fake()


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


# ---------- замечания ревью: требования без тестов ----------

def test_doctor_not_file_server_for_code(code):
    """Замечание 4: процессы wshub code* не считаются файловым сервером (иначе doctor уходит в «внимание»)."""
    from wshub.doctor import _is_wshub_cmd
    assert _is_wshub_cmd(["wshub"]) is True
    assert _is_wshub_cmd(["wshub", "code"]) is False
    assert _is_wshub_cmd(["wshub", "code-run", "20260101-000000-abcdef"]) is False
    assert _is_wshub_cmd(["wshub", "code-hook", "/x"]) is False
    assert _is_wshub_cmd(["wshub", "doctor"]) is False
    assert _is_wshub_cmd(["python", "-m", "wshub", "code-run", "x"]) is False
    assert _is_wshub_cmd(["python", "-m", "wshub"]) is True


def test_audit_every_tool(code):
    """Замечание 7: каждый инструмент MCP пишет строку в журнал; отказ — со status error."""
    import asyncio

    from wshub import code_server
    mcp = code_server.build_code_server(code.hub)

    def call(name, args):
        asyncio.run(mcp.call_tool(name, args))

    d = code.jobs.start(code.jobs.plan("proj", code.taskfile()))
    wait_terminal(code.jobs, d["id"])
    call("code_projects", {})
    call("code_status", {"job": d["id"]})
    call("code_result", {"job": d["id"]})
    call("code_jobs", {})
    call("code_wait", {"job": d["id"], "seconds": 1})
    call("code_cancel", {"job": d["id"]})
    call("code_status", {"job": "нет-такой"})  # отказ
    lines = [json.loads(x) for x in (code.state / "audit.jsonl").read_text().splitlines()]
    tools = {r["tool"] for r in lines}
    for t in ("code_projects", "code_status", "code_result", "code_jobs", "code_wait", "code_cancel"):
        assert t in tools, t
    assert any(r["tool"] == "code_status" and r.get("status") == "error" for r in lines)


def test_code_status_turns(code):
    """Замечание 16: code_status отдаёт число ходов по событиям."""
    import asyncio

    from wshub import code_server
    d = code.jobs.start(code.jobs.plan("proj", code.taskfile()))
    wait_terminal(code.jobs, d["id"])
    mcp = code_server.build_code_server(code.hub)
    out = asyncio.run(mcp.call_tool("code_status", {"job": d["id"]}))
    content = out[0] if isinstance(out, tuple) else out
    snap = json.loads(content[0].text)
    assert snap["turns"] == 2  # фейк шлёт два события assistant


def test_terminal_cli_bad_header_no_traceback(code, capsys):
    """Замечание 14: плохая шапка — «wshub code: отказ: …» и код 1, без трассировки."""
    from wshub import code_cli
    (code.root / "BAD.md").write_text("нет шапки\n", encoding="utf-8")
    rc = code_cli.code_main("code", ["start", "proj", "BAD.md"], code.cfg, code.state)
    err = capsys.readouterr().err
    assert rc == 1 and "отказ:" in err and "Traceback" not in err


def test_sup_pid_survives_in_job_json(code):
    """Замечание 20: надзиратель не затирает sup_pid/sup_start, записанные родителем (перечитывает под flock)."""
    d = code.jobs.start(code.jobs.plan("proj", code.taskfile()))
    wait_terminal(code.jobs, d["id"])
    jd = code.jobs.read(d["id"])
    assert jd.get("sup_pid") and jd.get("sup_start")  # были бы потеряны при затирании → ложный orphaned/lost


def test_dirs_must_be_git_root(code):
    """Замечание 3: каждый dir — корень рабочего дерева git (есть каталог .git), иначе ошибка реестра."""
    (code.root / "sub").mkdir()  # подпапка без .git
    code.write(dirs='["sub"]')
    proj = parse(code.cfg.read_text()).code.projects["proj"]
    with pytest.raises(RegistryError, match="не корень git"):
        code.jobs._dirs(code.root, proj)


def test_dirs_symlink_outside_root(code, tmp_path):
    """Замечание 12: dir-симлинк наружу — отказ «вне корня» (а не из-за отсутствия каталога)."""
    outside = make_repo(tmp_path / "ext")
    (code.root / "link").symlink_to(outside)
    code.write(dirs='["link"]')
    proj = parse(code.cfg.read_text()).code.projects["proj"]
    with pytest.raises(RegistryError, match="вне корня"):
        code.jobs._dirs(code.root, proj)


def test_status_regex_reads_taskfile_not_output(code, monkeypatch):
    """Замечание 8: статус берётся из ФАЙЛА ТЗ, не из текста Code."""
    code.write(status_regex=r"'\*\*Статус:\*\*\s*(\S+)'")
    t = "ST.md"
    (code.root / t).write_text("---\nticket: P\ndir: .\nprofile: task\nsize: S\nrisk: low\n---\n**Статус:** ГОТОВ\n",
                               encoding="utf-8")
    monkeypatch.setenv("FAKE_COST", "0.1")  # текст Code — «готово», слова «Статус» там нет
    d = code.jobs.start(code.jobs.plan("proj", t))
    wait_terminal(code.jobs, d["id"])
    assert code.jobs.read(d["id"])["result"]["taskfile_status"] == "ГОТОВ"


def test_daily_budget_counts_completed_cost(code, monkeypatch):
    """Замечание 12: дневной бюджет учитывает total_cost_usd задач, завершённых сегодня (не budget_usd)."""
    code.write(max_parallel="3", daily_budget_usd="2", max_budget_usd="1")
    monkeypatch.setenv("FAKE_COST", "1.5")  # первая задача завершится с реальной стоимостью 1.5
    d1 = code.jobs.start(code.jobs.plan("proj", code.taskfile("A.md"), budget_usd=1))
    wait_terminal(code.jobs, d1["id"])
    # по budget_usd было бы 1+1=2 (не превышает), по total_cost_usd — 1.5+1=2.5 > 2 → отказ
    with pytest.raises(JobError, match="дневной бюджет"):
        code.jobs.start(code.jobs.plan("proj", code.taskfile("B.md"), budget_usd=1))


def test_resume_pins_dir_profile(code):
    """Замечание 12/5: resume отказывает, если шапка ТЗ сменила профиль с момента родителя."""
    t = code.taskfile(profile="task")
    d = code.jobs.start(code.jobs.plan("proj", t))
    wait_terminal(code.jobs, d["id"])
    (code.root / t).write_text("---\nticket: P\ndir: .\nprofile: recon\n---\n#\n", encoding="utf-8")  # профиль сменился
    with pytest.raises(JobError, match="шапка ТЗ изменилась"):
        code.jobs.resume(d["id"], "task")


def test_resume_allowed_when_lost(code, monkeypatch):
    """Замечание 12: родителя в lost продолжить можно (session_id известен с первой секунды)."""
    monkeypatch.setenv("FAKE_SLEEP", "30")
    d = code.jobs.start(code.jobs.plan("proj", code.taskfile()))
    _wait_running(code.jobs, d["id"])
    jd = code.jobs.read(d["id"])
    os.kill(jd["sup_pid"], 9)
    os.killpg(jd["pgid"], 9)
    time.sleep(0.3)
    assert code.jobs.snapshot(d["id"])["state"] == "lost"
    monkeypatch.setenv("FAKE_SLEEP", "0")
    r = code.jobs.resume(d["id"], "task")
    assert r["parent"] == d["id"] and r["session_id"] == jd["session_id"]
    wait_terminal(code.jobs, r["id"])


def test_journal_written_before_terminal(code, monkeypatch):
    """Замечание 10: строка code_finish есть в журнале к моменту, когда состояние уже терминальное (10 прогонов)."""
    audit = code.state / "audit.jsonl"
    for _ in range(10):
        d = code.jobs.start(code.jobs.plan("proj", code.taskfile()))
        wait_terminal(code.jobs, d["id"])
        text = audit.read_text()
        assert text.count(f'"{d["id"]}"') and "code_finish" in text
        # строка code_finish для этого job уже на диске, как только снимок стал completed
        assert any("code_finish" in line and d["id"] in line for line in text.splitlines())


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

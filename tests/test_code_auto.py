"""wshub-code, ТЗ WSH-3.15: режим проекта auto — задача task в фазе исполнения идёт в --permission-mode auto без
песочницы и хука; фаза плана, recon и review — как в sandbox. ТЗ WSH-3.16, 1.1: пуш и API хостинга в auto выдаются
по регламенту, как в sandbox (wshub push / wshub forge в allow, PATH помощника). Настоящий claude не вызывается."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from test_code import GOLDEN, _golden_settings, git, make_code, wait_terminal
from test_code_plan import _wait_state

from wshub import claude_cli, code_cli, code_server
from wshub import jobs as jobs_mod
from wshub.registry import CodeGlobal, Gate, ProjectCode, parse

HEAD = "\n---\nОт хаба wshub (добавлено к шаблону):\n"
NOTE = "- Замечание оператора: проверь миграции\n"
MIXED = b"a\r\nb\nc\r\n"
REASON = "оператор в чате 26.10: запушить ветку"


@pytest.fixture
def code(tmp_path, monkeypatch):
    return make_code(tmp_path, monkeypatch)


@pytest.fixture(autouse=True)
def hub_wshub(tmp_path, monkeypatch) -> Path:
    """wshub хаба — исполняемый файл: в sandbox задача task получила бы гейты, wshub switch и помощника."""
    exe = tmp_path / "hubbin" / "wshub"
    exe.parent.mkdir()
    exe.write_text("#!/bin/sh\nexit 0\n")
    exe.chmod(0o755)
    monkeypatch.setattr(claude_cli, "wshub_exe", lambda: str(exe))
    return exe


def _call(code, name, args) -> str:
    out = asyncio.run(code_server.build_code_server(code.hub).call_tool(name, args))
    content = out[0] if isinstance(out, tuple) else out
    return content[0].text


def _block(prompt: str) -> str:
    return prompt.split(HEAD, 1)[1] if HEAD in prompt else ""


def _job_files(code, jid) -> tuple[list[str], dict, dict]:
    jdir = code.jobs.job_dir(jid)
    return (json.loads((jdir / "argv.json").read_text()), json.loads((jdir / "settings.json").read_text()),
            json.loads((jdir / "env.json").read_text()))


def _opt(argv: list[str], flag: str) -> str | None:
    return argv[argv.index(flag) + 1] if flag in argv else None


def _project_files(code):
    """Отслеживаемые файлы, которые песочница скрыла бы (.env*, deny_read), и файл со смешанными концами строк."""
    for rel, data in {".env.example": b"A=1\n", "secret/key.txt": b"k\n", "docs/mixed.txt": MIXED}.items():
        p = code.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
    git(code.root, "-c", "core.autocrlf=false", "add", "-f", ".env.example", "secret/key.txt", "docs/mixed.txt")
    git(code.root, "-c", "core.autocrlf=false", "commit", "-qm", "files")


def _write(code, **over):
    """Проект с гейтом, пушем always, deny_read и ключами песочницы: гейт, switch и ключи песочницы auto не выдаёт,
    пуш — выдаёт (ТЗ WSH-3.16, 1.1)."""
    code.write(**{"gates": '{ check = "scripts/check.sh" }', "push": '"always"', "deny_read": '["secret"]',
                  "bash_allow": '["docker compose run --rm migrations"]', "sandbox_exclude": '["docker"]',
                  "network": '["pypi.org"]', **over})


EOL_LINE = ("- Смешанные концы строк (CRLF и LF в одном файле): docs/mixed.txt. Edit приводит концы строк всего файла "
            "к одному виду — после правки такого файла сверь `git diff --stat -- <файл>` с "
            "`git diff --ignore-cr-at-eol --stat -- <файл>` и верни концы строк, которые не правил.\n")
# строки блока по ТЗ WSH-3.16 (1.1.4, 1.3) — дословно
PUSH_LINE = "- Пуш — только `wshub push <remote> <ветка>` (голый `git push` запрещён).\n"
FORGE_LINE = ("- API хостинга — `wshub forge <метод> <путь>`. Ждать пайплайн — в одном вызове Bash: цикл `sleep` + "
              "`wshub forge GET …` с таймаутом Bash до 3600000 мс.\n")
PLAN_LINE = ("- Фаза плана идёт в песочнице: docker, сборки и сеть недоступны — они будут в фазе исполнения. Не ставь "
             "BLOCKED из-за них: впиши в план, что проверишь при исполнении.\n")
SPLIT_LINE = ("- Отказ одной команды не значит, что Bash закрыт: составные команды (`;`, `&&`, `|` с неразрешённой "
              "частью) отклоняются целиком — разбей на отдельные вызовы.\n")
ALLOW_PUSH, ALLOW_FORGE = "Bash(wshub push *)", "Bash(wshub forge *)"


# ---------- 1.1 реестр ----------

def test_registry_mode(code):
    assert parse(code.cfg.read_text()).code.projects["proj"].mode == "sandbox"  # ключа нет — sandbox
    code.write(mode='"auto"')
    assert parse(code.cfg.read_text()).code.projects["proj"].mode == "auto"
    code.write(mode='"sandbox"')
    assert parse(code.cfg.read_text()).code.projects["proj"].mode == "sandbox"
    for bad in ('"strict"', '"Auto"', "true"):
        code.write(mode=bad)
        reg = parse(code.cfg.read_text())
        assert reg.code.projects.get("proj") is None
        assert reg.code.project_errors["proj"] == "[workspace.proj.code] mode: одно из sandbox, auto"


def test_code_projects_shows_mode(code):
    assert json.loads(_call(code, "code_projects", {}))[0]["mode"] == "sandbox"
    code.write(mode='"auto"')
    assert json.loads(_call(code, "code_projects", {}))[0]["mode"] == "auto"


# ---------- 1.2.1–1.2.3 settings и argv ----------

def _settings(profile, phase=None, mode="auto", **kw):
    proj = ProjectCode(enabled=True, dirs=(".",), prompts="p", deny_edit=("docker-compose*.yml",), **kw)
    protected = [Path("/home/u/.config/wshub"), Path("/home/u/.local/state/wshub")]
    return claude_cli.settings(profile, "org/TASK.md", CodeGlobal(), proj, protected, protected,
                               ["/py", "-m", "wshub.hook"], Path("/home/u"), Path("/home/u/proj"), phase=phase,
                               mode=mode, **{k: v for k, v in {"grants": GRANTS, "gates": GATES, "switch": True,
                                                                "key_helper": "KH"}.items() if profile == "task"})


GRANTS = {"push": True, "forge": True, "reason": REASON}
GATES = (Gate("check", ("scripts/check.sh",), 1500),)
# Read/Edit служебных каталогов и deny_read — те же правила, что в эталоне sandbox
PATH_RULES = [r for r in GOLDEN["task"]["permissions"]["deny"] if r.startswith(("Read(//", "Edit(//"))]
AUTO_DENY = ["Bash(git push)", "Bash(git push *)", *PATH_RULES, "Edit(./.claude/settings.json)",
             "Edit(./.claude/settings.local.json)", "Read(**/.env*)", "WebFetch", "WebSearch"]


def test_auto_settings_task_execute():
    """1.2.2 целиком: нет песочницы и хука, deny — ровно список ТЗ, потолок Bash — час. allow — только помощник по
    выданным разрешениям (ТЗ WSH-3.16, 1.1.2); гейты, switch, bash_allow, sandbox_exclude, network, deny_edit — не
    попадают."""
    s = _settings("task", bash_allow=("docker compose *",), sandbox_exclude=("docker",), network=("pypi.org",),
                  claude_md_excludes=("vendor/**",))
    assert {"Read(//home/u/.ssh)", "Edit(//home/u/.config/wshub/**)"} <= set(PATH_RULES)
    assert s == {"permissions": {"allow": [ALLOW_PUSH, ALLOW_FORGE], "deny": AUTO_DENY},
                 "env": {"BASH_MAX_TIMEOUT_MS": "3600000"}, "claudeMdExcludes": ["vendor/**"], "apiKeyHelper": "KH"}
    assert "sandbox" not in s and "hooks" not in s
    assert "wshub gate" not in json.dumps(s) and "wshub switch" not in json.dumps(s)


@pytest.mark.parametrize("grants,allow", [
    ({"push": True, "forge": False}, [ALLOW_PUSH]),
    ({"push": False, "forge": True}, [ALLOW_FORGE]),
    ({"push": False, "forge": False}, []),
    (None, []),
])
def test_auto_settings_allow_only_granted(grants, allow):
    """ТЗ WSH-3.16, 1.1.2–1.1.3: в allow — только выданное; ничего не выдано — allow пуст; deny прежний."""
    proj = ProjectCode(enabled=True, dirs=(".",), prompts="p", deny_edit=("docker-compose*.yml",))
    protected = [Path("/home/u/.config/wshub"), Path("/home/u/.local/state/wshub")]
    s = claude_cli.settings("task", "org/TASK.md", CodeGlobal(), proj, protected, protected,
                            ["/py", "-m", "wshub.hook"], Path("/home/u"), Path("/home/u/proj"), grants=grants,
                            gates=GATES, switch=True, mode="auto")
    assert s["permissions"] == {"allow": allow, "deny": AUTO_DENY}


def test_auto_settings_project_deny_read():
    """deny_read проекта — как сейчас, из deny_paths: относительная маска — от рабочего каталога."""
    deny = _settings("task", deny_read=("secret",))["permissions"]["deny"]
    assert {"Read(//home/u/proj/secret)", "Edit(//home/u/proj/secret/**)"} <= set(deny)


@pytest.mark.parametrize("profile", ["recon", "review"])
def test_auto_reading_profiles_settings_unchanged(profile):
    assert _settings(profile) == GOLDEN[profile]
    assert _settings(profile, phase="plan") == GOLDEN["plan"][profile]


def test_auto_plan_phase_settings_unchanged():
    """Фаза плана task в проекте auto — прежний settings фазы плана."""
    proj = ProjectCode(enabled=True, dirs=(".",), prompts="p", deny_edit=("docker-compose*.yml",))
    protected = [Path("/home/u/.config/wshub"), Path("/home/u/.local/state/wshub")]
    s = claude_cli.settings("task", "org/TASK.md", CodeGlobal(), proj, protected, protected,
                            ["/py", "-m", "wshub.hook"], Path("/home/u"), Path("/home/u/proj"), phase="plan",
                            mode="auto")
    assert s == GOLDEN["plan"]["task"]


def test_sandbox_mode_is_golden():
    """Режим sandbox явно и по умолчанию — эталон, golden_settings.json не меняется."""
    assert _settings("task", mode="sandbox")["sandbox"]["enabled"] is True
    proj = ProjectCode(enabled=True, dirs=(".",), prompts="p", deny_edit=("docker-compose*.yml",))
    protected = [Path("/home/u/.config/wshub"), Path("/home/u/.local/state/wshub")]
    s = claude_cli.settings("task", "org/TASK.md", CodeGlobal(), proj, protected, protected,
                            ["/py", "-m", "wshub.hook"], Path("/home/u"), Path("/home/u/proj"), mode="sandbox")
    assert s == _golden_settings("task") == GOLDEN["task"]


def test_argv_auto():
    flags = {"permission_prompts": True, "setting_sources": True}
    out = claude_cli.argv("/claude", "PROMPT", job_dir=Path("/j"), model="sonnet", effort="high", max_turns=80,
                          budget_usd=5, flags=flags, session_id="SID", permission_mode="auto")
    want = list(GOLDEN["argv_start"])
    want[want.index("--permission-mode") + 1] = "auto"
    assert out == want and "dontAsk" not in out
    with pytest.raises(claude_cli.CliError, match="не поддерживается"):
        claude_cli.argv("/claude", "P", job_dir=Path("/j"), model="sonnet", effort="high", max_turns=None,
                        budget_usd=None, flags=flags, session_id="SID", permission_mode="bypassPermissions")


# ---------- сквозной старт ----------

def test_sandbox_control(code, hub_wshub):
    """Контроль: тот же проект без mode — гейты, switch, пуш, скрытые файлы есть (иначе проверки auto пусты)."""
    _project_files(code)
    _write(code)
    p = code.jobs.plan("proj", code.taskfile(), note="проверь миграции")
    assert p.mode == "sandbox" and p.permission_mode == "dontAsk"
    assert p.gates and p.switch_helper and p.grants["push"] and p.sandbox_hidden == [".env.example", "secret/key.txt"]
    block = _block(p.prompt)
    assert "Гейты" in block and "wshub switch" in block and "Песочница скрывает" in block
    d = code.jobs.start(p)
    argv, settings, env = _job_files(code, d["id"])
    assert _opt(argv, "--permission-mode") == "dontAsk" and settings["sandbox"]["enabled"] is True
    assert env["PATH"].split(":")[0] == str(hub_wshub.parent)
    assert code.jobs.read(d["id"])["mode"] == "sandbox"
    wait_terminal(code.jobs, d["id"])
    assert "mode" not in code.jobs.result_view(d["id"])
    assert "mode" not in code_server.card_data(code.jobs, d["id"], 0)


def test_auto_task_start(code, hub_wshub, monkeypatch, capsys):
    """Задача task в auto: argv, settings, помощник — только wshub push (push = always; ТЗ WSH-3.16, 1.1), блок
    «От хаба» — замечание, пуш и концы строк, видимость."""
    monkeypatch.setenv("SSH_AUTH_SOCK", "/tmp/agent.sock")
    _project_files(code)
    _write(code, mode='"auto"')
    p = code.jobs.plan("proj", code.taskfile(), note="проверь миграции")
    assert p.mode == "auto" and p.permission_mode == "auto" and p.phase is None
    assert p.gates == () and p.switch_helper is False and p.sandbox_hidden == []
    assert p.grants == {"push": True, "forge": False, "reason": None, "auto": ["push"]}
    assert not [w for w in p.warnings if "auto" in w]  # строки «не выдаются» больше нет
    assert p.eol_mixed == ["docs/mixed.txt"]
    # без строк wshub switch, гейтов, скрытых файлов и составных команд песочницы
    assert _block(p.prompt) == NOTE + PUSH_LINE + EOL_LINE
    d = code.jobs.start(p)
    jid = d["id"]
    argv, settings, env = _job_files(code, jid)
    assert _opt(argv, "--permission-mode") == "auto" and "dontAsk" not in argv
    assert "sandbox" not in settings and "hooks" not in settings
    assert settings["permissions"]["allow"] == [ALLOW_PUSH]
    assert "wshub gate" not in json.dumps(settings) and "wshub switch" not in json.dumps(settings)
    assert settings["permissions"]["deny"][:2] == ["Bash(git push)", "Bash(git push *)"]
    assert settings["env"] == {"BASH_MAX_TIMEOUT_MS": "3600000"}
    assert env["PATH"].split(":")[0] == str(hub_wshub.parent)  # helper_env, как в sandbox
    assert env["WSHUB_JOB"] == jid and env["WSHUB_CONFIG"] == str(code.cfg) and env["WSHUB_STATE"] == str(code.state)
    assert (code.jobs.job_dir(jid) / "ssh_auth_sock").read_text() == "/tmp/agent.sock"
    job = code.jobs.read(jid)
    assert job["mode"] == "auto" and job["grants"] == p.grants
    assert job["sandbox_hidden"] == [] and job["eol_mixed"] == ["docs/mixed.txt"]
    wait_terminal(code.jobs, jid)
    assert code.jobs.result_view(jid)["mode"] == "режим: auto"
    assert json.loads(_call(code, "code_result", {"job": jid}))["mode"] == "режим: auto"
    capsys.readouterr()
    assert code_cli.code_main("code", ["result", jid], code.cfg, code.state) == 0
    assert '"mode": "режим: auto"' in capsys.readouterr().out
    assert code_server.card_data(code.jobs, jid, 0)["mode"] == "auto"


def test_auto_grants_requested(code, hub_wshub):
    """ТЗ WSH-3.16, 1.1: пуш по просьбе при регламенте task — как в sandbox: reason обязателен, разрешение пишется,
    allow — wshub push, PATH с хабом, deny с git push, строка пуша в блоке. Без просьбы — ничего не выдано."""
    code.write(mode='"auto"', push='"task"')
    t = code.taskfile()
    with pytest.raises(jobs_mod.JobError, match="reason"):
        code.jobs.plan("proj", t, push=True)
    p = code.jobs.plan("proj", t, push=True, reason=REASON)
    assert p.grants == {"push": True, "forge": False, "reason": REASON} and p.mode == "auto"
    assert PUSH_LINE in _block(p.prompt) and FORGE_LINE not in p.prompt and SPLIT_LINE not in p.prompt
    d = code.jobs.start(p)
    _argv, settings, env = _job_files(code, d["id"])
    assert settings["permissions"]["allow"] == [ALLOW_PUSH]
    assert {"Bash(git push)", "Bash(git push *)"} <= set(settings["permissions"]["deny"])
    assert env["PATH"].split(":")[0] == str(hub_wshub.parent)
    wait_terminal(code.jobs, d["id"])
    q = code.jobs.plan("proj", t)  # ничего не выдано
    assert q.grants == {"push": False, "forge": False, "reason": None} and PUSH_LINE not in q.prompt


def test_auto_nothing_granted(code, hub_wshub, monkeypatch):
    """ТЗ WSH-3.16, 1.1.3: регламент без пуша и API — allow пуст, PATH без хаба, ssh_auth_sock нет, строк помощника
    в блоке нет."""
    monkeypatch.setenv("SSH_AUTH_SOCK", "/tmp/agent.sock")
    code.write(mode='"auto"')
    p = code.jobs.plan("proj", code.taskfile(), note="проверь миграции")
    assert p.grants == {"push": False, "forge": False, "reason": None} and _block(p.prompt) == NOTE
    d = code.jobs.start(p)
    _argv, settings, env = _job_files(code, d["id"])
    assert settings["permissions"]["allow"] == [] and "Bash(wshub" not in json.dumps(settings)
    assert str(hub_wshub.parent) not in env["PATH"].split(":")
    assert not (code.jobs.job_dir(d["id"]) / "ssh_auth_sock").exists()
    wait_terminal(code.jobs, d["id"])


def _token(tmp_path) -> str:
    p = tmp_path / "secrets" / "forge.token"
    p.parent.mkdir(parents=True)
    p.write_text("tok\n")
    p.chmod(0o600)
    return str(p)


@pytest.mark.parametrize("push,allow,block", [
    ('"no"', [ALLOW_FORGE], FORGE_LINE),
    ('"always"', [ALLOW_PUSH, ALLOW_FORGE], PUSH_LINE + FORGE_LINE),
])
def test_auto_forge_granted(code, tmp_path, hub_wshub, push, allow, block):
    """ТЗ WSH-3.16, 1.1: API хостинга (always) в auto выдаётся — grants, allow, PATH с хабом, строка о wshub forge и
    ожидании пайплайна в блоке; с пушем — обе строки. gate и switch не выдаются."""
    code.write(mode='"auto"', push=push, forge='"github"', forge_access='"always"', forge_token=f'"{_token(tmp_path)}"',
               gates='{ check = "scripts/check.sh" }')
    p = code.jobs.plan("proj", code.taskfile())
    assert p.grants["forge"] is True and p.grants["push"] is (push == '"always"')
    assert _block(p.prompt) == block
    d = code.jobs.start(p)
    _argv, settings, env = _job_files(code, d["id"])
    assert d["grants"] == p.grants and settings["permissions"]["allow"] == allow
    assert env["PATH"].split(":")[0] == str(hub_wshub.parent)
    wait_terminal(code.jobs, d["id"])


def test_auto_resume(code, hub_wshub):
    """code_resume задачи task в auto — снова auto: argv, settings без песочницы, mode в job.json; пуш по просьбе
    (push = task, reason) наследуется (ТЗ WSH-3.16, 1.1): allow, PATH с хабом, строка пуша."""
    _project_files(code)
    _write(code, mode='"auto"', push='"task"')
    jid = code.jobs.start(code.jobs.plan("proj", code.taskfile(), push=True, reason=REASON))["id"]
    wait_terminal(code.jobs, jid)
    child = code.jobs.resume(jid, "cont", note="продолжи")
    argv, settings, env = _job_files(code, child["id"])
    assert _opt(argv, "--permission-mode") == "auto" and _opt(argv, "--resume")
    assert "sandbox" not in settings and "hooks" not in settings
    assert settings["permissions"]["allow"] == [ALLOW_PUSH]
    assert env["PATH"].split(":")[0] == str(hub_wshub.parent)
    assert child["mode"] == "auto" and child["sandbox_hidden"] == []
    assert child["grants"] == {"push": True, "forge": False, "reason": REASON}
    prompt = (code.jobs.job_dir(child["id"]) / "prompt.txt").read_text()
    assert "wshub switch" not in prompt and "Гейты" not in prompt and "Песочница" not in prompt
    assert PUSH_LINE in prompt and SPLIT_LINE not in prompt
    wait_terminal(code.jobs, child["id"])


def test_auto_header_switch(code):
    """switch: шапки в auto — как раньше: ветку ставит надзиратель до claude."""
    import subprocess
    code.write(mode='"auto"')
    main = subprocess.run(["git", "-C", str(code.root), "symbolic-ref", "--short", "HEAD"], capture_output=True,
                          text=True).stdout.strip()
    d = code.jobs.start(code.jobs.plan("proj", code.taskfile(extra=f"switch: -c WSH-X {main}\n")))
    assert d["mode"] == "auto" and d["switch"] == {"spec": f"-c WSH-X {main}"}
    wait_terminal(code.jobs, d["id"])
    assert code.jobs.read(d["id"])["switch"]["done"] is True
    assert subprocess.run(["git", "-C", str(code.root), "symbolic-ref", "--short", "HEAD"], capture_output=True,
                          text=True).stdout.strip() == "WSH-X"


def test_auto_billing_guard_and_one_task_per_dir(code):
    """Страж оплаты и одна задача на каталог в auto — как раньше."""
    import os

    from wshub.runtime import proc_start
    code.write(mode='"auto"', max_parallel="3")
    p = code.root / ".claude" / "settings.local.json"
    p.parent.mkdir()
    p.write_text('{"apiKeyHelper": "/bin/echo k"}')
    with pytest.raises(jobs_mod.JobError, match="задаёт apiKeyHelper"):
        code.jobs.start(code.jobs.plan("proj", code.taskfile()))
    p.unlink()
    jid = jobs_mod.new_id()
    code.jobs.job_dir(jid).mkdir(parents=True)
    code.jobs.write(jid, {"id": jid, "project": "proj", "dir": str(code.root), "profile": "task", "state": "running",
                          "sup_pid": os.getpid(), "sup_start": proc_start(os.getpid())})
    with pytest.raises(jobs_mod.JobError, match="одна живая задача на каталог"):
        code.jobs.start(code.jobs.plan("proj", code.taskfile()))


def test_auto_python_m_no_helper_warnings(code, monkeypatch):
    """Хаб запущен как python -m wshub: в auto гейтов и switch нет вовсе — и предупреждений о них тоже."""
    monkeypatch.setattr(claude_cli, "wshub_exe", lambda: None)
    code.write(mode='"auto"', gates='{ check = "scripts/check.sh" }')
    w = code.jobs.plan("proj", code.taskfile()).warnings
    assert jobs_mod.GATES_PYTHON_M not in w and jobs_mod.SWITCH_PYTHON_M not in w
    code.write(gates='{ check = "scripts/check.sh" }')  # контроль: в sandbox оба предупреждения есть
    w = code.jobs.plan("proj", code.taskfile()).warnings
    assert jobs_mod.GATES_PYTHON_M in w and jobs_mod.SWITCH_PYTHON_M in w


def test_auto_plan_chain(code, hub_wshub):
    """Фаза плана auto-проекта — plan и прежний settings фазы плана (песочница, скрытые файлы), без строки «Гейты
    будут…»; исполнение после утверждения — auto. ТЗ WSH-3.16: в блоке фазы плана — строки фазы плана auto-проекта и
    составных команд, без строки пуша; пуш по просьбе наследует исполнение (allow, PATH, строка пуша)."""
    _project_files(code)
    _write(code, mode='"auto"', push='"task"')
    p = code.jobs.plan("proj", code.taskfile(size="L"), push=True, reason=REASON)
    assert p.phase == "plan" and p.permission_mode == "plan" and p.mode == "sandbox"
    assert p.sandbox_hidden == [".env.example", "secret/key.txt"]
    assert "Гейты" not in p.prompt and "Песочница скрывает" in _block(p.prompt)
    assert _block(p.prompt).startswith(PLAN_LINE + SPLIT_LINE) and PUSH_LINE not in p.prompt
    root = code.jobs.start(p)["id"]
    argv, settings, _env = _job_files(code, root)
    assert _opt(argv, "--permission-mode") == "plan"
    assert settings["sandbox"]["enabled"] is True and settings["useAutoModeDuringPlan"] is False
    assert settings["permissions"]["disableAutoMode"] == "disable" and settings["hooks"]
    assert code.jobs.read(root)["mode"] == "sandbox"
    _wait_state(code.jobs, root, ("plan_ready",))
    # переделка плана — снова фаза плана в песочнице, со скрытыми файлами
    re_plan = code.jobs.replan(root, "короче")
    argv, settings, _env = _job_files(code, re_plan["id"])
    assert _opt(argv, "--permission-mode") == "plan" and settings["sandbox"]["enabled"] is True
    assert re_plan["mode"] == "sandbox" and re_plan["sandbox_hidden"] == [".env.example", "secret/key.txt"]
    re_block = _block((code.jobs.job_dir(re_plan["id"]) / "prompt.txt").read_text())
    assert "Песочница скрывает" in re_block and PLAN_LINE in re_block and SPLIT_LINE in re_block
    assert PUSH_LINE not in re_block
    _wait_state(code.jobs, re_plan["id"], ("plan_ready",))
    child = code.jobs.approve(re_plan["id"], "учти ревью")
    argv, settings, env = _job_files(code, child["id"])
    assert _opt(argv, "--permission-mode") == "auto" and _opt(argv, "--resume")
    assert "sandbox" not in settings and "hooks" not in settings
    assert settings["permissions"]["allow"] == [ALLOW_PUSH]
    assert env["PATH"].split(":")[0] == str(hub_wshub.parent)
    assert child["mode"] == "auto" and child["sandbox_hidden"] == []
    assert child["grants"] == {"push": True, "forge": False, "reason": REASON}
    prompt = (code.jobs.job_dir(child["id"]) / "prompt.txt").read_text()
    assert _block(prompt) == "- Замечание оператора: учти ревью\n" + PUSH_LINE + EOL_LINE
    wait_terminal(code.jobs, child["id"])
    assert code.jobs.result_view(root)["mode"] == "режим: auto"
    assert code_server.card_data(code.jobs, root, 0)["mode"] == "auto"


@pytest.mark.parametrize("profile", ["recon", "review"])
def test_auto_reading_profiles_unchanged(code, profile):
    """recon и review в проекте auto — те же settings, argv и блок, что в sandbox."""
    def run(**over):
        _write(code, **over)
        p = code.jobs.plan("proj", code.taskfile(profile=profile))
        assert p.mode == "sandbox" and p.permission_mode == "dontAsk"
        d = code.jobs.start(p)
        wait_terminal(code.jobs, d["id"])
        argv, settings, _env = _job_files(code, d["id"])
        return (_opt(argv, "--permission-mode"), settings, p.prompt, d["mode"],
                "mode" in code.jobs.result_view(d["id"]))

    auto = run(mode='"auto"')
    assert auto == run()
    assert auto[0] == "dontAsk" and auto[1]["sandbox"]["enabled"] is True and auto[3] == "sandbox" and not auto[4]


def test_card_html_shows_mode():
    html = (Path(code_server.__file__).parent / "code_card.html").read_text()
    assert 'if (d.mode === "auto") g.append(el("span", "tag", "режим: auto"));' in html
    assert 'g.hidden = !tags.length && d.mode !== "auto";' in html  # без разрешений тег не прячется

"""wshub setup / uninstall на «чистой машине»: временный HOME, фейковый диск C: и профиль Windows.

Настоящие конфиг Desktop, реестр и состояние не трогаются: всё — во временной папке pytest,
пути передаются через HOME, WSHUB_MNT, WSL_DISTRO_NAME, WSHUB_WIN_USER.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from wshub import install, jsonedit
from wshub.registry import parse

ENTRY = {"command": "wsl.exe", "args": ["-d", "U", "--", "/home/u/.local/bin/wshub"]}

# конфиг, как его пишет Desktop: чужой сервер, настройки, путь Cowork — всё должно остаться байт в байт
DESKTOP_CONFIG = """{
  "mcpServers": {
    "git": {
      "command": "uvx",
      "args": [
        "mcp-server-git"
      ]
    }
  },
  "preferences": {
    "menuBarEnabled": false
  },
  "coworkUserFilesPath": "C:\\\\Users\\\\me\\\\Documents\\\\Claude"
}"""


class Machine:
    def __init__(self, tmp: Path):
        self.tmp = tmp
        self.home = tmp / "home" / "u"
        self.mnt = tmp / "mnt"
        self.user = self.mnt / "c" / "Users" / "me"
        (self.mnt / "c" / "Users" / "Public").mkdir(parents=True)
        self.exe = self.home / ".local" / "bin" / "wshub"
        self.exe.parent.mkdir(parents=True)
        self.exe.write_text("#!/bin/sh\n")
        self.exe.chmod(0o755)
        pkg = self.user / "AppData/Local/Packages/Claude_pzs8sxrjxfjjc"
        self.msix = pkg / "LocalCache/Roaming/Claude/claude_desktop_config.json"
        self.msix.parent.mkdir(parents=True)
        self.msix.write_text(DESKTOP_CONFIG, encoding="utf-8")
        # классический файл виден из WSL, но MSIX-Desktop его не читает — setup его не трогает
        self.classic = self.user / "AppData/Roaming/Claude/claude_desktop_config.json"
        self.classic.parent.mkdir(parents=True)
        self.classic.write_text('{"mcpServers": {}}', encoding="utf-8")
        self.project = self.home / "myproject"
        (self.project / ".agents").mkdir(parents=True)
        (self.project / ".agents/BRIEF.md").write_text("# BRIEF\n")
        self.config = self.home / ".config/wshub/workspaces.toml"
        self.state = self.home / ".local/state/wshub"
        self.outbox = self.user / "ClaudeOutbox"

    def environ(self) -> dict:
        env = {k: v for k, v in os.environ.items() if not k.startswith(("WSHUB_", "UV_TOOL_BIN", "XDG_BIN"))}
        # PATH без Windows: cmd.exe и tasklist.exe недоступны, как в CI
        path = os.pathsep.join(p for p in env.get("PATH", "").split(os.pathsep) if not p.startswith("/mnt/"))
        env.update(HOME=str(self.home), WSHUB_MNT=str(self.mnt), WSL_DISTRO_NAME="Ubuntu-test",
                   WSHUB_WIN_USER="me", PATH=path)
        return env

    def wshub(self, *args: str, inp: str | None = None) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, "-m", "wshub", *args], env=self.environ(), input=inp,
                              capture_output=True, text=True, timeout=120)

    def snapshot(self) -> dict[str, bytes]:
        return {str(p.relative_to(self.tmp)): p.read_bytes() for p in sorted(self.tmp.rglob("*")) if p.is_file()}


@pytest.fixture
def machine(tmp_path) -> Machine:
    return Machine(tmp_path)


def entry(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))["mcpServers"].get("wshub")


def test_clean_machine_scenario(machine):
    """setup → doctor OK → повторный setup ничего не меняет → uninstall возвращает конфиг побайтово."""
    m = machine
    r = m.wshub("setup", "--yes", "--project", str(m.project), "--mode", "rw")
    assert r.returncode == 0, r.stdout + r.stderr
    assert entry(m.msix) == {"command": "wsl.exe", "args": ["-d", "Ubuntu-test", "--", str(m.exe)]}
    data = json.loads(m.msix.read_text(encoding="utf-8"))
    assert data["mcpServers"]["git"] == {"command": "uvx", "args": ["mcp-server-git"]}
    assert data["preferences"] == {"menuBarEnabled": False} and "coworkUserFilesPath" in data
    assert m.classic.read_text() == '{"mcpServers": {}}'
    backups = list(m.msix.parent.glob("*.wshub-*.bak"))
    assert len(backups) == 1 and backups[0].read_text(encoding="utf-8") == DESKTOP_CONFIG
    reg = parse(m.config.read_text(encoding="utf-8"))
    ws = reg.workspaces["myproject"]
    assert ws.path == m.project and ws.mode == "rw" and ws.brief == ".agents/BRIEF.md" and ".env" in ws.deny
    assert reg.outbox.path == m.outbox and m.outbox.is_dir()
    assert "Дальше: перезапусти Claude Desktop" in r.stdout and "«открой проект myproject через wshub»" in r.stdout
    assert "Always allow" in r.stdout

    d = m.wshub("doctor")
    assert d.returncode == 0, d.stdout
    assert "[OK  ] Запись wshub в Claude Desktop" in d.stdout and "[OK  ] Перевалка (publish)" in d.stdout

    before = m.snapshot()
    r2 = m.wshub("setup", "--yes", "--project", str(m.project), "--mode", "rw")
    assert r2.returncode == 0, r2.stdout + r2.stderr
    assert "уже настроено" in r2.stdout and "уже в реестре как myproject" in r2.stdout
    after = m.snapshot()
    changed = {k for k in after if before.get(k) != after[k]} | set(before) - set(after)
    # меняется только журнал doctor/процессов в состоянии, но не конфиг, реестр и перевалка
    assert not {k for k in changed if not k.startswith("home/u/.local/state/")}, changed

    u = m.wshub("uninstall", "--yes")
    assert u.returncode == 0, u.stdout + u.stderr
    assert m.msix.read_text(encoding="utf-8") == DESKTOP_CONFIG
    assert m.config.exists() and "uninstall --purge" in u.stdout and "uv tool uninstall wshub" in u.stdout

    p = m.wshub("uninstall", "--yes", "--purge")
    assert p.returncode == 0, p.stdout + p.stderr
    assert not m.config.exists() and not m.config.parent.exists() and not m.state.exists()
    assert m.outbox.is_dir() and m.project.is_dir()
    assert m.msix.read_text(encoding="utf-8") == DESKTOP_CONFIG


def test_dry_run_changes_nothing(machine):
    before = machine.snapshot()
    r = machine.wshub("setup", "--dry-run", "--project", str(machine.project), "--mode", "ro")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "будет: добавлена запись wshub" in r.stdout and "будет: добавлен проект myproject [ro]" in r.stdout
    assert "ничего не изменено" in r.stdout
    assert machine.snapshot() == before


def test_interactive_setup(machine):
    """Вопросы без подставленных значений: пустой путь, неверный режим — переспрос."""
    m = machine
    answers = "\n".join([str(m.tmp / "nope"), str(m.project), "", "rwx", "ro", ""]) + "\n"
    r = m.wshub("setup", inp=answers)
    # stdin не терминал → без --yes отказ: интерактив только у человека
    assert r.returncode == 2 and "--yes" in r.stderr
    out = []
    env = m.environ()
    old = dict(os.environ)
    os.environ.update(env)
    try:
        it = iter([str(m.tmp / "nope"), str(m.project), "rwx", "ro", ""])
        prompts = []

        def ask(prompt):
            prompts.append(prompt)
            return next(it)
        code = install.cli("setup", [], m.config, m.state, ask_fn=ask, out=out.append)
    finally:
        os.environ.clear()
        os.environ.update(old)
    text = "\n".join(out)
    assert code == 0, text
    assert any("Путь к папке проекта" in p and "пусто — пропустить" in p for p in prompts)
    assert any("Введи ro или rw" in p for p in prompts)
    assert any("Enter — C:\\Users\\me\\ClaudeOutbox" in p for p in prompts)
    assert "папки " in text and "нужно ro или rw" in text
    reg = parse(m.config.read_text(encoding="utf-8"))
    assert reg.workspaces["myproject"].mode == "ro" and reg.outbox.path == m.outbox


def test_no_desktop_and_broken_config(machine):
    m = machine
    m.msix.write_text("{not json", encoding="utf-8")
    r = m.wshub("setup", "--yes")
    assert r.returncode == 1
    assert "не читается как JSON" in r.stdout and "файл не тронут" in r.stdout
    assert m.msix.read_text(encoding="utf-8") == "{not json"
    assert "проектов нет" in r.stdout and m.config.exists()


def test_desktop_never_started(machine):
    """Пакет MSIX есть, но Desktop ещё не запускали: конфига нет — setup создаёт, uninstall удаляет."""
    m = machine
    m.msix.unlink()
    m.msix.parent.rmdir()
    assert m.wshub("setup", "--yes").returncode in (0, 1)
    assert entry(m.msix)["command"] == "wsl.exe"
    assert m.wshub("uninstall", "--yes").returncode == 0
    assert not m.msix.exists()


def test_classic_install_and_existing_entry(machine):
    """Классический Desktop; старая запись wshub со своим env обновляется, env остаётся."""
    m = machine
    import shutil
    shutil.rmtree(m.user / "AppData/Local/Packages")
    (m.user / "AppData/Local/AnthropicClaude").mkdir(parents=True)
    old = {"mcpServers": {"wshub": {"command": "wsl.exe", "args": ["-d", "Old", "--", "/old/wshub"],
                                    "env": {"X": "1"}}, "git": {"command": "g"}}}
    m.classic.write_text(json.dumps(old, indent=2), encoding="utf-8")
    r = m.wshub("setup", "--yes")
    assert "обновлена запись wshub" in r.stdout, r.stdout
    e = entry(m.classic)
    assert e == {"command": "wsl.exe", "args": ["-d", "Ubuntu-test", "--", str(m.exe)], "env": {"X": "1"}}
    assert m.wshub("uninstall", "--yes").returncode == 0
    assert json.loads(m.classic.read_text()) == {"mcpServers": {"git": {"command": "g"}}}


def test_bad_flags(machine):
    assert machine.wshub("setup", "--bogus").returncode == 2
    r = machine.wshub("setup", "--yes", "--project", str(machine.project))
    assert r.returncode == 2 and "--mode" in r.stderr
    assert machine.wshub("setup", "--help").returncode == 0


# ---------- точечная правка JSON ----------

CASES = {
    "pretty": DESKTOP_CONFIG,
    "no-servers": '{\n  "preferences": {\n    "menuBarEnabled": false\n  }\n}\n',
    "empty-servers": '{\n  "mcpServers": {},\n  "a": 1\n}\n',
    "compact": '{"mcpServers":{"git":{"command":"x"}},"p":1}',
    "crlf-bom": '\ufeff{\r\n    "mcpServers": {\r\n        "git": {"command": "x"}\r\n    }\r\n}\r\n',
    "tabs": '{\n\t"mcpServers": {\n\t\t"a": 1\n\t}\n}',
    "unicode": '{"mcpServers": {"ё": {"command": "\\u0451"}}, "s": "строка с \\" и }"}',
}


@pytest.mark.parametrize("name", CASES)
def test_jsonedit_roundtrip(name):
    text = CASES[name]
    new, action = jsonedit.set_server(text, "wshub", ENTRY)
    assert json.loads(new.lstrip("\ufeff"))["mcpServers"]["wshub"] == ENTRY
    assert jsonedit.set_server(new, "wshub", ENTRY) == (new, "unchanged")
    created = {"added-parent": "mcpServers"}.get(action)
    back, removed = jsonedit.remove_server(new, "wshub", created)
    assert removed and back == text
    if "\r\n" in text:
        assert "\n" not in new.replace("\r\n", "")
    if text.startswith("\ufeff"):
        assert new.startswith("\ufeff")


def test_jsonedit_empty_file_and_errors():
    new, action = jsonedit.set_server("", "wshub", ENTRY)
    assert action == "created" and json.loads(new) == {"mcpServers": {"wshub": ENTRY}}
    assert jsonedit.remove_server(new, "wshub", "file") == (None, True)
    assert jsonedit.remove_server(new, "wshub", "empty") == ("", True)
    with pytest.raises(jsonedit.ConfigError):
        jsonedit.set_server("[1]", "wshub", ENTRY)
    with pytest.raises(jsonedit.ConfigError):
        jsonedit.set_server('{"mcpServers": []}', "wshub", ENTRY)
    assert jsonedit.remove_server('{"mcpServers": {"git": {}}}', "wshub") == ('{"mcpServers": {"git": {}}}', False)


def test_jsonedit_remove_first_and_middle():
    text = '{"mcpServers": {"wshub": {"command": "a"}, "git": {}, "x": 1}}'
    out, removed = jsonedit.remove_server(text, "wshub")
    assert removed and out == '{"mcpServers": {"git": {}, "x": 1}}'
    text = '{"mcpServers": {"git": {}, "wshub": {"command": "a"}, "x": 1}}'
    assert jsonedit.remove_server(text, "wshub")[0] == '{"mcpServers": {"git": {}, "x": 1}}'


# ---------- настоящий WSL (на машине разработчика) ----------

@pytest.mark.wsl
def test_detect_on_real_wsl(tmp_path):
    """Только чтение: дистрибутив, пользователь Windows и конфиги Desktop определяются на этой машине."""
    env = install.detect(tmp_path / "workspaces.toml", tmp_path / "state")
    assert env.distro == os.environ.get("WSL_DISTRO_NAME")
    assert env.win_user and (env.users_dir / env.win_user).is_dir(), "пользователь Windows не определён"
    for c in install.desktop_configs(env, only_used=False):
        assert c["path"].is_relative_to(env.users_dir / env.win_user)


# ---------- мелкие ветки ----------

def _ui(answers=(), interactive=True):
    out, it = [], iter(answers)
    return install.UI(interactive=interactive, dry=False, ask_fn=lambda _p: next(it), out=out.append), out


def test_ambiguous_windows_user(machine, monkeypatch):
    m = machine
    other = m.mnt / "c/Users/other/AppData/Local/Packages/Claude_x"
    other.mkdir(parents=True)
    env = {**m.environ()}
    env.pop("WSHUB_WIN_USER")
    r = subprocess.run([sys.executable, "-m", "wshub", "setup", "--yes"], env=env, capture_output=True, text=True,
                       timeout=120)
    assert r.returncode == 1 and "WSHUB_WIN_USER" in r.stdout
    assert m.msix.read_text(encoding="utf-8") == DESKTOP_CONFIG
    # в диалоге — выбор из списка
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("WSHUB_WIN_USER", raising=False)
    e = install.detect(m.config, m.state)
    assert e.win_user is None
    ui, out = _ui(["nobody", "me"])
    install.choose_win_user(e, ui)
    assert e.win_user == "me" and any("нет такого" in x for x in out)


def test_wait_for_desktop_to_close(machine, monkeypatch):
    seq = iter([True, True, False])
    monkeypatch.setattr(install, "desktop_running", lambda env: next(seq))
    env = install.Env(home=machine.home, config=machine.config, state=machine.state, mnt=machine.mnt, distro="U")
    ui, out = _ui(["", ""])
    assert install.wait_desktop_closed(env, ui) is True
    monkeypatch.setattr(install, "desktop_running", lambda env: True)
    ui, out = _ui(["дальше"])
    assert install.wait_desktop_closed(env, ui) is False
    ui, out = _ui(interactive=False)
    assert install.wait_desktop_closed(env, ui) is False and "может перезаписать" in out[0]


def test_update_paths(machine, monkeypatch, tmp_path):
    env = install.Env(home=machine.home, config=machine.config, state=machine.state, mnt=machine.mnt, distro="U")
    monkeypatch.setattr("wshub.runtime.repo_dir", lambda: tmp_path)
    ui, out = _ui()
    assert install.update(env, ui) == 0 and any("git -C" in x and "pull" in x for x in out)
    monkeypatch.setattr("wshub.runtime.repo_dir", lambda: None)
    uv = tmp_path / "uv"
    uv.write_text("#!/bin/sh\nexit 0\n")
    uv.chmod(0o755)
    monkeypatch.setattr(install, "_uv", lambda: str(uv))
    env.exe = tmp_path / "wshub"
    env.exe.write_text("#!/bin/sh\necho 'wshub 9.9.9 (abcdef12, из git)'\n")
    env.exe.chmod(0o755)
    ui, out = _ui()
    assert install.update(env, ui) == 0
    assert any("обновлено: wshub 9.9.9" in x for x in out) and any("перезапусти Claude Desktop" in x for x in out)


def test_installed_commit(monkeypatch):
    from wshub import runtime

    class Dist:
        def __init__(self, text):
            self.text = text

        def read_text(self, _name):
            return self.text
    monkeypatch.setattr("importlib.metadata.distribution",
                        lambda _n: Dist('{"url": "https://github.com/Sufir/wshub", '
                                        '"vcs_info": {"vcs": "git", "commit_id": "' + "c" * 40 + '"}}'))
    assert runtime.installed_commit() == "c" * 40
    assert runtime.code_head(None) == "c" * 40
    monkeypatch.setattr("importlib.metadata.distribution", lambda _n: Dist('{"url": "file:///x", "dir_info": {}}'))
    assert runtime.installed_commit() is None


def test_version_cli(machine):
    r = machine.wshub("--version")
    assert r.returncode == 0 and r.stdout.startswith("wshub ")

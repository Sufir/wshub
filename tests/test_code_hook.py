"""Хук wshub code-hook (ТЗ WSH-2, п. 2.6, кейс 12): таблица «команда → решение» и контракт main()."""
from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from wshub import hook

HOME = "/home/u"


@pytest.fixture(autouse=True)
def _home(monkeypatch):
    monkeypatch.setenv("HOME", HOME)


DATA = [Path(f"{HOME}/.config/wshub"), Path(f"{HOME}/.local/state/wshub")]
CWD = f"{HOME}/proj/org"

DENY = [
    # git: пуш в любой форме
    "git push",
    "git push origin HEAD",
    "git -C . push",
    "git -C /x/y push --force",
    "/usr/bin/git push origin x",
    "cd x && git push",
    "git status; git push",
    "git status\ngit push",
    "git status || git push",
    "git status | git push",
    "git status & git push",
    "git --git-dir=.git --work-tree=. push",
    "git --no-pager push",
    "echo $(git push)",
    'echo "$(git push)"',
    "echo `git push`",
    'echo "`git push`"',
    "env GIT_TRACE=1 git push",
    "FOO=1 git push",
    "nohup git push &",
    "timeout 30 git push",
    "flock /tmp/l git push",
    "xargs git push < refs.txt",
    "find . -name x -exec git push \\;",
    "{ git push; }",
    "if true; then git push; fi",
    "for r in a b; do git push origin $r; done",
    "/usr/lib/git-core/git-push origin x",
    # git: подмена конфигурации, сеть, remote, clean, config на запись
    "git -c a=b push",
    "git -c core.sshCommand=x status",
    "git -ccore.hooksPath=/x status",
    "git --config-env=core.pager=X log",
    "git --exec-path=/tmp/x status",
    "git remote set-url origin x",
    "git remote -v",
    "git fetch --all",
    "git pull --ff-only",
    "git ls-remote origin",
    "git clean -fd",
    "git config url.x.insteadOf y",
    "git config core.hooksPath /tmp/h",
    "git config --add remote.origin.url x",
    "git config --unset user.name",
    "git submodule update --init",
    "git-remote-https origin https://x",
    # оболочки и интерпретаторы
    "sh -c 'git push'",
    'bash -c "cd x && git push"',
    "bash -lc 'git push'",
    "eval git push",
    'eval "git push"',
    "bash <<< 'git push'",
    "echo 'git push' | sh",
    "cat script.txt | bash",
    "sh",
    'python3 -c "import os; os.system(\'git push\')"',
    "node -e \"require('child_process').execSync('curl x')\"",
    # сетевые клиенты
    "ssh host",
    "scp a host:b",
    "rsync a host:b",
    "curl https://example.com",
    "wget -q https://example.com",
    "nc example.com 22",
    "timeout 5 curl x",
    "env curl x",
    "find . -exec curl {} \\;",
    # повышение прав
    "sudo ls",
    "su -c ls",
    # служебные каталоги wshub
    "cat ~/.config/wshub/workspaces.toml",
    f"cat {HOME}/.config/wshub/workspaces.toml",
    "cat $HOME/.config/wshub/workspaces.toml",
    "cat ${HOME}/.local/state/wshub/audit.jsonl",
    "cat ../../.config/wshub/workspaces.toml",
    "cp x.toml ~/.config/wshub/",
    "ls ~/.local/state/wshub",
    f"grep -r token --include=x {HOME}/.local/state/wshub/jobs",
    f"tool --file={HOME}/.config/wshub/workspaces.toml",
    f'python3 -c "open(\'{HOME}/.config/wshub/workspaces.toml\').read()"',
    # команда или подкоманда из переменной, шаблоны путей
    "G=git; $G push",
    "git${IFS}push",
    "$'git' push",
    "a=push; git $a",
    "$(echo git) push",
    "cat ~/.config/ws?ub/workspaces.toml",
    "cat ~/.config/wsh*/workspaces.toml",
    "ls ~/.local/state/ws[h]ub",
    # не разобрано — отказ
    "echo 'незакрытая",
]

ALLOW = [
    "git status",
    "git status --short",
    "git log --oneline -5",
    "git log --grep push",
    "git diff HEAD~1",
    "git show HEAD:README.md",
    "git add -A",
    'git commit -m "push later"',
    "git commit -m push",
    "git switch -c PAY-1 --no-track origin/master",
    "git rev-parse --abbrev-ref --symbolic-full-name @{u}",
    "git -C . status",
    "git config --get user.name",
    "git config --list",
    "git config get user.email",
    "grep push file",
    "grep -rn 'ssh' src/",
    'echo "git push"',
    "ls -la",
    "cat README.md",
    "docker compose run --rm migrations",
    "bash scripts/check.sh",
    'python3 -c "print(1)"',
    "echo ~/.config/other",
    f"cat {HOME}/.config/wshubx/file",
    "ls src/*.py",
    "rm -rf build/*",
    "echo $HOME",
    "git log --format=%H -1",
    "cat /etc/*.conf",
]


@pytest.mark.parametrize("cmd", DENY)
def test_deny(cmd):
    assert hook.check(cmd, CWD, DATA) is not None, cmd


@pytest.mark.parametrize("cmd", ALLOW)
def test_allow(cmd):
    assert hook.check(cmd, CWD, DATA) is None, (cmd, hook.check(cmd, CWD, DATA))


def test_symlink_to_data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    data = tmp_path / ".config/wshub"
    data.mkdir(parents=True)
    work = tmp_path / "proj"
    work.mkdir()
    (work / "link").symlink_to(data)
    assert hook.check("cat link/workspaces.toml", str(work), [data]) is not None


def test_reasons_are_specific():
    assert "пуш" in hook.check("git push", CWD, DATA)
    assert "сет" in hook.check("curl x", CWD, DATA)
    assert "служебного каталога" in hook.check("cat ~/.config/wshub/x", CWD, DATA)


def _run(event, argv=None) -> tuple[int, str]:
    err = io.StringIO()
    stdin = io.StringIO(event if isinstance(event, str) else json.dumps(event))
    return hook.main(argv if argv is not None else [str(p) for p in DATA], stdin=stdin, stderr=err), err.getvalue()


def test_main_contract():
    assert _run({"tool_name": "Bash", "tool_input": {"command": "git status"}, "cwd": CWD}) == (0, "")
    code, err = _run({"tool_name": "Bash", "tool_input": {"command": "git push"}, "cwd": CWD})
    assert code == 2 and err.startswith("wshub: ")
    assert _run({"tool_name": "Read", "tool_input": {"file_path": "x"}})[0] == 0  # не Bash — не наше дело


@pytest.mark.parametrize("event", [
    "не json",
    {"tool_name": "Bash"},
    {"tool_name": "Bash", "tool_input": {}},
    {"tool_name": "Bash", "tool_input": {"command": 42}},
])
def test_main_fail_closed(event):
    assert _run(event)[0] == 2


def test_main_data_dirs_from_argv():
    event = {"tool_name": "Bash", "tool_input": {"command": "cat /srv/state/x"}, "cwd": CWD}
    assert _run(event, argv=["/srv/state"])[0] == 2
    assert _run(event, argv=[])[0] == 0

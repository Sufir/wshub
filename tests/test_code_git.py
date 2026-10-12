"""Защита git хаба вне песочницы (ТЗ WSH-2, решение 4, слои б/в): переопределения, проверка .git/config,
отказ при worktree/commondir, хуки не запускаются."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from wshub import gitsafe


def _git(repo: Path, *args: str):
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True,
                   env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@e",
                        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@e"})


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "r"
    r.mkdir()
    _git(r, "init", "-q")
    (r / "a.txt").write_text("1\n")
    _git(r, "add", "-A")
    _git(r, "commit", "-qm", "init")
    return r


def test_overrides_present():
    a = gitsafe.argv("git", Path("/x"), "status", "--short")
    assert "--no-optional-locks" in a and a[:2] == ["git", "--no-optional-locks"]
    joined = " ".join(a)
    for o in ("core.hooksPath=/dev/null", "core.fsmonitor=false", "credential.helper=",
              "protocol.ext.allow=never"):
        assert o in joined
    assert a[-2:] == ["status", "--short"]


def test_status_runs(repo):
    r = gitsafe.run("git", repo, ["status", "--short"], dict(os.environ), 30)
    assert r.returncode == 0


def test_fsmonitor_in_config_refused_no_marker(repo, tmp_path):
    marker = tmp_path / "PWNED"
    _git(repo, "config", "core.fsmonitor", f"touch {marker}")
    with pytest.raises(gitsafe.GitUnsafe) as e:
        gitsafe.run("git", repo, ["status", "--short"], dict(os.environ), 30)
    assert e.value.reason == "git_config_unsafe" and "core.fsmonitor" in e.value.keys
    assert not marker.exists()


def test_hookspath_in_config_refused(repo):
    _git(repo, "config", "core.hooksPath", "/tmp/hooks")
    with pytest.raises(gitsafe.GitUnsafe):
        gitsafe.run("git", repo, ["status"], dict(os.environ), 30)


def test_reference_transaction_hook_not_run(tmp_path):
    """prefetch из локального bare-репозитория: исполняемый .git/hooks/reference-transaction не запускается
    (переопределение hooksPath=/dev/null)."""
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True, capture_output=True)
    work = tmp_path / "w"
    work.mkdir()
    _git(work, "init", "-q")
    (work / "f").write_text("x\n")
    _git(work, "add", "-A")
    _git(work, "commit", "-qm", "c")
    _git(work, "remote", "add", "origin", str(origin))
    _git(work, "push", "-q", "origin", "HEAD")
    _git(work, "fetch", "-q", "origin")  # завести remote-tracking ветку
    # делаем origin впереди (через второй клон), чтобы fetch реально обновлял ссылку и запускал хук
    pusher = tmp_path / "p"
    subprocess.run(["git", "clone", "-q", str(origin), str(pusher)], check=True, capture_output=True)
    (pusher / "g").write_text("y\n")
    _git(pusher, "add", "-A")
    _git(pusher, "commit", "-qm", "c2")
    _git(pusher, "push", "-q", "origin", "HEAD")
    marker = tmp_path / "HOOK-RAN"
    hook = work / ".git/hooks/reference-transaction"
    hook.write_text(f"#!/bin/sh\ntouch {marker}\n")
    hook.chmod(0o755)
    # fetch обновит refs/remotes/origin/*, но core.hooksPath=/dev/null не даст запустить хук
    r = gitsafe.run("git", work, ["fetch", "--all", "--prune", "--no-recurse-submodules"], dict(os.environ), 60)
    assert r.returncode == 0
    fh = subprocess.run(["git", "-C", str(work), "rev-parse", "FETCH_HEAD"], capture_output=True, text=True,
                        check=True)
    assert fh.stdout.strip()  # fetch действительно обновил ссылку (транзакция была)
    assert not marker.exists()  # но хук reference-transaction не запущен


def test_worktree_git_file_refused(tmp_path, repo):
    wt = tmp_path / "wt"
    _git(repo, "worktree", "add", "-q", str(wt))
    with pytest.raises(gitsafe.GitUnsafe) as e:
        gitsafe.run("git", wt, ["status"], dict(os.environ), 30)
    assert e.value.reason == "worktree"


def test_commondir_refused(repo, tmp_path):
    # .git/commondir в обычном репозитории уводит конфиг в другое место — отказ (находка к слою в)
    (repo / ".git/commondir").write_text("../elsewhere\n")
    with pytest.raises(gitsafe.GitUnsafe) as e:
        gitsafe.run("git", repo, ["status"], dict(os.environ), 30)
    assert e.value.reason == "worktree"


def test_env_no_ssh_agent_by_default():
    env = gitsafe.env({"SSH_AUTH_SOCK": "/s", "HOME": "/h", "PATH": "/b", "GIT_TOKEN": "x"})
    assert "SSH_AUTH_SOCK" not in env and "GIT_TOKEN" not in env
    assert env["GIT_ASKPASS"] == "/bin/false" and env["GIT_TERMINAL_PROMPT"] == "0"
    agent = gitsafe.env({"SSH_AUTH_SOCK": "/s", "HOME": "/h"}, ssh_agent=True)
    assert agent["SSH_AUTH_SOCK"] == "/s"

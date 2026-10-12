"""Вызовы git хабом (prefetch, снимок) в каталоге, где работал Claude Code.

Хаб запускает git вне песочницы, с правами пользователя, поэтому до запуска проверяет конфиг репозитория
по запретному списку, а сам вызов делает с фиксированными переопределениями, белым списком окружения,
stdin = /dev/null и таймаутом (ТЗ WSH-2, решение 4).
"""
from __future__ import annotations

import fnmatch
import os
import subprocess
from pathlib import Path

# Ключи .git/config, при которых хаб git не запускает (сравнение без учёта регистра)
UNSAFE_KEYS = (
    "include.*", "includeif.*", "core.fsmonitor", "core.hookspath", "core.sshcommand", "core.gitproxy",
    "core.askpass", "credential.*", "filter.*", "remote.*.uploadpack", "remote.*.receivepack",
    "protocol.*.allow", "url.*.insteadof", "url.*.pushinsteadof",
    # найдено при сверке с git help config (git 2.55)
    "hook.*", "gc.recentobjectshook", "core.alternaterefscommand", "remote.*.vcs", "protocol.allow",
    "gpg.program", "gpg.*.program", "gpg.ssh.defaultkeycommand",
)
OVERRIDES = (
    "core.hooksPath=/dev/null", "core.fsmonitor=false", "core.sshCommand=ssh", "credential.helper=",
    "protocol.ext.allow=never", "core.pager=cat",
    "core.alternateRefsCommand=", "gc.auto=0", "maintenance.auto=false", "log.showSignature=false",
    "submodule.recurse=false", "fetch.recurseSubmodules=false", "status.submoduleSummary=false",
    "diff.ignoreSubmodules=all",
)
ENV_PASS = ("HOME", "USER", "LOGNAME", "PATH", "LANG", "TZ")
FIXED_ENV = {"GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "/bin/false", "SSH_ASKPASS": "/bin/false"}
CONFIG_TIMEOUT = 15


class GitUnsafe(Exception):
    """Git в этом каталоге не запускается. reason — для job.json: git_config_unsafe | worktree | no_repo."""

    def __init__(self, reason: str, message: str, keys: list[str] | None = None):
        super().__init__(message)
        self.reason = reason
        self.keys = keys or []


def git_dir(start: Path) -> Path:
    """Каталог .git для start, без запуска git: ближайший .git вверх по дереву."""
    for d in (start, *start.parents):
        g = d / ".git"
        if g.is_symlink() or g.is_file():
            raise GitUnsafe("worktree", f"{g} — не каталог (worktree, подмодуль или ссылка): в v0 хаб не "
                                        "запускает git в таком репозитории")
        if g.is_dir():
            if (g / "commondir").exists():
                raise GitUnsafe("worktree", f"{g}/commondir есть: общий каталог git в другом месте — в v0 хаб "
                                            "не запускает git в таком репозитории")
            return g
    raise GitUnsafe("no_repo", f"{start} — не git-репозиторий")


def env(environ: dict, ssh_agent: bool = False) -> dict:
    out = {k: v for k, v in environ.items() if k in ENV_PASS or k.startswith("LC_")}
    if ssh_agent and environ.get("SSH_AUTH_SOCK"):  # только для fetch: доступ к git-хосту — у хаба, не у Code
        out["SSH_AUTH_SOCK"] = environ["SSH_AUTH_SOCK"]
    out.update(FIXED_ENV)
    return out


def unsafe_keys(git: str, gdir: Path, environ: dict) -> list[str]:
    """Ключи из запретного списка в .git/config (и config.worktree, если есть). Файл читается через
    git config --file … --list --no-includes: чтение ничего не запускает."""
    found: list[str] = []
    for f in (gdir / "config", gdir / "config.worktree"):
        if not f.exists():
            continue
        try:
            r = subprocess.run([git, "config", "--file", str(f), "--list", "--no-includes"], capture_output=True,
                               text=True, timeout=CONFIG_TIMEOUT, env=env(environ), stdin=subprocess.DEVNULL,
                               check=False)
        except (OSError, subprocess.TimeoutExpired) as e:
            raise GitUnsafe("git_config_unsafe", f"{f} не прочитан: {e}") from None
        if r.returncode != 0:
            raise GitUnsafe("git_config_unsafe", f"{f} не разбирается: {r.stderr.strip()[:300]}")
        for line in r.stdout.splitlines():
            key = line.split("=", 1)[0].lower()
            if any(fnmatch.fnmatchcase(key, p) for p in UNSAFE_KEYS) and key not in found:
                found.append(key)
    return found


def argv(git: str, cwd: Path, *args: str) -> list[str]:
    out = [git, "--no-optional-locks", "-C", str(cwd)]
    for o in OVERRIDES:
        out += ["-c", o]
    return [*out, *args]


def run(git: str, cwd: Path, args: list[str], environ: dict, timeout: float, ssh_agent: bool = False,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE) -> subprocess.CompletedProcess:
    """Проверка конфига, затем git с переопределениями. GitUnsafe — git не запускался."""
    gdir = git_dir(cwd)
    keys = unsafe_keys(git, gdir, environ)
    if keys:
        raise GitUnsafe("git_config_unsafe", f"в {gdir / 'config'} ключи, при которых хаб не запускает git: "
                                             f"{', '.join(keys)}", keys)
    return subprocess.run(argv(git, cwd, *args), stdout=stdout, stderr=stderr, text=True, timeout=timeout,
                          env=env(environ, ssh_agent), stdin=subprocess.DEVNULL, check=False,
                          cwd=os.fspath(cwd))

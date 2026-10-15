"""Вызовы git хабом (prefetch, снимок) в каталоге, где работал Claude Code.

Хаб запускает git вне песочницы, с правами пользователя, поэтому до запуска проверяет конфиг репозитория
по запретному списку, а сам вызов делает с фиксированными переопределениями, белым списком окружения,
stdin = /dev/null и таймаутом (ТЗ WSH-2, решение 4).
"""
from __future__ import annotations

import fnmatch
import os
import re
import shlex
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
# git push помощника (ТЗ WSH-3.4, А3.2): учётные данные — обычные у пользователя (ssh или credential helper из
# глобальной конфигурации), поэтому без подмены sshCommand и credential.helper. Те же ключи в .git/config
# репозитория — в UNSAFE_KEYS, с ними git не запускается
PUSH_OVERRIDES = tuple(o for o in OVERRIDES if not o.startswith(("core.sshCommand=", "credential.helper=")))
ENV_PASS = ("HOME", "USER", "LOGNAME", "PATH", "LANG", "TZ")
VALUE_MAX = 200  # символов значения ключа в тексте отказа
FIXED_ENV = {"GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "/bin/false", "SSH_ASKPASS": "/bin/false"}
CONFIG_TIMEOUT = 15
LAUNCH_FAIL = "Unable to read current working directory"  # сбой запуска git, а не плохой конфиг


class GitUnsafe(Exception):
    """Git в этом каталоге не запускается. reason — для job.json: git_config_unsafe (плохой ключ или конфиг не
    прочитан) | prefetch (сам git не запустился) | worktree | no_repo."""

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
    """Ключи из запретного списка в .git/config (и config.worktree, если есть)."""
    return list(dict.fromkeys(k for k, _v, _f in unsafe_entries(git, gdir, environ)))


def unsafe_entries(git: str, gdir: Path, environ: dict) -> list[tuple[str, str, Path]]:
    """(ключ, значение, файл) из запретного списка в .git/config (и config.worktree, если есть). Файл читается
    через git config --file … --list --no-includes: чтение ничего не запускает."""
    found: list[tuple[str, str, Path]] = []
    for f in (gdir / "config", gdir / "config.worktree"):
        if not f.exists():
            continue
        try:
            # cwd="/": рабочий каталог процесса хаба мог быть удалён — тогда git падает ещё до чтения файла
            r = subprocess.run([git, "config", "--file", str(f), "--list", "--no-includes"], capture_output=True,
                               text=True, timeout=CONFIG_TIMEOUT, env=env(environ), stdin=subprocess.DEVNULL,
                               check=False, cwd="/")
        except (OSError, subprocess.TimeoutExpired) as e:
            raise GitUnsafe("prefetch", f"git не запустился для чтения {f}: {e}") from None
        if r.returncode != 0:
            err = r.stderr.strip()
            if not r.stdout and LAUNCH_FAIL in err:
                raise GitUnsafe("prefetch", f"git не запустился для чтения {f}: рабочий каталог процесса хаба удалён "
                                            f"({err[:300]}) — перезапусти сервер wshub-code")
            raise GitUnsafe("git_config_unsafe", f"конфиг не прочитан: {f} не разбирается git ({err[:300]})")
        for line in r.stdout.splitlines():
            key, _, value = line.partition("=")
            key = key.lower()
            if any(fnmatch.fnmatchcase(key, p) for p in UNSAFE_KEYS):
                found.append((key, value, f))
    return found


def _show(text: str) -> str:
    """Значение для текста отказа: без учётных данных в адресе (url.<база>.insteadOf) и не длиннее VALUE_MAX."""
    text = re.sub(r"(://)[^/@\s]+@", r"\1***@", text)
    return text if len(text) <= VALUE_MAX else text[:VALUE_MAX] + "…"


def unsafe_message(repo: Path, entries: list[tuple[str, str, Path]]) -> str:
    """Отказ git_config_unsafe (ТЗ WSH-3.4, А4.5): ключ, значение и способ починки на каждый ключ."""
    rows = []
    for key, value, f in entries:
        where = "" if f.name == "config" else f" (в {f.name})"
        opt = "" if f.name == "config" else " --worktree"
        rows.append(f"{_show(key)} = {_show(value)}{where} — исправь: git -C {shlex.quote(str(repo))} config{opt} "
                    f"--unset-all {shlex.quote(_show(key))} или укажи верное значение")
    return "плохой ключ: хаб не запускает git в репозитории с такими ключами в .git/config: " + "; ".join(rows)


def unsafe_key(key: str) -> bool:
    """Ключ из запретного списка (без учёта регистра) — «опасный» в блоке «Вне кода»."""
    return any(fnmatch.fnmatchcase(key.lower(), p) for p in UNSAFE_KEYS)


def argv(git: str, cwd: Path, *args: str, overrides: tuple[str, ...] = OVERRIDES) -> list[str]:
    out = [git, "--no-optional-locks", "-C", str(cwd)]
    for o in overrides:
        out += ["-c", o]
    return [*out, *args]


def run(git: str, cwd: Path, args: list[str], environ: dict, timeout: float, ssh_agent: bool = False,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, push: bool = False) -> subprocess.CompletedProcess:
    """Проверка конфига, затем git с переопределениями. GitUnsafe — git не запускался. push=True — git push
    помощника: учётные данные пользователя (PUSH_OVERRIDES) и агент ssh из environ."""
    gdir = git_dir(cwd)
    entries = unsafe_entries(git, gdir, environ)
    if entries:
        raise GitUnsafe("git_config_unsafe", unsafe_message(cwd, entries),
                        list(dict.fromkeys(k for k, _v, _f in entries)))
    return subprocess.run(argv(git, cwd, *args, overrides=PUSH_OVERRIDES if push else OVERRIDES), stdout=stdout,
                          stderr=stderr, text=True, timeout=timeout, env=env(environ, ssh_agent or push),
                          stdin=subprocess.DEVNULL, check=False, cwd=os.fspath(cwd))

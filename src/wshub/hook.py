"""wshub code-hook: PreToolUse-хук Claude Code для Bash (ТЗ WSH-2, п. 2.6).

Последний рубеж для команд, которые прошли allow-правила профиля, и единственный — для команд из
sandbox_exclude, которые идут вне песочницы. Отказ, если в любом звене команды есть:

1. git с подкомандой, которая ходит в сеть, меняет remote или конфигурацию, удаляет неотслеживаемые файлы
   (push, fetch, pull, clean, config на запись и т. п.; remote — кроме чтения: git remote, -v, get-url), или
   с глобальной опцией, подменяющей конфигурацию (-c, --config-env, --exec-path=…); git grep -O /
   --open-files-in-pager (запускает программу, в том числе сокращения и склейки коротких опций); --output у
   log/show/diff — только в пишущем профиле и только в файл внутри рабочего каталога (review — внутри
   .agents/), не в .git; git с переменной окружения GIT_*, PAGER, LESSOPEN, LESSCLOSE перед ним (внешний diff,
   пейджер, чужой каталог git);
2. сетевой клиент (ssh, scp, rsync, curl, wget, nc …);
3. путь внутрь служебных каталогов wshub (реестр, состояние) — их передаёт settings.json аргументами;
4. оболочка, читающая команды из stdin, или интерпретатор с кодом в строке, где упомянуто запрещённое;
5. docker и docker compose, если команда выходит из изоляции контейнера на хост или стирает данные:
   монтирование не из рабочего каталога (источник — только именованный том или абсолютный путь внутри
   cwd), --privileged и другие флаги ослабления изоляции, --network host, другой демон или конфигурация
   docker (-H, --context, --config), файлы хоста в командах (cp, --env-file, --cidfile …), выгрузка наружу
   (push, login, save/export), docker build (контекст сборки читает хост), удаление томов (down -v,
   volume rm/prune, system prune), compose-файл с чужим именем (-f), переменные окружения перед docker.
   Сама команда docker — вне песочницы (sandbox_exclude), поэтому этот пол — единственная её граница;
6. wshub — кроме помощника хаба (ТЗ WSH-3.4, А3): wshub push и wshub forge идут вне песочницы, поэтому
   разрешены только командой целиком — одно звено, без обёрток, присваиваний, $, обратных кавычек и
   перенаправлений; wshub push — только в профиле task; --out у помощника — по тем же правилам, что --output
   у git. Разрешение задачи и регламент проекта проверяет сам помощник, хук держит форму команды.

Профиль задачи приходит аргументом --profile (task, review, recon, plan). Без него — как у читающего профиля.

Звенья команды: ; && || | & и перевод строки, $( ), обратные кавычки, <( ), {…}, sh -c / bash -c, eval,
обёртки (env, nohup, timeout, xargs, find -exec …). Ошибка разбора — отказ (fail closed).

Хук не заменяет deny-правила и песочницу: он закрывает формы, которые правила по тексту не ловят.
Остаточные дыры известны и закрыты другими слоями: псевдонимы git из ~/.gitconfig (Code его не пишет —
песочница), скрипты и Makefile, которые запускает разрешённая команда (сеть и ~/.ssh закрыты песочницей,
вне песочницы — только sandbox_exclude, его ведёт человек).
"""
from __future__ import annotations

import fnmatch
import json
import os
import re
import shlex
import sys
from pathlib import Path
from typing import NamedTuple

MAX_COMMAND = 200_000  # длиннее — отказ: разбор такой команды ненадёжен
MAX_DEPTH = 8  # вложенность sh -c / eval / $( ) / ``

# Подкоманды git, закрытые в задачах: сеть и remote, удаление неотслеживаемого, внешние транспорты
GIT_DENY = {
    "push", "fetch", "pull", "remote", "ls-remote", "clean", "submodule", "send-pack", "send-email",
    "request-pull", "credential", "credential-store", "credential-cache", "daemon", "http-backend",
    "upload-pack", "receive-pack", "upload-archive", "svn", "p4", "imap-send", "instaweb",
}
# git config: разрешено только чтение
GIT_CONFIG_READ = {"--get", "--get-all", "--get-regexp", "--get-urlmatch", "--list", "-l", "get", "list"}
GIT_CONFIG_WRITE = {"--add", "--unset", "--unset-all", "--replace-all", "--rename-section", "--remove-section",
                    "--edit", "-e", "set", "unset", "rename-section", "remove-section", "edit"}
# глобальные опции git, за которыми идёт значение отдельным аргументом
GIT_OPT_WITH_VALUE = {"-C", "--git-dir", "--work-tree", "--namespace", "--super-prefix", "--attr-source"}
# «читающие» подкоманды (allow профилей recon/review и фазы плана), которые пишут файл или запускают программу:
# git log/show/diff --output=<файл>; git grep -O<программа> / --open-files-in-pager[=<программа>] — git
# принимает однозначные сокращения длинных опций (--op=…) и склейку коротких (-nO…)
GIT_GREP_PAGER = "open-files-in-pager"
GIT_GREP_SHORT_WITH_VALUE = set("efABCm")  # после них в склейке идёт значение, а не опции
# переменные окружения перед git, которые задают ему программу, конфигурацию или чужой каталог
GIT_ENV_DENY = re.compile(r"^(GIT_[A-Za-z0-9_]*|PAGER|LESSOPEN|LESSCLOSE)\+?=")

NET_TOOLS = {
    "ssh", "scp", "sftp", "rsync", "curl", "wget", "nc", "ncat", "netcat", "socat", "telnet", "ftp", "lftp",
    "ssh-agent", "ssh-add", "aria2c", "httpie", "http", "https",
}
PRIV = {"sudo", "su", "doas", "pkexec", "runuser"}
SHELLS = {"sh", "bash", "dash", "zsh", "ksh", "mksh", "ash", "busybox"}
INTERPRETERS = {"python", "python3", "node", "nodejs", "perl", "ruby", "php", "deno", "bun"}
# обёртки: после них команда идёт дальше в той же строке
WRAPPERS = {
    "env", "nohup", "time", "command", "exec", "nice", "ionice", "timeout", "stdbuf", "setsid", "chronic",
    "builtin", "unbuffer", "flock", "watch", "xargs", "parallel", "strace", "ltrace", "chroot", "nsenter",
    "unshare", "script",
}
FIND_EXEC = {"-exec", "-execdir", "-ok", "-okdir"}
KEYWORDS = {"!", "{", "}", "if", "then", "else", "elif", "fi", "do", "done", "while", "until", "for", "in",
            "case", "esac", "select", "function", "coproc"}
ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*\+?=")
PUNCT = set("();<>|&")
DOCKER = {"docker", "docker-compose"}
# глобальные опции docker: другой демон, контекст или конфигурация (в каталоге конфигурации — cli-plugins)
DOCKER_GLOBAL_DENY = {"-H", "--host", "-c", "--context", "--config"}
DOCKER_GLOBAL_WITH_VALUE = {"-H", "--host", "-c", "--context", "--config", "-l", "--log-level", "--tlscacert",
                            "--tlscert", "--tlskey"}
# подкоманды docker: файлы хоста, выгрузка наружу, swarm и плагины, сборка с контекстом с хоста
DOCKER_SUB_DENY = {
    "cp": "копирует файлы между хостом и контейнером в обход песочницы",
    "push": "выгружает образ наружу", "login": "работает с учётными данными реестра",
    "logout": "работает с учётными данными реестра", "save": "пишет образ в файл хоста",
    "load": "читает образ из файла хоста", "import": "читает файл хоста", "export": "пишет файл хоста",
    "build": "контекст сборки читается с хоста — используй docker compose build",
    "buildx": "сборщик с выводом на хост и удалёнными сборщиками", "builder": "управляет сборщиками",
    "context": "переключает демон docker", "plugin": "ставит плагины демона",
    "swarm": "swarm вне задач", "node": "swarm вне задач", "service": "swarm вне задач",
    "stack": "swarm вне задач", "secret": "swarm вне задач", "config": "swarm вне задач",
    "trust": "подпись образов вне задач", "manifest": "выгрузка манифестов вне задач",
}
# управляющие группы docker: docker container cp, docker image push … — то же, что верхний уровень
DOCKER_MGMT = {"container", "image", "volume", "system", "network"}
COMPOSE_GLOBAL_WITH_VALUE = {"-f", "--file", "-p", "--project-name", "--project-directory", "--env-file",
                             "--profile", "--ansi", "--progress", "--parallel"}
COMPOSE_SUB_DENY = {"cp": "копирует файлы между хостом и контейнером в обход песочницы",
                    "push": "выгружает образы наружу", "publish": "выгружает проект наружу"}
COMPOSE_FILE_RE = re.compile(r"^(docker-)?compose[\w.-]*\.ya?ml$")
# флаги, ослабляющие изоляцию контейнера, и файлы хоста в аргументах
DOCKER_FLAG_DENY = {"--privileged", "--cap-add", "--device", "--device-cgroup-rule", "--pid", "--ipc", "--uts",
                    "--userns", "--security-opt", "--cgroupns", "--cgroup-parent", "--runtime", "--volumes-from"}
DOCKER_FILE_FLAGS = {"--env-file", "--label-file", "--cidfile", "--iidfile"}
DOCKER_SHORT_BOOL = set("itdPqT")  # короткие флаги без значения, которые бывают в склейке с -v
NAMED_VOLUME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")

# git remote на чтение (ТЗ WSH-3.4, А4.2): имена и адреса remote; show ходит в сеть, остальное меняет remote
GIT_REMOTE_READ_FLAGS = {"-v", "--verbose"}
GIT_REMOTE_GETURL_FLAGS = {"--push", "--all"}
# профили, которым можно писать файлы (Edit): task — рабочий каталог, review — только .agents/
WRITE_PROFILES = {"task", "review"}
# помощник хаба (ТЗ WSH-3.4, А3): вне песочницы, поэтому только командой целиком
HELPER = "wshub"
HELPER_SUBS = {"push", "forge"}

CODE_DENY = re.compile(
    r"\bgit\b[^\n;|&]{0,200}?\b(" + "|".join(sorted(GIT_DENY | {"config"})) + r")\b"
    r"|\b(" + "|".join(sorted(NET_TOOLS - {"http", "https"})) + r")\b")


class _Deny(Exception):
    pass


class _Ctx(NamedTuple):
    cwd: Path
    roots: list[str]
    profile: str | None  # task, review, recon, plan; None — как у читающего профиля
    helper: bool  # вся команда — один вызов помощника хаба (_helper_call)


def check(command: str, cwd: str, protected: list[Path], profile: str | None = None) -> str | None:
    """Причина отказа для Bash-команды или None — разрешено. protected — служебные каталоги wshub (данные);
    profile — профиль задачи (--profile хука)."""
    if not isinstance(command, str):
        return "команда не строка"
    if len(command) > MAX_COMMAND:
        return f"команда длиннее {MAX_COMMAND} символов — разбор ненадёжен"
    roots = _roots(protected)
    try:
        hit = _paths_in_text(command, roots)
        if hit:
            raise _Deny(_path_reason(hit))
        ctx = _Ctx(Path(cwd or "."), roots, profile, _helper_call(command))
        _check_script(command, ctx, 0)
    except _Deny as e:
        return str(e)
    except ValueError as e:  # shlex: незакрытая кавычка и т. п.
        return f"команда не разобрана ({e}) — отказ"
    return None


# ---------- разбор ----------

def _check_script(script: str, ctx: _Ctx, depth: int) -> None:
    if depth > MAX_DEPTH:
        raise _Deny("слишком глубокая вложенность команд — отказ")
    for seg in _segments(script):
        _check_segment(seg, ctx, depth)


def _segments(script: str) -> list[list[str]]:
    """Звенья команды. Перевод строки и обратная кавычка становятся разделителями (в кавычках они остаются
    внутри строки — их разбирает _nested)."""
    text = script.replace("\r", "\n").replace("\n", " ; ").replace("`", " ` ")
    lex = shlex.shlex(text, posix=True, punctuation_chars=True)
    lex.whitespace_split = True
    lex.commenters = ""
    segs: list[list[str]] = [[]]
    for tok in lex:
        if tok == "`" or (tok and set(tok) <= PUNCT and any(c in tok for c in ";|&()")):
            segs.append([])
            continue
        segs[-1].append(tok)
    return [s for s in segs if s]


def _check_segment(tokens: list[str], ctx: _Ctx, depth: int) -> None:
    cwd = ctx.cwd
    for tok in tokens:  # подстановки внутри строк в кавычках
        for inner in _nested(tok):
            _check_script(inner, ctx, depth + 1)
        hit = _path_hit(tok, cwd, ctx.roots)
        if hit:
            raise _Deny(_path_reason(hit))

    i = 0
    while i < len(tokens) and (tokens[i] in KEYWORDS or ASSIGN.match(tokens[i]) or set(tokens[i]) <= set("<>")):
        i += 1
    if i >= len(tokens):
        return
    if "$" in tokens[i] or tokens[i] in ("$", "`"):
        raise _Deny("команда собрана из переменной или подстановки — хук не может её проверить, отказ")
    candidates = {i}
    name = _base(tokens[i])
    if name in WRAPPERS:
        candidates |= set(range(i + 1, len(tokens)))
    if name == "find":
        candidates |= {j + 1 for j in range(i, len(tokens) - 1) if tokens[j] in FIND_EXEC}
    if name == "eval":
        _check_script(" ".join(tokens[i + 1:]), ctx, depth + 1)

    for j in sorted(candidates):
        n = _base(tokens[j])
        if n == HELPER:
            if not (ctx.helper and depth == 0 and j == 0):
                raise _Deny("wshub в задаче — только помощник хаба (wshub push, wshub forge) и только командой "
                            "целиком: одно звено, без обёрток, присваиваний, $, обратных кавычек и перенаправлений")
            _check_helper(tokens, ctx)
            return
        if n in NET_TOOLS:
            raise _Deny(f"сетевой клиент {n} в задачах wshub-code запрещён: сети у задачи нет")
        if n in PRIV:
            raise _Deny(f"{n}: повышение прав в задачах wshub-code запрещено")
        if n in SHELLS:
            _check_shell(tokens, j, ctx, depth)
        if n in INTERPRETERS:
            _check_interpreter(tokens, j)
        if n in DOCKER:
            _check_docker(tokens, j, cwd)
    for j, tok in enumerate(tokens):  # git в любой позиции: обёртки, которые обёртка не распознала
        if _is_git(tok):
            _check_git(tokens, j, ctx)


def _helper_call(command: str) -> bool:
    """Вся команда — один вызов помощника хаба: wshub push|forge … Одно звено без обёрток и присваиваний, без $,
    обратных кавычек, переводов строки, перенаправлений и фона. Помощник идёт вне песочницы (sandbox_exclude), и
    рядом с ним в той же команде не должно быть ничего."""
    if any(c in command for c in "$`\n\r"):
        return False
    lex = shlex.shlex(command, posix=True, punctuation_chars=True)
    lex.whitespace_split = True
    lex.commenters = ""
    toks = list(lex)
    if any(t and set(t) <= PUNCT for t in toks):  # ; & | > < ( ) в любом виде, кроме как внутри кавычек
        return False
    return len(toks) >= 2 and _base(toks[0]) == HELPER and toks[1] in HELPER_SUBS


def _check_helper(tokens: list[str], ctx: _Ctx) -> None:
    """Помощник хаба, уже проверенный по форме (_helper_call): профиль для push и путь --out."""
    sub = tokens[1]
    if sub == "push" and ctx.profile != "task":
        raise _Deny("wshub push — только в профиле task: пушит задача, которая пишет код")
    for idx, t in enumerate(tokens[2:], 2):
        if t == "--out" or t.startswith("--out="):
            val = t.split("=", 1)[1] if "=" in t else (tokens[idx + 1] if idx + 1 < len(tokens) else "")
            _check_write_path(f"wshub {sub} --out", val, ctx)


def _check_write_path(label: str, val: str, ctx: _Ctx) -> None:
    """Файл, который пишет «читающая» команда (git --output, wshub forge --out): только в пишущем профиле, внутри
    рабочего каталога (review — внутри .agents/), не в .git; путь явный — без переменных, шаблонов и ~."""
    if ctx.profile not in WRITE_PROFILES:
        raise _Deny(f"{label} пишет файл — в читающем профиле запрещено")
    if not val or any(c in val for c in "$`*?[~"):
        raise _Deny(f"{label}: нужен явный путь к файлу — без переменных, шаблонов и ~")
    base = os.path.normpath(str(ctx.cwd))
    area = base if ctx.profile == "task" else os.path.join(base, ".agents")
    where = "рабочего каталога задачи" if ctx.profile == "task" else ".agents/ рабочего каталога"
    p = os.path.normpath(val if os.path.isabs(val) else os.path.join(base, val))
    real, real_area = os.path.realpath(p), os.path.realpath(area)
    if not p.startswith(area + "/") or not real.startswith(real_area + "/"):
        raise _Deny(f"{label} {val}: файл — только внутри {where}")
    if any(part.lower() == ".git" for part in os.path.relpath(p, base).split("/")):
        raise _Deny(f"{label} {val}: запись в .git запрещена")


def _check_git_remote(args: list[str]) -> None:
    """git remote — только чтение: без аргументов, -v, get-url <имя> [--push | --all]."""
    rest = [a for a in args if a not in GIT_REMOTE_READ_FLAGS]
    if not rest:
        return
    if rest[0] == "get-url":
        names = [a for a in rest[1:] if a not in GIT_REMOTE_GETURL_FLAGS]
        if len(names) == 1 and not names[0].startswith("-") and "$" not in names[0]:
            return
    raise _Deny("git remote: в задачах — только чтение (git remote, git remote -v, git remote get-url <имя>); "
                "show ходит в сеть, остальное меняет remote")


def _check_shell(tokens: list[str], j: int, ctx: _Ctx, depth: int) -> None:
    args = tokens[j + 1:]
    k = 0
    while k < len(args) and args[k].startswith("-") and args[k] != "--":
        opt = args[k]
        if not opt.startswith("--") and "c" in opt[1:]:  # -c, -lc, -ec …
            if k + 1 >= len(args):
                raise _Deny("оболочка с -c без команды — отказ")
            _check_script(args[k + 1], ctx, depth + 1)
            return
        k += 1
    if "<<<" in args:
        idx = args.index("<<<")
        if idx + 1 < len(args):
            _check_script(args[idx + 1], ctx, depth + 1)
            return
    rest = [a for a in args[k:] if a != "--"]
    if not rest or rest[0] in ("-", "/dev/stdin") or set(rest[0]) <= set("<>"):
        raise _Deny(f"{_base(tokens[j])} читает команды из stdin — хук не может их проверить, отказ")


def _check_interpreter(tokens: list[str], j: int) -> None:
    args = tokens[j + 1:]
    for k, a in enumerate(args):
        if a in ("-c", "-e", "-r", "--eval", "-E") and k + 1 < len(args):
            m = CODE_DENY.search(args[k + 1])
            if m:
                raise _Deny(f"{_base(tokens[j])} {a}: в коде упомянуто «{m.group(0).strip()}» — запрещено в "
                            "задачах wshub-code")
            return


def _is_git(tok: str) -> bool:
    n = _base(tok)
    return n == "git" or n.startswith("git-")


def _check_git(tokens: list[str], j: int, ctx: _Ctx) -> None:
    for t in tokens[:j]:
        m = GIT_ENV_DENY.match(t)
        if m:
            raise _Deny(f"переменная {m.group(1)} перед git задаёт ему программу, конфигурацию или каталог — "
                        "в задачах запрещено")
    n = _base(tokens[j])
    if n.startswith("git-"):  # прямой вызов исполняемого файла подкоманды: git-push, git-remote-https …
        sub = n[4:]
        if sub in GIT_DENY or sub.startswith("remote") or sub == "config":
            raise _Deny(f"{n}: {_git_reason(sub)}")
        _check_git_args(sub, tokens[j + 1:], ctx)
        return
    k = j + 1
    while k < len(tokens):
        t = tokens[k]
        if t in ("-c", "--config-env") or t.startswith("--config-env=") or t.startswith("-c") and len(t) > 2:
            raise _Deny("git -c / --config-env подменяет конфигурацию git — в задачах запрещено")
        if t.startswith("--exec-path="):
            raise _Deny("git --exec-path=… подменяет исполняемые файлы git — в задачах запрещено")
        if t in GIT_OPT_WITH_VALUE:
            k += 2
            continue
        if t.startswith("-"):
            k += 1
            continue
        break
    if k >= len(tokens):
        return
    sub = tokens[k]
    if "$" in sub or any(c in sub for c in "*?["):
        raise _Deny("подкоманда git собрана из переменной или шаблона — хук не может её проверить, отказ")
    if sub == "remote":
        _check_git_remote(tokens[k + 1:])
        return
    if sub in GIT_DENY:
        raise _Deny(f"git {sub}: {_git_reason(sub)}")
    if sub == "config":
        args = set(tokens[k + 1:])
        if args & GIT_CONFIG_WRITE or not args & GIT_CONFIG_READ:
            raise _Deny("git config: в задачах разрешено только чтение (--get, --list)")
    _check_git_args(sub, tokens[k + 1:], ctx)


def _check_git_args(sub: str, args: list[str], ctx: _Ctx) -> None:
    """Опции подкоманды, которые пишут файл или запускают программу. После «--» — пути, не опции.
    --output: git принимает только точное имя — сокращения неоднозначны с --output-indicator-*."""
    for idx, t in enumerate(args):
        if t == "--":
            return
        if t == "--output" or t.startswith("--output="):
            val = t.split("=", 1)[1] if "=" in t else (args[idx + 1] if idx + 1 < len(args) else "")
            _check_write_path(f"git {sub} --output", val, ctx)
            continue
        if sub != "grep" or not t.startswith("-") or t == "-":
            continue
        if t.startswith("--"):
            name = t[2:].split("=", 1)[0]
            if len(name) >= 2 and GIT_GREP_PAGER.startswith(name):
                raise _Deny("git grep --open-files-in-pager запускает программу — в задачах запрещено")
            continue
        for ch in t[1:]:
            if ch == "O":
                raise _Deny("git grep -O запускает программу — в задачах запрещено")
            if ch in GIT_GREP_SHORT_WITH_VALUE:
                break


def _check_docker(tokens: list[str], j: int, cwd: Path) -> None:
    """docker и docker compose: выход из изоляции контейнера на хост и удаление данных (пункт 5 докстринга)."""
    for t in tokens[:j]:
        if ASSIGN.match(t):
            raise _Deny("переменные окружения перед docker меняют подстановки compose и адрес демона — "
                        "в задачах запрещено")
    args = tokens[j + 1:]
    k = 0
    compose = _base(tokens[j]) == "docker-compose"
    if not compose:
        while k < len(args) and args[k].startswith("-"):
            opt = args[k].split("=", 1)[0]
            if opt in DOCKER_GLOBAL_DENY:
                raise _Deny(f"docker {opt} меняет демон или конфигурацию docker — в задачах запрещено")
            k += 2 if opt in DOCKER_GLOBAL_WITH_VALUE and "=" not in args[k] else 1
        if k >= len(args):
            return
        sub = _docker_word(args[k])
        k += 1
        if sub != "compose":
            if sub in DOCKER_MGMT and k < len(args):  # docker container cp, docker image push …
                sub2 = _docker_word(args[k])
                if sub == "system" and sub2 == "prune":
                    raise _Deny("docker system prune удаляет тома и данные — в задачах запрещено")
                if sub == "volume" and sub2 in ("rm", "remove", "prune"):
                    raise _Deny(f"docker volume {sub2} удаляет тома (данные БД) — в задачах запрещено")
                if sub2 in DOCKER_SUB_DENY:
                    raise _Deny(f"docker {sub} {sub2}: {DOCKER_SUB_DENY[sub2]} — в задачах запрещено")
                if sub == "container" and sub2 in ("run", "create", "exec", "rm"):
                    _check_docker_sub(sub2, args[k + 1:], cwd, "docker container")
                return
            _check_docker_sub(sub, args[k:], cwd, "docker")
            return
    while k < len(args) and args[k].startswith("-"):  # глобальные опции compose
        t = args[k]
        opt, eq, val = t.partition("=")
        if not eq and len(t) > 2 and not t.startswith("--") and t[:2] in ("-f", "-p"):
            opt, val, eq = t[:2], t[2:], "="  # -fcompose.yml
        if opt in COMPOSE_GLOBAL_WITH_VALUE:
            if not eq:
                val = args[k + 1] if k + 1 < len(args) else ""
                k += 1
            if opt in ("-f", "--file") and ("$" in val or not COMPOSE_FILE_RE.match(_base(val))):
                raise _Deny(f"docker compose {opt} {val}: compose-файл только с именем docker-compose*.yml или "
                            "compose*.yaml — в задачах запрещено")
            if opt == "--env-file":
                raise _Deny("docker compose --env-file подменяет подстановки compose — в задачах запрещено")
        k += 1
    if k >= len(args):
        return
    _check_docker_sub(_docker_word(args[k]), args[k + 1:], cwd, "docker compose")


def _docker_word(tok: str) -> str:
    if "$" in tok or any(c in tok for c in "*?[`"):
        raise _Deny("подкоманда docker собрана из переменной или шаблона — хук не может её проверить, отказ")
    return tok


def _check_docker_sub(sub: str, rest: list[str], cwd: Path, label: str) -> None:
    deny = COMPOSE_SUB_DENY if label == "docker compose" else DOCKER_SUB_DENY
    if sub in deny:
        raise _Deny(f"{label} {sub}: {deny[sub]} — в задачах запрещено")
    if sub in ("down", "rm") and any(t in ("-v", "--volumes") or t.startswith("--volumes=")
                                     or (t.startswith("-") and not t.startswith("--") and "v" in t[1:])
                                     for t in rest):
        raise _Deny(f"{label} {sub} -v удаляет тома (данные БД) — в задачах запрещено")
    if sub == "build" and any(t.split("=", 1)[0] in ("--ssh", "--secret", "-o", "--output") for t in rest):
        raise _Deny(f"{label} build --ssh/--secret/--output передаёт сборке ключи или файлы хоста — в задачах "
                    "запрещено")
    _check_container_flags(rest, cwd, label, mounts=sub in ("run", "create"))


def _check_container_flags(rest: list[str], cwd: Path, label: str, mounts: bool) -> None:
    """Флаги по всем аргументам, включая команду внутри контейнера: опасные имена там редки, а точное
    место конца опций без полного списка флагов docker не найти (отказ здесь — закрытый)."""
    for i, t in enumerate(rest):
        nxt = rest[i + 1] if i + 1 < len(rest) else ""
        opt, eq, val = t.partition("=")
        if opt in DOCKER_FLAG_DENY:
            raise _Deny(f"{label} {opt} ослабляет изоляцию контейнера — в задачах запрещено")
        if opt in DOCKER_FILE_FLAGS:
            raise _Deny(f"{label} {opt} читает или пишет файл хоста в обход песочницы — в задачах запрещено")
        if opt in ("--network", "--net") and (val if eq else nxt) == "host":
            raise _Deny(f"{label} {opt} host отдаёт контейнеру сеть хоста — в задачах запрещено")
        if not mounts:
            continue
        if opt in ("-v", "--volume"):
            _check_mount_spec(val if eq else nxt, cwd, label)
        elif opt == "--mount":
            _check_mount_opt(val if eq else nxt, cwd, label)
        elif t.startswith("-") and not t.startswith("--") and len(t) > 2:  # склейка: -itv /a:/b, -v/a:/b
            for idx, ch in enumerate(t[1:], 1):
                if ch == "v":
                    _check_mount_spec(t[idx + 1:] or nxt, cwd, label)
                    break
                if ch not in DOCKER_SHORT_BOOL:
                    break


def _check_mount_spec(spec: str, cwd: Path, label: str) -> None:
    """-v ИСТОЧНИК:ЦЕЛЬ[:опции]. Без «:» или с неабсолютной целью — анонимный том или чужой -v внутренней команды
    (pytest -v file::test): не монтирование хоста.
    Подстановка ($, `) — отказ: источник не проверить ($(pwd) разбор режет на звенья, остаётся «$»)."""
    if any(c in spec for c in "$`"):
        raise _Deny(f"{label}: источник монтирования из подстановки — хук не может его проверить, укажи "
                    "абсолютный путь внутри рабочего каталога")
    src, sep, dst = spec.partition(":")
    if not sep or not dst.startswith("/"):
        return  # цель в контейнере у монтирования всегда абсолютная: «pytest -v file::test» — не монтирование
    _check_mount_source(src, cwd, label)


def _check_mount_opt(spec: str, cwd: Path, label: str) -> None:
    if any(c in spec for c in "$`"):
        raise _Deny(f"{label} --mount из подстановки — хук не может его проверить, отказ")
    kv = dict(p.split("=", 1) if "=" in p else (p, "") for p in spec.split(","))
    kind = kv.get("type", "volume")
    src = kv.get("source", kv.get("src", ""))
    if kind == "bind":
        _check_mount_source(src, cwd, label)
    elif kind == "volume":
        if src and not NAMED_VOLUME_RE.match(src):
            raise _Deny(f"{label} --mount: у тома {src!r} имя должно быть именем тома — в задачах запрещено")
    elif kind != "tmpfs":
        raise _Deny(f"{label} --mount type={kind}: в задачах только volume, bind внутри рабочего каталога, tmpfs")


def _check_mount_source(src: str, cwd: Path, label: str) -> None:
    if NAMED_VOLUME_RE.match(src):
        return  # именованный том
    if not src.startswith("/") or any(c in src for c in "$`*?["):
        raise _Deny(f"{label}: источник монтирования {src!r} — только именованный том или абсолютный путь "
                    "внутри рабочего каталога задачи")
    real, base = os.path.realpath(src), os.path.realpath(str(cwd))
    if real != base and not real.startswith(base + "/"):
        raise _Deny(f"{label}: монтирование {src} вне рабочего каталога задачи ({base}) открывает контейнеру "
                    "файлы хоста — в задачах запрещено")


def _git_reason(sub: str) -> str:
    if sub == "push":
        return ("голый git push в задачах запрещён: пуш — только помощником хаба wshub push, если его разрешают "
                "регламент проекта и задача")
    if sub in ("fetch", "pull", "ls-remote", "submodule"):
        return "сети у задачи нет; свежие ветки подтягивает хаб перед стартом (prefetch)"
    if sub == "clean":
        return "удаляет неотслеживаемые файлы, включая .agents/ — запрещено"
    if sub == "config":
        return "в задачах разрешено только чтение конфигурации"
    return "в задачах wshub-code запрещено"


def _nested(tok: str) -> list[str]:
    """Команды внутри $( ) и `` в одном токене (строка в кавычках)."""
    out = re.findall(r"`([^`]*)`", tok)
    i = 0
    while True:
        i = tok.find("$(", i)
        if i < 0:
            break
        level, k = 0, i + 1
        while k < len(tok):
            if tok[k] == "(":
                level += 1
            elif tok[k] == ")":
                level -= 1
                if level == 0:
                    break
            k += 1
        out.append(tok[i + 2:k])
        i = k
    for marker in ("<(", ">("):
        if marker in tok:
            out.append(tok.split(marker, 1)[1].rsplit(")", 1)[0])
    return [s for s in out if s.strip()]


def _base(tok: str) -> str:
    return tok.rsplit("/", 1)[-1]


# ---------- пути служебных каталогов ----------

def _roots(protected: list[Path]) -> list[str]:
    out: list[str] = []
    for p in protected:
        for q in (os.path.normpath(str(p)), os.path.realpath(str(p))):
            if q not in out and q not in ("/", ""):
                out.append(q)
    return out


def _expand(s: str) -> str:
    home = os.environ.get("HOME") or str(Path.home())
    s = s.replace("${HOME}", home).replace("$HOME", home)
    if s == "~" or s.startswith("~/"):
        s = home + s[1:]
    return s


def _paths_in_text(text: str, roots: list[str]) -> str | None:
    """Служебный каталог, упомянутый в тексте команды как подстрока (в том числе внутри кавычек и кода)."""
    home = os.environ.get("HOME") or str(Path.home())
    for r in roots:
        forms = {r}
        if r.startswith(home + "/"):
            rel = r[len(home) + 1:]
            forms |= {"~/" + rel, "$HOME/" + rel, "${HOME}/" + rel}
        for f in forms:  # граница после имени: «…/wshub» не совпадает с «…/wshubx»
            if re.search(re.escape(f) + r"(?![^/\s'\"`;|&()<>=,])", text):
                return r
    return None


def _path_hit(tok: str, cwd: Path, roots: list[str]) -> str | None:
    if not roots:
        return None
    if any(c in tok for c in "*?["):  # шаблон пути: «~/.config/ws?ub/…» раскроет оболочка
        hit = _glob_hit(tok, cwd, roots)
        if hit:
            return hit
    parts = [tok, *(p for p in tok.split("=")[1:] if p)]  # --file=/путь
    for part in parts:
        if "/" not in part and not part.startswith("~") and part not in (".", ".."):
            continue
        s = _expand(part)
        p = s if os.path.isabs(s) else os.path.join(str(cwd), s)
        for cand in (os.path.normpath(p), os.path.realpath(p)):
            for r in roots:
                if cand == r or cand.startswith(r + "/"):
                    return r
    return None


def _glob_hit(tok: str, cwd: Path, roots: list[str]) -> str | None:
    """Шаблон пути, который оболочка может раскрыть внутрь служебного каталога: «~/.config/ws?ub/…»."""
    for part in [tok, *(p for p in tok.split("=")[1:] if p)]:
        s = _expand(part)
        pieces = os.path.normpath(s if os.path.isabs(s) else os.path.join(str(cwd), s)).split("/")
        prefixes = ["/".join(pieces[:n]) for n in range(2, len(pieces) + 1)]
        for r in roots:
            if any(fnmatch.fnmatchcase(r, pre) for pre in prefixes):
                return r
    return None


def _path_reason(root: str) -> str:
    return f"путь внутрь служебного каталога wshub ({root}): реестр и состояние хаба задачам закрыты"


# ---------- точка входа ----------

def main(argv: list[str], stdin=None, stderr=None) -> int:
    """argv — [--profile <профиль>] и служебные каталоги wshub (как их передаёт settings.json). 0 — разрешить,
    2 — отказ (причина в stderr). Любая ошибка — 2."""
    stdin = stdin or sys.stdin
    stderr = stderr or sys.stderr
    try:
        args, profile = list(argv), None
        if args and args[0] == "--profile":
            if len(args) < 2 or not args[1] or args[1].startswith("-"):
                raise ValueError("--profile без значения")
            profile, args = args[1], args[2:]
        event = json.loads(stdin.read())
        if event.get("tool_name") != "Bash":
            return 0
        command = event["tool_input"]["command"]
        if not isinstance(command, str):
            raise TypeError("tool_input.command — не строка")
        reason = check(command, str(event.get("cwd") or "."), [Path(p) for p in args], profile)
    except Exception as e:  # noqa: BLE001 — fail closed
        reason = f"хук не разобрал событие: {e}"
    if reason is None:
        return 0
    print(f"wshub: {reason}", file=stderr)
    return 2


if __name__ == "__main__":  # python -m wshub.hook [--profile P] <каталоги>: без тяжёлых импортов сервера
    sys.exit(main(sys.argv[1:]))

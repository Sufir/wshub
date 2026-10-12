"""wshub code-hook: PreToolUse-хук Claude Code для Bash (ТЗ WSH-2, п. 2.6).

Последний рубеж для команд, которые прошли allow-правила профиля, и единственный — для команд из
sandbox_exclude, которые идут вне песочницы. Отказ, если в любом звене команды есть:

1. git с подкомандой, которая ходит в сеть, меняет remote или конфигурацию, удаляет неотслеживаемые файлы
   (push, fetch, pull, remote, clean, config на запись и т. п.), или с глобальной опцией, подменяющей
   конфигурацию (-c, --config-env, --exec-path=…);
2. сетевой клиент (ssh, scp, rsync, curl, wget, nc …);
3. путь внутрь служебных каталогов wshub (реестр, состояние) — их передаёт settings.json аргументами;
4. оболочка, читающая команды из stdin, или интерпретатор с кодом в строке, где упомянуто запрещённое.

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
CODE_DENY = re.compile(
    r"\bgit\b[^\n;|&]{0,200}?\b(" + "|".join(sorted(GIT_DENY | {"config"})) + r")\b"
    r"|\b(" + "|".join(sorted(NET_TOOLS - {"http", "https"})) + r")\b")


class _Deny(Exception):
    pass


def check(command: str, cwd: str, protected: list[Path]) -> str | None:
    """Причина отказа для Bash-команды или None — разрешено. protected — служебные каталоги wshub (данные)."""
    if not isinstance(command, str):
        return "команда не строка"
    if len(command) > MAX_COMMAND:
        return f"команда длиннее {MAX_COMMAND} символов — разбор ненадёжен"
    roots = _roots(protected)
    try:
        hit = _paths_in_text(command, roots)
        if hit:
            raise _Deny(_path_reason(hit))
        _check_script(command, Path(cwd or "."), roots, 0)
    except _Deny as e:
        return str(e)
    except ValueError as e:  # shlex: незакрытая кавычка и т. п.
        return f"команда не разобрана ({e}) — отказ"
    return None


# ---------- разбор ----------

def _check_script(script: str, cwd: Path, roots: list[str], depth: int) -> None:
    if depth > MAX_DEPTH:
        raise _Deny("слишком глубокая вложенность команд — отказ")
    for seg in _segments(script):
        _check_segment(seg, cwd, roots, depth)


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


def _check_segment(tokens: list[str], cwd: Path, roots: list[str], depth: int) -> None:
    for tok in tokens:  # подстановки внутри строк в кавычках
        for inner in _nested(tok):
            _check_script(inner, cwd, roots, depth + 1)
        hit = _path_hit(tok, cwd, roots)
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
        _check_script(" ".join(tokens[i + 1:]), cwd, roots, depth + 1)

    for j in sorted(candidates):
        n = _base(tokens[j])
        if n in NET_TOOLS:
            raise _Deny(f"сетевой клиент {n} в задачах wshub-code запрещён: сети у задачи нет")
        if n in PRIV:
            raise _Deny(f"{n}: повышение прав в задачах wshub-code запрещено")
        if n in SHELLS:
            _check_shell(tokens, j, cwd, roots, depth)
        if n in INTERPRETERS:
            _check_interpreter(tokens, j)
    for j, tok in enumerate(tokens):  # git в любой позиции: обёртки, которые обёртка не распознала
        if _is_git(tok):
            _check_git(tokens, j)


def _check_shell(tokens: list[str], j: int, cwd: Path, roots: list[str], depth: int) -> None:
    args = tokens[j + 1:]
    k = 0
    while k < len(args) and args[k].startswith("-") and args[k] != "--":
        opt = args[k]
        if not opt.startswith("--") and "c" in opt[1:]:  # -c, -lc, -ec …
            if k + 1 >= len(args):
                raise _Deny("оболочка с -c без команды — отказ")
            _check_script(args[k + 1], cwd, roots, depth + 1)
            return
        k += 1
    if "<<<" in args:
        idx = args.index("<<<")
        if idx + 1 < len(args):
            _check_script(args[idx + 1], cwd, roots, depth + 1)
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


def _check_git(tokens: list[str], j: int) -> None:
    n = _base(tokens[j])
    if n.startswith("git-"):  # прямой вызов исполняемого файла подкоманды: git-push, git-remote-https …
        sub = n[4:]
        if sub in GIT_DENY or sub.startswith("remote") or sub == "config":
            raise _Deny(f"{n}: {_git_reason(sub)}")
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
    if sub in GIT_DENY:
        raise _Deny(f"git {sub}: {_git_reason(sub)}")
    if sub == "config":
        args = set(tokens[k + 1:])
        if args & GIT_CONFIG_WRITE or not args & GIT_CONFIG_READ:
            raise _Deny("git config: в задачах разрешено только чтение (--get, --list)")


def _git_reason(sub: str) -> str:
    if sub == "push":
        return "пушит оператор, в задачах wshub-code пуш запрещён"
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
    """argv — служебные каталоги wshub (как их передаёт settings.json). 0 — разрешить, 2 — отказ (причина
    в stderr). Любая ошибка — 2."""
    stdin = stdin or sys.stdin
    stderr = stderr or sys.stderr
    try:
        event = json.loads(stdin.read())
        if event.get("tool_name") != "Bash":
            return 0
        command = event["tool_input"]["command"]
        if not isinstance(command, str):
            raise TypeError("tool_input.command — не строка")
        reason = check(command, str(event.get("cwd") or "."), [Path(p) for p in argv])
    except Exception as e:  # noqa: BLE001 — fail closed
        reason = f"хук не разобрал событие: {e}"
    if reason is None:
        return 0
    print(f"wshub: {reason}", file=stderr)
    return 2


if __name__ == "__main__":  # python -m wshub.hook <каталоги>: без тяжёлых импортов сервера
    sys.exit(main(sys.argv[1:]))

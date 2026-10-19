"""Помощник хаба для задач Claude Code: `wshub push` и `wshub forge` (ТЗ WSH-3.4, А3), `wshub gate` (ТЗ WSH-3.5, Г3).

Code вызывает его из Bash; помощник идёт вне песочницы (sandbox.excludedCommands) и для git и API хостинга —
единственный путь в сеть. Форму команды держит хук (одно звено, без обёрток), регламент и разрешение задачи —
здесь: задача жива, рабочий каталог помощника — внутри её каталога, разрешение есть в job.json и регламент
проекта в реестре его не закрыл. Токен хостинга читается из файла и не попадает ни в argv, ни в окружение,
ни в вывод, ни в файлы. Гейт — проверка проекта из реестра: argv из реестра без оболочки, окружение — белый список
задачи (env.json) без WSHUB_*, таймаут, полный лог. Каждый вызов — строка в <каталог задачи>/external.jsonl и в
журнале хаба.
"""
from __future__ import annotations

import contextlib
import fnmatch
import json
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

from . import gitsafe
from .jobs import GATE_PID, SANDBOX_MOUNTS, deny_read_hit, gate_group_alive, kill_gate_group, stop_gate_group
from .registry import forge_url
from .runtime import atomic_write_text, proc_start, read_json

EXTERNAL = "external.jsonl"  # внешние действия звена, в каталоге задачи
SSH_SOCK = "ssh_auth_sock"  # SSH_AUTH_SOCK хаба на старте задачи — только для git push помощника
PUSH_TIMEOUT = 120
FORGE_TIMEOUT = 30
OUT_MAX = 20 * 1024  # байт ответа API в stdout; больше — только с --out
BODY_MAX = 32 * 1024 * 1024  # больше ответ не читаем
DATA_MAX = 1024 * 1024  # --data
TOKEN_MAX = 64 * 1024
WRITE_METHODS = ("POST", "PUT", "PATCH")
METHODS = ("GET", *WRITE_METHODS)
# мерж — действие оператора: push-опции с такими именами — отказ при любом регламенте (А3.2)
MERGE_OPT_RE = re.compile(r"auto_merge|merge_when_pipeline_succeeds", re.IGNORECASE)
SECRET_RE = re.compile(r"TOKEN|SECRET|PASSWORD|KEY", re.IGNORECASE)
# ссылка на MR/PR в выводе git push (хостинг печатает её в remote:) и в ответе API
LINK_RE = re.compile(r"https?://[^\s'\"<>]+/(?:merge_requests|pull|pulls)/(?:new\b[^\s'\"<>]*|\d+)")
JOB_RE = re.compile(r"^[0-9]{8}-[0-9]{6}-[a-z2-7]{6}$")
USAGE = {"push": "wshub push <remote> <ветка> [-o ключ=значение]…",
         "forge": "wshub forge <GET|POST|PUT|PATCH> <путь> [--data <json-файл>] [--out <файл>]",
         "gate": "wshub gate <имя> [--out <файл>]"}
# гейт (ТЗ WSH-3.5, Г3.6): полный лог и --out — не больше GATE_LOG_MAX байт; в stdout — хвост GATE_TAIL символов.
# Claude показывает модели около 10000 символов вывода неуспешной команды и около 30000 успешной: хвост с итогом
# виден целиком в обоих случаях
GATE_LOG_MAX = 50 * 1024 * 1024
GATE_TAIL = 8000
GATE_JOIN = 5  # с: ждать чтения вывода после конца гейта (потомок мог унести канал с собой)
GATE_SIGNALS = (signal.SIGTERM, signal.SIGINT, signal.SIGHUP)


class HelperError(Exception):
    pass


def read_token(raw: str) -> str:
    """Токен из файла forge_token (~ раскрывается): обычный файл пользователя, права не шире 0600.
    Нарушение — HelperError с понятным текстом; значение токена в текст не попадает."""
    p = Path(os.path.expanduser(raw))
    try:
        fd = os.open(p, os.O_RDONLY | os.O_NONBLOCK)
    except OSError as e:
        raise HelperError(f"файл токена {p} не открывается: {e.strerror}") from None
    with os.fdopen(fd, "rb") as fh:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise HelperError(f"файл токена {p} — не обычный файл")
        if st.st_uid != os.getuid() or st.st_mode & 0o077:
            raise HelperError(f"файл токена {p}: права {stat.S_IMODE(st.st_mode):04o}, владелец uid {st.st_uid} — "
                              f"нужны 0600 и владелец uid {os.getuid()}; исправь: chmod 600 {p}")
        data = fh.read(TOKEN_MAX + 1)
    token = data.decode("utf-8", "replace").strip()
    if not token or len(data) > TOKEN_MAX or any(c in token for c in "\r\n\x00"):
        raise HelperError(f"файл токена {p}: нужна одна непустая строка")
    return token


def redact(text: str, token: str | None) -> str:
    return text.replace(token, "***") if token else text


# ---------- контекст задачи ----------

class Ctx:
    """Задача, от имени которой вызван помощник: job.json, проект в реестре, рабочий каталог."""

    def __init__(self, hub, jobs, jid: str, d: dict):
        self.hub, self.jobs, self.jid, self.d = hub, jobs, jid, d
        self.dir = Path(d["dir"])
        self.proj = None
        self.glob = None

    def record(self, cmd: str, args: list[str], code: int, link: str | None = None, error: str | None = None,
               extra: dict | None = None) -> None:
        """Строка учёта: external.jsonl звена и журнал хаба. Аргументы — уже без секретов."""
        rec = {"ts": datetime.now().astimezone().isoformat(timespec="seconds"), "cmd": cmd, "args": args,
               "exit": code, **({"link": link} if link else {}), **({"error": error[:500]} if error else {}),
               **(extra or {})}
        try:
            with (self.jobs.job_dir(self.jid) / EXTERNAL).open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except OSError as e:
            print(f"wshub {cmd}: учёт не записан: {e}", file=sys.stderr)
        self.hub._audit_code(f"wshub_{cmd}", self.d.get("project"), self.jid,
                             params={k: v for k, v in rec.items() if k not in ("ts", "cmd", "error")},
                             status="ok" if code == 0 else "error", error=error)


def context(hub, environ: dict, cwd: str) -> Ctx:
    """Задача из WSHUB_JOB: она идёт, и рабочий каталог помощника — внутри её каталога. Иначе — HelperError."""
    from .jobs import JobError, Jobs
    jid = environ.get("WSHUB_JOB") or ""
    if not jid:
        raise HelperError("нет WSHUB_JOB: помощник работает только внутри задачи wshub-code")
    if not JOB_RE.match(jid):
        raise HelperError(f"WSHUB_JOB={jid[:40]!r} — не id задачи")
    jobs = Jobs(hub)
    try:
        d = jobs.read(jid)
    except JobError as e:
        raise HelperError(str(e)) from None
    return Ctx(hub, jobs, jid, d)


def check(ctx: Ctx, cwd: str, kind: str) -> None:
    """Задача идёт, cwd внутри её каталога, разрешение kind (push | forge) есть и регламент его не закрыл; gate —
    профиль task в фазе исполнения (гейты проекта — из перечитанного реестра, ctx.proj)."""
    from .jobs import JobError
    state = ctx.jobs.effective_state(ctx.d)
    if state != "running":
        raise HelperError(f"задача {ctx.jid} не идёт (состояние {state})")
    try:
        here = Path(cwd).resolve()
        top = ctx.dir.resolve()  # симлинк в пути к каталогу задачи — не повод для отказа
    except OSError as e:
        raise HelperError(f"рабочий каталог не определяется: {e}") from None
    if not (here == top or here.is_relative_to(top)):
        raise HelperError(f"рабочий каталог {here} вне каталога задачи {ctx.dir}")
    try:
        _ws, ctx.proj, ctx.glob = ctx.jobs._project(ctx.d["project"])
    except JobError as e:
        raise HelperError(str(e)) from None
    grants = ctx.d.get("grants") or {}
    name = ctx.d["project"]
    if kind == "gate":
        if ctx.d.get("profile") != "task" or ctx.d.get("phase") == "plan":
            raise HelperError("гейт — только задаче профиля task в фазе исполнения: он запускает код рабочего "
                              "каталога вне песочницы")
    elif kind == "push":
        if ctx.proj.push == "no":
            raise HelperError(f"в проекте {name} пуш закрыт реестром (push = no)")
        if ctx.d.get("profile") != "task" or ctx.d.get("phase") == "plan":
            raise HelperError("пуш — только задаче профиля task в фазе исполнения")
        if not grants.get("push"):
            raise HelperError(f"у задачи {ctx.jid} нет разрешения на пуш (code_start/code_resume push=true; "
                              f"регламент проекта push = {ctx.proj.push})")
    else:
        if ctx.proj.forge_access == "no":
            raise HelperError(f"в проекте {name} API хостинга закрыт реестром (forge_access = no)")
        if not grants.get("forge"):
            raise HelperError(f"у задачи {ctx.jid} нет разрешения на API хостинга (code_start/code_resume "
                              f"forge=true; регламент проекта forge_access = {ctx.proj.forge_access})")


def _work_file(ctx: Ctx, cwd: str, val: str, what: str) -> Path:
    """Файл --data/--out: явный путь внутри каталога задачи и не в .git — по normpath и по realpath. Помощник
    идёт вне песочницы, поэтому её запреты повторяются здесь (ревью WSH-3.4): --out — не в .claude/, не в точку
    монтирования песочницы и не по маске deny_edit проекта; --data — не .env* и не по маскам deny_read проекта
    и [code] (относительные — от рабочего каталога)."""
    if not val or any(c in val for c in "$`*?[~\x00"):
        raise HelperError(f"{what}: нужен явный путь к файлу — без переменных, шаблонов и ~")
    base = os.path.normpath(str(ctx.dir))
    p = os.path.normpath(val if os.path.isabs(val) else os.path.join(cwd, val))
    real, real_base = os.path.realpath(p), os.path.realpath(base)
    if not p.startswith(base + "/") or not real.startswith(real_base + "/"):
        raise HelperError(f"{what} {val}: файл — только внутри рабочего каталога задачи")
    rels = [os.path.relpath(q, b) for q, b in ((p, base), (real, real_base))]
    for rel in rels:
        parts = rel.split("/")
        if any(part.lower() == ".git" for part in parts):
            raise HelperError(f"{what} {val}: файлы в .git — нельзя")
        if what == "--data" and parts[-1].startswith(".env"):
            raise HelperError(f"{what} {val}: .env* — нельзя")
        if what == "--data" and _deny_read_hit(ctx, rel, real):
            raise HelperError(f"{what} {val}: путь закрыт deny_read")
        if what == "--out" and (parts[0] == ".claude" or any(rel == m or rel.startswith(m + "/")
                                                             for m in SANDBOX_MOUNTS)):
            raise HelperError(f"{what} {val}: защищённый путь песочницы — помощник туда не пишет")
        if what == "--out" and any(fnmatch.fnmatchcase(rel, m) or fnmatch.fnmatchcase(parts[-1], m)
                                   for m in ctx.proj.deny_edit):
            raise HelperError(f"{what} {val}: путь закрыт deny_edit проекта")
    return Path(p)


def _deny_read_hit(ctx: Ctx, rel: str, real: str) -> bool:
    """Маски deny_read проекта и [code]: абсолютные и ~ — по реальному пути, относительные — от рабочего каталога.
    Сопоставление — jobs.deny_read_hit, та же функция, что считает скрытые песочницей файлы (ТЗ WSH-3.5, П2)."""
    masks = [*(ctx.glob.deny_read if ctx.glob else ()), *(ctx.proj.deny_read if ctx.proj else ())]
    return deny_read_hit(masks, rel, real)


def open_out(top: Path, rel: str) -> int:
    """--out без гонки с симлинком в родителе (ревью WSH-3.4, круг 1): каталоги от рабочего каталога задачи —
    по одному, O_DIRECTORY | O_NOFOLLOW относительно предыдущего; файл — O_NOFOLLOW относительно последнего.
    Подменённый на симлинк каталог — OSError."""
    parts = rel.split("/")
    fd = os.open(os.path.realpath(top), os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in parts[:-1]:
            nfd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = nfd
        return os.open(parts[-1], os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o644, dir_fd=fd)
    finally:
        os.close(fd)


# ---------- wshub push ----------

def _parse_push(args: list[str]) -> tuple[list[str], list[str]]:
    pos, opts = [], []
    i = 0
    while i < len(args):
        a = args[i]
        if a == "-o":
            if i + 1 >= len(args):
                raise HelperError("после -o нужна push-опция ключ=значение")
            opts.append(args[i + 1])
            i += 2
        elif a.startswith("-o") and len(a) > 2:
            opts.append(a[2:])
            i += 1
        elif a.startswith("-"):
            raise HelperError(f"{a}: у wshub push только -o; force, delete, теги, --all и --mirror — никогда")
        else:
            pos.append(a)
            i += 1
    if len(pos) != 2:
        raise HelperError(f"нужно: {USAGE['push']}")
    return pos, opts


def _shown_opt(o: str) -> str:
    """Push-опция для учёта: значение переменной (ci.variable=ИМЯ=значение) и секретоподобные — ***."""
    key, sep, val = o.partition("=")
    if not sep:
        return o
    if "=" in val:
        name, _, _ = val.partition("=")
        return f"{key}={name}=***"
    return f"{key}=***" if SECRET_RE.search(key) else o


def push(ctx: Ctx, args: list[str], environ: dict, cwd: str) -> int:
    shown = list(args)
    try:
        check(ctx, cwd, "push")
        (remote, branch), opts = _parse_push(args)
        shown = [remote, branch, *(x for o in opts for x in ("-o", _shown_opt(o)))]
        if remote.startswith("-") or any(c in remote for c in ":/\\@") or remote in (".", ".."):
            raise HelperError(f"{remote}: нужно имя remote из git remote, а не адрес")
        if ":" in branch:
            raise HelperError(f"{branch}: форма src:dst запрещена — ветка уходит под своим именем")
        if branch.startswith(("-", "+")) or branch.startswith("refs/") or not branch:
            raise HelperError(f"{branch}: нужна локальная ветка по имени (без +, - и refs/)")
        hit = next((p for p in ctx.proj.push_protect if fnmatch.fnmatchcase(branch, p)), None)
        if hit:
            raise HelperError(f"ветка {branch} защищена регламентом проекта (push_protect: {hit}) — пуш в неё "
                              "запрещён")
        bad = [o for o in opts if MERGE_OPT_RE.search(o.partition("=")[0])]
        if bad:
            raise HelperError(f"push-опция {bad[0].partition('=')[0]}: мерж — действие оператора, при любом "
                              "регламенте")
        git = shutil.which("git", path=environ.get("PATH")) or "git"
        r = gitsafe.run(git, ctx.dir, ["remote"], environ, 30)
        if r.returncode != 0:
            raise HelperError(f"git remote вернул {r.returncode}: {r.stderr.strip()[:300]}")
        if remote not in r.stdout.split():
            raise HelperError(f"remote {remote} нет в репозитории (git remote: {', '.join(r.stdout.split()) or '—'})")
        r = gitsafe.run(git, ctx.dir, ["rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"], environ, 30)
        if r.returncode != 0:
            raise HelperError(f"локальной ветки {branch} нет")
    except gitsafe.GitUnsafe as e:
        return _refuse(ctx, "push", shown, str(e))
    except HelperError as e:
        return _refuse(ctx, "push", shown, str(e))
    env = dict(environ)
    env.pop("SSH_AUTH_SOCK", None)
    sock = _ssh_sock(ctx)
    if sock:
        env["SSH_AUTH_SOCK"] = sock
    ref = f"refs/heads/{branch}"
    argv = ["push", "-u", "--no-follow-tags", "--recurse-submodules=no",
            *(x for o in opts for x in ("-o", o)), remote, f"{ref}:{ref}"]
    try:
        r = gitsafe.run(git, ctx.dir, argv, env, PUSH_TIMEOUT, push=True)
    except gitsafe.GitUnsafe as e:
        return _refuse(ctx, "push", shown, str(e))
    except subprocess.TimeoutExpired:
        return _refuse(ctx, "push", shown, f"git push не уложился в {PUSH_TIMEOUT} с")
    except OSError as e:
        return _refuse(ctx, "push", shown, f"git push не запустился: {e}")
    sys.stdout.write(r.stdout)
    sys.stdout.write(r.stderr)  # remote: … и ссылка на MR/PR — в stderr git; всё отдаём в stdout как есть
    sys.stdout.flush()
    m = LINK_RE.search(r.stdout + r.stderr)
    ctx.record("push", shown, r.returncode, m.group(0) if m else None,
               None if r.returncode == 0 else f"git push вернул {r.returncode}")
    return r.returncode


def _ssh_sock(ctx: Ctx) -> str | None:
    try:
        v = (ctx.jobs.job_dir(ctx.jid) / SSH_SOCK).read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return v or None


# ---------- wshub forge ----------

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Перенаправление — отказ: urllib переслал бы заголовок с токеном на другой адрес."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise HelperError(f"API ответил перенаправлением {code} на {newurl[:200]} — помощник не следует "
                          "перенаправлениям; проверь forge_url")


def _parse_forge(args: list[str]) -> tuple[str, str, str | None, str | None]:
    pos, opts = [], {}
    i = 0
    while i < len(args):
        a = args[i]
        name, eq, val = a.partition("=")
        if name in ("--data", "--out"):
            if not eq:
                if i + 1 >= len(args):
                    raise HelperError(f"после {name} нужен путь к файлу")
                val = args[i + 1]
                i += 1
            if name in opts:
                raise HelperError(f"{name} — один раз")
            opts[name] = val
        elif a.startswith("-") and pos:
            raise HelperError(f"{a}: у wshub forge только --data и --out")
        else:
            pos.append(a)
        i += 1
    if len(pos) != 2:
        raise HelperError(f"нужно: {USAGE['forge']}")
    return pos[0], pos[1], opts.get("--data"), opts.get("--out")


def api_path(preset, raw: str) -> tuple[str, str, str]:
    """(путь с префиксом пресета и query — для URL, раскодированный путь в нижнем регистре — для пола и чтения
    секретов, сырой путь с префиксом без query — для списка разрешённой записи).
    Путь — от адреса API: начинается с /, без схемы, //, .., \\, пробелов и фрагмента."""
    if not raw.startswith("/") or raw.startswith("//") or any(c in raw for c in "\\#") \
            or any(c.isspace() or ord(c) < 32 for c in raw):
        raise HelperError(f"путь {raw[:200]!r}: от адреса API, начинается с / — без схемы, //, \\, # и пробелов")
    path, q, query = raw.partition("?")
    decoded = path
    for _ in range(3):  # %252e → %2e → . : раскодируем до неподвижной точки
        nxt = urllib.parse.unquote(decoded)
        if nxt == decoded:
            break
        decoded = nxt
    if urllib.parse.unquote(decoded) != decoded:
        raise HelperError(f"путь {raw[:200]!r}: слишком глубокое кодирование")
    if any(part in (".", "..") for part in decoded.split("/")) or "//" in decoded or "\\" in decoded:
        raise HelperError(f"путь {raw[:200]!r}: без . и .. в пути")
    full = preset.path(path)
    return full + (q + query if q else ""), preset.path(decoded).lower(), full


def forge(ctx: Ctx, args: list[str], environ: dict, cwd: str) -> int:
    from .forges import PRESETS
    shown = list(args)
    token = None
    try:
        check(ctx, cwd, "forge")
        method, raw, data_arg, out_arg = _parse_forge(args)
        method = method.upper()
        shown = [method, raw[:300], *(["--data", data_arg] if data_arg else []),
                 *(["--out", out_arg] if out_arg else [])]
        if method == "DELETE":
            raise HelperError("DELETE через API хостинга — никогда")
        if method not in METHODS:
            raise HelperError(f"метод {method}: одно из {', '.join(METHODS)}")
        proj = ctx.proj
        if method in WRITE_METHODS and not proj.forge_write:
            raise HelperError(f"{method}: запись через API закрыта регламентом проекта (forge_write = false)")
        preset = PRESETS[proj.forge]
        url_path, check_path, raw_full = api_path(preset, raw)
        if method in WRITE_METHODS:
            what = preset.floor_hit(check_path)
            if what:
                raise HelperError(f"{method} {raw[:200]}: {what} — через API запрещено при любом регламенте "
                                  "(мерж и одобрение — оператор, запись в репозиторий — wshub push)")
            if not preset.write_allowed(method, raw_full):
                raise HelperError(f"{method} {raw[:200]}: запись через API — только MR/PR, issues и комментарии")
        else:
            what = preset.secret_hit(check_path)
            if what:
                raise HelperError(f"GET {raw[:200]}: {what} — чтение через API запрещено: ответ уходит в транскрипт")
        body = None
        if data_arg is not None:
            if method not in WRITE_METHODS:
                raise HelperError("--data — только для POST, PUT, PATCH")
            dp = _work_file(ctx, cwd, data_arg, "--data")
            try:
                with dp.open("rb") as fh:
                    body = fh.read(DATA_MAX + 1)
            except OSError as e:
                raise HelperError(f"--data {data_arg}: не читается ({e.strerror})") from None
            if len(body) > DATA_MAX:
                raise HelperError(f"--data {data_arg}: больше 1 МБ")
            try:
                json.loads(body)
            except ValueError as e:
                raise HelperError(f"--data {data_arg}: не JSON ({e})") from None
        out_path = _work_file(ctx, cwd, out_arg, "--out") if out_arg is not None else None
        token = read_token(proj.forge_token)
        base = forge_url(proj)
        req = urllib.request.Request(base + url_path, data=body, method=method,
                                     headers={**preset.headers(token), "User-Agent": "wshub-forge",
                                              **({"Content-Type": "application/json"} if body is not None else {})})
        status, payload = _send(req)
    except HelperError as e:
        return _refuse(ctx, "forge", shown, redact(str(e), token))
    text = redact(payload.decode("utf-8", "replace"), token)
    data = text.encode("utf-8")
    link = _link(text) if method in WRITE_METHODS else None
    extra = {"status": status}
    if status >= 400:
        sys.stderr.write(f"wshub forge: HTTP {status}\n{_clip(text)}\n")
        ctx.record("forge", shown, 1, None, f"HTTP {status}", extra)
        return 1
    if out_path is not None:
        try:
            fd = open_out(ctx.dir, os.path.relpath(out_path, os.path.normpath(str(ctx.dir))))
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
        except OSError as e:
            return _refuse(ctx, "forge", shown, f"--out {out_arg}: не записан ({e.strerror})")
        print(f"HTTP {status}: {len(data)} байт → {out_arg}")
    elif len(data) > OUT_MAX:
        return _refuse(ctx, "forge", shown, f"ответ {len(data)} байт больше 20 КБ — повтори с --out <файл> "
                                            "внутри рабочего каталога")
    else:
        sys.stdout.write(text + ("" if text.endswith("\n") else "\n"))
    ctx.record("forge", shown, 0, link, None, extra)
    return 0


def _send(req) -> tuple[int, bytes]:
    opener = urllib.request.build_opener(_NoRedirect, urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=FORGE_TIMEOUT) as resp:
            return resp.status, _read(resp)
    except urllib.error.HTTPError as e:
        try:
            return e.code, _read(e)
        finally:
            e.close()
    except urllib.error.URLError as e:
        reason = e.reason if isinstance(e.reason, HelperError) else None
        if reason:
            raise reason from None
        raise HelperError(f"API хостинга недоступен: {e.reason}") from None
    except (OSError, TimeoutError) as e:
        raise HelperError(f"API хостинга недоступен: {e}") from None


def _read(resp) -> bytes:
    data = resp.read(BODY_MAX + 1)
    if len(data) > BODY_MAX:
        raise HelperError(f"ответ API больше {BODY_MAX // (1024 * 1024)} МБ")
    return data


def _link(text: str) -> str | None:
    """Ссылка на MR/PR из ответа API (web_url у GitLab, html_url у GitHub) — для учёта и итога."""
    try:
        obj = json.loads(text)
    except ValueError:
        obj = None
    if isinstance(obj, dict):
        for k in ("web_url", "html_url"):
            v = obj.get(k)
            if isinstance(v, str) and LINK_RE.fullmatch(v):
                return v
    return None


def _clip(text: str) -> str:
    b = text.encode("utf-8")
    return text if len(b) <= OUT_MAX else b[:OUT_MAX].decode("utf-8", "ignore") + "…"


# ---------- wshub gate ----------

def _parse_gate(args: list[str]) -> tuple[str, str | None]:
    if not args or args[0].startswith("-"):
        raise HelperError(f"нужно: {USAGE['gate']}")
    rest = args[1:]
    if not rest:
        return args[0], None
    if len(rest) == 1 and rest[0].startswith("--out="):
        return args[0], rest[0].split("=", 1)[1]
    if len(rest) == 2 and rest[0] == "--out":
        return args[0], rest[1]
    raise HelperError(f"после имени гейта — только --out <файл>; нужно: {USAGE['gate']}")


def _gate_env(ctx: Ctx) -> dict:
    """Окружение гейта (Г3.3) — белый список, который хаб дал claude (env.json звена), без WSHUB_*. Окружение самого
    помощника не берём: claude мог добавить в него своё."""
    try:
        env = json.loads((ctx.jobs.job_dir(ctx.jid) / "env.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise HelperError(f"окружение задачи (env.json) не читается: {e}") from None
    if not isinstance(env, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in env.items()):
        raise HelperError("окружение задачи (env.json) — не объект строк")
    return {k: v for k, v in env.items() if not k.startswith("WSHUB_")}


def _gate_argv(ctx: Ctx, g, env: dict) -> list[str]:
    """argv из реестра (Г1): cmd[0] абсолютный — как есть, относительный с / — от рабочего каталога задачи (.. реестр
    не пропускает), голое имя — из PATH окружения гейта."""
    c0 = g.cmd[0]
    if os.path.isabs(c0):
        exe = c0
    elif "/" in c0:
        exe = os.path.join(os.path.normpath(str(ctx.dir)), c0)
    else:
        exe = shutil.which(c0, path=env.get("PATH") or "")
        if not exe:
            raise HelperError(f"гейт {g.name}: {c0} нет в PATH окружения задачи")
    return [exe, *g.cmd[1:]]


def _gate_lock(ctx: Ctx, name: str) -> tuple[int, Path, dict | None]:
    """Один гейт на проект за раз (Г3.2): неблокирующий flock в каталоге состояния хаба; кто держит — в файле рядом
    (.holder: задача, гейт, помощник и группа гейта). Лок взят, а в .holder — живая группа гейта (защита proc_start):
    прежний помощник умер, не погасив гейт, — у сироты нет ни таймаута, ни учёта, её гасим (круг 2, Р1). Ответ —
    (fd лока, путь .holder, {job, gate} погашенной сироты или None)."""
    import fcntl
    d = ctx.hub.state_dir / "code" / "gates"
    proj = ctx.d["project"]
    holder = d / f"{proj}.holder"
    try:
        d.mkdir(parents=True, exist_ok=True)
        fd = os.open(d / f"{proj}.lock", os.O_RDWR | os.O_CREAT, 0o600)
    except OSError as e:
        raise HelperError(f"лок гейтов проекта не открывается: {e}") from None
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        h = read_json(holder)
        raise HelperError(f"у проекта уже идёт гейт {h.get('gate', '?')} задачи {h.get('job', '?')}") from None
    old = read_json(holder)
    orphan = {"job": old.get("job"), "gate": old.get("gate")} if old and kill_gate_group(old) else None
    _write_holder(holder, {"job": ctx.jid, "gate": name, "helper": os.getpid()})
    return fd, holder, orphan


def _write_holder(holder: Path, data: dict) -> None:
    try:
        atomic_write_text(holder, json.dumps(data))
    except OSError:
        pass  # держатель — для текста отказа второму гейту и поиска сироты; лок уже наш


def _gate_log(jdir: Path, name: str) -> tuple[int, str]:
    """Полный лог гейта: <каталог звена>/gates/<NN>-<имя>.log, NN — номер вызова в звене."""
    d = jdir / "gates"
    d.mkdir(exist_ok=True)
    n = sum(1 for p in d.iterdir() if p.suffix == ".log") + 1
    while True:
        rel = f"gates/{n:02d}-{name}.log"
        try:
            return os.open(jdir / rel, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), rel
        except FileExistsError:
            n += 1


class _GateSink:
    """Вывод гейта (Г3.6): в лог и --out — не больше GATE_LOG_MAX байт, дальше только счёт обрезанного; хвост — для
    stdout помощника. Запись и close — под одним локом: потомок в своём сеансе может держать канал дольше GATE_JOIN,
    и после close читатель только дочитывает канал — иначе он писал бы в номер fd, уже отданный другому файлу
    (круг 2, Р4)."""

    def __init__(self, fds: list[int]):
        self.all = list(fds)
        self.fds = list(fds)  # ещё пишутся
        self.written = 0
        self.dropped = 0
        self.tail = bytearray()
        self.lock = threading.Lock()
        self.closed = False

    def pump(self, stream) -> None:
        for chunk in iter(lambda: stream.read1(65536), b""):
            with self.lock:
                if self.closed:
                    continue
                room = max(GATE_LOG_MAX - self.written, 0)
                part = chunk[:room]
                if part:
                    self._write(part)
                    self.written += len(part)
                self.dropped += len(chunk) - len(part)
                self.tail += chunk
                del self.tail[:-4 * GATE_TAIL]  # до 4 байт UTF-8 на символ

    def _write(self, data: bytes) -> None:
        for fd in list(self.fds):
            view = memoryview(data)
            try:
                while view:
                    view = view[os.write(fd, view):]
            except OSError:
                self.fds.remove(fd)  # диск полон и т. п.: гейт идёт дальше, файл остаётся как есть

    def close(self) -> None:
        with self.lock:
            if self.closed:
                return
            self.closed = True
            if self.dropped:
                self._write(f"\n… обрезано: {self.dropped} байт\n".encode())
            for fd in self.all:
                with contextlib.suppress(OSError):
                    os.close(fd)
            self.all = self.fds = []

    def tail_text(self) -> str:
        with self.lock:
            return self.tail.decode("utf-8", "replace")[-GATE_TAIL:]


def gate(ctx: Ctx, args: list[str], environ: dict, cwd: str) -> int:
    """wshub gate <имя> [--out <файл>] (ТЗ WSH-3.5, Г3): гейт проекта из перечитанного реестра — вне песочницы, без
    оболочки, в рабочем каталоге задачи, со своим сеансом, таймаутом, полным логом и строкой учёта."""
    shown = list(args[:3])
    try:
        name, out_arg = _parse_gate(args)
        shown = [name, *(["--out", out_arg] if out_arg is not None else [])]
        check(ctx, cwd, "gate")
        g = next((x for x in ctx.proj.gates if x.name == name), None)
        if g is None:
            have = ", ".join(x.name for x in ctx.proj.gates) or "—"
            raise HelperError(f"гейта {name} нет в реестре проекта (есть: {have})")
        out_path = _work_file(ctx, cwd, out_arg, "--out") if out_arg is not None else None
        env = _gate_env(ctx)
        argv = _gate_argv(ctx, g, env)
        lock, holder, orphan = _gate_lock(ctx, name)
    except HelperError as e:
        return _refuse(ctx, "gate", shown, str(e))
    try:
        if orphan:
            _say(f"погашен осиротевший гейт {orphan['gate']} задачи {orphan['job']}\n")
        return _run_gate(ctx, g, argv, env, out_path, out_arg, shown, holder, orphan)
    finally:
        _write_holder(holder, {"job": ctx.jid, "gate": name, "helper": os.getpid()})  # гейт кончился — группы нет
        os.close(lock)


def _say(text: str) -> None:
    """stdout помощника: канал закрыт (claude вышел, задачу остановили) — не ошибка, код гейта важнее (круг 2, Р2).
    Закрытый stdout подменяем /dev/null: иначе сброс буфера при выходе сменил бы код процесса на 120."""
    try:
        sys.stdout.write(text)
        sys.stdout.flush()
    except OSError:
        with contextlib.suppress(OSError, ValueError, AttributeError):
            devnull = os.open(os.devnull, os.O_WRONLY)
            os.dup2(devnull, sys.stdout.fileno())
            os.close(devnull)


def _job_running(ctx: Ctx) -> bool:
    from .jobs import JobError
    try:
        return ctx.jobs.effective_state(ctx.jobs.read(ctx.jid)) == "running"
    except JobError:
        return False


def _run_gate(ctx: Ctx, g, argv: list[str], env: dict, out_path: Path | None, out_arg: str | None,
              shown: list[str], holder: Path, orphan: dict | None) -> int:
    jdir = ctx.jobs.job_dir(ctx.jid)
    base = {"orphan_killed": orphan} if orphan else {}
    fds: list[int] = []
    try:
        log_fd, log_rel = _gate_log(jdir, g.name)
        fds.append(log_fd)
        if out_path is not None:
            fds.append(open_out(ctx.dir, os.path.relpath(out_path, os.path.normpath(str(ctx.dir)))))
    except OSError as e:
        for fd in fds:
            os.close(fd)
        what = f"--out {out_arg}" if fds else "лог гейта"
        return _refuse(ctx, "gate", shown, f"{what}: не открывается ({e.strerror or e})", base)
    stop: list[int] = []
    old = {s: signal.signal(s, lambda signum, _frame: stop.append(signum)) for s in GATE_SIGNALS}
    sink = _GateSink(fds)
    pidfile = jdir / GATE_PID
    t0 = time.monotonic()
    try:
        try:
            proc = subprocess.Popen(argv, cwd=os.path.normpath(str(ctx.dir)), env=env, stdin=subprocess.DEVNULL,
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True)
        except OSError as e:
            sink.close()
            return _refuse(ctx, "gate", shown, f"гейт {g.name} не запустился: {e}", base)
        pgid = proc.pid  # свой сеанс: группа — по pid лидера
        group = {"pid": proc.pid, "pgid": pgid, "proc_start": proc_start(proc.pid)}
        reader = threading.Thread(target=sink.pump, args=(proc.stdout,), daemon=True)
        reader.start()

        def alive() -> bool:
            return proc.poll() is None or gate_group_alive(pgid)

        try:
            atomic_write_text(pidfile, json.dumps(group))
        except OSError as e:  # без gate.pid остановка задачи гейт не найдёт — гасим и отказываем (круг 2, Р6)
            stop_gate_group(pgid, alive)
            proc.wait()
            reader.join(GATE_JOIN)
            sink.close()
            return _refuse(ctx, "gate", shown, f"gate.pid не записан ({e.strerror or e}) — гейт погашен", base)
        _write_holder(holder, {"job": ctx.jid, "gate": g.name, "helper": os.getpid(), **group})  # для сироты (Р1)
        # отмена пришла между check() и запуском: надзиратель уже искал gate.pid и не нашёл (Р6) — как сигнал
        interrupted = None if _job_running(ctx) else "signal"
        deadline = t0 + g.timeout_s
        while interrupted is None:
            try:
                proc.wait(timeout=0.2)
                break
            except subprocess.TimeoutExpired:
                pass
            if stop:
                interrupted = "signal"
            elif time.monotonic() >= deadline:
                interrupted = "timeout"
        if interrupted:
            stop_gate_group(pgid, alive)  # сигнал во время лестницы итог не меняет; после выхода гейта — тоже (Р10)
            proc.wait()
        if gate_group_alive(pgid):  # лидер вышел, а его фоновые потомки ещё идут — не переживут гейт
            stop_gate_group(pgid)
        reader.join(GATE_JOIN)
        sink.close()
        duration = round(time.monotonic() - t0)
        rc = proc.returncode
        code = 130 if interrupted == "signal" else 124 if interrupted == "timeout" else (128 - rc if rc < 0 else rc)
        why = {"timeout": f"прерван: timeout_s = {g.timeout_s}", "signal": "прерван сигналом"}.get(interrupted)
        # учёт — до вывода: после отмены канал stdout бывает закрыт; обработчики сигналов — до конца записи (Р2)
        ctx.record("gate", shown, code, None, None if code == 0 else why or f"гейт вернул {code}",
                   {"duration_s": duration, "timeout": interrupted == "timeout", "log": log_rel, **base})
    finally:
        for s, h in old.items():
            signal.signal(s, h)
        with contextlib.suppress(OSError):
            pidfile.unlink()
    tail = sink.tail_text()
    _say(tail + ("" if not tail or tail.endswith("\n") else "\n") + f"wshub gate {g.name}: код {code}"
         + (f" · {why}" if why else "") + f" · {duration} с · полный вывод: "
         + (out_arg if out_arg is not None else "--out <файл>") + "\n")
    return code


def _refuse(ctx: Ctx, cmd: str, shown: list[str], reason: str, extra: dict | None = None) -> int:
    print(f"wshub {cmd}: отказ: {reason}", file=sys.stderr)
    ctx.record(cmd, shown, 1, None, reason, extra)
    return 1


# ---------- точка входа ----------

def main(cmd: str, args: list[str], config: Path, state: Path, environ: dict | None = None,
         cwd: str | None = None) -> int:
    """wshub push|forge|gate …: 0 — сделано, иначе отказ, код git или код гейта (причина — в stderr)."""
    from .core import Hub
    environ = dict(os.environ) if environ is None else environ
    cwd = cwd or os.getcwd()
    if args[:1] in (["-h"], ["--help"]):
        print(USAGE[cmd])
        return 0
    hub = Hub(config, state)
    try:
        ctx = context(hub, environ, cwd)
    except HelperError as e:
        print(f"wshub {cmd}: отказ: {e}", file=sys.stderr)
        hub._audit_code(f"wshub_{cmd}", None, environ.get("WSHUB_JOB") or None, status="error", error=str(e))
        return 1
    return {"push": push, "forge": forge, "gate": gate}[cmd](ctx, args, environ, cwd)

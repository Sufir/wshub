"""Помощник хаба для задач Claude Code: `wshub push` и `wshub forge` (ТЗ WSH-3.4, А3).

Code вызывает его из Bash; помощник идёт вне песочницы (sandbox.excludedCommands) и для git и API хостинга —
единственный путь в сеть. Форму команды держит хук (одно звено, без обёрток), регламент и разрешение задачи —
здесь: задача жива, рабочий каталог помощника — внутри её каталога, разрешение есть в job.json и регламент
проекта в реестре его не закрыл. Токен хостинга читается из файла и не попадает ни в argv, ни в окружение,
ни в вывод, ни в файлы. Каждый вызов — строка в <каталог задачи>/external.jsonl и в журнале хаба.
"""
from __future__ import annotations

import fnmatch
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

from . import gitsafe
from .jobs import SANDBOX_MOUNTS
from .registry import forge_url

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
         "forge": "wshub forge <GET|POST|PUT|PATCH> <путь> [--data <json-файл>] [--out <файл>]"}


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
    """Задача идёт, cwd внутри её каталога, разрешение kind (push | forge) есть и регламент его не закрыл."""
    from .jobs import JobError
    state = ctx.jobs.effective_state(ctx.d)
    if state != "running":
        raise HelperError(f"задача {ctx.jid} не идёт (состояние {state})")
    try:
        here = Path(cwd).resolve()
    except OSError as e:
        raise HelperError(f"рабочий каталог не определяется: {e}") from None
    if not (here == ctx.dir or here.is_relative_to(ctx.dir)):
        raise HelperError(f"рабочий каталог {here} вне каталога задачи {ctx.dir}")
    try:
        _ws, ctx.proj, _glob = ctx.jobs._project(ctx.d["project"])
    except JobError as e:
        raise HelperError(str(e)) from None
    grants = ctx.d.get("grants") or {}
    name = ctx.d["project"]
    if kind == "push":
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
    монтирования песочницы и не по маске deny_edit проекта; --data — не .env*."""
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
        if what == "--out" and (parts[0] == ".claude" or any(rel == m or rel.startswith(m + "/")
                                                             for m in SANDBOX_MOUNTS)):
            raise HelperError(f"{what} {val}: защищённый путь песочницы — помощник туда не пишет")
        if what == "--out" and any(fnmatch.fnmatchcase(rel, m) or fnmatch.fnmatchcase(parts[-1], m)
                                   for m in ctx.proj.deny_edit):
            raise HelperError(f"{what} {val}: путь закрыт deny_edit проекта")
    return Path(p)


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


def api_path(preset, raw: str) -> tuple[str, str]:
    """(путь с префиксом пресета и query — для URL, раскодированный путь в нижнем регистре — для пола).
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
    return full + (q + query if q else ""), preset.path(decoded).lower()


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
        url_path, check_path = api_path(preset, raw)
        if method in WRITE_METHODS:
            what = preset.floor_hit(check_path)
            if what:
                raise HelperError(f"{method} {raw[:200]}: {what} — через API запрещено при любом регламенте "
                                  "(мерж и одобрение — оператор, запись в репозиторий — wshub push)")
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
            fd = os.open(out_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o644)
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


def _refuse(ctx: Ctx, cmd: str, shown: list[str], reason: str) -> int:
    print(f"wshub {cmd}: отказ: {reason}", file=sys.stderr)
    ctx.record(cmd, shown, 1, None, reason)
    return 1


# ---------- точка входа ----------

def main(cmd: str, args: list[str], config: Path, state: Path, environ: dict | None = None,
         cwd: str | None = None) -> int:
    """wshub push|forge …: 0 — сделано, иначе отказ или код git (причина — в stderr)."""
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
    return (push if cmd == "push" else forge)(ctx, args, environ, cwd)

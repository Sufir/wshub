"""Ядро wshub: хэндлы, проверка путей, инструменты чтения и записи, копии, журнал."""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .extract import EXTRACTABLE
from .policy import Policy, glob_match
from . import housekeeping, outbox
from .registry import MODES, Limits, Outbox, RegistryError, RegistryFile, Workspace
from .runtime import Runtime, handle_id

HOME = Path.home()
DEFAULT_CONFIG = HOME / ".config/wshub/workspaces.toml"
DEFAULT_STATE = HOME / ".local/state/wshub"

GREP_MAX = 300
GREP_TIMEOUT = 30
GREP_MAX_FILE = 50 * 1024 * 1024
EXTRACT_TIMEOUT = 60
TREE_MAX = 2000
TREE_DEPTH = 4
FIND_MAX = 1000
LS_MAX = 2000
BRIEF_MAX = 16 * 1024
LINE_MAX = 300  # длина строки в выдаче grep
NTFS_ROOT = Path("/mnt")  # под ним «:» в имени — это поток NTFS

REOPEN = "хэндл ws неизвестен или истёк — вызови workspace_open заново"
REVOKED = ("хэндл ws отозван пользователем в панели wshub; не открывай проект заново, "
           "пока пользователь не попросит")
BLOCKED = "проект заблокирован пользователем в панели wshub; не пытайся открыть его снова"
NO_OUTBOX = ("перевалка для publish не настроена (в реестре нет секции [outbox]); настраивает пользователь: "
             "wshub outbox set /mnt/c/Users/<имя>/ClaudeOutbox или поля «Перевалка» в панели wshub. "
             "Пока показать файл карточкой нельзя")
COPY_CHUNK = 1024 * 1024

_UMASK = os.umask(0)
os.umask(_UMASK)


class WsError(Exception):
    pass


@dataclass
class Handle:
    name: str
    mode: str
    root: Path
    expires: float
    hid: str = ""
    opened: float = 0.0


@dataclass
class Session:
    ws: Workspace
    root: Path  # resolve() корня
    mode: str
    policy: Policy
    max_read: int  # байт


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _is_binary(f: Path) -> bool:
    with f.open("rb") as fh:
        return b"\x00" in fh.read(8192)


class _Deadline(Exception):
    """Истёк таймаут обхода. Не TimeoutError: тот — подкласс OSError и смешался бы с ошибками доступа."""


@contextmanager
def _perm(rel: str, what: str = "чтение"):
    """PermissionError → понятная ошибка без трассировки."""
    try:
        yield
    except PermissionError:
        raise WsError(f"нет прав на {what}: {rel}") from None


def _plural(n: int, one: str, few: str, many: str) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return one
    return few if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14 else many


class Skipped:
    """Пути, пропущенные при обходе из-за ошибок доступа: счётчик и до 5 примеров.
    Пути под deny не считаются — их обход пропускает молча."""

    EXAMPLES = 5

    def __init__(self, s: Session):
        self.s, self.n, self.examples = s, 0, []

    def add(self, p, err: OSError | str | None = None) -> None:
        p = Path(p)
        rel = p.relative_to(self.s.root).as_posix() if p.is_relative_to(self.s.root) else str(p)
        if self.s.policy.denied(rel):
            return
        self.n += 1
        if len(self.examples) < self.EXAMPLES:
            if isinstance(err, OSError):
                err = "нет прав" if isinstance(err, PermissionError) else (err.strerror or type(err).__name__)
            self.examples.append(f"{rel} — {err}" if err else rel)

    def note(self) -> str:
        if not self.n:
            return ""
        word = _plural(self.n, "пропущен {} недоступный путь", "пропущено {} недоступных пути",
                       "пропущено {} недоступных путей").format(self.n)
        return f"\n({word}, например: {'; '.join(self.examples)})"


class Hub:
    def __init__(self, config: Path = DEFAULT_CONFIG, state_dir: Path = DEFAULT_STATE, clock=time.time):
        self.registry = RegistryFile(config)
        self.state_dir = Path(state_dir)
        self.backup_dir = self.state_dir / "backup"
        self.audit_path = self.state_dir / "audit.jsonl"
        self.handles: dict[str, Handle] = {}
        self.clock = clock
        self.rg = shutil.which("rg")  # None → питоновский обход
        self.protected = self._protected_paths(Path(config).parent)
        self.runtime = Runtime(self.state_dir, clock=clock)
        self._next_cleanup = 0.0  # раньше этого времени очистку копий не проверяем
        self.mnt_root = outbox.MNT  # тесты подменяют: «диск Windows» во временной папке

    # ---------- самозащита, хэндлы, пути ----------

    def _protected_paths(self, config_dir: Path) -> list[Path]:
        pkg = Path(__file__).resolve().parent
        uv_tools = os.environ.get("UV_TOOL_DIR") or Path(
            os.environ.get("XDG_DATA_HOME") or HOME / ".local/share") / "uv/tools"
        ps = [pkg, config_dir, self.state_dir, DEFAULT_CONFIG.parent, DEFAULT_STATE, Path(uv_tools) / "wshub"]
        if (pkg.parents[1] / "pyproject.toml").is_file():  # editable-установка: корень репозитория
            ps.append(pkg.parents[1])
        if sys.prefix != sys.base_prefix:  # venv, из которого запущен сервер
            ps.append(Path(sys.prefix))
        return [p.resolve() for p in ps]

    def protected_overlap(self, root: Path) -> Path | None:
        """Служебный каталог wshub, с которым пересекается root (внутри или снаружи), или None."""
        for p in self.protected:
            if p.is_relative_to(root) or root.is_relative_to(p):
                return p
        return None

    def handles_info(self) -> list[dict]:
        """Живые хэндлы без токенов — для run-файла и панели."""
        now = self.clock()
        return [{"hid": h.hid, "prefix": t[:4], "name": h.name, "mode": h.mode, "opened": h.opened,
                 "expires": h.expires} for t, h in self.handles.items() if now < h.expires]

    def publish(self) -> None:
        self.runtime.publish(self.handles_info())

    def _session(self, token: str) -> Session:
        h = self.handles.get(token) if isinstance(token, str) else None
        if h is None:
            raise WsError(REOPEN)
        if self.clock() >= h.expires:
            del self.handles[token]
            raise WsError(REOPEN)
        if h.hid in self.runtime.revoked():
            del self.handles[token]
            raise WsError(REVOKED)
        self._check_blocked(h.name, token)
        reg = self.registry.get()
        ws = reg.workspaces.get(h.name)
        # проект убрали из реестра или сменили ему путь — старый хэндл больше не действует
        if ws is None or ws.path.resolve() != h.root:
            del self.handles[token]
            raise WsError(REOPEN)
        mode = "rw" if h.mode == "rw" and ws.mode == "rw" else "ro"
        return Session(ws, h.root, mode, Policy(ws.deny), reg.max_read_kb * 1024)

    def _check_blocked(self, name: str, token: str | None = None) -> None:
        """Запрет из панели действует на все процессы: и на workspace_open, и на уже выданные хэндлы."""
        blocked = self.runtime.blocked()
        if self.runtime.blocked_broken:
            raise WsError(f"файл запретов {self.runtime.blocked_path} повреждён — проекты не открываются, "
                          "пока пользователь его не исправит")
        if name in blocked:
            if token is not None:
                self.handles.pop(token, None)
            raise WsError(BLOCKED)

    def _rel(self, s: Session, p: Path) -> str:
        r = p.relative_to(s.root).as_posix()
        return "." if r == "" else r

    def _check_input(self, s: Session, path: str) -> Path:
        if not isinstance(path, str) or "\x00" in path:
            raise WsError("недопустимый путь (символ NUL)")
        p = Path(path or ".")
        if s.root.is_relative_to(NTFS_ROOT) and any(":" in part for part in p.parts):
            raise WsError("«:» в имени под /mnt запрещён (потоки NTFS)")
        return p if p.is_absolute() else s.root / p

    def _resolve(self, s: Session, path: str) -> tuple[Path, str]:
        """Путь → (resolve(), путь от корня). Отказ, если путь вне корня или попадает под deny
        по запрошенному имени либо по цели симлинка."""
        joined = self._check_input(s, path)
        real = joined.resolve()
        if not real.is_relative_to(s.root):
            raise WsError(f"путь вне проекта: {path}")
        rel = self._rel(s, real)
        lexical = Path(os.path.normpath(joined))
        lex_rel = self._rel(s, lexical) if lexical.is_relative_to(s.root) else lexical.name
        if s.policy.denied(lex_rel) or s.policy.denied(rel):
            raise WsError(f"доступ запрещён политикой deny: {path}")
        return real, rel

    def _resolve_for_write(self, s: Session, path: str) -> tuple[Path, str]:
        if s.mode != "rw":
            raise WsError("проект открыт только для чтения (ro); запись — после workspace_open(name, mode=\"rw\"), "
                          "если реестр это разрешает")
        joined = self._check_input(s, path)
        name = Path(os.path.normpath(joined)).name
        if name in ("", ".", ".."):
            raise WsError(f"нужен путь к файлу: {path}")
        # Родителя разрешаем через resolve(), конечный компонент — нет: симлинк в конце не следуем, а отказываем
        target = Path(os.path.normpath(joined)).parent.resolve() / name
        if not target.is_relative_to(s.root) or target == s.root:
            raise WsError(f"путь вне проекта: {path}")
        rel = self._rel(s, target)
        lex = Path(os.path.normpath(joined))
        lex_rel = self._rel(s, lex) if lex.is_relative_to(s.root) else name
        if s.policy.denied(lex_rel) or s.policy.denied(rel):
            raise WsError(f"доступ запрещён политикой deny: {path}")
        if any(part.lower() == ".git" for part in (rel + "/" + lex_rel).split("/")):
            raise WsError("запись внутрь .git запрещена")
        if target.is_symlink():
            raise WsError(f"конечный компонент пути — симлинк, запись запрещена: {rel}")
        if target.is_dir():
            raise WsError(f"это каталог: {rel}")
        return target, rel

    # ---------- журнал ----------

    @staticmethod
    def _new_rec(tool: str, path: str | None = None) -> dict:
        return {"ts": datetime.now().astimezone().isoformat(timespec="seconds"), "tool": tool, "ws": None, "path": path}

    @contextmanager
    def _audit(self, tool: str, path: str | None = None):
        """Запись журнала о вызове. rec["_logged"] = True — вызов сам записал свои строки (publish: по файлу)."""
        rec = self._new_rec(tool, path)
        try:
            yield rec
        except Exception as e:
            if isinstance(e, PermissionError):  # запасной путь: места, где права не проверены явно
                e = WsError(f"нет прав доступа: {e.filename or path}")
            if not rec.pop("_logged", False):
                rec["status"] = "error"
                rec["error"] = str(e)[:500]
                self._write_audit(rec)
            raise e from None
        else:
            if not rec.pop("_logged", False):
                rec["status"] = "ok"
                self._write_audit(rec)

    def _write_audit(self, rec: dict) -> None:
        self.runtime.last_call = {k: rec.get(k) for k in ("ts", "tool", "ws", "status")}
        self.publish()
        self._append_journal(rec)

    def _append_journal(self, rec: dict) -> None:
        try:
            self.state_dir.mkdir(parents=True, exist_ok=True)
            with self.audit_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            lim = self.limits()
            housekeeping.rotate_if_needed(self.state_dir, lim.journal_max_mb * 1024 * 1024,
                                          lim.journal_keep_files, self.clock())
        except OSError as e:
            print(f"wshub: не удалось записать журнал: {e}", file=sys.stderr)

    def limits(self) -> Limits:
        """[limits] из реестра; реестра нет или в нём ошибка — значения по умолчанию."""
        try:
            return self.registry.get().limits
        except (RegistryError, OSError):
            return Limits()

    def maybe_cleanup(self) -> dict | None:
        """Очистка старых копий: при старте сервера и потом не чаще раза в сутки (на все процессы).
        Что удалено — одной записью в журнале."""
        now = self.clock()
        if now < self._next_cleanup:
            return None
        self._next_cleanup = now + housekeeping.CLEANUP_EVERY
        lim = self.limits()
        try:
            res = housekeeping.cleanup_if_due(self.state_dir, self.backup_dir, lim.backup_keep_days,
                                              lim.backup_keep_per_file, now)
        except OSError as e:
            print(f"wshub: очистка копий не удалась: {e}", file=sys.stderr)
            return None
        if res and res["deleted"]:
            n = len(res["deleted"])
            self._append_journal({
                "ts": datetime.now().astimezone().isoformat(timespec="seconds"), "tool": "backup_cleanup",
                "ws": None, "path": None, "status": "ok", "deleted": n, "freed": res["freed"],
                "files": res["deleted"][:housekeeping.CLEANUP_LIST_MAX],
                "files_truncated": n > housekeeping.CLEANUP_LIST_MAX,
                "rule": f"старше {lim.backup_keep_days} дн. и есть ≥ {lim.backup_keep_per_file} более новых копий"})
        return res

    # ---------- проекты ----------

    def workspaces_list(self) -> str:
        with self._audit("workspaces_list"):
            reg = self.registry.get()
            if not reg.workspaces:
                return "(реестр пуст)"
            blocked = self.runtime.blocked()
            return "\n".join(f"{w.name} [{w.mode}] — {w.description or '(без описания)'}"
                             + (" — заблокирован пользователем, не открывать" if w.name in blocked else "")
                             for w in reg.workspaces.values())

    def panel_data(self) -> dict:
        """Данные для панели: проекты из реестра и число живых хэндлов. Ничего не меняет и в журнал
        не пишется: служебное чтение панели, иначе каждое открытие панели добавляло бы строку."""
        reg = self.registry.get()
        now = self.clock()
        return {
            "workspaces": [{"name": w.name, "mode": w.mode, "path": str(w.path)}
                           for w in reg.workspaces.values()],
            "open_handles": sum(1 for h in self.handles.values() if now < h.expires),
        }

    def workspace_open(self, name: str, mode: str = "ro") -> str:
        with self._audit("workspace_open") as rec:
            rec["ws"] = name
            rec["mode"] = mode
            reg = self.registry.get()
            ws = reg.workspaces.get(name)
            if ws is None:
                raise WsError(f"нет проекта «{name}»; есть: {', '.join(reg.workspaces) or '(пусто)'}")
            self._check_blocked(name)
            if mode not in MODES:
                raise WsError("mode — \"ro\" или \"rw\"")
            if mode == "rw" and ws.mode != "rw":
                raise WsError(f"проект «{name}» в реестре только для чтения (ro); rw недоступен")
            root = ws.path.resolve()
            if not root.is_dir():
                raise WsError(f"папка проекта не найдена: {ws.path}")
            p = self.protected_overlap(root)
            if p is not None:
                raise WsError(f"отказ: проект {root} пересекается со служебным каталогом wshub {p}")
            now = self.clock()
            for t in [t for t, h in self.handles.items() if now >= h.expires]:
                del self.handles[t]
            token = secrets.token_urlsafe(18)
            h = Handle(name, mode, root, now + reg.ttl_hours * 3600, handle_id(token), now)
            self.handles[token] = h
            s = Session(ws, root, mode, Policy(ws.deny), reg.max_read_kb * 1024)
            until = datetime.fromtimestamp(h.expires).strftime("%Y-%m-%d %H:%M")
            write = (f"разрешена (write, edit), кроме .git; копии перед изменением — в {self.backup_dir / name}"
                     if mode == "rw" else "запрещена (режим ro)")
            lines = [
                f"ws: {token}",
                f"проект: {name} ({mode}), корень {root}",
                f"хэндл действует до {until} ({reg.ttl_hours:g} ч); пути — от корня проекта",
                f"запись: {write}",
                f"deny (без учёта регистра; ls/find имена показывают, read/extract/write/edit отказывают, "
                f"grep пропускает): {', '.join(ws.deny) or '(нет)'}",
                f"лимиты: read/extract ≤ {reg.max_read_kb} КБ, grep ≤ {GREP_MAX} совпадений и {GREP_TIMEOUT} с, "
                f"tree ≤ {TREE_MAX} строк и depth ≤ {TREE_DEPTH}, extract ≤ {EXTRACT_TIMEOUT} с",
                "",
                self._brief(s),
            ]
            return "\n".join(lines)

    def _brief(self, s: Session) -> str:
        b = s.ws.brief
        if not b:
            return "BRIEF: в реестре не задан."
        try:
            f, rel = self._resolve(s, b)
            if not f.is_file():
                return f"BRIEF ({b}): файла нет."
            data = f.read_bytes()
        except WsError as e:
            return f"BRIEF ({b}): недоступен — {e}"
        except PermissionError:
            return f"BRIEF ({b}): нет прав на чтение."
        text = data[:BRIEF_MAX].decode("utf-8", "replace")
        note = f"\n(BRIEF обрезан до {BRIEF_MAX // 1024} КБ из {len(data)} байт; остальное — read)" \
            if len(data) > BRIEF_MAX else ""
        return f"BRIEF:\n=== содержимое файла {rel} ===\n{text}\n=== конец файла ==={note}"

    # ---------- чтение ----------

    @staticmethod
    def _scandir(d: Path, skipped: Skipped) -> list[tuple[os.DirEntry, bool, bool]]:
        """Содержимое каталога как (запись, это каталог, это симлинк), каталоги первыми.
        Тип берётся из d_type без stat; запись, тип которой не узнать, уходит в skipped.
        Симлинк считается каталогом, если ведёт на каталог, но по нему никто не спускается."""
        out = []
        with os.scandir(d) as it:
            for e in it:
                try:
                    link = e.is_symlink()
                    if link:
                        try:
                            is_dir = e.is_dir()
                        except OSError:
                            is_dir = False
                    else:
                        is_dir = e.is_dir(follow_symlinks=False)
                except OSError as err:
                    skipped.add(e.path, err)
                    continue
                out.append((e, is_dir, link))
        out.sort(key=lambda t: (not t[1] or t[2], t[0].name.lower()))
        return out

    def ls(self, ws: str, path: str = ".") -> str:
        with self._audit("ls", path) as rec:
            s = self._session(ws)
            rec["ws"] = s.ws.name
            d = self._dir(s, path)
            skipped = Skipped(s)
            with _perm(self._rel(s, d)):
                entries = self._scandir(d, skipped)
            rows = []
            for e, is_dir, link in entries[:LS_MAX]:
                rel = self._rel(s, Path(e.path))
                mark = "  [deny]" if s.policy.denied(rel) else ""
                if link:
                    rows.append(f"l {'':>10} {e.name} -> {self._link_target(s, Path(e.path))}{mark}")
                elif is_dir:
                    rows.append(f"d {'':>10} {e.name}/{mark}")
                else:
                    try:
                        size = e.stat(follow_symlinks=False).st_size
                    except OSError as err:
                        skipped.add(e.path, err)
                        continue
                    rows.append(f"- {size:>10} {e.name}{mark}")
            if len(entries) > LS_MAX:
                rows.append(f"(обрезано: показано {LS_MAX} из {len(entries)}; сузь запрос через find)")
            return ("\n".join(rows) or "(пусто)") + skipped.note()

    def _dir(self, s: Session, path: str) -> Path:
        joined = self._check_input(s, path)
        d = joined.resolve()
        if not d.is_relative_to(s.root):
            raise WsError(f"путь вне проекта: {path}")
        with _perm(self._rel(s, d)):
            if not d.is_dir():
                raise WsError(f"не каталог: {path}")
        return d

    def _link_target(self, s: Session, p: Path) -> str:
        t = p.resolve()
        return self._rel(s, t) if t.is_relative_to(s.root) else "(вне проекта)"

    def _walk(self, s: Session, top: Path, skipped: Skipped, deadline: float | None = None):
        """Обход в глубину без перехода по симлинкам; в .git и запрещённые каталоги не спускаемся.
        Недоступный каталог или запись не обрывают обход, а попадают в skipped.
        Отдаёт (каталог, имена подкаталогов, имена остальных записей)."""
        stack = [top]
        while stack:
            if deadline and time.monotonic() > deadline:
                raise _Deadline
            base = stack.pop()
            try:
                entries = self._scandir(base, skipped)
            except OSError as e:
                skipped.add(base, e)
                continue
            dirs, files, descend = [], [], []
            for e, is_dir, link in sorted(entries, key=lambda t: t[0].name):
                if is_dir:
                    dirs.append(e.name)
                    if not link and e.name.lower() != ".git" and not s.policy.denied(self._rel(s, Path(e.path))):
                        descend.append(Path(e.path))
                else:
                    files.append(e.name)
            yield base, dirs, files
            stack.extend(reversed(descend))

    def tree(self, ws: str, path: str = ".", depth: int = 2) -> str:
        with self._audit("tree", path) as rec:
            s = self._session(ws)
            rec["ws"] = s.ws.name
            top = self._dir(s, path)
            note = ""
            if depth > TREE_DEPTH:
                depth, note = TREE_DEPTH, f"(depth уменьшен до {TREE_DEPTH})"
            depth = max(depth, 1)
            out: list[str] = [f"{self._rel(s, top)}/"]
            truncated = False
            skipped = Skipped(s)

            def walk(d: Path, level: int):
                nonlocal truncated
                try:
                    entries = self._scandir(d, skipped)
                except OSError as e:
                    skipped.add(d, e)
                    return
                for e, is_dir, link in entries:
                    if len(out) >= TREE_MAX:
                        truncated = True
                        return
                    rel = self._rel(s, Path(e.path))
                    denied = s.policy.denied(rel)
                    pad = "  " * level
                    if link:
                        out.append(f"{pad}{e.name} -> {self._link_target(s, Path(e.path))}")
                    elif is_dir:
                        skip = denied or e.name.lower() == ".git"
                        out.append(f"{pad}{e.name}/" + ("  [deny]" if denied else "  [не раскрыт]" if skip else ""))
                        if not skip and level < depth:
                            walk(Path(e.path), level + 1)
                    else:
                        out.append(f"{pad}{e.name}" + ("  [deny]" if denied else ""))

            walk(top, 1)
            if truncated:
                out.append(f"(обрезано на {TREE_MAX} строках: уменьши depth или укажи подпапку в path)")
            if note:
                out.append(note)
            return "\n".join(out) + skipped.note()

    def find(self, ws: str, glob: str, path: str = ".") -> str:
        with self._audit("find", path) as rec:
            s = self._session(ws)
            rec["ws"] = s.ws.name
            rec["glob"] = glob
            top = self._dir(s, path)
            hits, more, timeout = [], False, False
            skipped = Skipped(s)
            try:
                for base, dirs, files in self._walk(s, top, skipped, time.monotonic() + GREP_TIMEOUT):
                    for n, slash in [(d, "/") for d in dirs] + [(f, "") for f in files]:
                        rel = self._rel(s, base / n)
                        if glob_match(glob, rel):
                            if len(hits) >= FIND_MAX:
                                more = True
                                break
                            hits.append(rel + slash + ("  [deny]" if s.policy.denied(rel) else ""))
                    if more:
                        break
            except _Deadline:
                timeout = True
            hits.sort()
            if more:
                hits.append(f"(обрезано на {FIND_MAX}: уточни glob или path)")
            if timeout:
                hits.append(f"(остановлено по таймауту {GREP_TIMEOUT} с, список неполный: сузь path)")
            return ("\n".join(hits) or "(ничего не найдено)") + skipped.note()

    def grep(self, ws: str, pattern: str, path: str = ".", glob: str = "*") -> str:
        with self._audit("grep", path) as rec:
            s = self._session(ws)
            rec["ws"] = s.ws.name
            rec["glob"] = glob
            top, rel = self._resolve(s, path)
            with _perm(rel):
                top.stat()
            try:
                rx = re.compile(pattern)
            except re.error as e:
                raise WsError(f"неверное регулярное выражение: {e}") from None
            skipped = Skipped(s)
            if self.rg:
                hits, stop = self._grep_rg(s, top, pattern, glob, skipped)
            else:
                hits, stop = self._grep_py(s, top, rx, glob, skipped)
            rec["engine"] = "rg" if self.rg else "python"
            rec["size"] = len(hits)
            rec["skipped"] = skipped.n
            out = "\n".join(hits) or "(совпадений нет)"
            if stop == "max":
                out += f"\n(обрезано на {GREP_MAX} совпадениях: сузь path или glob, уточни pattern)"
            elif stop == "timeout":
                out += f"\n(остановлено по таймауту {GREP_TIMEOUT} с, результаты неполные: сузь path или glob)"
            return out + skipped.note()

    def _grep_ok(self, s: Session, f: Path, glob: str) -> str | None:
        """Можно ли показывать совпадения из файла f; возвращает путь от корня или None."""
        real = f.resolve()
        if not real.is_relative_to(s.root):
            return None
        lex = Path(os.path.normpath(f))
        rel_l = self._rel(s, lex) if lex.is_relative_to(s.root) else f.name
        rel = self._rel(s, real)
        if s.policy.denied(rel_l) or s.policy.denied(rel) or not glob_match(glob, rel_l):
            return None
        if any(p.lower() == ".git" for p in rel.split("/")):
            return None
        return rel_l

    @staticmethod
    def _fmt(rel: str, n: int, line: str) -> str:
        line = line.strip()
        if len(line) > LINE_MAX:
            line = line[:LINE_MAX] + "…"
        return f"{rel}:{n}: {line}"

    def _grep_py(self, s: Session, top: Path, rx: re.Pattern, glob: str, skipped: Skipped):
        deadline = time.monotonic() + GREP_TIMEOUT
        hits: list[str] = []
        if top.is_file():
            files = iter([top])
        else:
            files = (b / n for b, _d, fs in self._walk(s, top, skipped, deadline) for n in fs)
        try:
            for f in files:
                try:
                    if (f.is_symlink() and f != top) or not f.is_file():  # как rg: симлинки при обходе не открываем
                        continue
                    rel = self._grep_ok(s, f, glob)
                    if rel is None:
                        continue
                    if f.stat().st_size > GREP_MAX_FILE or _is_binary(f):
                        continue
                    with f.open(encoding="utf-8", errors="replace") as fh:
                        for n, line in enumerate(fh, 1):
                            if n % 2000 == 0 and time.monotonic() > deadline:
                                raise _Deadline
                            if rx.search(line):
                                hits.append(self._fmt(rel, n, line))
                                if len(hits) >= GREP_MAX:
                                    return hits, "max"
                except OSError as e:
                    skipped.add(f, e)
                    continue
                if time.monotonic() > deadline:
                    raise _Deadline
        except _Deadline:
            return hits, "timeout"
        return hits, None

    # rg пишет ошибки в stderr: «rg: <путь>: Permission denied (os error 13)»
    _RG_ERR = re.compile(r"^(?:rg: )?(.+?): ([^:]*\(os error \d+\))$")

    def _grep_rg(self, s: Session, top: Path, pattern: str, glob: str, skipped: Skipped):
        cmd = [self.rg, "--no-config", "--null", "--line-number", "--no-heading", "--with-filename",
               "--color", "never", "--hidden", "--no-ignore", "--max-filesize", "50M",
               "--max-columns", "2000", "--max-columns-preview"]
        if "/" not in glob.strip("/"):
            cmd += ["--iglob", glob]
        cmd += ["--glob", "!.git"]
        for m in s.policy.masks:  # чтобы rg вообще не открывал запрещённые файлы; итог всё равно фильтруем ниже
            cmd += ["--iglob", "!" + m]
        cmd += ["-e", pattern, "--", str(top)]
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL)
        timed_out = threading.Event()

        def kill():
            timed_out.set()
            proc.kill()

        errors: list[tuple[str, str]] = []

        def read_stderr():  # отдельный поток, иначе rg может встать на переполненном stderr
            for raw in proc.stderr:
                m = self._RG_ERR.match(raw.decode("utf-8", "replace").rstrip("\r\n"))
                if m:
                    errors.append((m.group(1), m.group(2)))

        err_reader = threading.Thread(target=read_stderr, daemon=True)
        err_reader.start()
        timer = threading.Timer(GREP_TIMEOUT, kill)
        timer.start()
        hits: list[str] = []
        stop = None
        cache: dict[bytes, str | None] = {}
        try:
            for raw in proc.stdout:
                fname, sep, rest = raw.partition(b"\x00")
                if not sep:
                    continue
                if fname not in cache:
                    cache[fname] = self._grep_ok(s, Path(os.fsdecode(fname)), glob)
                rel = cache[fname]
                if rel is None:
                    continue
                num, _, text = rest.partition(b":")
                try:
                    n = int(num)
                except ValueError:
                    continue
                hits.append(self._fmt(rel, n, text.decode("utf-8", "replace")))
                if len(hits) >= GREP_MAX:
                    stop = "max"
                    break
        finally:
            timer.cancel()
            if proc.poll() is None:
                proc.kill()
            proc.wait()
            err_reader.join(timeout=5)
            proc.stdout.close()
            proc.stderr.close()
        for p, msg in errors:
            skipped.add(p, "нет прав" if "os error 13)" in msg or "os error 1)" in msg else msg)
        if timed_out.is_set() and stop is None:
            stop = "timeout"
        return hits, stop

    def read(self, ws: str, path: str, offset: int = 1, limit: int = 2000) -> str:
        with self._audit("read", path) as rec:
            s = self._session(ws)
            rec["ws"] = s.ws.name
            f, rel = self._resolve(s, path)
            with _perm(rel):
                return self._read(s, f, rel, offset, limit, rec)

    def _read(self, s: Session, f: Path, rel: str, offset: int, limit: int, rec: dict) -> str:
        if not f.is_file():
            raise WsError(f"не файл: {rel}")
        rec["size"] = f.stat().st_size
        if f.suffix.lower() in EXTRACTABLE or _is_binary(f):
            hint = "используй extract" if f.suffix.lower() in EXTRACTABLE else \
                "текст умею извлекать только из PDF, DOCX, XLSX — через extract"
            raise WsError(f"бинарный файл, read его не показывает: {rel}; {hint}")
        offset = max(int(offset), 1)
        limit = int(limit)
        if limit < 1:
            raise WsError("limit должен быть ≥ 1")
        budget = s.max_read
        out, used, last, why = [], 0, offset - 1, None
        with f.open(encoding="utf-8", errors="replace", newline=None) as fh:
            for n, line in enumerate(fh, 1):
                if n < offset:
                    continue
                if n >= offset + limit:
                    why = "limit"
                    break
                row = f"{n}\t{line.rstrip(chr(10))}"
                size = len(row.encode("utf-8")) + 1
                if used + size > budget:
                    if not out:  # одна строка больше лимита — показываем её начало
                        room = max(budget - used - 64, 0)
                        out.append(row.encode("utf-8")[:room].decode("utf-8", "ignore") + " …[строка обрезана]")
                        last = n
                    why = "size"
                    break
                out.append(row)
                used += size
                last = n
        head = f"=== содержимое файла {rel} ==="
        if not out:
            body = "(файл пуст)" if offset == 1 else f"(в файле меньше {offset} строк)"
            return f"{head}\n{body}\n=== конец файла ==="
        tail = "=== конец файла ===" if why is None else "=== конец фрагмента ==="
        res = "\n".join([head, *out, tail])
        if why == "size":
            res += (f"\n(обрезано по лимиту {budget // 1024} КБ на строке {last}; "
                    f"продолжи с offset={last + 1} или уменьши limit)")
        elif why == "limit":
            res += f"\n(показаны строки {offset}–{last}; дальше есть ещё — продолжи с offset={last + 1})"
        return res

    def extract(self, ws: str, path: str) -> str:
        with self._audit("extract", path) as rec:
            s = self._session(ws)
            rec["ws"] = s.ws.name
            f, rel = self._resolve(s, path)
            with _perm(rel):
                if not f.is_file():
                    raise WsError(f"не файл: {path}")
                rec["size"] = f.stat().st_size
                if f.suffix.lower() not in EXTRACTABLE:
                    raise WsError(f"extract поддерживает {', '.join(EXTRACTABLE)}; для текстовых файлов — read")
                with f.open("rb"):  # права проверяем здесь, а не по сообщению дочернего процесса
                    pass
            try:
                p = subprocess.run([sys.executable, "-m", "wshub.extract", str(f), str(s.max_read)],
                                   capture_output=True, timeout=EXTRACT_TIMEOUT, stdin=subprocess.DEVNULL)
            except subprocess.TimeoutExpired:
                raise WsError(f"extract не уложился в {EXTRACT_TIMEOUT} с: {rel}") from None
            try:
                res = json.loads(p.stdout.decode("utf-8"))
            except ValueError:
                raise WsError(f"extract завершился с ошибкой (код {p.returncode})") from None
            if "error" in res:
                raise WsError(f"не удалось извлечь текст из {rel}: {res['error']}")
            out = f"=== содержимое файла {rel} ===\n{res['text']}\n"
            if res["truncated"]:
                out += (f"=== конец фрагмента ===\n(обрезано по лимиту {s.max_read // 1024} КБ {res['truncated']}; "
                        f"полный текст extract не отдаёт — попроси пользователя разбить файл)")
            else:
                out += "=== конец файла ==="
            return out

    # ---------- запись ----------

    def _backup(self, s: Session, f: Path, rel: str) -> Path:
        stamp = datetime.fromtimestamp(self.clock()).strftime("%Y%m%d-%H%M%S")
        dst = self.backup_dir / s.ws.name / stamp / rel
        n = 1
        while dst.exists():  # несколько изменений одного файла за секунду
            dst = dst.with_name(f"{f.name}.{n}")
            n += 1
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(f, dst, follow_symlinks=False)
        return dst

    @staticmethod
    def _atomic_write(target: Path, data: bytes) -> None:
        try:
            mode = stat.S_IMODE(target.stat().st_mode)
        except FileNotFoundError:
            mode = 0o666 & ~_UMASK
        fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=f".{target.name}.", suffix=".wshub-tmp")
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
                fh.flush()
                os.fsync(fh.fileno())
            os.chmod(tmp, mode)
            os.replace(tmp, target)
        except BaseException:
            try:
                os.unlink(tmp)
            except FileNotFoundError:
                pass
            raise

    def _store(self, s: Session, target: Path, rel: str, data: bytes, before: bytes | None, rec: dict) -> str:
        b = self._backup(s, target, rel) if before is not None else None
        target.parent.mkdir(parents=True, exist_ok=True)
        self._atomic_write(target, data)
        rec.update(size=len(data), sha256_before=_sha(before) if before is not None else None,
                   sha256_after=_sha(data), backup=str(b) if b else None)
        if b:
            self.maybe_cleanup()
        return f"; копия: {b}" if b else "; новый файл, копия не нужна"

    def write(self, ws: str, path: str, content: str) -> str:
        with self._audit("write", path) as rec:
            s = self._session(ws)
            rec["ws"] = s.ws.name
            target, rel = self._resolve_for_write(s, path)
            before = target.read_bytes() if target.exists() else None
            data = content.encode("utf-8")
            note = self._store(s, target, rel, data, before, rec)
            return f"записано: {rel} ({len(data)} байт){note}"

    def edit(self, ws: str, path: str, old: str, new: str) -> str:
        with self._audit("edit", path) as rec:
            s = self._session(ws)
            rec["ws"] = s.ws.name
            target, rel = self._resolve_for_write(s, path)
            if not target.is_file():
                raise WsError(f"файла нет: {rel}; для нового файла — write")
            if not old:
                raise WsError("old не может быть пустым")
            before = target.read_bytes()
            try:
                text = before.decode("utf-8")
            except UnicodeDecodeError:
                raise WsError(f"файл не в UTF-8, edit невозможен: {rel}") from None
            n = text.count(old)
            if n != 1:
                raise WsError(f"фрагмент old найден {n} раз, нужен ровно один")
            note = self._store(s, target, rel, text.replace(old, new, 1).encode("utf-8"), before, rec)
            return f"изменено: {rel}{note}"

    # ---------- publish: перевалка для показа файла человеку ----------

    def outbox_config(self) -> tuple[Outbox, Path, str]:
        """Секция [outbox] и её проверенный путь (WSL и Windows). Не настроена или путь не годится — WsError."""
        reg = self.registry.get()
        ob = reg.outbox
        if not ob.present:
            raise WsError(NO_OUTBOX)
        if ob.error:
            raise WsError(f"ошибка в секции [outbox] реестра: {ob.error}; исправляет пользователь "
                          "(wshub outbox set или панель wshub)")
        errs = self.outbox_path_problems(str(ob.path), {w.name: w.path for w in reg.workspaces.values()})
        if errs:
            raise WsError("перевалка [outbox] не годится: " + "; ".join(errs) + "; исправляет пользователь "
                          "(wshub doctor покажет, что не так)")
        return ob, ob.path, outbox.win_path(ob.path, self.mnt_root)

    def outbox_path_problems(self, path, roots: dict[str, Path] | None = None) -> list[str]:
        """Проверка пути перевалки; roots — корни проектов (по умолчанию из реестра)."""
        if roots is None:
            try:
                roots = {w.name: w.path for w in self.registry.get().workspaces.values()}
            except (RegistryError, OSError):
                roots = {}
        return outbox.path_problems(path, roots, self.mnt_root, self.protected_overlap)

    def _outbox_sweep(self, ob: Outbox, root: Path, incoming: int = 0, everything: bool = False) -> dict:
        now = self.clock()
        res = outbox.cleanup(root, ob.ttl_minutes * 60, now, None if everything else ob.max_total_mb * 1024 * 1024,
                             incoming, everything)
        outbox.write_mark(self.state_dir, res, now)
        return res

    def outbox_cleanup(self) -> dict | None:
        """Очистка перевалки по сроку и объёму — при старте сервера. Не настроена — ничего не делает."""
        try:
            ob, root, _win = self.outbox_config()
            return self._outbox_sweep(ob, root)
        except (WsError, RegistryError, OSError):
            return None

    def outbox_clean_all(self, rec: dict) -> list[str]:
        """Удалить все каталоги перевалки wshub независимо от срока (кнопка панели, wshub outbox clean)."""
        ob, root, win = self.outbox_config()
        res = self._outbox_sweep(ob, root, everything=True)
        rec.update(deleted=res["deleted"], freed=res["freed"], busy=res["busy"])
        msg = f"перевалка {win}: удалено каталогов {res['deleted']}, освобождено {res['freed']} байт"
        if res["busy"]:
            msg += f"; заняты и удалятся при следующей очистке: {res['busy']}"
        rec["changes"] = [msg]
        return rec["changes"]

    def _publish_check(self, s: Session, path: str, max_bytes: int) -> tuple[Path, str, int]:
        """Те же проверки, что у read: путь внутри корня, симлинки, deny; плюс обычный файл и размер."""
        f, rel = self._resolve(s, path)
        try:
            with _perm(rel):
                st = f.stat()
        except FileNotFoundError:
            raise WsError(f"файла нет: {rel}") from None
        if not stat.S_ISREG(st.st_mode):
            raise WsError(f"не обычный файл: {rel}")
        if st.st_size > max_bytes:
            raise WsError(f"файл {st.st_size} байт — больше лимита {max_bytes // 1024 // 1024} МБ: {rel}")
        return f, rel, st.st_size

    @staticmethod
    def _stage_copy(src: Path, stage: Path, name: str, max_bytes: int) -> tuple[int, str]:
        """Копия потоком во временное имя .partial и rename; при ошибке .partial удаляется."""
        part = stage / (name + outbox.PARTIAL)
        h, size = hashlib.sha256(), 0
        try:
            fd = os.open(src, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
            with os.fdopen(fd, "rb") as fi, open(part, "xb") as fo:
                if not stat.S_ISREG(os.fstat(fi.fileno()).st_mode):
                    raise WsError("файл подменён на время копирования — повтори")
                while chunk := fi.read(COPY_CHUNK):
                    size += len(chunk)
                    if size > max_bytes:
                        raise WsError(f"файл вырос при копировании больше лимита {max_bytes // 1024 // 1024} МБ")
                    h.update(chunk)
                    fo.write(chunk)
            os.rename(part, stage / name)
        except BaseException:
            try:
                os.unlink(part)
            except OSError:
                pass
            raise
        return size, h.hexdigest()

    def publish_files(self, ws: str, paths: list[str]) -> str:
        with self._audit("publish") as call:
            s = self._session(ws)
            call["ws"] = s.ws.name
            ob, root, win_root = self.outbox_config()
            if isinstance(paths, str):
                paths = [paths]
            if not isinstance(paths, list) or not paths or not all(isinstance(p, str) for p in paths):
                raise WsError("paths — непустой список путей от корня проекта")
            if len(paths) > ob.max_files_per_call:
                raise WsError(f"не больше {ob.max_files_per_call} файлов за вызов, передано {len(paths)} — "
                              "раздели на несколько вызовов")
            max_bytes = ob.max_file_mb * 1024 * 1024
            ok, refused = [], []
            for p in paths:
                try:
                    ok.append((p, *self._publish_check(s, p, max_bytes)))
                    continue
                except WsError as e:
                    msg = str(e)
                except OSError as e:
                    msg = f"не удалось прочитать: {e.strerror or e}"
                refused.append((p, msg))
                self._write_audit({**self._new_rec("publish", p), "ws": s.ws.name, "status": "error", "error": msg[:500]})
            stage = None
            if ok:
                incoming = sum(x[3] for x in ok)
                res = self._outbox_sweep(ob, root, incoming)
                if not res["fits"]:
                    busy = f"; занятых каталогов: {res['busy']}" if res["busy"] else ""
                    raise WsError(f"перевалка переполнена: занято {res['total']} байт, нужно ещё {incoming}, "
                                  f"лимит max_total_mb = {ob.max_total_mb}{busy}; попроси пользователя нажать "
                                  "«Очистить сейчас» в панели wshub или увеличить лимит")
                with _perm(win_root, "запись в перевалку"):
                    stage = outbox.make_stage(root, self.clock())
            call["_logged"] = True  # дальше журнал — по строке на файл; отказы проверки уже записаны
            # имя — как его запросили (у симлинка — имя ссылки, а не цели)
            names = outbox.unique_names([outbox.ntfs_name(Path(os.path.normpath(p)).name or Path(rel).name)
                                         for p, _f, rel, _size in ok])
            lines = []
            for (p, f, rel, _size), name in zip(ok, names):
                rec = {**self._new_rec("publish", rel), "ws": s.ws.name, "outbox_id": stage.name}
                try:
                    with _perm(rel, "чтение или запись копии"):
                        size, sha = self._stage_copy(f, stage, name, max_bytes)
                except (WsError, OSError) as e:
                    msg = str(e) if isinstance(e, WsError) else f"не удалось скопировать: {e.strerror or e}"
                    refused.append((p, msg))
                    self._write_audit({**rec, "status": "error", "error": msg[:500]})
                    continue
                self._write_audit({**rec, "status": "ok", "size": size, "sha256": sha, "name": name})
                lines.append(f"{win_root}\\{stage.name}\\{name} — {size} байт")
            if stage is not None and not lines:
                outbox._remove(stage)
            out = [f"отказ: {p} — {msg}" for p, msg in refused]
            if not lines:
                raise WsError("ни один файл не скопирован:\n" + "\n".join(out))
            head = f"перевалка {win_root}; копии удаляются через {ob.ttl_minutes} мин"
            return "\n".join([head, *lines, *out])

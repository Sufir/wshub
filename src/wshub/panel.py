"""Операции панели wshub (MCP Apps). Все инструменты отсюда — только для приложения (visibility ["app"]).

Защита в два слоя: хост не показывает эти инструменты модели, а сервер дополнительно требует код,
который выдаёт только panel_data (её модель вызвать не может):
- key — для чтения (обход каталогов, журнал, копии), живёт 10 минут с последнего использования;
- nonce — для каждого изменения, одноразовый, живёт 10 минут с выдачи.

Служебные чтения панели в журнал не пишутся: иначе каждое «Обновить» добавляло бы строки.
Изменения и отказы по коду (_mutation) пишутся.
"""
from __future__ import annotations

import difflib
import os
import re
import secrets
import sys
import time
from contextlib import contextmanager
from pathlib import Path

from .core import HOME, Hub, Session, WsError
from .doctor import Ctx
from .doctor import run as run_doctor
from .housekeeping import journal_files, orig_rel, read_journal
from .policy import Policy
from .registry import NAME_RE, RegistryError, parse
from .registry_edit import EditError, RegistryEditor, mask_error, revision
from .runtime import live_processes

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover
    import tomli as tomllib

CODE_TTL = 600
CODES_MAX = 50
AUDIT_PAGE = 100
AUDIT_PAGE_MAX = 500
# «Только изменения»: запись, восстановление, правки реестра, отзывы, очистка копий
# publish сюда не входит: это экспорт копии, как чтение
CHANGE_TOOLS = {"write", "edit", "panel_restore", "panel_save", "panel_delete", "panel_limits", "panel_revoke",
                "panel_block", "panel_unblock", "backup_cleanup", "panel_outbox", "panel_outbox_clean",
                "outbox_set", "outbox_clean"}
AUDIT_TOOLS = ["workspaces_list", "workspace_open", "ls", "tree", "find", "grep", "read", "extract", "publish",
               "write", "edit", "panel_save", "panel_delete", "panel_limits", "panel_outbox", "panel_outbox_clean",
               "panel_revoke", "panel_block", "panel_unblock", "panel_restore", "backup_cleanup", "outbox_set",
               "outbox_clean"]
BROWSE_MAX = 1000
PREVIEW_SAMPLE = 50
PREVIEW_TIMEOUT = 5.0
PREVIEW_MAX = 200_000
DIFF_MAX_FILE = 2 * 1024 * 1024
DIFF_MAX_LINES = 3000
HID_RE = re.compile(r"^[0-9a-f]{16}$")


class Panel:
    def __init__(self, hub: Hub, roots: list[Path] | None = None, doctor_ctx: Ctx | None = None):
        self.hub = hub
        self.roots = [Path(r) for r in (roots if roots is not None else [HOME, Path("/mnt/c")])]
        self.editor = RegistryEditor(hub.registry.path, hub.state_dir / "registry-history", hub.workspace_conflict)
        self.doctor_ctx = doctor_ctx or Ctx(config=hub.registry.path, state=hub.state_dir,
                                            protected_overlap=hub.protected_overlap,
                                            workspace_conflict=hub.workspace_conflict, mnt_root=hub.mnt_root)
        self._nonces: dict[str, float] = {}  # код → срок
        self._keys: dict[str, float] = {}

    # ---------- коды ----------

    def _issue(self, store: dict[str, float]) -> str:
        now = self.hub.clock()
        for k in [k for k, exp in store.items() if exp + CODE_TTL <= now]:  # недавно истёкшие — для понятной ошибки
            del store[k]
        while len(store) >= CODES_MAX:
            del store[min(store, key=store.get)]
        code = secrets.token_urlsafe(18)
        store[code] = now + CODE_TTL
        return code

    def _use_nonce(self, nonce) -> None:
        if not isinstance(nonce, str) or not nonce:
            raise WsError("нужен одноразовый код из панели: изменения делает только человек в панели wshub")
        exp = self._nonces.pop(nonce, None)
        if exp is None:
            raise WsError("код недействителен или уже использован — нажми «Обновить» и повтори")
        if self.hub.clock() >= exp:
            raise WsError("код истёк (действует 10 минут) — нажми «Обновить» и повтори")

    def _use_key(self, key) -> None:
        now = self.hub.clock()
        exp = self._keys.get(key) if isinstance(key, str) else None
        if exp is None or now >= exp:
            self._keys.pop(key, None) if isinstance(key, str) else None
            raise WsError("ключ панели недействителен или истёк — нажми «Обновить»")
        self._keys[key] = now + CODE_TTL

    @contextmanager
    def _mutation(self, tool: str, nonce, path: str | None = None):
        """Журнал + проверка nonce: неудачные попытки тоже попадают в журнал."""
        with self.hub._audit(tool, path) as rec:
            self._use_nonce(nonce)
            yield rec

    # ---------- данные ----------

    def _raw_registry(self) -> tuple[bytes, dict | None, str | None]:
        try:
            data = self.hub.registry.path.read_bytes()
        except FileNotFoundError:
            return b"", {}, None  # первый проект из панели создаст файл
        try:
            parse(data.decode("utf-8"))
            return data, tomllib.loads(data.decode("utf-8")), None
        except (RegistryError, UnicodeDecodeError) as e:
            return data, None, f"ошибка в реестре: {e} — исправь файл {self.hub.registry.path}"

    def data(self) -> dict:
        """Всё для панели и свежие коды. Ничего не меняет, кроме выдачи кодов в памяти процесса."""
        now = self.hub.clock()
        raw, doc, err = self._raw_registry()
        workspaces, defaults = [], {}
        if doc is not None:
            d = doc.get("defaults", {})
            defaults = {"deny": d.get("deny", []), "max_read_kb": d.get("max_read_kb", 512),
                        "ttl_hours": d.get("ttl_hours", 8)}
            for name, w in doc.get("workspace", {}).items():
                p = Path(w["path"])
                brief = w.get("brief") or ""
                workspaces.append({
                    "name": name, "path": w["path"], "mode": w.get("mode", "ro"),
                    "description": w.get("description", ""), "brief": brief,
                    "brief_exists": bool(brief) and (p / brief).is_file(),
                    "deny": list(w.get("deny", [])), "path_ok": p.is_dir()})
        doc_res = run_doctor(self.doctor_ctx)
        revoked = self.hub.runtime.revoked()
        blocked = sorted(({"name": n, "ts": v.get("ts")} for n, v in self.hub.runtime.blocked().items()),
                         key=lambda b: b["name"])
        sessions = []
        for proc in live_processes(self.hub.state_dir, self.doctor_ctx.proc):
            for h in proc.get("handles") or []:
                if h.get("expires", 0) > now:
                    sessions.append({**h, "pid": proc["pid"], "revoked": h.get("hid") in revoked})
        return {
            "generated": now,
            "nonce": self._issue(self._nonces),
            "key": self._issue(self._keys),
            "code_ttl": CODE_TTL,
            "rev": revision(raw),
            "registry_path": str(self.hub.registry.path),
            "registry_error": err,
            "defaults": defaults,
            "workspaces": workspaces,
            "open_handles": sum(1 for h in self.hub.handles.values() if now < h.expires),
            "doctor": doc_res["checks"],
            "processes": doc_res["processes"],
            "sessions": sorted(sessions, key=lambda s: s.get("opened", 0)),
            "blocked": blocked,
            "blocked_error": (f"файл запретов {self.hub.runtime.blocked_path} повреждён — проекты не открываются; "
                              "исправь или удали его вручную") if self.hub.runtime.blocked_broken else None,
            "storage": doc_res["storage"],
            "outbox": doc_res["outbox"],
            "roots": [str(r) for r in self.roots],
            "pid": os.getpid(),
        }

    # ---------- обход каталогов ----------

    def _in_roots(self, path: str) -> Path:
        if not isinstance(path, str) or not path or "\x00" in path:
            raise WsError("путь не задан — выбери корень")
        p = Path(path)
        if not p.is_absolute():
            raise WsError(f"нужен абсолютный путь: {path}")
        real = p.resolve()
        for r in self.roots:
            if r.exists() and real.is_relative_to(r.resolve()):
                return real
        raise WsError(f"{path} вне разрешённых корней ({', '.join(map(str, self.roots))}) — выбери папку внутри них")

    def browse(self, key, path: str = "") -> dict:
        self._use_key(key)
        if not path:
            return {"path": "", "parent": None,
                    "dirs": [{"name": str(r), "path": str(r)} for r in self.roots if r.is_dir()]}
        d = self._in_roots(path)
        if not d.is_dir():
            raise WsError(f"не каталог: {path} — выбери папку")
        dirs = []
        try:
            with os.scandir(d) as it:
                for e in it:
                    try:
                        if e.is_dir():
                            dirs.append(e.name)
                    except OSError:
                        continue
        except PermissionError:
            raise WsError(f"нет прав на чтение каталога {d} — выбери другой") from None
        dirs.sort(key=str.lower)
        parent = None
        if any(d != r.resolve() and d.is_relative_to(r.resolve()) for r in self.roots if r.exists()):
            parent = str(d.parent)
        return {"path": str(d), "parent": parent, "truncated": len(dirs) > BROWSE_MAX,
                "dirs": [{"name": n, "path": str(d / n)} for n in dirs[:BROWSE_MAX]], **self._protected(d)}

    def _protected(self, d: Path) -> dict:
        k = self.hub.protected_kind(d)
        return {"protected": str(k[0]) if k else None, "protected_kind": k[1] if k else None}

    def path_check(self, key, path: str) -> dict:
        """Пересекается ли папка со служебными каталогами wshub — для формы проекта."""
        self._use_key(key)
        root = Path(path) if isinstance(path, str) and path and "\x00" not in path else None
        if root is None or not root.is_absolute() or not root.is_dir():
            return {"protected": None, "protected_kind": None}
        return self._protected(root.resolve())

    def brief_check(self, key, path: str, brief: str) -> dict:
        self._use_key(key)
        if not path or not brief:
            return {"status": "none", "text": "BRIEF не задан"}
        root = Path(path)
        if not root.is_absolute() or not root.is_dir():
            return {"status": "bad", "text": "сначала выбери существующую папку проекта"}
        b = Path(brief)
        if b.is_absolute() or ".." in b.parts:
            return {"status": "bad", "text": "путь — от корня проекта, без «..»"}
        f = (root / b).resolve()
        if not f.is_relative_to(root.resolve()):
            return {"status": "bad", "text": "файл ведёт за пределы проекта (симлинк) — укажи другой"}
        if f.is_file():
            return {"status": "ok", "text": f"файл есть ({f.stat().st_size} байт)"}
        return {"status": "missing", "text": "файла нет — модель получит «файла нет»; создай его или поправь путь"}

    def mask_preview(self, key, path: str, masks: list) -> dict:
        """Какие файлы закрывает каждая маска: число и первые 50 путей. Каталог под маской — одной строкой «dir/»."""
        self._use_key(key)
        root = self._in_roots(path)
        if not root.is_dir():
            raise WsError(f"не каталог: {path} — выбери папку проекта")
        why = self.hub.workspace_conflict(root, "ro")
        if why is not None:
            raise WsError(why)
        if not isinstance(masks, list):
            raise WsError("masks — список масок")
        res = [{"mask": m, "error": mask_error(m), "count": 0, "sample": []} for m in masks]
        active = [i for i, r in enumerate(res) if r["error"] is None]
        pols = {i: Policy((masks[i],)) for i in active}
        deadline = time.monotonic() + PREVIEW_TIMEOUT
        stack, seen, stop = [(root, tuple(active))], 0, None
        while stack and active:
            d, want = stack.pop()
            if time.monotonic() > deadline:
                stop = "timeout"
                break
            try:
                with os.scandir(d) as it:
                    entries = sorted(it, key=lambda e: e.name)
            except OSError:
                continue
            for e in entries:
                seen += 1
                try:
                    is_dir = e.is_dir(follow_symlinks=False)
                except OSError:
                    is_dir = False
                rel = Path(e.path).relative_to(root).as_posix()
                rest = []
                for i in want:
                    if pols[i].denied(rel):
                        r = res[i]
                        r["count"] += 1
                        if len(r["sample"]) < PREVIEW_SAMPLE:
                            r["sample"].append(rel + ("/" if is_dir else ""))
                    else:
                        rest.append(i)
                if is_dir and rest:
                    stack.append((Path(e.path), tuple(rest)))
            if seen > PREVIEW_MAX:
                stop = "limit"
                break
        return {"path": str(root), "masks": res, "scanned": seen, "stopped": stop}

    # ---------- журнал ----------

    def audit(self, key, cursor: str = "", limit: int = AUDIT_PAGE, ws: str = "", tool: str = "",
              only_errors: bool = False, only_changes: bool = False) -> dict:
        """Страница журнала (текущий файл и архивы), новые первыми, с фильтрами. cursor — из прошлой страницы."""
        self._use_key(key)
        try:
            limit = max(1, min(int(limit), AUDIT_PAGE_MAX))
        except (TypeError, ValueError):
            raise WsError("limit — целое число") from None

        def match(r: dict) -> bool:
            return ((not ws or r.get("ws") == ws) and (not tool or r.get("tool") == tool)
                    and (not only_errors or r.get("status") == "error")
                    and (not only_changes or r.get("tool") in CHANGE_TOOLS))
        try:
            recs, nxt = read_journal(self.hub.state_dir, limit, cursor or None, match)
        except ValueError:
            raise WsError("курсор журнала некорректен — нажми «Загрузить заново»") from None
        files = journal_files(self.hub.state_dir)
        total = 0
        for f in files:
            try:
                total += f.stat().st_size
            except OSError:
                pass
        projects = set()
        try:
            projects |= set(self.hub.registry.get().workspaces)
        except RegistryError:
            pass
        return {"records": recs, "cursor": nxt, "total_bytes": total, "files": len(files),
                "tools": AUDIT_TOOLS, "projects": sorted(projects), "change_tools": sorted(CHANGE_TOOLS)}

    # ---------- копии ----------

    def _project_dir(self, project) -> Path:
        if not isinstance(project, str) or not NAME_RE.match(project):
            raise WsError("проект не выбран — выбери проект")
        return self.hub.backup_dir / project

    def _workspace(self, project):
        try:
            return self.hub.registry.get().workspaces.get(project)
        except RegistryError:
            return None

    def _policy(self, project) -> Policy:
        """deny проекта; проекта нет в реестре — defaults.deny; реестр не читается — закрыто всё."""
        ws = self._workspace(project)
        if ws is not None:
            return Policy(ws.deny)
        _raw, doc, err = self._raw_registry()
        if doc is None:
            return Policy(("**",))
        return Policy(tuple(doc.get("defaults", {}).get("deny", [])))

    def backups(self, key, project: str = "") -> dict:
        self._use_key(key)
        projects = set()
        try:
            projects |= set(self.hub.registry.get().workspaces)
        except RegistryError:
            pass
        if self.hub.backup_dir.is_dir():
            projects |= {p.name for p in self.hub.backup_dir.iterdir() if p.is_dir() and NAME_RE.match(p.name)}
        out = {"projects": sorted(projects), "project": project or None, "items": []}
        if not project:
            return out
        base = self._project_dir(project)
        ws = self._workspace(project)
        out["in_registry"] = ws is not None
        pol = self._policy(project)
        if not base.is_dir():
            return out
        for stamp_dir in sorted((p for p in base.iterdir() if p.is_dir()), reverse=True):
            for dirpath, _dirs, files in os.walk(stamp_dir):
                for n in sorted(files):
                    f = Path(dirpath) / n
                    rel = orig_rel(stamp_dir, f)
                    try:
                        st = f.lstat()
                    except OSError:
                        continue
                    out["items"].append({"id": f.relative_to(base).as_posix(), "stamp": stamp_dir.name, "rel": rel,
                                         "size": st.st_size, "mtime": st.st_mtime, "denied": pol.denied(rel)})
        return out

    def _backup_file(self, project, backup_id) -> tuple[Path, str]:
        base = self._project_dir(project)
        if not isinstance(backup_id, str) or not backup_id or "\x00" in backup_id:
            raise WsError("копия не выбрана — выбери версию в списке")
        f = (base / backup_id).resolve()
        if not f.is_relative_to(base.resolve()) or f == base.resolve() or not f.is_file():
            raise WsError("копия не найдена — нажми «Обновить»")
        stamp_dir = base.resolve() / Path(backup_id).parts[0]
        rel = orig_rel(stamp_dir, f)
        if self._policy(project).denied(rel):
            raise WsError(f"{rel} под deny — копия не открывается и не сравнивается")
        return f, rel

    def _current(self, project, rel, mode: str) -> tuple[Session, Path]:
        ws = self._workspace(project)
        if ws is None:
            raise WsError(f"проекта «{project}» нет в реестре — сравнивать и восстанавливать не с чем")
        root = ws.path.resolve()
        if not root.is_dir():
            raise WsError(f"папки проекта нет: {ws.path} — исправь путь на вкладке «Проекты»")
        why = self.hub.workspace_conflict(root, mode)
        if why is not None:
            raise WsError(why)
        s = Session(ws, root, "rw", Policy(ws.deny), self.hub.registry.get().max_read_kb * 1024)
        target, _ = self.hub._resolve_for_write(s, rel)
        return s, target

    def diff(self, key, project: str, backup_id: str) -> dict:
        self._use_key(key)
        f, rel = self._backup_file(project, backup_id)
        _s, cur = self._current(project, rel, "ro")
        exists = cur.is_file()
        if f.stat().st_size > DIFF_MAX_FILE or (exists and cur.stat().st_size > DIFF_MAX_FILE):
            return {"rel": rel, "current_exists": exists, "binary": False,
                    "diff": None, "note": f"файл больше {DIFF_MAX_FILE // 1024 // 1024} МБ — сравнение не показывается"}
        old = f.read_bytes()
        new = cur.read_bytes() if exists else b""
        if b"\x00" in old[:8192] or b"\x00" in new[:8192]:
            return {"rel": rel, "current_exists": exists, "binary": True, "same": old == new,
                    "diff": None, "note": "бинарный файл — построчное сравнение не показывается"}
        lines = list(difflib.unified_diff(
            old.decode("utf-8", "replace").splitlines(), new.decode("utf-8", "replace").splitlines(),
            f"копия {backup_id.split('/')[0]}", "текущая версия" if exists else "(файла нет)", lineterm="", n=3))
        note = None
        if len(lines) > DIFF_MAX_LINES:
            note = f"показаны первые {DIFF_MAX_LINES} строк из {len(lines)}"
            lines = lines[:DIFF_MAX_LINES]
        return {"rel": rel, "current_exists": exists, "binary": False, "same": old == new,
                "diff": "\n".join(lines), "note": note}

    # ---------- изменения ----------

    def restore(self, nonce, project: str, backup_id: str) -> dict:
        with self._mutation("panel_restore", nonce, backup_id) as rec:
            rec["ws"] = project
            f, rel = self._backup_file(project, backup_id)
            rec["path"] = rel
            s, target = self._current(project, rel, "rw")
            data = f.read_bytes()
            before = target.read_bytes() if target.is_file() else None
            if before == data:
                raise WsError("текущая версия совпадает с копией — восстанавливать нечего")
            rec["restored_from"] = str(f)
            note = self.hub._store(s, target, rel, data, before, rec)
            return {"changes": [f"{project}/{rel}: восстановлен из копии {backup_id.split('/')[0]}"
                                f"{note.replace('; ', ', ', 1)}"]}

    def save_workspace(self, nonce, *, name, path, mode, description, brief, deny, rev, create) -> dict:
        with self._mutation("panel_save", nonce) as rec:
            rec["ws"] = name
            rec["create"] = bool(create)
            try:
                changes = self.editor.save(name=name, path=path, mode=mode, description=description,
                                           brief=brief, deny=deny, rev=rev, create=bool(create))
            except EditError as e:
                raise WsError(str(e)) from None
            rec["changes"] = changes
            return {"changes": changes}

    def save_limits(self, nonce, *, values, rev) -> dict:
        with self._mutation("panel_limits", nonce) as rec:
            try:
                changes = self.editor.save_limits(values=values, rev=rev)
            except EditError as e:
                raise WsError(str(e)) from None
            rec["changes"] = changes
            return {"changes": changes}

    def save_outbox(self, nonce, *, values, rev) -> dict:
        with self._mutation("panel_outbox", nonce) as rec:
            try:
                changes = self.editor.save_outbox(values=values, rev=rev, check_path=self.hub.outbox_path_problems)
            except EditError as e:
                raise WsError(str(e)) from None
            rec["changes"] = changes
            return {"changes": changes}

    def outbox_clean(self, nonce) -> dict:
        with self._mutation("panel_outbox_clean", nonce) as rec:
            return {"changes": self.hub.outbox_clean_all(rec)}

    def delete_workspace(self, nonce, *, name, rev) -> dict:
        with self._mutation("panel_delete", nonce) as rec:
            rec["ws"] = name
            try:
                changes = self.editor.delete(name=name, rev=rev)
            except EditError as e:
                raise WsError(str(e)) from None
            return {"changes": changes}

    def revoke(self, nonce, hid: str, block: bool = False) -> dict:
        """Отозвать хэндл; block — ещё и запретить открывать проект (во всех процессах, до снятия запрета)."""
        with self._mutation("panel_revoke", nonce) as rec:
            if not isinstance(hid, str) or not HID_RE.match(hid):
                raise WsError("хэндл не выбран — нажми «Отозвать» в строке хэндла")
            found = None
            for proc in live_processes(self.hub.state_dir, self.doctor_ctx.proc):
                for h in proc.get("handles") or []:
                    if h.get("hid") == hid:
                        found = (proc["pid"], h)
            mine = [t for t, h in self.hub.handles.items() if h.hid == hid]
            if found is None and not mine:
                raise WsError("хэндл не найден: истёк или процесс завершился — нажми «Обновить»")
            name = found[1]["name"] if found else self.hub.handles[mine[0]].name
            rec["ws"] = name
            rec["hid"] = hid
            self.hub.runtime.revoke(hid)
            for t in mine:
                del self.hub.handles[t]
            pid = found[0] if found else os.getpid()
            rec["changes"] = [f"отозван хэндл {(found[1]['prefix'] if found else mine[0][:4])}… "
                              f"проекта {name} (процесс {pid})"]
        changes = list(rec["changes"])
        if block:
            changes += self._block(name)
        return {"changes": changes}

    def _block(self, name: str) -> list[str]:
        """Отдельная запись panel_block: в журнале запрет виден и фильтруется сам по себе.
        Код уже проверен в revoke — второй не нужен."""
        with self.hub._audit("panel_block") as rec:
            rec["ws"] = name
            try:
                changed = self.hub.runtime.block(name)
            except OSError as e:
                raise WsError(f"хэндл отозван, но запрет не записан: {e}") from None
            rec["changes"] = [f"проект {name} заблокирован: workspace_open отказывает до снятия запрета"
                              if changed else f"проект {name} уже был заблокирован"]
            return rec["changes"]

    def unblock(self, nonce, name: str) -> dict:
        with self._mutation("panel_unblock", nonce) as rec:
            if not isinstance(name, str) or not name:
                raise WsError("проект не выбран — нажми «Снять запрет» в строке проекта")
            rec["ws"] = name
            try:
                changed = self.hub.runtime.unblock(name)
            except OSError as e:
                raise WsError(str(e)) from None
            if not changed:
                raise WsError(f"проект {name} не заблокирован — нажми «Обновить»")
            rec["changes"] = [f"с проекта {name} снят запрет: его снова можно открыть"]
            return {"changes": rec["changes"]}

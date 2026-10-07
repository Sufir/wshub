"""Перевалка для publish: папка на диске Windows, куда wshub копирует файлы, чтобы мост Desktop
(device_stage_files) забрал их в чат карточкой. Копия нужна только до этого шага.

Один вызов publish — один каталог <YYYYMMDD-HHMMSS>-<6 символов [a-z0-9]>. wshub трогает и считает только
каталоги с таким именем: чужие файлы в папке остаются. Несколько процессов сервера работают без блокировок:
имена каталогов уникальны, по сроку удаляются только каталоги старше ttl_minutes.
"""
from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import string
from collections.abc import Callable
from datetime import datetime
from functools import lru_cache
from pathlib import Path

from .runtime import atomic_write_text

# где смонтированы диски Windows; WSHUB_MNT — только для тестов и нестандартного automount.root
MNT = Path(os.environ.get("WSHUB_MNT") or "/mnt")
STAGE_RE = re.compile(r"^\d{8}-\d{6}-[a-z0-9]{6}$")
STAGE_ALPHABET = string.ascii_lowercase + string.digits
PARTIAL = ".partial"
NAME_MAX = 120
EXT_MAX = 16  # длиннее — не считаем расширением при обрезке имени
CLEAN_MARK = "outbox-cleanup.json"
CLOUD = ("onedrive", "yandexdisk", "yandex.disk", "dropbox")
RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
_BAD = re.compile(r'[<>:"/\\|?*\x00-\x1f\x7f]')


# ---------- имена NTFS ----------

def _split_ext(name: str) -> tuple[str, str]:
    stem, dot, ext = name.rpartition(".")
    if dot and stem and 0 < len(ext) <= EXT_MAX:
        return stem, "." + ext
    return name, ""


def _fit(name: str, tail: str = "") -> str:
    """Имя не длиннее NAME_MAX с хвостом tail перед расширением; расширение сохраняется."""
    stem, ext = _split_ext(name)
    room = NAME_MAX - len(ext) - len(tail)
    if len(stem) > room:
        stem = stem[:room].rstrip(". ") or "_"
    return stem + tail + ext


def ntfs_name(name: str) -> str:
    """Имя файла, допустимое в Windows: запрещённые и управляющие символы → «_», без точек и пробелов в конце,
    зарезервированные имена (CON, COM1, LPT1… — и с расширением) получают префикс «_», длина ≤ NAME_MAX."""
    n = _BAD.sub("_", name).rstrip(". ") or "_"
    if n.split(".")[0].rstrip(" ").upper() in RESERVED:
        n = "_" + n
    return _fit(n)


def unique_names(names: list[str]) -> list[str]:
    """Совпавшие (без учёта регистра, как в NTFS) имена получают суффикс « (2)», « (3)»…"""
    seen, out = set(), []
    for n in names:
        cand, k = n, 2
        while cand.lower() in seen:
            cand, k = _fit(n, f" ({k})"), k + 1
        seen.add(cand.lower())
        out.append(cand)
    return out


# ---------- путь перевалки ----------

@lru_cache(maxsize=64)
def win_path(path: Path, mnt: Path = MNT) -> str | None:
    """/mnt/c/Users/x → C:\\Users\\x; None — путь не на диске Windows."""
    try:
        parts = Path(path).relative_to(mnt).parts
    except ValueError:
        return None
    if not parts or not re.fullmatch(r"[a-zA-Z]", parts[0]):
        return None
    return f"{parts[0].upper()}:\\" + "\\".join(parts[1:])


def from_windows(path: str) -> str:
    """C:\\Users\\x → /mnt/c/Users/x; остальное — как есть."""
    m = re.fullmatch(r"([a-zA-Z]):[\\/]*(.*)", path.strip())
    if not m:
        return path
    rest = m.group(2).replace("\\", "/").strip("/")
    return f"/mnt/{m.group(1).lower()}" + (f"/{rest}" if rest else "")


def path_problems(path, roots: dict[str, Path], mnt: Path = MNT,
                  protected_overlap: Callable[[Path], Path | None] | None = None) -> list[str]:
    """Почему путь не годится для перевалки; пустой список — годится. roots — корни проектов реестра."""
    if not isinstance(path, str) or not path.strip():
        return ["путь не задан — впиши папку, например /mnt/c/Users/<имя>/ClaudeOutbox"]
    if "\x00" in path or not Path(path).is_absolute():
        return [f"«{path}»: нужен абсолютный путь вида /mnt/c/Users/<имя>/ClaudeOutbox"]
    p = Path(os.path.normpath(path))
    errs = []
    win = win_path(p, mnt)
    if win is None:
        errs.append(f"{p} не на диске Windows: нужен путь вида {mnt}/<буква>/… (drvfs), иначе Desktop его не видит")
    else:
        rel = p.relative_to(mnt).parts
        if len(rel) == 1:
            errs.append(f"{p} — корень диска {win}; создай отдельную папку, например {p}/Users/<имя>/ClaudeOutbox")
        elif rel[1].lower() == "users" and len(rel) <= 3:
            errs.append(f"{p} — домашняя папка Windows или её родитель; создай отдельную папку, "
                        f"например {mnt}/{rel[0]}/Users/<имя>/ClaudeOutbox")
    if not os.path.lexists(p):
        errs.append(f"папки {p} нет — создай её (например, в Проводнике) или исправь путь")
    elif p.is_symlink() or p.resolve() != p:
        errs.append(f"{p}: путь — симлинк или проходит через симлинк; укажи настоящую папку")
    elif not p.is_dir():
        errs.append(f"{p} — не каталог")
    real = p.resolve()
    for name, root in roots.items():
        r = Path(root).resolve()
        if real == r or real.is_relative_to(r):
            errs.append(f"{p} внутри проекта {name} ({r}) — перевалка должна быть вне проектов")
    if protected_overlap is not None:
        bad = protected_overlap(real)
        if bad is not None:
            errs.append(f"{p} пересекается со служебным каталогом wshub {bad}")
    return errs


def cloud_synced(path: Path) -> str | None:
    low = str(path).lower()
    return next((c for c in CLOUD if c in low), None)


# ---------- каталоги перевалки ----------

def new_stage_name(now: float) -> str:
    stamp = datetime.fromtimestamp(now).strftime("%Y%m%d-%H%M%S")
    return f"{stamp}-{''.join(secrets.choice(STAGE_ALPHABET) for _ in range(6))}"


def make_stage(root: Path, now: float) -> Path:
    for _ in range(100):
        d = Path(root) / new_stage_name(now)
        try:
            d.mkdir()
            return d
        except FileExistsError:
            continue
    raise OSError(f"не удалось создать каталог перевалки в {root}")


def _tree_size(d: Path) -> int:
    total = 0
    for base, _dirs, files in os.walk(d):
        for f in files:
            try:
                total += os.lstat(os.path.join(base, f)).st_size
            except OSError:
                pass
    return total


def stage_dirs(root: Path) -> list[dict]:
    """Каталоги перевалки wshub (только с нашим шаблоном имени, не симлинки), старые первыми."""
    out = []
    try:
        with os.scandir(root) as it:
            entries = list(it)
    except OSError:
        return out
    for e in entries:
        if not STAGE_RE.match(e.name):
            continue
        try:
            if not e.is_dir(follow_symlinks=False):
                continue
            mtime = e.stat(follow_symlinks=False).st_mtime
        except OSError:
            continue
        out.append({"name": e.name, "path": Path(e.path), "mtime": mtime, "bytes": _tree_size(Path(e.path))})
    return sorted(out, key=lambda d: (d["mtime"], d["name"]))


def _remove(d: Path) -> bool:
    """Удалить каталог; занятые файлы (Windows их держит) остаются до следующей очистки."""
    failed = []

    def onerror(_fn, path, exc):
        if not isinstance(exc[1], FileNotFoundError):  # другой процесс успел удалить — это не ошибка
            failed.append(path)
    shutil.rmtree(d, onerror=onerror)
    return not failed and not os.path.lexists(d)


def cleanup(root: Path, ttl_s: float, now: float, max_total: int | None = None, incoming: int = 0,
            everything: bool = False) -> dict:
    """Удалить каталоги старше ttl_s (everything — все); затем, если сумма с incoming больше max_total,
    удалять самые старые, пока не уложится. fits — уложилась ли."""
    deleted = freed = busy = 0
    keep = []
    for d in stage_dirs(root):
        if everything or now - d["mtime"] > ttl_s:
            if _remove(d["path"]):
                deleted, freed = deleted + 1, freed + d["bytes"]
                continue
            busy += 1
            d["bytes"] = _tree_size(d["path"])
            d["busy"] = True
        keep.append(d)
    total = sum(d["bytes"] for d in keep)
    if max_total is not None:
        for d in keep:
            if total + incoming <= max_total:
                break
            if d.get("busy"):
                continue
            if _remove(d["path"]):
                deleted, freed, total = deleted + 1, freed + d["bytes"], total - d["bytes"]
            else:
                busy += 1
                left = _tree_size(d["path"])
                freed += d["bytes"] - left
                total -= d["bytes"] - left
    return {"deleted": deleted, "freed": freed, "busy": busy, "total": total,
            "fits": max_total is None or total + incoming <= max_total}


def write_mark(state: Path, res: dict, now: float) -> None:
    try:
        mark = {"ts": now, "deleted": res["deleted"], "freed": res["freed"], "busy": res["busy"]}
        atomic_write_text(Path(state) / CLEAN_MARK, json.dumps(mark))
    except OSError:
        pass


def last_cleanup(state: Path) -> dict | None:
    try:
        d = json.loads((Path(state) / CLEAN_MARK).read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else None
    except (OSError, ValueError):
        return None


def stats(root: Path) -> dict:
    dirs = stage_dirs(root)
    return {"dirs": len(dirs), "bytes": sum(d["bytes"] for d in dirs),
            "oldest": dirs[0]["mtime"] if dirs else None}

"""Журнал и копии: ротация audit.jsonl, чтение журнала через архивы, срок хранения копий.

Журнал — audit.jsonl; когда он больше journal_max_mb, он переименовывается в audit-<YYYYMMDD-HHMMSS>.jsonl,
хранятся последние journal_keep_files архивов. Копия удаляется, только если она старше backup_keep_days
и у того же файла есть не меньше backup_keep_per_file более новых копий; последняя копия не удаляется никогда.
Несколько процессов сервера согласуются через flock на файлах .*.lock в каталоге состояния.
"""
from __future__ import annotations

import json
import os
import re
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

try:
    import fcntl
except ImportError:  # pragma: no cover — не POSIX: без блокировки
    fcntl = None

JOURNAL = "audit.jsonl"
ARCHIVE_RE = re.compile(r"^audit-(\d{8}-\d{6})(?:-(\d+))?\.jsonl$")
STAMP_RE = re.compile(r"^\d{8}-\d{6}$")
CLEANUP_MARK = "backup-cleanup.json"
CLEANUP_EVERY = 24 * 3600
CLEANUP_LIST_MAX = 200  # сколько удалённых копий перечислить в записи журнала
CHUNK = 64 * 1024


@contextmanager
def _lock(state: Path, name: str):
    state.mkdir(parents=True, exist_ok=True)
    with open(state / f".{name}.lock", "a") as fh:
        if fcntl:
            fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            if fcntl:
                fcntl.flock(fh, fcntl.LOCK_UN)


# ---------- журнал ----------

def archives(state: Path) -> list[Path]:
    """Архивы журнала, новые первыми."""
    found = []
    try:
        for p in Path(state).iterdir():
            m = ARCHIVE_RE.match(p.name)
            if m and p.is_file():
                found.append(((m.group(1), int(m.group(2) or 0)), p))
    except FileNotFoundError:
        return []
    return [p for _k, p in sorted(found, reverse=True)]


def journal_files(state: Path) -> list[Path]:
    """Текущий журнал (если есть) и архивы — от новых к старым."""
    cur = Path(state) / JOURNAL
    return ([cur] if cur.is_file() else []) + archives(state)


def journal_stats(state: Path) -> dict:
    def size(p):
        try:
            return p.stat().st_size
        except OSError:
            return 0
    arch = archives(state)
    return {"path": str(Path(state) / JOURNAL), "current_bytes": size(Path(state) / JOURNAL),
            "archives": len(arch), "archives_bytes": sum(size(p) for p in arch)}


def rotate_if_needed(state: Path, max_bytes: int, keep: int, now: float) -> Path | None:
    """Переименовать журнал в архив, если он больше max_bytes; лишние старые архивы удалить."""
    state = Path(state)
    cur = state / JOURNAL
    try:
        if cur.stat().st_size <= max_bytes:
            return None
    except FileNotFoundError:
        return None
    with _lock(state, "audit"):
        try:  # другой процесс мог успеть раньше
            if cur.stat().st_size <= max_bytes:
                return None
        except FileNotFoundError:
            return None
        stamp = datetime.fromtimestamp(now).strftime("%Y%m%d-%H%M%S")
        dst, n = state / f"audit-{stamp}.jsonl", 1
        while dst.exists():
            dst, n = state / f"audit-{stamp}-{n}.jsonl", n + 1
        os.rename(cur, dst)
        for old in archives(state)[keep:]:
            try:
                old.unlink()
            except OSError:
                pass
        return dst


def _lines_backward(path: Path, end: int | None) -> Iterator[tuple[int, bytes]]:
    """Строки файла от конца к началу: (смещение начала строки, строка без \\n). end — читать до этого смещения."""
    with path.open("rb") as fh:
        size = fh.seek(0, 2)
        pos = size if end is None else min(end, size)
        buf = b""  # байты [pos, pos + len(buf))
        while True:
            j = buf.rfind(b"\n", 0, max(len(buf) - 1, 0))  # конец предыдущей строки; \n в конце buf — свой
            if j >= 0:
                yield pos + j + 1, buf[j + 1:].rstrip(b"\n")
                buf = buf[:j + 1]
                continue
            if pos == 0:
                if buf:
                    yield 0, buf.rstrip(b"\n")
                return
            step = min(CHUNK, pos)
            pos -= step
            fh.seek(pos)
            buf = fh.read(step) + buf


def parse_cursor(cursor) -> tuple[int, int] | None:
    if not cursor:
        return None
    m = re.fullmatch(r"(\d+):(\d+)", str(cursor))
    if not m:
        raise ValueError("cursor")
    return int(m.group(1)), int(m.group(2))


def read_journal(state: Path, limit: int, cursor: str | None = None,
                 match: Callable[[dict], bool] | None = None) -> tuple[list[dict], str | None]:
    """Записи от новых к старым через текущий файл и архивы.

    cursor «inode:смещение» — откуда продолжить (его возвращает предыдущий вызов); inode переживает ротацию,
    поэтому «Показать ещё» продолжает с того же места, даже если журнал успел уйти в архив.
    Возвращает записи и курсор следующей страницы (None — дальше записей нет)."""
    files = journal_files(state)
    start = parse_cursor(cursor)
    plan: list[tuple[Path, int, int | None]] = []
    for p in files:
        try:
            ino = p.stat().st_ino
        except OSError:
            continue
        plan.append((p, ino, None))
    if start is not None:
        idx = next((i for i, (_p, ino, _e) in enumerate(plan) if ino == start[0]), None)
        if idx is None:
            return [], None  # файл курсора уже удалён ротацией
        plan = [(plan[idx][0], plan[idx][1], start[1])] + plan[idx + 1:]
    out: list[dict] = []
    last = None
    for p, ino, end in plan:
        try:
            for off, line in _lines_backward(p, end):
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(r, dict) or (match and not match(r)):
                    continue
                if len(out) == limit:
                    return out, f"{last[0]}:{last[1]}"
                out.append(r)
                last = (ino, off)
        except OSError:
            continue
    return out, None


# ---------- копии ----------

def orig_rel(stamp_dir: Path, f: Path) -> str:
    """Путь файла в проекте: копия «x.1» — второе изменение x за ту же секунду."""
    m = re.fullmatch(r"(.+)\.(\d+)", f.name)
    if m and (f.parent / m.group(1)).is_file():
        f = f.with_name(m.group(1))
    return f.relative_to(stamp_dir).as_posix()


def _seq(f: Path, rel: str) -> int:
    """Порядок копий одного файла внутри одной секунды: x → 0, x.1 → 1, …"""
    tail = f.name[len(Path(rel).name):]
    return int(tail[1:]) if tail.startswith(".") and tail[1:].isdigit() else 0


def backup_groups(backup_dir: Path) -> dict[tuple[str, str], list[dict]]:
    """Копии по файлам: (проект, путь) → список копий, новые первыми. Папки не в формате даты не трогаются."""
    groups: dict[tuple[str, str], list[dict]] = {}
    if not Path(backup_dir).is_dir():
        return groups
    for proj in Path(backup_dir).iterdir():
        if not proj.is_dir() or proj.is_symlink():
            continue
        for stamp_dir in proj.iterdir():
            if not STAMP_RE.match(stamp_dir.name) or not stamp_dir.is_dir() or stamp_dir.is_symlink():
                continue
            try:
                ts = datetime.strptime(stamp_dir.name, "%Y%m%d-%H%M%S").timestamp()
            except ValueError:
                continue
            for dirpath, _dirs, files in os.walk(stamp_dir):
                for n in files:
                    f = Path(dirpath) / n
                    rel = orig_rel(stamp_dir, f)
                    try:
                        size = f.lstat().st_size
                    except OSError:
                        continue
                    groups.setdefault((proj.name, rel), []).append(
                        {"path": f, "stamp_dir": stamp_dir, "ts": ts, "seq": _seq(f, rel), "size": size,
                         "id": f"{proj.name}/{f.relative_to(proj).as_posix()}"})
    for g in groups.values():
        g.sort(key=lambda c: (c["ts"], c["seq"]), reverse=True)
    return groups


def expired_backups(backup_dir: Path, keep_days: int, keep_per_file: int, now: float) -> list[dict]:
    """Копии, которые можно удалить: старше keep_days и с keep_per_file более новыми копиями того же файла."""
    out = []
    for g in backup_groups(backup_dir).values():
        for newer, c in enumerate(g):
            if newer >= max(keep_per_file, 1) and now - c["ts"] > keep_days * 86400:
                out.append(c)
    return out


def _prune_empty(stamp_dir: Path) -> None:
    for dirpath, _dirs, _files in os.walk(stamp_dir, topdown=False):
        try:
            os.rmdir(dirpath)  # только пустые
        except OSError:
            pass


def cleanup_backups(backup_dir: Path, keep_days: int, keep_per_file: int, now: float) -> dict:
    deleted, freed, stamps = [], 0, set()
    for c in expired_backups(backup_dir, keep_days, keep_per_file, now):
        try:
            c["path"].unlink()
        except OSError:
            continue
        deleted.append(c["id"])
        freed += c["size"]
        stamps.add(c["stamp_dir"])
    for s in stamps:
        _prune_empty(s)
    return {"deleted": deleted, "freed": freed}


def last_cleanup(state: Path) -> dict | None:
    try:
        d = json.loads((Path(state) / CLEANUP_MARK).read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else None
    except (OSError, ValueError):
        return None


def cleanup_if_due(state: Path, backup_dir: Path, keep_days: int, keep_per_file: int, now: float) -> dict | None:
    """Очистка копий не чаще раза в CLEANUP_EVERY на все процессы. None — ещё рано."""
    state = Path(state)
    with _lock(state, "backup-cleanup"):
        last = last_cleanup(state)
        if last and isinstance(last.get("ts"), (int, float)) and 0 <= now - last["ts"] < CLEANUP_EVERY:
            return None
        res = cleanup_backups(backup_dir, keep_days, keep_per_file, now)
        mark = state / CLEANUP_MARK
        tmp = mark.with_suffix(".tmp")
        tmp.write_text(json.dumps({"ts": now, "deleted": len(res["deleted"]), "freed": res["freed"]}),
                       encoding="utf-8")
        os.replace(tmp, mark)
        return res

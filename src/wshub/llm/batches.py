"""Учёт пакетов API (ТЗ WSH-4.5, п. 2.1.3): ~/.local/state/wshub/api/batches/<id>.json — провайдер, проект, модель,
число запросов, custom_id → краткое описание входа и имя файла ответа, время, статус, куда писать ответы. Ключа в
файле нет. Имена custom_id и файлов ответа — по решению Р4 ТЗ."""
from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path

ID_RE = re.compile(r"[A-Za-z0-9_-]{1,128}")  # id пакета — в имени файла учёта и в пути запроса
CID_MAX = 64  # custom_id: [A-Za-z0-9_-]{1,64} — требование API
BRIEF = 80  # текст входа в учёте — первые 80 символов
FILE_MODE = 0o600


class BatchError(ValueError):
    pass


def batches_dir(state_dir: Path) -> Path:
    return Path(state_dir) / "api" / "batches"


def check_id(batch_id) -> str:
    """ID пакета от пользователя или провайдера — до любого пути: только [A-Za-z0-9_-]."""
    if not isinstance(batch_id, str) or not ID_RE.fullmatch(batch_id):
        raise BatchError(f"неверный id пакета: {str(batch_id)[:80]!r}")
    return batch_id


def path(state_dir: Path, batch_id: str) -> Path:
    return batches_dir(state_dir) / f"{check_id(batch_id)}.json"


def load(state_dir: Path, batch_id: str) -> dict:
    p = path(state_dir, batch_id)
    try:
        rec = json.loads(p.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise BatchError(f"пакета {batch_id} нет в учёте хаба ({p.parent})") from None
    except ValueError:
        rec = None
    # повреждённый вручную учёт — отказ, а не трассировка ниже (ревью, п. 15)
    if not isinstance(rec, dict) or rec.get("id") != batch_id or not isinstance(rec.get("inputs"), dict) \
            or not all(isinstance(rec.get(k), str) for k in ("provider", "project", "model")) \
            or not all(isinstance(v, dict) for v in rec["inputs"].values()):
        raise BatchError(f"учёт пакета {p} повреждён")
    return rec


def save(state_dir: Path, rec: dict) -> None:
    """Атомарно: временный файл 0600 в том же каталоге, затем os.replace."""
    p = path(state_dir, rec["id"])
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=p.parent, prefix=f".{p.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            os.fchmod(fh.fileno(), FILE_MODE)
            json.dump(rec, fh, ensure_ascii=False, indent=1)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, p)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def listing(state_dir: Path) -> tuple[list[dict], int]:
    """Пакеты учёта, новые сверху, и число нечитаемых файлов."""
    d = batches_dir(state_dir)
    recs, bad = [], 0
    for p in sorted(d.glob("*.json")) if d.is_dir() else []:
        try:
            recs.append(load(state_dir, p.stem))
        except (BatchError, OSError):
            bad += 1
    recs.sort(key=lambda r: str(r.get("created", "")), reverse=True)
    return recs, bad


def _unique(base: str, seen: set[str], limit: int) -> str:
    """Без учёта регистра: на нечувствительной к регистру ФС (/mnt) a.md и A.md — один файл."""
    cand, k = base, 2
    while cand.lower() in seen:
        suffix = f"-{k}"
        cand, k = base[:limit - len(suffix)] + suffix, k + 1
    seen.add(cand.lower())
    return cand


def custom_ids(names: list[str]) -> list[str]:
    """custom_id по имени входа: [A-Za-z0-9_-], прочее — «_», до 64 символов; дубли — -2, -3…; без латинских букв и
    цифр — r<номер>."""
    seen: set[str] = set()
    out = []
    for i, n in enumerate(names, 1):
        base = re.sub(r"[^A-Za-z0-9_-]", "_", n)[:CID_MAX]
        if not re.search(r"[A-Za-z0-9]", base):
            base = f"r{i}"
        out.append(_unique(base, seen, CID_MAX))
    return out


def file_names(names: list[str]) -> list[str]:
    """Имена файлов ответов (без расширения) — имена входов как есть, дубли — -2, -3…"""
    seen: set[str] = set()
    return [_unique(n or f"r{i}", seen, 200) for i, n in enumerate(names, 1)]

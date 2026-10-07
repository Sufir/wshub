"""Реестр проектов (workspaces.toml). Сервер его только читает и перечитывает при смене mtime."""
from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from pathlib import Path

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover
    import tomli as tomllib

MODES = ("ro", "rw")
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")  # имя идёт в путь резервных копий
DEFAULT_KEYS = {"deny", "max_read_kb", "ttl_hours"}
WS_KEYS = {"path", "mode", "description", "brief", "deny"}
# [limits]: ключ → (значение по умолчанию, подпись для панели и doctor)
LIMITS = {
    "journal_max_mb": (5, "размер файла журнала, МБ"),
    "journal_keep_files": (5, "архивов журнала хранить"),
    "backup_keep_days": (30, "копии хранить не меньше, дней"),
    "backup_keep_per_file": (10, "копий каждого файла хранить не меньше"),
}
# [outbox] — перевалка для publish: числовой ключ → (значение по умолчанию, подпись); path задаётся отдельно
OUTBOX = {
    "ttl_minutes": (15, "копии в перевалке живут, минут"),
    "max_file_mb": (50, "размер одного файла, МБ"),
    "max_total_mb": (500, "вся перевалка, МБ"),
    "max_files_per_call": (10, "файлов за один вызов publish"),
}
OUTBOX_KEYS = {"path", *OUTBOX}


class RegistryError(Exception):
    pass


@dataclass(frozen=True)
class Workspace:
    name: str
    path: Path
    mode: str
    description: str
    brief: str | None
    deny: tuple[str, ...]  # defaults.deny + собственные маски


@dataclass(frozen=True)
class Limits:
    journal_max_mb: int = LIMITS["journal_max_mb"][0]
    journal_keep_files: int = LIMITS["journal_keep_files"][0]
    backup_keep_days: int = LIMITS["backup_keep_days"][0]
    backup_keep_per_file: int = LIMITS["backup_keep_per_file"][0]


@dataclass(frozen=True)
class Outbox:
    present: bool = False  # секция [outbox] есть в реестре
    path: Path | None = None
    ttl_minutes: int = OUTBOX["ttl_minutes"][0]
    max_file_mb: int = OUTBOX["max_file_mb"][0]
    max_total_mb: int = OUTBOX["max_total_mb"][0]
    max_files_per_call: int = OUTBOX["max_files_per_call"][0]
    # ошибка в секции не ломает реестр: отказывает только publish, doctor и «Обзор» её показывают
    error: str | None = None


@dataclass(frozen=True)
class Registry:
    workspaces: dict[str, Workspace]
    max_read_kb: int
    ttl_hours: float
    limits: Limits = Limits()
    outbox: Outbox = Outbox()
    # неизвестные разделы и ключи: сервер их пропускает, doctor и «Обзор» показывают
    warnings: tuple[str, ...] = ()


def is_limit(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool) and v > 0


def _str_list(v, where: str) -> tuple[str, ...]:
    if not isinstance(v, list) or not all(isinstance(x, str) and x for x in v):
        raise RegistryError(f"{where}: ожидается список непустых строк")
    return tuple(v)


def unknown_key(name: str) -> str:
    return f"неизвестный ключ {name} — пропущен"


def _table(v, where: str) -> dict:
    if not isinstance(v, dict):
        raise RegistryError(f"{where}: ожидается таблица")
    return v


def parse(text: str) -> Registry:
    """Ошибка — только битый TOML и неверные значения известных ключей. Неизвестные разделы и ключи
    (например, из более новой версии wshub) пропускаются с предупреждением в Registry.warnings."""
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        raise RegistryError(f"синтаксис TOML: {e}") from None
    warnings = [unknown_key(k) for k in data if k not in ("defaults", "workspace", "limits", "outbox")]

    d = _table(data.get("defaults", {}), "[defaults]")
    warnings += [unknown_key(f"defaults.{k}") for k in d if k not in DEFAULT_KEYS]
    deny = _str_list(d.get("deny", []), "defaults.deny")
    max_kb = d.get("max_read_kb", 512)
    ttl = d.get("ttl_hours", 8)
    if not isinstance(max_kb, int) or isinstance(max_kb, bool) or max_kb < 1:
        raise RegistryError("defaults.max_read_kb: нужно целое ≥ 1")
    if not isinstance(ttl, (int, float)) or isinstance(ttl, bool) or ttl <= 0:
        raise RegistryError("defaults.ttl_hours: нужно число > 0")

    lim = _table(data.get("limits", {}), "[limits]")
    warnings += [unknown_key(f"limits.{k}") for k in lim if k not in LIMITS]
    lim = {k: v for k, v in lim.items() if k in LIMITS}
    for k, v in lim.items():
        if not is_limit(v):
            raise RegistryError(f"limits.{k}: нужно целое число больше 0")
    limits = Limits(**lim)
    outbox = _outbox(data.get("outbox"), warnings)

    wss: dict[str, Workspace] = {}
    for name, w in _table(data.get("workspace", {}), "[workspace]").items():
        where = f"[workspace.{name}]"
        if not NAME_RE.match(name):
            raise RegistryError(f"{where}: имя — только латиница, цифры, _ и -")
        if not isinstance(w, dict):
            raise RegistryError(f"{where}: ожидается таблица")
        warnings += [unknown_key(f"workspace.{name}.{k}") for k in w if k not in WS_KEYS]
        path = w.get("path")
        if not isinstance(path, str) or not Path(path).is_absolute():
            raise RegistryError(f"{where}: path — абсолютный путь")
        mode = w.get("mode", "ro")
        if mode not in MODES:
            raise RegistryError(f"{where}: mode — \"ro\" или \"rw\"")
        brief = w.get("brief")
        if brief is not None and (not isinstance(brief, str) or not brief):
            raise RegistryError(f"{where}: brief — путь от корня проекта")
        desc = w.get("description", "")
        if not isinstance(desc, str):
            raise RegistryError(f"{where}: description — строка")
        own = _str_list(w.get("deny", []), f"{where} deny")
        wss[name] = Workspace(name, Path(path), mode, desc, brief, deny + own)
    return Registry(wss, max_kb, float(ttl), limits, outbox, tuple(warnings))


def _outbox(t, warnings: list[str]) -> Outbox:
    """[outbox] читается толерантно: неверное значение — Outbox.error, а не ошибка всего реестра."""
    if t is None:
        return Outbox()
    if not isinstance(t, dict):
        return Outbox(present=True, error="[outbox]: ожидается таблица")
    warnings += [unknown_key(f"outbox.{k}") for k in t if k not in OUTBOX_KEYS]
    errs, vals = [], {}
    path = t.get("path")
    if path is None:
        errs.append("outbox.path не задан")
    elif not isinstance(path, str) or not Path(path).is_absolute():
        errs.append("outbox.path — абсолютный путь, например /mnt/c/Users/<имя>/ClaudeOutbox")
    else:
        vals["path"] = Path(path)
    for k in OUTBOX:
        if k in t:
            if is_limit(t[k]):
                vals[k] = t[k]
            else:
                errs.append(f"outbox.{k}: нужно целое число больше 0")
    return Outbox(present=True, error="; ".join(errs) or None, **vals)


class RegistryFile:
    """Читает файл реестра; перечитывает, если изменились mtime/размер. Ошибка в файле — отказ (fail closed)."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._stamp = None
        self._reg: Registry | None = None

    def get(self) -> Registry:
        try:
            st = self.path.stat()
        except FileNotFoundError:
            raise RegistryError(f"реестр не найден: {self.path}") from None
        stamp = (st.st_mtime_ns, st.st_size, st.st_ino)
        if stamp != self._stamp or self._reg is None:
            self._reg = None
            reg = parse(self.path.read_text(encoding="utf-8"))
            self._reg, self._stamp = reg, stamp
        return self._reg

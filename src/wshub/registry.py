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
class Registry:
    workspaces: dict[str, Workspace]
    max_read_kb: int
    ttl_hours: float
    limits: Limits = Limits()


def is_limit(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool) and v > 0


def _str_list(v, where: str) -> tuple[str, ...]:
    if not isinstance(v, list) or not all(isinstance(x, str) and x for x in v):
        raise RegistryError(f"{where}: ожидается список непустых строк")
    return tuple(v)


def parse(text: str) -> Registry:
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        raise RegistryError(f"синтаксис TOML: {e}") from None
    unknown = set(data) - {"defaults", "workspace", "limits"}
    if unknown:
        raise RegistryError(f"неизвестные разделы: {sorted(unknown)}")

    d = data.get("defaults", {})
    if set(d) - DEFAULT_KEYS:
        raise RegistryError(f"[defaults]: неизвестные ключи {sorted(set(d) - DEFAULT_KEYS)}")
    deny = _str_list(d.get("deny", []), "defaults.deny")
    max_kb = d.get("max_read_kb", 512)
    ttl = d.get("ttl_hours", 8)
    if not isinstance(max_kb, int) or isinstance(max_kb, bool) or max_kb < 1:
        raise RegistryError("defaults.max_read_kb: нужно целое ≥ 1")
    if not isinstance(ttl, (int, float)) or isinstance(ttl, bool) or ttl <= 0:
        raise RegistryError("defaults.ttl_hours: нужно число > 0")

    lim = data.get("limits", {})
    if not isinstance(lim, dict):
        raise RegistryError("[limits]: ожидается таблица")
    if set(lim) - set(LIMITS):
        raise RegistryError(f"[limits]: неизвестные ключи {sorted(set(lim) - set(LIMITS))}")
    for k, v in lim.items():
        if not is_limit(v):
            raise RegistryError(f"limits.{k}: нужно целое число больше 0")
    limits = Limits(**lim)

    wss: dict[str, Workspace] = {}
    for name, w in data.get("workspace", {}).items():
        where = f"[workspace.{name}]"
        if not NAME_RE.match(name):
            raise RegistryError(f"{where}: имя — только латиница, цифры, _ и -")
        if not isinstance(w, dict):
            raise RegistryError(f"{where}: ожидается таблица")
        if set(w) - WS_KEYS:
            raise RegistryError(f"{where}: неизвестные ключи {sorted(set(w) - WS_KEYS)}")
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
    return Registry(wss, max_kb, float(ttl), limits)


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

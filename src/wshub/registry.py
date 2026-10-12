"""Реестр проектов (workspaces.toml). Сервер его только читает и перечитывает при смене mtime."""
from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover
    import tomli as tomllib

MODES = ("ro", "rw")
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")  # имя идёт в путь резервных копий
DEFAULT_KEYS = {"deny", "max_read_kb", "ttl_hours"}
WS_KEYS = {"path", "mode", "description", "brief", "deny", "code"}
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
# [code] и [workspace.<имя>.code] — запуск Claude Code (wshub-code); порядок — от слабого к сильному
MODELS = ("haiku", "sonnet", "opus", "fable")
EFFORTS = ("low", "medium", "high", "xhigh", "max")
PROFILES = ("recon", "task", "review")
CODE_DENY_READ = ("~/.ssh", "~/.aws", "~/.gnupg", "~/.config/wshub", "~/.local/state/wshub",
                  "~/.claude/.credentials.json", "~/.claude.json")
CODE_KEYS = {"claude", "max_wait_s", "network", "deny_read", "pass_env"}
WS_CODE_LISTS = ("bash_allow", "sandbox_exclude", "network", "deny_read", "allow_read", "deny_edit",
                 "claude_md_excludes")
WS_CODE_KEYS = {"enabled", "dirs", "prompts", "profiles", "model_min", "model_max", "effort_max", "max_turns",
                "max_budget_usd", "daily_budget_usd", "max_parallel", "max_hours", "prefetch", "status_regex",
                *WS_CODE_LISTS}
ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
SECRET_ENV_RE = re.compile(r"TOKEN|SECRET|PASSWORD|KEY", re.IGNORECASE)


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
class CodeGlobal:
    claude: str = "claude"
    max_wait_s: int = 40
    network: tuple[str, ...] = ()
    deny_read: tuple[str, ...] = CODE_DENY_READ
    pass_env: tuple[str, ...] = ()


@dataclass(frozen=True)
class ProjectCode:
    enabled: bool = False
    dirs: tuple[str, ...] = ()
    prompts: str = ""
    profiles: tuple[str, ...] = PROFILES
    model_min: str = "haiku"
    model_max: str = "opus"
    effort_max: str = "high"
    max_turns: int = 100
    max_budget_usd: float = 10
    daily_budget_usd: float = 30
    max_parallel: int = 1
    max_hours: float = 4
    prefetch: bool = False
    status_regex: str | None = None
    bash_allow: tuple[str, ...] = ()
    sandbox_exclude: tuple[str, ...] = ()
    network: tuple[str, ...] = ()
    deny_read: tuple[str, ...] = ()
    allow_read: tuple[str, ...] = ()  # sandbox.filesystem.allowRead сверх рабочего каталога и настроек git
    deny_edit: tuple[str, ...] = ()
    claude_md_excludes: tuple[str, ...] = ()


@dataclass(frozen=True)
class Code:
    """[code] и [workspace.<имя>.code]. Ошибка в них не ломает реестр: отказывает только wshub-code
    (error — для всех проектов, project_errors — для одного), doctor их показывает."""
    present: bool = False  # секция [code] есть в реестре
    glob: CodeGlobal = CodeGlobal()
    error: str | None = None
    projects: dict[str, ProjectCode] = field(default_factory=dict)
    project_errors: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class Registry:
    workspaces: dict[str, Workspace]
    max_read_kb: int
    ttl_hours: float
    limits: Limits = Limits()
    outbox: Outbox = Outbox()
    # неизвестные разделы и ключи: сервер их пропускает, doctor и «Обзор» показывают
    warnings: tuple[str, ...] = ()
    code: Code = field(default_factory=Code)


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
    warnings = [unknown_key(k) for k in data if k not in ("defaults", "workspace", "limits", "outbox", "code")]

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
    code_projects: dict[str, ProjectCode] = {}
    code_errors: dict[str, str] = {}
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
        if "code" in w:
            try:
                code_projects[name] = _ws_code(name, w["code"], warnings)
            except RegistryError as e:
                code_errors[name] = str(e)
    code_glob, code_error = CodeGlobal(), None
    try:
        code_glob = _code(data.get("code", {}), warnings)
    except RegistryError as e:
        code_error = str(e)
    code = Code("code" in data, code_glob, code_error, code_projects, code_errors)
    return Registry(wss, max_kb, float(ttl), limits, outbox, tuple(warnings), code)


def _number(v, where: str, integer: bool = False):
    ok = isinstance(v, int) if integer else isinstance(v, (int, float))
    if not ok or isinstance(v, bool) or v <= 0:
        raise RegistryError(f"{where}: нужно {'целое ' if integer else ''}число больше 0")
    return v


def _rel_path(v, where: str) -> str:
    if not isinstance(v, str) or not v or Path(v).is_absolute() or "\x00" in v:
        raise RegistryError(f"{where}: путь от корня проекта, например \"org\" или \".\"")
    return v


def _code(t, warnings: list[str]) -> CodeGlobal:
    """[code]: общие настройки запуска Claude Code."""
    t = _table(t, "[code]")
    warnings += [unknown_key(f"code.{k}") for k in t if k not in CODE_KEYS]
    vals: dict = {}
    if "claude" in t:
        if not isinstance(t["claude"], str) or not t["claude"].strip():
            raise RegistryError("code.claude: имя в PATH или абсолютный путь")
        vals["claude"] = t["claude"]
    if "max_wait_s" in t:
        vals["max_wait_s"] = _number(t["max_wait_s"], "code.max_wait_s", integer=True)
    for k in ("network", "deny_read"):
        if k in t:
            vals[k] = _str_list(t[k], f"code.{k}")
    if "pass_env" in t:
        names = _str_list(t["pass_env"], "code.pass_env")
        for n in names:
            if not ENV_NAME_RE.match(n):
                raise RegistryError(f"code.pass_env: {n!r} — не имя переменной окружения")
            if SECRET_ENV_RE.search(n) or n == "SSH_AUTH_SOCK":
                raise RegistryError(f"code.pass_env: {n} — секреты (TOKEN, SECRET, PASSWORD, KEY, SSH_AUTH_SOCK) "
                                    "в Claude Code не передаются")
        vals["pass_env"] = names
    return CodeGlobal(**vals)


def _ws_code(name: str, t, warnings: list[str]) -> ProjectCode:
    """[workspace.<имя>.code]: где и с какими потолками можно запускать Claude Code в проекте."""
    where = f"[workspace.{name}.code]"
    t = _table(t, where)
    warnings += [unknown_key(f"workspace.{name}.code.{k}") for k in t if k not in WS_CODE_KEYS]
    vals: dict = {}
    for k in ("enabled", "prefetch"):
        if k in t:
            if not isinstance(t[k], bool):
                raise RegistryError(f"{where} {k}: true или false")
            vals[k] = t[k]
    if "dirs" in t:
        vals["dirs"] = tuple(_rel_path(d, f"{where} dirs") for d in _str_list(t["dirs"], f"{where} dirs"))
    if "prompts" in t:
        vals["prompts"] = _rel_path(t["prompts"], f"{where} prompts")
    if "profiles" in t:
        prof = _str_list(t["profiles"], f"{where} profiles")
        bad = [p for p in prof if p not in PROFILES]
        if bad or not prof:
            raise RegistryError(f"{where} profiles: только {', '.join(PROFILES)}")
        vals["profiles"] = prof
    for k, order in (("model_min", MODELS), ("model_max", MODELS), ("effort_max", EFFORTS)):
        if k in t:
            if t[k] not in order:
                raise RegistryError(f"{where} {k}: одно из {', '.join(order)}")
            vals[k] = t[k]
    for k in ("max_turns", "max_parallel"):
        if k in t:
            vals[k] = _number(t[k], f"{where} {k}", integer=True)
    for k in ("max_budget_usd", "daily_budget_usd", "max_hours"):
        if k in t:
            vals[k] = _number(t[k], f"{where} {k}")
    if "status_regex" in t:
        rx = t["status_regex"]
        try:
            if not isinstance(rx, str) or not rx:
                raise re.error("пустое")
            re.compile(rx)
        except re.error as e:
            raise RegistryError(f"{where} status_regex: не регулярное выражение ({e})") from None
        vals["status_regex"] = rx
    for k in WS_CODE_LISTS:
        if k in t:
            vals[k] = _str_list(t[k], f"{where} {k}")
    pc = ProjectCode(**vals)
    if MODELS.index(pc.model_min) > MODELS.index(pc.model_max):
        raise RegistryError(f"{where}: model_min ({pc.model_min}) выше model_max ({pc.model_max})")
    if pc.enabled and (not pc.dirs or not pc.prompts):
        raise RegistryError(f"{where}: при enabled = true нужны dirs и prompts")
    return pc


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

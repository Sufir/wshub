"""wshub setup / uninstall / update: подключить wshub к Claude Desktop, убрать, обновить.

Всё, что определяется само (дистрибутив WSL, пользователь Windows, где Desktop берёт конфиг, путь к wshub),
не спрашивается. Спрашиваются только путь первого проекта и режим доступа — без подставленных значений.
Повторный запуск безопасен: что уже настроено, не трогается. --dry-run только показывает план.

Пути берутся из окружения, поэтому всё проверяется на временном HOME:
HOME, WSHUB_CONFIG, WSHUB_STATE, WSHUB_MNT (диски Windows, по умолчанию /mnt), WSL_DISTRO_NAME,
WSHUB_WIN_USER (пользователь Windows, если автоопределение не подходит).
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from . import doctor, jsonedit, outbox
from .registry import RegistryError, parse

REPO_URL = "https://github.com/Sufir/wshub"
MARKER = "desktop-setup.json"  # что setup создал в конфигах Desktop: нужно uninstall для точного отката
BRIEF_NAMES = (".agents/BRIEF.md", "BRIEF.md")
OUTBOX_NAME = "ClaudeOutbox"
RESTART = "перезапусти Claude Desktop полностью: значок в трее → Quit, затем запусти снова"

REGISTRY_TEMPLATE = f"""\
# Реестр wshub: какие папки видит Claude. Правит только человек — здесь, в панели wshub или командами wshub.
# Формат: {REPO_URL}/blob/main/docs/registry.md

[defaults]
# закрыто во всех проектах: имена видны, содержимое — нет
deny = [
  ".env", ".env.*", "*.pem", "*.key", "id_rsa*", "id_ed25519*",
  "**/secrets/**", "**/node_modules/**", "**/.git/objects/**", "**/.git/config",
]
max_read_kb = 512  # больше за один read не отдаётся
ttl_hours = 8      # срок жизни хэндла workspace_open
"""

USAGE = """\
wshub setup [--yes] [--dry-run] [--project <путь> --mode ro|rw]
    подключить wshub к Claude Desktop, создать реестр, первый проект и перевалку; повторный запуск безопасен
      --yes       не задавать вопросов: всё по умолчанию, проект — только из --project
      --dry-run   показать, что изменится, ничего не меняя
wshub uninstall [--purge] [--yes] [--dry-run]
    убрать запись wshub из конфига Desktop (с копией); --purge — ещё реестр и состояние (журнал, копии)
wshub update
    обновить wshub и напомнить о перезапуске Desktop"""


class SetupError(Exception):
    pass


# ---------- окружение ----------

@dataclass
class Env:
    home: Path
    config: Path
    state: Path
    mnt: Path
    distro: str | None  # None — не WSL
    win_user: str | None = None
    win_user_source: str = ""
    exe: Path | None = None

    @property
    def users_dir(self) -> Path:
        return self.mnt / "c" / "Users"

    @property
    def win_home(self) -> Path | None:
        return self.users_dir / self.win_user if self.win_user else None


def _run(args: list[str], cwd: Path | None = None, timeout: float = 15) -> bytes | None:
    """Команда Windows из WSL (cmd.exe, tasklist.exe): вывод или None, если её нет или она не ответила."""
    if not shutil.which(args[0]):
        return None
    try:
        r = subprocess.run(args, capture_output=True, timeout=timeout, check=False,
                           cwd=str(cwd) if cwd and cwd.is_dir() else None)
    except (OSError, subprocess.SubprocessError):
        return None
    return r.stdout if r.returncode == 0 else None


def find_exe(home: Path) -> Path | None:
    """Стабильный путь к wshub для записи в Desktop: ~/.local/bin/wshub (ссылка uv tool), а не путь в venv."""
    bin_dir = os.environ.get("UV_TOOL_BIN_DIR") or os.environ.get("XDG_BIN_HOME") or str(home / ".local" / "bin")
    cand = Path(bin_dir) / "wshub"
    if cand.exists():
        return cand
    found = shutil.which("wshub")
    return Path(found).absolute() if found else None


def _desktop_users(users_dir: Path) -> list[str]:
    out = []
    try:
        dirs = sorted(p for p in users_dir.iterdir() if p.name not in doctor.SKIP_USERS)
    except OSError:
        return out
    for u in dirs:
        ad = u / "AppData"
        try:
            if any((ad / "Local" / "Packages").glob("Claude_*")) or (ad / "Local" / "AnthropicClaude").is_dir() \
                    or (ad / "Roaming" / "Claude").is_dir():
                out.append(u.name)
        except OSError:
            continue
    return out


def _user_dirs(users_dir: Path) -> dict[str, str]:
    """Папки профилей Windows: имя в нижнем регистре → настоящее имя (NTFS не различает регистр)."""
    try:
        return {p.name.lower(): p.name for p in users_dir.iterdir()
                if p.name not in doctor.SKIP_USERS and p.is_dir()}
    except OSError:
        return {}


def detect_win_user(env: Env) -> tuple[str | None, str, list[str]]:
    """(имя папки профиля, откуда взято, кандидаты при неоднозначности)."""
    if not env.distro:
        return None, "", []
    dirs = _user_dirs(env.users_dir)
    forced = os.environ.get("WSHUB_WIN_USER")
    if forced:
        return dirs.get(forced.lower(), forced), "WSHUB_WIN_USER", []
    raw = _run(["cmd.exe", "/d", "/c", "echo %USERNAME%"], cwd=env.mnt / "c", timeout=10) or b""
    for enc in ("utf-8", "cp866", "cp1251"):  # cmd.exe пишет в OEM-кодировке консоли
        try:
            lines = raw.decode(enc).strip().splitlines()
        except UnicodeDecodeError:
            continue
        name = lines[-1].strip().lower() if lines else ""
        if name in dirs:
            return dirs[name], "cmd.exe", []
    with_desktop = _desktop_users(env.users_dir)
    if len(with_desktop) == 1:
        return with_desktop[0], "папка с Claude Desktop", []
    return None, "", with_desktop


def detect(config: Path, state: Path) -> Env:
    home = Path.home()
    distro = os.environ.get("WSL_DISTRO_NAME") or None
    mnt = Path(os.environ.get("WSHUB_MNT") or outbox.MNT)
    env = Env(home=home, config=Path(config), state=Path(state), mnt=mnt, distro=distro)
    env.exe = find_exe(home)
    env.win_user, env.win_user_source, _ = detect_win_user(env)
    return env


def desktop_running(env: Env) -> bool | None:
    """Запущен ли Claude Desktop (Windows). None — не удалось узнать."""
    if not env.distro:
        return None
    out = _run(["tasklist.exe", "/FI", "IMAGENAME eq claude.exe", "/NH", "/FO", "CSV"], cwd=env.mnt / "c")
    return None if out is None else b"claude.exe" in out.lower()


def entry_for(env: Env) -> dict:
    if env.distro:
        return {"command": "wsl.exe", "args": ["-d", env.distro, "--", str(env.exe)]}
    return {"command": str(env.exe)}


def desktop_configs(env: Env, only_used: bool = True) -> list[dict]:
    """Конфиги Desktop текущего пользователя (Windows — только его профиль; Linux/macOS — свой)."""
    ctx = doctor.Ctx(config=env.config, state=env.state, home=env.home, win_users=env.users_dir, distro=env.distro)
    cands = doctor.desktop_candidates(ctx)
    if env.distro:
        if not env.win_home:
            return []
        home = str(env.win_home).lower() + "/"
        cands = [c for c in cands if c["kind"] != "native" and str(c["path"]).lower().startswith(home)]
    return [c for c in cands if c["used"] or not only_used]


# ---------- ввод и вывод ----------

@dataclass
class UI:
    interactive: bool
    dry: bool
    ask_fn: Callable[[str], str] = input
    out: Callable[[str], None] = print
    log: list[str] = field(default_factory=list)

    def say(self, text: str = "") -> None:
        self.out(text)

    def step(self, title: str) -> None:
        self.out(f"\n— {title}")

    def done(self, what: str) -> None:
        self.out(f"  {'будет' if self.dry else 'готово'}: {what}")

    def note(self, what: str) -> None:
        self.out(f"  {what}")

    def ask(self, prompt: str) -> str:
        try:
            return self.ask_fn(prompt).strip()
        except EOFError:
            raise SetupError("ввод закончился — запусти заново или с --yes") from None


# ---------- шаги ----------

def _backup(path: Path, ui: UI) -> Path | None:
    if not path.exists():
        return None
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    dst, n = path.with_name(f"{path.name}.wshub-{stamp}.bak"), 1
    while dst.exists():
        dst, n = path.with_name(f"{path.name}.wshub-{stamp}-{n}.bak"), n + 1
    if not ui.dry:
        shutil.copy2(path, dst)
    return dst


def _write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.wshub-tmp")
    try:
        tmp.write_bytes(data)
        os.replace(tmp, path)
    except BaseException:
        if tmp.is_file():
            tmp.unlink()
        raise


def _read_text(path: Path) -> str:
    return path.read_bytes().decode("utf-8") if path.exists() else ""


def _load_marker(state: Path) -> dict:
    try:
        d = json.loads((state / MARKER).read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_marker(state: Path, data: dict) -> None:
    state.mkdir(parents=True, exist_ok=True)
    _write_bytes(state / MARKER, json.dumps(data, ensure_ascii=False, indent=2).encode())


def check_deps(env: Env, ui: UI) -> bool:
    ui.step("Зависимости")
    ok = True
    if env.exe:
        ui.note(f"wshub: {env.exe}")
    else:
        ok = False
        ui.note("wshub не найден в ~/.local/bin и PATH — установи: uv tool install git+" + REPO_URL)
    rg = shutil.which("rg")
    ui.note(f"ripgrep: {rg}" if rg else "ripgrep нет: grep будет медленным и может не уложиться в таймаут — "
            "sudo apt install ripgrep")
    if env.distro:
        ui.note(f"WSL: дистрибутив {env.distro}")
        if env.win_user:
            ui.note(f"пользователь Windows: {env.win_user} (откуда: {env.win_user_source})")
    else:
        ui.note("не WSL: запись для Desktop под Linux/macOS, перевалка publish не настраивается")
    return ok


def choose_win_user(env: Env, ui: UI) -> None:
    if not env.distro or env.win_user:
        return
    _n, _s, cands = detect_win_user(env)
    if not ui.interactive or not cands:
        ui.note(f"пользователь Windows не определён (папок с Claude Desktop: {len(cands)}): "
                "задай WSHUB_WIN_USER=<имя> и повтори")
        return
    ui.say("  Несколько пользователей Windows с Claude Desktop: " + ", ".join(cands))
    while True:
        name = ui.ask("  Твоё имя пользователя Windows (из списка): ")
        if name in cands:
            env.win_user, env.win_user_source = name, "выбран вручную"
            return
        ui.say("  нет такого в списке — введи имя точно как в списке")


def wait_desktop_closed(env: Env, ui: UI) -> bool:
    """True — Desktop закрыт или это не узнать; False — пишем при запущенном Desktop."""
    if ui.dry:
        return True
    running = desktop_running(env)
    if not running:
        return True
    if not ui.interactive:
        ui.note("Claude Desktop запущен: при выходе он может перезаписать конфиг своей копией — "
                "если wshub не появится, закрой Desktop и повтори wshub setup")
        return False
    while running:
        ans = ui.ask("  Claude Desktop запущен. Закрой его полностью (значок в трее → Quit) и нажми Enter "
                     "или введи «дальше», чтобы записать при запущенном: ")
        if ans.lower() in ("дальше", "lfkmit", "next"):
            return False
        running = desktop_running(env)
    return True


def setup_desktop(env: Env, ui: UI) -> tuple[bool, list[str]]:
    """Запись wshub в конфиги, которые читает Desktop. (успех, изменённые файлы)."""
    ui.step("Claude Desktop")
    if not env.exe:
        ui.note("пропущено: не найден исполняемый файл wshub")
        return False, []
    if env.distro and not env.win_user:
        ui.note("пропущено: не определён пользователь Windows (см. выше)")
        return False, []
    targets = desktop_configs(env)
    if not targets:
        where = f"у пользователя Windows {env.win_user}" if env.win_user else "на этой машине"
        ui.note(f"Claude Desktop не найден {where}: установи его, запусти один раз и повтори wshub setup")
        return False, []
    entry = entry_for(env)
    marker = _load_marker(env.state)
    ok, changed, plan = True, [], []
    for c in targets:
        path: Path = c["path"]
        try:
            text = _read_text(path)
            inner = jsonedit.empty_inner(text)
            new, action = jsonedit.set_server(text, "wshub", entry)
            data = new.encode("utf-8")
        except (OSError, UnicodeError, jsonedit.ConfigError) as e:
            ok = False
            ui.note(f"{_show(path, env)}: {e} — файл не тронут; исправь его или удали и запусти Desktop, "
                    "затем повтори")
            continue
        if action == "unchanged":
            ui.note(f"уже настроено: {_show(path, env)}")
        else:
            plan.append((c, path, text, data, action, inner))
    if plan:
        wait_desktop_closed(env, ui)
    for c, path, text, data, action, inner in plan:
        # что вставили — для точного отката в uninstall; при обновлении записи прежняя отметка остаётся
        created = {"added-parent": "mcpServers", "created": "file" if not path.exists() else "empty"}.get(action)
        note = {"created": created, "inner": inner, "original": text if created == "empty" else None}
        backup = None
        if ui.dry:
            backup = _backup(path, ui)
        else:
            prev = marker.get(str(path))
            try:
                if action != "updated":
                    marker.pop(str(path), None)
                    if created or inner is not None:
                        marker[str(path)] = note
                    _save_marker(env.state, marker)
                backup = _backup(path, ui)
                _write_bytes(path, data)
            except OSError as e:
                ok = False
                marker.pop(str(path), None)
                if prev is not None:
                    marker[str(path)] = prev
                try:
                    _save_marker(env.state, marker)
                except OSError:
                    pass
                ui.note(f"{_show(path, env)}: запись не удалась ({e.strerror or e}) — "
                        "закрой Desktop из трея и повтори wshub setup")
                continue
        verb = {"updated": "обновлена запись wshub", "added": "добавлена запись wshub",
                "added-parent": "добавлен раздел mcpServers с записью wshub",
                "created": "создан конфиг с записью wshub"}[action]
        ui.done(f"{verb}: {_show(path, env)}")
        ui.note(f"  {c['why']}")
        ui.note(f"  {entry['command']} {' '.join(entry.get('args', []))}")
        if backup:
            ui.note(f"  копия до изменения: {backup.name}")
        changed.append(str(path))
    stale = [c for c in desktop_configs(env, only_used=False) if not c["used"] and c.get("entry")]
    for c in stale:
        ui.note(f"в {_show(c['path'], env)} есть запись wshub, но этот файл Desktop не читает — "
                "она ни на что не влияет")
    return ok, changed


def _project_name(path: Path, taken: set[str]) -> str | None:
    base = re.sub(r"[^A-Za-z0-9-]+", "-", path.name).strip("-").lower()
    if not base or not base[0].isalnum():
        return None
    name, n = base, 2
    while name.lower() in taken:
        name, n = f"{base}-{n}", n + 1
    return name


def _ask_project(env: Env, ui: UI, taken: set[str]) -> tuple[Path, str, str] | None:
    ui.say("  Первый проект — папка, которую увидит Claude. Позже проекты добавляются в панели wshub.")
    while True:
        raw = ui.ask("  Путь к папке проекта в WSL (например /home/<user>/myproject; пусто — пропустить): ")
        if not raw:
            return None
        p = Path(os.path.expanduser(outbox.from_windows(raw, env.mnt)))
        if not p.is_absolute():
            ui.say("  нужен абсолютный путь, начиная с /")
        elif not p.is_dir():
            ui.say(f"  папки {p} нет — проверь путь")
        else:
            break
    while True:
        mode = ui.ask("  Режим доступа: ro — только чтение, rw — чтение и запись. Введи ro или rw: ").lower()
        if mode in ("ro", "rw"):
            break
        ui.say("  нужно ro или rw")
    name = _project_name(p, taken)
    while name is None:
        cand = ui.ask("  Имя проекта (латиница, цифры, дефис): ")
        name = cand if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]*", cand) and cand.lower() not in taken else None
        if name is None:
            ui.say("  имя занято или с недопустимыми символами")
    return p, mode, name


def setup_registry(env: Env, ui: UI, hub, project: tuple[str, str] | None) -> tuple[bool, str | None]:
    """Реестр и первый проект. (успех, имя добавленного или первого проекта)."""
    from .registry_edit import EditError, RegistryEditor, revision

    ui.step("Реестр проектов")
    first = None
    if env.config.exists():
        try:
            reg = parse(env.config.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, RegistryError) as e:
            ui.note(f"{env.config}: {e} — реестр не тронут, исправь его (wshub doctor покажет, что не так)")
            return False, None
        ui.note(f"уже есть: {env.config}, проектов {len(reg.workspaces)}")
        workspaces = dict(reg.workspaces)
    else:
        if not ui.dry:
            env.config.parent.mkdir(parents=True, exist_ok=True)
            _write_bytes(env.config, REGISTRY_TEMPLATE.encode())
        ui.done(f"создан {env.config} (маски секретов по умолчанию)")
        workspaces = {}
    if workspaces:
        first = next(iter(workspaces))
    want = None
    if project:
        p = Path(os.path.expanduser(outbox.from_windows(project[0], env.mnt)))
        if not p.is_absolute() or not p.is_dir():
            ui.note(f"--project {project[0]}: папки нет или путь не абсолютный — проект не добавлен")
            return False, first
        want = (p, project[1], None)
    elif not workspaces and ui.interactive:
        asked = _ask_project(env, ui, set())
        if asked:
            want = (asked[0], asked[1], asked[2])
    elif not workspaces:
        ui.note("проектов нет: добавь позже — wshub setup --project <путь> --mode ro|rw или в панели wshub")
    if want is None:
        return True, first
    path, mode, name = want
    same = [w.name for w in workspaces.values() if w.path.resolve() == path.resolve()]
    if same:
        ui.note(f"{path} уже в реестре как {same[0]}")
        return True, same[0]
    name = name or _project_name(path, {n.lower() for n in workspaces})
    if name is None:
        ui.note(f"{path}: из имени папки не получилось имя проекта — добавь его в панели wshub")
        return False, first
    brief = next((b for b in BRIEF_NAMES if (path / b).is_file()), "")
    if ui.dry:
        ui.done(f"добавлен проект {name} [{mode}] → {path}" + (f", BRIEF {brief}" if brief else ""))
        return True, name
    editor = RegistryEditor(env.config, env.state / "registry-history", hub.workspace_conflict)
    try:
        changes = editor.save(name=name, path=str(path), mode=mode, description="", brief=brief, deny=[],
                              rev=revision(env.config.read_bytes()), create=True)
    except (EditError, OSError) as e:
        ui.note(f"проект не добавлен: {e}")
        return False, first
    for line in changes:
        ui.done(line + (f", BRIEF {brief}" if brief else ""))
    return True, name


def setup_outbox(env: Env, ui: UI, hub) -> bool:
    from .registry_edit import EditError, RegistryEditor, revision

    ui.step("Перевалка для publish (файл → карточка в чате)")
    if not env.distro:
        ui.note("не WSL — пропущено")
        return True
    try:
        reg = parse(_read_text(env.config)) if env.config.exists() else None
    except (OSError, UnicodeDecodeError, RegistryError):
        ui.note("пропущено: реестр не читается")
        return False
    if reg is not None and reg.outbox.present:
        win = outbox.win_path(reg.outbox.path, env.mnt) if reg.outbox.path else None
        ui.note(f"уже настроена: {reg.outbox.path}" + (f" ({win})" if win else ""))
        return True
    if not env.win_home or not env.win_home.is_dir():
        ui.note("пропущено: не найден профиль Windows; позже — wshub outbox set C:\\Users\\<имя>\\" + OUTBOX_NAME)
        return True
    target = env.win_home / OUTBOX_NAME
    if ui.interactive:
        ans = ui.ask(f"  Папка перевалки: Enter — {outbox.win_path(target, env.mnt)}, «-» — не настраивать, "
                     "или введи свой путь: ")
        if ans == "-":
            ui.note("не настроена: publish будет отказывать; позже — wshub outbox set <путь>")
            return True
        if ans:
            target = Path(os.path.normpath(outbox.from_windows(ans, env.mnt)))
    win = outbox.win_path(target, env.mnt)
    roots = {w.name: w.path for w in reg.workspaces.values()} if reg else {}
    if ui.dry:
        errs = [e for e in outbox.path_problems(str(target), roots, env.mnt, hub.protected_overlap)
                if "папки" not in e or "нет" not in e]
        if errs:
            ui.note("путь не годится: " + "; ".join(errs))
            return False
        ui.done(f"папка {target} ({win}) и секция [outbox] в реестре")
        return True
    try:
        if win and not target.exists() and target.parent.is_dir():
            target.mkdir()
            ui.done(f"создана папка {win}")
    except OSError as e:
        ui.note(f"папку {target} создать не удалось: {e}")
        return False
    editor = RegistryEditor(env.config, env.state / "registry-history", hub.workspace_conflict)
    try:
        changes = editor.save_outbox(values={"path": str(target)}, rev=revision(env.config.read_bytes()),
                                     check_path=lambda p, r: outbox.path_problems(p, r, env.mnt,
                                                                                  hub.protected_overlap))
    except (EditError, OSError) as e:
        ui.note(f"перевалка не настроена: {e}")
        return False
    for line in changes:
        ui.done(line)
    ui.note("в каждом новом чате Claude один раз спросит доступ к этой папке — разреши")
    return True


def run_doctor(env: Env, hub, ui: UI) -> bool:
    """Короткая сводка doctor: только то, что требует внимания; полный отчёт — wshub doctor."""
    ui.step("Проверка (wshub doctor)")
    ctx = doctor.Ctx(config=env.config, state=env.state, home=env.home, win_users=env.users_dir, distro=env.distro,
                     protected_overlap=hub.protected_overlap, workspace_conflict=hub.workspace_conflict,
                     mnt_root=env.mnt)
    checks = doctor.run(ctx)["checks"]
    for c in checks:
        if c["status"] in ("warn", "fail"):
            ui.note(f"[{doctor.STATUS[c['status']].strip()}] {c['title']}: {c['detail'][0]}")
            if c["fix"]:
                ui.note(f"  → {c['fix']}")
    n = {s: sum(c["status"] == s for c in checks) for s in doctor.STATUS}
    ui.note(f"ok {n['ok']}, внимание {n['warn']}, ошибки {n['fail']} — подробно: wshub doctor")
    return not n["fail"]


def _show(path: Path, env: Env) -> str:
    """Путь так, как его видит человек: для диска Windows — C:\\…"""
    return outbox.win_path(path, env.mnt) or str(path)


def setup(env: Env, ui: UI, project: tuple[str, str] | None = None) -> int:
    from .core import Hub

    hub = Hub(env.config, env.state)
    hub.mnt_root = env.mnt
    if ui.dry:
        ui.say("--dry-run: ничего не меняется, вопросы не задаются; проект — только из --project")
    deps = check_deps(env, ui)
    choose_win_user(env, ui)
    d_ok, changed = setup_desktop(env, ui)
    r_ok, name = setup_registry(env, ui, hub, project)
    o_ok = setup_outbox(env, ui, hub)
    if ui.dry:
        ui.say("\n--dry-run: ничего не изменено")
        return 0 if deps and d_ok and r_ok and o_ok else 1
    doc_ok = run_doctor(env, hub, ui)
    ok = deps and d_ok and r_ok and o_ok and doc_ok
    first = f"«открой проект {name} через wshub»" if name else "«покажи проекты wshub»"
    ui.say("")
    if not ok:
        ui.say("Не всё настроено — исправь то, что выше, и запусти wshub setup ещё раз (это безопасно).")
    elif changed:
        ui.say(f"Дальше: {RESTART}; в новом чате, начатом в Desktop, напиши {first} и разреши инструменты wshub "
               "(«Always allow»).")
    else:
        ui.say(f"Всё уже настроено. Проверка: новый чат, начатый в Desktop → {first}. Не видно wshub — {RESTART}.")
    return 0 if ok else 1


# ---------- uninstall ----------

def uninstall(env: Env, ui: UI, purge: bool) -> int:
    ui.step("Claude Desktop")
    choose_win_user(env, ui)
    if env.distro and not env.win_user:
        ui.say("Ничего не изменено: не определён пользователь Windows — задай WSHUB_WIN_USER=<имя> и повтори.")
        return 1
    marker = _load_marker(env.state)
    ok, plan = True, []
    for c in desktop_configs(env, only_used=False):
        path: Path = c["path"]
        if not c["exists"]:
            continue
        m = marker.get(str(path)) or {}
        try:
            text = _read_text(path)
            new, removed = jsonedit.remove_server(text, "wshub", m.get("created"), m.get("inner"), m.get("original"))
            data = None if new is None else new.encode("utf-8")
        except (OSError, UnicodeError, jsonedit.ConfigError) as e:
            ok = ok and not c["used"]  # нечитаемый файл, который Desktop не читает, удалению не мешает
            ui.note(f"{_show(path, env)}: {e} — файл не тронут")
            continue
        if removed:
            plan.append((path, data))
        else:
            marker.pop(str(path), None)  # записи нет (убрали руками или Desktop) — отметка больше не нужна
    if plan:
        wait_desktop_closed(env, ui)
    else:
        ui.note("записи wshub в конфигах Desktop нет")
    for path, data in plan:
        backup = None
        if not ui.dry:
            try:
                backup = _backup(path, ui)
                if data is None:
                    path.unlink()
                else:
                    _write_bytes(path, data)
            except OSError as e:
                ok = False
                ui.note(f"{_show(path, env)}: изменить не удалось ({e.strerror or e}) — закрой Desktop и повтори")
                continue
            marker.pop(str(path), None)
        what = "удалён конфиг (его создал setup)" if data is None else "убрана запись wshub"
        ui.done(f"{what}: {_show(path, env)}")
        if backup:
            ui.note(f"  копия до изменения: {backup.name}")
    if not ui.dry and env.state.is_dir():
        try:
            if marker:
                _save_marker(env.state, marker)
            else:
                (env.state / MARKER).unlink(missing_ok=True)
        except OSError:
            pass
    ui.step("Реестр и состояние")
    if not purge:
        ui.note(f"оставлены: {env.config} и {env.state} (удалить — wshub uninstall --purge)")
    elif not ok:
        ui.note("--purge пропущен: запись wshub убрана не везде — исправь и повтори (иначе потеряется отметка "
                "для точного отката конфига)")
    else:
        ok = purge_data(env, ui)
    ui.say("")
    ui.say(f"Дальше: {RESTART}. Удалить саму программу: uv tool uninstall wshub")
    return 0 if ok else 1


def _own_dir(p: Path, default: Path) -> bool:
    """Каталог целиком удаляется, только если это каталог wshub по умолчанию и не симлинк."""
    return os.path.abspath(p) == os.path.abspath(default) and p.is_dir() and not p.is_symlink()


def purge_data(env: Env, ui: UI) -> bool:
    if ui.interactive and not ui.dry:
        ans = ui.ask(f"  Удалить реестр {env.config} и состояние {env.state} (журнал, копии файлов до правок)? "
                     "Введи «да»: ")
        if ans.lower() not in ("да", "yes"):
            ui.note("не удалено")
            return True
    ok = True
    try:
        reg = parse(_read_text(env.config)) if env.config.is_file() else None
    except (OSError, UnicodeError, RegistryError):
        reg = None
    if reg is not None and reg.outbox.path and reg.outbox.path.is_dir():
        if not ui.dry:
            res = outbox.cleanup(reg.outbox.path, 0, 0, everything=True)
            ui.done(f"перевалка {reg.outbox.path}: удалено каталогов wshub {res['deleted']} (папка оставлена)")
        else:
            ui.done(f"перевалка {reg.outbox.path}: удалить каталоги wshub (папка останется)")
    cfg_dir, state_dir = env.home / ".config" / "wshub", env.home / ".local" / "state" / "wshub"
    try:
        if env.config.is_file():
            if not ui.dry:
                env.config.unlink()
                if _own_dir(env.config.parent, cfg_dir):
                    shutil.rmtree(env.config.parent)
            ui.done(f"удалён реестр {env.config}")
        if _own_dir(env.state, state_dir):
            if not ui.dry:
                shutil.rmtree(env.state)
            ui.done(f"удалено состояние {env.state}")
        elif env.state.exists():
            ok = False
            ui.note(f"{env.state} — не каталог wshub по умолчанию (или симлинк): не удаляю, удали вручную")
    except OSError as e:
        ok = False
        ui.note(f"удалить не удалось: {e}")
    return ok


# ---------- update ----------

def version_line() -> str:
    from importlib.metadata import PackageNotFoundError, version

    from .runtime import code_head, repo_dir

    try:
        ver = version("wshub")
    except PackageNotFoundError:
        ver = "?"
    repo = repo_dir()
    head = code_head(repo)
    how = f"рабочая копия {repo}" if repo else ("из git" if head else "пакет")
    return f"wshub {ver}" + (f" ({head[:8]}, {how})" if head else f" ({how})")


def _uv() -> str | None:
    return shutil.which("uv") or next((str(p) for p in (Path.home() / ".local/bin/uv", Path.home() / ".cargo/bin/uv")
                                       if p.exists()), None)


def update(env: Env, ui: UI) -> int:
    from .runtime import repo_dir

    repo = repo_dir()
    before = version_line()
    ui.say(f"сейчас: {before}")
    if repo:
        ui.say(f"wshub установлен из рабочей копии {repo} (editable): код обновляется через git.")
        ui.say(f"  git -C {repo} pull && uv tool install --editable --reinstall {repo}")
        ui.say(f"Затем {RESTART}.")
        return 0
    uv = _uv()
    if not uv:
        ui.say("uv не найден — поставь его: curl -LsSf https://astral.sh/uv/install.sh | sh")
        return 1
    r = subprocess.run([uv, "tool", "upgrade", "wshub"], check=False)
    if r.returncode != 0:
        ui.say(f"обновление не удалось; переустанови: uv tool install --force git+{REPO_URL}")
        return 1
    exe = env.exe or find_exe(env.home)
    after = subprocess.run([str(exe), "--version"], capture_output=True, text=True, check=False).stdout.strip() \
        if exe else "?"
    if after == before:
        ui.say("уже последняя версия.")
        return 0
    ui.say(f"обновлено: {after}")
    ui.say(f"Дальше: {RESTART} — до этого Desktop работает со старым кодом (wshub doctor покажет «устарел»).")
    return 0


# ---------- CLI ----------

def cli(cmd: str, args: list[str], config: Path, state: Path, ask_fn=input, out=print) -> int:
    flags = {"setup": {"--yes", "--dry-run", "--project", "--mode"}, "uninstall": {"--yes", "--dry-run", "--purge"},
             "update": set()}[cmd]
    opts: dict[str, str | bool] = {}
    i = 0
    while i < len(args):
        a = args[i]
        if a in ("-h", "--help"):
            out(USAGE)
            return 0
        if a not in flags:
            print(f"wshub {cmd}: неизвестный параметр {a}\n\n{USAGE}", file=sys.stderr)
            return 2
        if a in ("--project", "--mode"):
            if i + 1 >= len(args):
                print(f"wshub {cmd}: после {a} нужно значение", file=sys.stderr)
                return 2
            opts[a] = args[i + 1]
            i += 2
            continue
        opts[a] = True
        i += 1
    project = None
    if "--project" in opts or "--mode" in opts:
        if opts.get("--mode") not in ("ro", "rw") or "--project" not in opts:
            print(f"wshub {cmd}: --project и --mode задаются вместе, --mode — ro или rw", file=sys.stderr)
            return 2
        project = (str(opts["--project"]), str(opts["--mode"]))
    dry = bool(opts.get("--dry-run"))
    interactive = not dry and not opts.get("--yes")
    if interactive and cmd != "update" and not sys.stdin.isatty() and ask_fn is input:
        print(f"wshub {cmd}: нет терминала для вопросов — запусти с --yes (и --project/--mode)", file=sys.stderr)
        return 2
    env = detect(config, state)
    ui = UI(interactive=interactive, dry=dry, ask_fn=ask_fn, out=out)
    try:
        if cmd == "setup":
            return setup(env, ui, project)
        if cmd == "uninstall":
            return uninstall(env, ui, purge=bool(opts.get("--purge")))
        return update(env, ui)
    except SetupError as e:
        print(f"wshub {cmd}: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print(f"\nwshub {cmd}: прервано", file=sys.stderr)
        return 130

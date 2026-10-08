"""Проверки окружения wshub: общий модуль для `wshub doctor` и вкладки «Обзор» панели.

Каждая проверка — словарь: id, title, status (ok | warn | fail | info), detail (строки), fix (что сделать).
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from . import housekeeping, outbox
from .registry import LIMITS, OUTBOX, Limits, Outbox, RegistryError, parse
from .runtime import code_head, live_processes, pid_alive, proc_start, repo_dir

BACKUP_WARN = 1024 * 1024 * 1024
if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover
    import tomli as tomllib

SKIP_USERS = {"Default", "Default User", "Public", "All Users", "WsiAccount", "desktop.ini"}


@dataclass
class Ctx:
    config: Path
    state: Path
    home: Path = field(default_factory=Path.home)
    win_users: Path = field(default_factory=lambda: outbox.MNT / "c" / "Users")
    proc: Path = Path("/proc")
    repo: Path | None = field(default_factory=repo_dir)
    distro: str | None = field(default_factory=lambda: os.environ.get("WSL_DISTRO_NAME"))
    self_pid: int = field(default_factory=os.getpid)
    protected_overlap: object = None  # Hub.protected_overlap, если есть: строгая проверка перевалки
    workspace_conflict: object = None  # Hub.workspace_conflict, если есть: самозащита для проектов
    mnt_root: Path = outbox.MNT  # где смонтированы диски Windows


def _check(id_, title, status, detail, fix=""):
    if isinstance(detail, str):
        detail = [detail]
    return {"id": id_, "title": title, "status": status, "detail": detail, "fix": fix}


def human_size(n: int) -> str:
    for unit in ("байт", "КБ", "МБ", "ГБ"):
        if n < 1024 or unit == "ГБ":
            return f"{n:.0f} {unit}" if unit == "байт" else f"{n:.1f} {unit}"
        n /= 1024
    return str(n)


def _tree_size(d: Path) -> tuple[int, int]:
    total = count = 0
    for base, _dirs, files in os.walk(d):
        for f in files:
            try:
                total += os.lstat(os.path.join(base, f)).st_size
                count += 1
            except OSError:
                pass
    return total, count


# ---------- отдельные проверки ----------

def check_rg() -> dict:
    rg = shutil.which("rg")
    if rg:
        return _check("rg", "ripgrep", "ok", f"rg: {rg}")
    return _check("rg", "ripgrep", "warn", "rg нет в PATH: grep работает медленнее, обходом на Python",
                  "установи ripgrep: sudo apt install ripgrep")


def check_tomlkit() -> dict:
    try:
        import tomlkit
    except ImportError:
        return _check("tomlkit", "tomlkit", "fail", "tomlkit не установлен: панель не может сохранять реестр",
                      "переустанови wshub: uv tool install --reinstall git+https://github.com/Sufir/wshub")
    return _check("tomlkit", "tomlkit", "ok", f"tomlkit {getattr(tomlkit, '__version__', '?')}")


def check_registry(ctx: Ctx) -> tuple[dict, dict | None]:
    title = "Реестр"
    try:
        text = ctx.config.read_text(encoding="utf-8")
    except FileNotFoundError:
        return _check("registry", title, "fail", f"реестра нет: {ctx.config}",
                      "запусти wshub setup — он создаст реестр и добавит первый проект"), None
    except OSError as e:
        return _check("registry", title, "fail", f"не читается: {ctx.config}: {e}", "проверь права на файл"), None
    try:
        reg = parse(text)
    except RegistryError as e:
        return _check("registry", title, "fail", [f"{ctx.config}: {e}", "пока ошибка не исправлена, все вызовы "
                                                  "инструментов отказывают"], "исправь файл реестра"), None
    head = f"{ctx.config}: проектов {len(reg.workspaces)}"
    if reg.warnings:
        return _check("registry", title, "warn", [head, *reg.warnings],
                      "ключ из более новой версии wshub или опечатка: исправь имя или удали ключ"), reg
    return _check("registry", title, "ok", head), reg


def check_paths(ctx: Ctx, reg) -> dict:
    title = "Пути проектов"
    if reg is None:
        return _check("paths", title, "info", "не проверены: реестр не прочитан", "сначала исправь реестр")
    lines, bad, rw_code = [], [], []
    for w in reg.workspaces.values():
        if not w.path.is_dir():
            bad.append(w.name)
            lines.append(f"{w.name}: папки нет — {w.path}")
            continue
        root = w.path.resolve()
        conflict = ctx.workspace_conflict if ctx.workspace_conflict else lambda _root, _mode: None
        why = conflict(root, "ro")
        if why is not None:
            bad.append(w.name)
            lines.append(f"{w.name}: {why} — не откроется")
            continue
        code = conflict(root, "rw") is not None
        if code and w.mode == "rw":
            rw_code.append(w.name)
            p = ctx.protected_overlap(root) if ctx.protected_overlap else None
            lines.append(f"{w.name}: пересекается с кодом wshub {p} — в rw не откроется, в ro откроется")
            continue
        brief = ""
        if w.brief:
            brief = ", BRIEF есть" if (w.path / w.brief).is_file() else f", BRIEF {w.brief} — файла нет"
        lines.append(f"{w.name}: {w.path}{brief}{', код wshub — только чтение' if code else ''}")
    if not reg.workspaces:
        return _check("paths", title, "info", "реестр пуст", "добавь проект на вкладке «Проекты»")
    fixes = []
    if bad:
        fixes.append(f"исправь путь или удали проект: {', '.join(bad)} (вкладка «Проекты»)")
    if rw_code:
        fixes.append(f"смени режим на ro: {', '.join(rw_code)} (вкладка «Проекты»)")
    if fixes:
        return _check("paths", title, "fail" if bad else "warn", lines, "; ".join(fixes))
    return _check("paths", title, "ok", lines)


# ---------- Claude Desktop ----------

def desktop_candidates(ctx: Ctx) -> list[dict]:
    """Все файлы, откуда Desktop может брать конфиг, с пометкой, читает ли его установленная версия."""
    out = []
    if ctx.win_users.is_dir():
        try:
            users = sorted(p for p in ctx.win_users.iterdir() if p.name not in SKIP_USERS)
        except OSError:
            users = []
        for u in users:
            appdata = u / "AppData"
            try:
                if not appdata.is_dir():
                    continue
                pkgs = sorted((appdata / "Local/Packages").glob("Claude_*"))
            except OSError:
                continue
            classic_install = (appdata / "Local/AnthropicClaude").is_dir()
            for pkg in pkgs:
                out.append({"kind": "msix", "path": pkg / "LocalCache/Roaming/Claude/claude_desktop_config.json",
                            "used": True,
                            "why": f"Desktop из MSIX-пакета {pkg.name} читает свою копию в LocalCache пакета"})
            classic = appdata / "Roaming/Claude/claude_desktop_config.json"
            if classic_install:
                used, why = True, "классическая установка (AnthropicClaude) читает этот файл"
            elif pkgs:
                used, why = False, ("установлен только MSIX-Desktop: он читает копию в пакете, а этот файл виден "
                                    "лишь процессам вне пакета (WSL, Проводник)")
            else:
                used, why = False, "Desktop для этого пользователя не найден"
            out.append({"kind": "classic", "path": classic, "used": used, "why": why})
    for p, why in ((ctx.home / ".config/Claude/claude_desktop_config.json", "Desktop под Linux"),
                   (ctx.home / "Library/Application Support/Claude/claude_desktop_config.json", "Desktop под macOS")):
        if p.parent.is_dir():
            out.append({"kind": "native", "path": p, "used": True, "why": why})
    for c in out:
        c.update(_read_entry(ctx, c["path"]))
    return out


def _read_entry(ctx: Ctx, path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        return {"exists": False}
    except (OSError, ValueError) as e:
        return {"exists": True, "error": f"не читается как JSON: {e}"}
    servers = data.get("mcpServers") if isinstance(data, dict) else None
    servers = servers if isinstance(servers, dict) else {}
    res = {"exists": True, "servers": sorted(servers)}
    entry = servers.get("wshub")
    if isinstance(entry, dict):
        res["entry"] = _describe_entry(ctx, entry)
    return res


def _describe_entry(ctx: Ctx, e: dict) -> dict:
    cmd = str(e.get("command", ""))
    args = [str(a) for a in e.get("args", []) if isinstance(a, (str, int))]
    distro, target = None, cmd
    if Path(cmd.replace("\\", "/")).name.lower() in ("wsl.exe", "wsl"):
        target = None
        for i, a in enumerate(args):
            if a in ("-d", "--distribution") and i + 1 < len(args):
                distro = args[i + 1]
            if a in ("--", "-e", "--exec") and i + 1 < len(args):
                target = args[i + 1]
                break
    info = {"command": " ".join([cmd, *args]), "distro": distro, "target": target}
    problems = []
    if distro and ctx.distro and distro != ctx.distro:
        problems.append(f"дистрибутив {distro}, а этот doctor запущен в {ctx.distro}")
    if target:
        t = Path(target)
        if not t.exists():
            problems.append(f"{target} не существует")
        else:
            real = t.resolve()
            info["resolved"] = str(real)
            if not os.access(real, os.X_OK):
                problems.append(f"{real} не исполняемый")
    else:
        problems.append("не удалось разобрать, какой файл запускается")
    info["problems"] = problems
    return info


def check_desktop(ctx: Ctx) -> dict:
    title = "Запись wshub в Claude Desktop"
    cands = desktop_candidates(ctx)
    if not cands:
        return _check("desktop", title, "info", "конфигов Claude Desktop не найдено (не Windows/WSL и не Linux/macOS "
                      "Desktop)", "установи Claude Desktop, запусти его один раз, затем wshub setup")
    lines, active = [], []
    for c in cands:
        mark = "читается Desktop" if c["used"] else "не читается"
        head = f"[{mark}] {c['path']}"
        if not c["exists"]:
            lines.append(f"{head} — файла нет")
        elif "error" in c:
            lines.append(f"{head} — {c['error']}")
        elif "entry" in c:
            e = c["entry"]
            where = e.get("resolved") or e.get("target") or "?"
            dist = f" в {e['distro']}" if e.get("distro") else ""
            lines.append(f"{head} — wshub → {e['command']} (файл {where}{dist})"
                         + (f"; проблемы: {'; '.join(e['problems'])}" if e["problems"] else ""))
        else:
            lines.append(f"{head} — записи wshub нет (серверы: {', '.join(c['servers']) or 'нет'})")
        lines.append(f"    {c['why']}")
        if c["used"] and c.get("entry"):
            active.append(c)
    stale = [c for c in cands if not c["used"] and c.get("entry")]
    if not active:
        return _check("desktop", title, "fail", lines,
                      "запусти wshub setup — он добавит запись в файл с пометкой «читается Desktop»")
    broken = [c for c in active if c["entry"]["problems"]]
    if broken:
        return _check("desktop", title, "fail", lines, "запусти wshub setup — он исправит запись")
    if stale:
        return _check("desktop", title, "warn", lines,
                      "запись в нечитаемом файле ни на что не влияет — её можно удалить, чтобы не путаться")
    return _check("desktop", title, "ok", lines)


# ---------- процессы ----------

def _is_wshub_cmd(argv: list[str]) -> bool:
    if not argv:
        return False
    rest = argv[1:]
    if Path(argv[0]).name == "wshub":
        return not rest or rest[0] != "doctor"
    for i, a in enumerate(rest):
        if Path(a).name == "wshub" and Path(argv[0]).name.startswith("python"):
            return rest[i + 1:i + 2] != ["doctor"]
        if a == "-m" and rest[i + 1:i + 2] == ["wshub"]:
            return rest[i + 2:i + 3] != ["doctor"]
    return False


def processes(ctx: Ctx) -> dict:
    """Живые процессы сервера: с run-файлом и без него (запущены версией до панели)."""
    head_disk = code_head(ctx.repo)
    reg = live_processes(ctx.state, ctx.proc)
    known = {int(d["pid"]) for d in reg}
    rows = []
    for d in reg:
        rows.append({"pid": d["pid"], "started": d.get("started"), "head": d.get("head"),
                     "stale": bool(head_disk and d.get("head") and d["head"] != head_disk),
                     "handles": len(d.get("handles") or []), "last_call": d.get("last_call"),
                     "current": int(d["pid"]) == ctx.self_pid})
    legacy = []
    uid = os.getuid() if hasattr(os, "getuid") else None
    try:
        entries = list(ctx.proc.iterdir()) if ctx.proc.is_dir() else []
    except OSError:
        entries = []
    for p in entries:
        if not p.name.isdigit() or int(p.name) in known or int(p.name) == ctx.self_pid:
            continue
        try:
            if uid is not None and p.stat().st_uid != uid:
                continue
            argv = [a for a in (p / "cmdline").read_bytes().decode("utf-8", "replace").split("\0") if a]
        except OSError:
            continue
        if _is_wshub_cmd(argv) and pid_alive(int(p.name), proc_start(int(p.name), ctx.proc), ctx.proc):
            legacy.append({"pid": int(p.name), "cmd": " ".join(argv)})
    return {"head_disk": head_disk, "registered": rows, "legacy": sorted(legacy, key=lambda r: r["pid"])}


def check_processes(ctx: Ctx, procs: dict) -> dict:
    title = "Процессы сервера"
    lines = []
    for r in procs["registered"]:
        started = datetime.fromtimestamp(r["started"]).strftime("%Y-%m-%d %H:%M") if r["started"] else "?"
        code = (r["head"] or "?")[:8]
        lines.append(f"pid {r['pid']}: старт {started}, код {code}{' (устарел)' if r['stale'] else ''}, "
                     f"хэндлов {r['handles']}")
    for r in procs["legacy"]:
        lines.append(f"pid {r['pid']}: без run-файла — запущен версией wshub до панели ({r['cmd']})")
    head = procs["head_disk"]
    lines.append(f"код на диске: {head[:8] if head else 'версия неизвестна (не git-checkout и не установка из git)'}")
    if any(r["stale"] for r in procs["registered"]) or procs["legacy"]:
        return _check("procs", title, "warn", lines,
                      "код новее запущенного — перезапусти Desktop (полностью, из трея), "
                      "чтобы процессы взяли новый код")
    if not procs["registered"]:
        return _check("procs", title, "info", lines + ["живых процессов нет: Desktop не запущен или wshub отключён"],
                      "")
    return _check("procs", title, "ok", lines)


def storage(ctx: Ctx, reg) -> dict:
    """Размеры журнала и копий и действующие лимиты — для doctor и вкладки «Обзор»."""
    has_section = False
    if reg is not None:
        try:
            has_section = "limits" in tomllib.loads(ctx.config.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
    lim = reg.limits if reg is not None else Limits()
    backup = ctx.state / "backup"
    size, count = _tree_size(backup) if backup.is_dir() else (0, 0)
    return {"limits": {k: getattr(lim, k) for k in LIMITS},
            "limit_labels": {k: label for k, (_d, label) in LIMITS.items()},
            "limits_source": "реестр, [limits]" if has_section else
            ("по умолчанию: секции [limits] нет" if reg is not None else "по умолчанию: реестр не прочитан"),
            "journal": housekeeping.journal_stats(ctx.state),
            "backup": {"path": str(backup), "exists": backup.is_dir(), "bytes": size, "files": count},
            "last_cleanup": housekeeping.last_cleanup(ctx.state)}


def check_storage(st: dict) -> list[dict]:
    lim, j, b = st["limits"], st["journal"], st["backup"]
    lines = [f"{j['path']}: {human_size(j['current_bytes'])} из {lim['journal_max_mb']} МБ — больше уходит в архив",
             f"архивов: {j['archives']} из {lim['journal_keep_files']}"
             + (f", {human_size(j['archives_bytes'])}" if j["archives"] else "")]
    st_j = "ok" if j["current_bytes"] or j["archives"] else "info"
    res = [_check("audit", "Журнал", st_j, lines if st_j == "ok" else [f"журнала ещё нет: {j['path']}"] + lines[1:])]
    lc = st["last_cleanup"]
    when = (datetime.fromtimestamp(lc["ts"]).strftime("%Y-%m-%d %H:%M") + f", удалено копий: {lc.get('deleted', 0)}"
            if lc and isinstance(lc.get("ts"), (int, float)) else "ещё не было")
    rule = (f"хранение: копия удаляется, если она старше {lim['backup_keep_days']} дн. и у файла есть "
            f"≥ {lim['backup_keep_per_file']} более новых копий; последняя не удаляется никогда")
    if b["exists"]:
        warn = b["bytes"] > BACKUP_WARN
        res.append(_check("backup", "Копии", "warn" if warn else "ok",
                          [f"{b['path']}: {human_size(b['bytes'])}, файлов {b['files']}", rule,
                           f"последняя очистка: {when}"],
                          "копий много — уменьши лимиты хранения на вкладке «Обзор»" if warn else ""))
    else:
        res.append(_check("backup", "Копии", "info", [f"копий ещё нет: {b['path']}", rule]))
    res.append(_check("limits", "Лимиты", "ok" if st["limits_source"].startswith("реестр") else "info",
                      [f"{k} = {v} ({LIMITS[k][1]})" for k, v in lim.items()] + [f"источник: {st['limits_source']}"]))
    return res


# ---------- перевалка (publish) ----------

def _writable(d: Path) -> str | None:
    """None — запись есть; иначе причина. Пробный файл не совпадает с шаблоном каталогов и сразу удаляется."""
    try:
        fd, tmp = tempfile.mkstemp(dir=d, prefix=".wshub-probe-")
        os.close(fd)
        os.unlink(tmp)
        return None
    except OSError as e:
        return e.strerror or str(e)


def outbox_info(ctx: Ctx, reg) -> dict:
    """Состояние перевалки для doctor и блока «Перевалка» на «Обзоре»."""
    ob = reg.outbox if reg is not None else Outbox()
    info = {"present": ob.present, "error": ob.error, "path": str(ob.path) if ob.path else None,
            "win_path": outbox.win_path(ob.path, ctx.mnt_root) if ob.path else None,
            "values": {k: getattr(ob, k) for k in OUTBOX}, "labels": {k: lab for k, (_d, lab) in OUTBOX.items()},
            "problems": [], "write_error": None, "cloud": None, "dirs": 0, "bytes": 0, "oldest": None,
            "last_cleanup": outbox.last_cleanup(ctx.state), "registry_ok": reg is not None}
    if ob.path is None:
        return info
    roots = {w.name: w.path for w in reg.workspaces.values()}
    info["problems"] = outbox.path_problems(str(ob.path), roots, ctx.mnt_root, ctx.protected_overlap)
    info["cloud"] = outbox.cloud_synced(ob.path)
    if ob.path.is_dir() and not ob.path.is_symlink():
        info["write_error"] = _writable(ob.path)
        info.update(outbox.stats(ob.path))
    return info


def check_outbox(info: dict, now: float | None = None) -> dict:
    title = "Перевалка (publish)"
    how = "wshub outbox set /mnt/c/Users/<имя>/ClaudeOutbox — или поля «Перевалка» на «Обзоре» панели"
    if not info["registry_ok"]:
        return _check("outbox", title, "info", "не проверена: реестр не прочитан", "сначала исправь реестр")
    if not info["present"]:
        return _check("outbox", title, "info", "секции [outbox] в реестре нет: publish отказывает", how)
    if info["error"]:
        return _check("outbox", title, "fail", f"ошибка в [outbox]: {info['error']}", how)
    now = time.time() if now is None else now
    v = info["values"]
    lines = [f"{info['path']} → {info['win_path'] or '(не диск Windows)'}"]
    if info["problems"]:
        return _check("outbox", title, "fail", lines + info["problems"], "исправь путь: " + how)
    if info["write_error"]:
        return _check("outbox", title, "fail", lines + [f"запись в папку не удалась: {info['write_error']}"],
                      "проверь права на папку в Windows")
    age = f", самый старый — {int((now - info['oldest']) // 60)} мин назад" if info["oldest"] else ""
    lc = info["last_cleanup"]
    when = datetime.fromtimestamp(lc["ts"]).strftime("%Y-%m-%d %H:%M") if lc and isinstance(
        lc.get("ts"), (int, float)) else "ещё не было"
    lines += ["на диске Windows (drvfs), вне проектов, запись есть",
              f"каталогов wshub: {info['dirs']}, {human_size(info['bytes'])} из {v['max_total_mb']} МБ{age}",
              f"копии живут {v['ttl_minutes']} мин; файл ≤ {v['max_file_mb']} МБ; "
              f"≤ {v['max_files_per_call']} файлов за вызов",
              f"последняя очистка: {when}"]
    if info["cloud"]:
        return _check("outbox", title, "warn", lines + [f"путь похож на облачную папку ({info['cloud']}): "
                                                        "синхронизация будет копировать и держать файлы"],
                      "перенеси перевалку в папку вне OneDrive/YandexDisk/Dropbox")
    return _check("outbox", title, "ok", lines)


def run(ctx: Ctx) -> dict:
    reg_check, reg = check_registry(ctx)
    procs = processes(ctx)
    st = storage(ctx, reg)
    ob = outbox_info(ctx, reg)
    checks = [check_rg(), check_tomlkit(), reg_check, check_paths(ctx, reg), check_desktop(ctx),
              check_processes(ctx, procs), *check_storage(st), check_outbox(ob)]
    return {"checks": checks, "processes": procs, "storage": st, "outbox": ob}


STATUS = {"ok": "OK  ", "warn": "ВНИМ", "fail": "ОШИБ", "info": "инфо"}


def format_report(res: dict) -> str:
    out = []
    for c in res["checks"]:
        out.append(f"[{STATUS[c['status']]}] {c['title']}")
        out += [f"       {line}" for line in c["detail"]]
        if c["fix"]:
            out.append(f"       → {c['fix']}")
    worst = {s: sum(c["status"] == s for c in res["checks"]) for s in STATUS}
    out.append("")
    out.append(f"итог: ok {worst['ok']}, внимание {worst['warn']}, ошибки {worst['fail']}, инфо {worst['info']}")
    return "\n".join(out)


def main(config: Path, state: Path) -> int:
    from .core import Hub

    hub = Hub(config, state)
    res = run(Ctx(config=Path(config), state=Path(state), protected_overlap=hub.protected_overlap,
                  workspace_conflict=hub.workspace_conflict, mnt_root=hub.mnt_root))
    print(format_report(res))
    return 1 if any(c["status"] == "fail" for c in res["checks"]) else 0

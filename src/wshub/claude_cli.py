"""Запуск Claude Code для задачи wshub-code: argv, settings.json, mcp.json, окружение.

Ограничения задаёт хаб, а не текст промпта: профиль прав (allow/deny), песочница, хук на Bash, белый список
окружения. Ключи настроек сверены с Claude Code 2.1.291 и документацией от 2026-10-08 (ТЗ WSH-2, раздел 0.3).
Встроенные Read/Edit/Write песочница не ограничивает — только правила permissions, поэтому файловые
инструменты разрешены лишь с путём от рабочего каталога (и Read — по read_extra проекта), а служебные каталоги
и deny_read закрыты явно.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

from .registry import CodeGlobal, ProjectCode, billing_env
from .runtime import atomic_write_text

SUBAGENT_TOOL = "Task"  # так инструмент называется в init.tools у 2.1.291; в документации Agent — алиас
FILE_TOOLS = ("Read", "Edit", "Write", "NotebookEdit")  # Grep и Glob нет в init.tools у 2.1.291 (WSH-3.4)
RECON_BASH = ("git status *", "git log *", "git show *", "git diff *", "git grep *")
# git switch — нет (ТЗ WSH-3.8, круг 3, Р2): ветку меняет только wshub switch, хук git switch отклоняет; git checkout —
# для восстановления файлов (git checkout [<коммит>] -- <путь>), форму держит хук
TASK_BASH = ("git add *", "git commit *", "git branch *", "git rev-parse *", "git rev-list *",
             "git merge --ff-only *", "git checkout *")
# git remote на чтение (ТЗ WSH-3.4, А4.2): правила — удобство, настоящая граница — хук
GIT_REMOTE_READ = ("git remote", "git remote -v", "git remote get-url *")
GIT_REMOTE_DENY = tuple(f"git remote {s} *" for s in ("add", "set-url", "remove", "rm", "rename", "prune", "update",
                                                        "set-head", "set-branches", "show"))
# git на чтение и список файлов — всем профилям и фазе плана; ls и git ls-files вместо Glob (ревью WSH-3.4)
READ_BASH = (*RECON_BASH, *GIT_REMOTE_READ, "git ls-files *", "ls *")
# фильтры в конвейере (А4.1): всем профилям и фазе плана; sort и uniq — только task
FILTER_BASH = ("head *", "tail *", "wc *", "grep *", "cut *")
TASK_FILTER_BASH = ("sort *", "uniq *")
DENY_BASH = ("git push *", *GIT_REMOTE_DENY, "git clean *", "ssh *", "scp *", "sudo *", "curl *", "wget *")
# помощник хаба (А3): вне песочницы и только с разрешением задачи; пуш — только профилю task
HELPER_PUSH = "wshub push *"
HELPER_FORGE = "wshub forge *"
HELPER_GATE = "wshub gate *"  # гейты проекта (ТЗ WSH-3.5, Г2): профиль task, фаза исполнения
HELPER_SWITCH = "wshub switch *"  # смена ветки вне песочницы (ТЗ WSH-3.8, В4): профили task и review, исполнение
BASH_MAX_TIMEOUT_MS = 600000  # потолок таймаута Bash у claude по умолчанию (документация, env-vars)
AUTO_BASH_MAX_TIMEOUT_MS = 3600000  # режим auto (ТЗ WSH-3.15): долгие прогоны проекта не уходят в фон
ENV_PASS = ("HOME", "USER", "LOGNAME", "PATH", "LANG", "TZ", "DOCKER_HOST", "DOCKER_CONFIG")
# исполняемая часть git: Code её не меняет — иначе git хаба (вне песочницы) запустит его программу
GIT_PROTECTED = (".git/config", ".git/hooks", ".git/info", ".git/modules")
# настройки claude в рабочем каталоге: claude перечитывает их на ходу, в том числе apiKeyHelper и env — правка из
# задачи переключила бы оплату посреди сессии (ТЗ WSH-4.1, факт 6). До старта их проверяет jobs
CLAUDE_SETTINGS = (".claude/settings.json", ".claude/settings.local.json")
SANDBOX_DENY_READ = ("~/", "/mnt/")  # домашний каталог и диски Windows; нужное открывает allowRead
SANDBOX_ALLOW_READ = ("~/.gitconfig", "~/.config/git/")
RULE_UNSAFE = re.compile(r"[*?\[\]{}()\\]")  # путь с такими символами в правиле — шаблон, а не один файл
FLAGS_CACHE = "claude-flags.json"


class CliError(Exception):
    pass


def wshub_cmd() -> list[str]:
    """Как запустить этот же wshub из другого процесса: абсолютный путь к исполняемому файлу или python -m."""
    argv0 = Path(sys.argv[0]) if sys.argv and sys.argv[0] else None
    if argv0 is not None and argv0.name == "wshub" and argv0.is_file():
        return [str(argv0.absolute())]
    exe = shutil.which("wshub")
    if exe and Path(sys.executable).parent == Path(exe).parent:  # тот же venv
        return [exe]
    return [sys.executable, "-m", "wshub"]


def wshub_exe() -> str | None:
    """Исполняемый файл wshub хаба — для помощника в задаче; None, если хаб запущен как python -m wshub."""
    cmd = wshub_cmd()
    return cmd[0] if len(cmd) == 1 and os.path.isabs(cmd[0]) else None


def _abs_rule(p: str) -> str:
    return "/" + p if p.startswith("/") else p  # «//путь» — абсолютный путь в правилах Claude Code


def deny_read_abs(mask: str, cwd: Path) -> str:
    """Относительная маска deny_read — абсолютным путём от рабочего каталога задачи: сырую относительную строку
    песочница не применяет (стенд ТЗ WSH-3.5, С6). ~ и абсолютные — как есть."""
    if mask == "~" or mask.startswith("~/") or os.path.isabs(mask):
        return mask
    return os.path.normpath(os.path.join(cwd, mask))


def deny_paths(glob: CodeGlobal, proj: ProjectCode, protected: list[Path], home: Path, cwd: Path) -> list[str]:
    """Абсолютные пути, закрытые для встроенных файловых инструментов: deny_read (с раскрытым ~, относительные — от
    рабочего каталога) и служебные каталоги wshub. Симлинк даёт два пути: как записан и куда ведёт."""
    out: list[str] = []
    for raw in (*glob.deny_read, *proj.deny_read):
        raw = deny_read_abs(raw, cwd)
        p = Path(str(home) + raw[1:]) if raw == "~" or raw.startswith("~/") else Path(raw)
        for q in (Path(os.path.normpath(p)), p.resolve()):
            if str(q) not in out:
                out.append(str(q))
    for p in protected:
        if str(p) not in out:
            out.append(str(p))
    return out


def settings(profile: str, task_rel: str, glob: CodeGlobal, proj: ProjectCode, protected: list[Path],
             data_dirs: list[Path], hook_cmd: list[str], home: Path, cwd: Path, phase: str | None = None,
             read_extra: list[str] = (), grants: dict | None = None, gates: tuple = (),
             key_helper: str | None = None, switch: bool = False, mode: str = "sandbox") -> dict:
    """settings.json задачи. cwd — dir (рабочий каталог Code), task_rel — путь к ТЗ от него; protected — все
    служебные каталоги wshub (deny для файловых инструментов), data_dirs — реестр и состояние (их ищет хук).
    phase="plan" — фаза плана (ТЗ WSH-3, решение 5): отличие от эталона профиля — ровно plan_overrides.
    read_extra — абсолютные пути и шаблоны, проверенные jobs (ТЗ WSH-3.1, п. 2.2): Read вне cwd.
    grants — разрешения задачи {push, forge} (ТЗ WSH-3.4, А3.5): помощник в allow и excludedCommands — только с ними.
    gates — гейты, доступные звену (ТЗ WSH-3.5, Г2): wshub gate в allow и excludedCommands и потолок таймаута Bash.
    key_helper — команда apiKeyHelper задачи на ключе (ТЗ WSH-4.6, п. 2.3): сама команда, ключа в settings.json нет.
    switch — wshub switch в allow и excludedCommands профилям task и review в фазе исполнения (ТЗ WSH-3.8, В4).
    mode="auto" — у профиля task в фазе исполнения settings режима auto (auto_settings, ТЗ WSH-3.15); фаза плана
    и читающие профили — как в sandbox."""
    if RULE_UNSAFE.search(task_rel):
        raise CliError(f"путь к ТЗ {task_rel!r} содержит символы шаблона (*?[]{{}}()\\) — переименуй файл")
    if mode == "auto" and profile == "task" and phase is None:
        return auto_settings(glob, proj, protected, home, cwd, key_helper)
    allow = ["Read(./**)", *_read_extra_rules(read_extra), f"Edit(./{task_rel})",
             *(f"Bash({c})" for c in (*READ_BASH, *FILTER_BASH))]
    if profile == "task":
        allow += ["Edit(./**)", SUBAGENT_TOOL, *(f"Bash({c})" for c in (*TASK_BASH, *TASK_FILTER_BASH)),
                  *(f"Bash({c})" for c in proj.bash_allow)]
    elif profile == "review":
        allow += ["Edit(./.agents/**)", SUBAGENT_TOOL]
    elif profile != "recon":
        raise CliError(f"неизвестный профиль {profile}")
    gates = gates if profile == "task" and phase != "plan" else ()
    helper = helper_commands(profile, grants, bool(gates), switch and phase != "plan")
    allow += [f"Bash({c})" for c in helper]
    deny = [*(f"Bash({c})" for c in DENY_BASH), "Read(**/.env*)", "WebFetch", "WebSearch",
            *(f"Edit({m})" for m in proj.deny_edit),
            *(f"Edit(./{g}{'' if g.endswith('config') else '/**'})" for g in GIT_PROTECTED),
            *(f"Edit(./{s})" for s in CLAUDE_SETTINGS), *_deny_path_rules(glob, proj, protected, home, cwd)]
    data: dict = {
        "permissions": {"allow": list(dict.fromkeys(allow)), "deny": list(dict.fromkeys(deny))},
        "sandbox": {
            "enabled": True,
            "failIfUnavailable": True,
            "allowUnsandboxedCommands": False,
            "autoAllowBashIfSandboxed": False,  # Bash — только по allow-правилам профиля
            # читающие профили никого не выпускают из песочницы (решение 9.2, правка 2026-10-14)
            # помощник хаба — вне песочницы при разрешении задачи (ТЗ WSH-3.4, А3.5): форму держит хук
            "excludedCommands": [*(proj.sandbox_exclude if profile == "task" else ()), *helper],
            "network": {"allowedDomains": [*glob.network, *proj.network]},
            "filesystem": {
                # служебные каталоги wshub — абсолютными путями тоже: при нестандартном WSHUB_STATE/CONFIG
                # вне ~ и /mnt их бы не закрыли ни ~/, ни /mnt/ (чтение через Bash в песочнице)
                "denyRead": list(dict.fromkeys([*(deny_read_abs(m, cwd) for m in (*glob.deny_read, *proj.deny_read)),
                                                *SANDBOX_DENY_READ,
                                                *(str(p) for p in protected)])),
                "allowRead": list(dict.fromkeys([str(cwd), *SANDBOX_ALLOW_READ, *proj.allow_read])),
                # у всех профилей — только исполняемая часть git (ТЗ WSH-3.1): весь cwd в denyWrite не даёт bwrap
                # смонтировать защищённый <cwd>/.gitconfig, и падает любой Bash. Запись читающих профилей
                # закрывают права (Edit, перенаправления) и хук (--output, -O)
                "denyWrite": [str(cwd / g) for g in GIT_PROTECTED],
            },
        },
        # хук запускается на каждую Bash-команду: python -m wshub.hook (~0.03 с) без тяжёлых импортов сервера
        "hooks": {"PreToolUse": [{"matcher": "Bash",
                                  "hooks": [{"type": "command",
                                             "command": shlex.join([*hook_cmd, "--profile",
                                                                    "plan" if phase == "plan" else profile,
                                                                    *map(str, data_dirs)])}]}]},
    }
    if proj.claude_md_excludes:
        data["claudeMdExcludes"] = list(proj.claude_md_excludes)
    if key_helper:
        data["apiKeyHelper"] = key_helper
    if gates:
        # гейт дольше потолка Bash ушёл бы в фон (Г2.4): потолок — наибольший timeout_s плюс минута
        data["env"] = {"BASH_MAX_TIMEOUT_MS": str(max(BASH_MAX_TIMEOUT_MS,
                                                      (max(g.timeout_s for g in gates) + 60) * 1000))}
    if phase == "plan":
        plan_overrides(data, read_extra)
    elif phase is not None:
        raise CliError(f"неизвестная фаза {phase}")
    return data


def _deny_path_rules(glob: CodeGlobal, proj: ProjectCode, protected: list[Path], home: Path, cwd: Path) -> list[str]:
    """Read и Edit закрыты для deny_read и служебных каталогов wshub — сам путь и всё под ним."""
    out = []
    for p in deny_paths(glob, proj, protected, home, cwd):
        r = _abs_rule(p)
        out += [f"Read({r})", f"Edit({r})", f"Read({r}/**)", f"Edit({r}/**)"]
    return out


def auto_settings(glob: CodeGlobal, proj: ProjectCode, protected: list[Path], home: Path, cwd: Path,
                  key_helper: str | None = None) -> dict:
    """settings.json задачи task в режиме auto, фаза исполнения (ТЗ WSH-3.15, п. 1.2.2): без песочницы и хука, allow
    пуст — решает классификатор auto; deny — только git push, служебные каталоги wshub и deny_read, настройки claude
    рабочего каталога (страж оплаты WSH-4.1), .env, WebFetch и WebSearch."""
    deny = ["Bash(git push)", "Bash(git push *)", *_deny_path_rules(glob, proj, protected, home, cwd),
            *(f"Edit(./{s})" for s in CLAUDE_SETTINGS), "Read(**/.env*)", "WebFetch", "WebSearch"]
    data: dict = {"permissions": {"allow": [], "deny": list(dict.fromkeys(deny))},
                  "env": {"BASH_MAX_TIMEOUT_MS": str(AUTO_BASH_MAX_TIMEOUT_MS)}}
    if proj.claude_md_excludes:
        data["claudeMdExcludes"] = list(proj.claude_md_excludes)
    if key_helper:
        data["apiKeyHelper"] = key_helper
    return data


def plan_overrides(data: dict, read_extra: list[str] = ()) -> None:
    """Фаза плана (PoC WSH-3, 0.3.2 и решение 5): в --permission-mode plan классификатор auto mode
    пропускал любые Bash-команды мимо allow профиля. Выключаем его; allow — чтение (с read_extra) и git на
    чтение (как recon, без Edit и bash_allow); песочница никого из себя не выпускает. denyWrite — как у
    профиля, исполняемая часть git (ТЗ WSH-3.1)."""
    data["useAutoModeDuringPlan"] = False
    data["permissions"]["disableAutoMode"] = "disable"
    data["permissions"]["allow"] = ["Read(./**)", *_read_extra_rules(read_extra),
                                    *(f"Bash({c})" for c in (*READ_BASH, *FILTER_BASH))]
    data["sandbox"]["excludedCommands"] = []


def helper_commands(profile: str, grants: dict | None, gates: bool = False, switch: bool = False) -> list[str]:
    """Правила помощника хаба по разрешениям задачи: пуш — только профилю task, API хостинга — любому; гейты — только
    профилю task, если у звена они есть (ТЗ WSH-3.5, Г2.1); смена ветки — профилям task и review без разрешений задачи
    (ТЗ WSH-3.8, В4)."""
    g = grants or {}
    return [*([HELPER_PUSH] if g.get("push") and profile == "task" else []),
            *([HELPER_FORGE] if g.get("forge") else []),
            *([HELPER_GATE] if gates and profile == "task" else []),
            *([HELPER_SWITCH] if switch and profile in ("task", "review") else [])]


def _read_extra_rules(read_extra: list[str]) -> list[str]:
    """Read(//<абсолютный путь или шаблон>) — read_extra проекта; в allowRead песочницы не идут."""
    return [f"Read({_abs_rule(p)})" for p in read_extra]


PERMISSION_MODES = ("dontAsk", "plan", "auto")


def argv(claude: str, prompt: str, *, job_dir: Path, model: str, effort: str, max_turns: int | None,
         budget_usd: float | None, flags: dict, session_id: str | None = None, resume: str | None = None,
         permission_mode: str = "dontAsk", name: str | None = None) -> list[str]:
    """name — имя сессии (--name, PoC WSH-3 0.3.4); permission_mode plan — только для фазы плана, auto — задача task
    проекта в режиме auto (ТЗ WSH-3.15).
    max_turns и budget_usd равны None — флаг не передаётся: у claude умолчания нет, лимита нет (WSH-4.7)."""
    if (session_id is None) == (resume is None):
        raise CliError("нужен ровно один из session_id и resume")
    if permission_mode not in PERMISSION_MODES:
        raise CliError(f"режим прав {permission_mode} не поддерживается")
    out = [claude, "-p", prompt, "--output-format", "stream-json", "--verbose"]
    out += ["--session-id", session_id] if session_id else ["--resume", str(resume)]
    if name:
        out += ["--name", name]
    out += ["--model", model, "--effort", effort]
    if max_turns is not None:
        out += ["--max-turns", str(max_turns)]
    if budget_usd is not None:
        out += ["--max-budget-usd", f"{budget_usd:g}"]
    out += ["--permission-mode", permission_mode]
    if flags.get("permission_prompts"):
        out += ["--permission-prompts", "none"]
    out += ["--settings", str(job_dir / "settings.json")]
    if flags.get("setting_sources"):
        out += ["--setting-sources", "project,local"]
    out += ["--strict-mcp-config", "--mcp-config", str(job_dir / "mcp.json")]
    return out


def environment(environ: dict, pass_env: tuple[str, ...]) -> dict:
    """Белый список, а не наследование. Секретов нет даже из pass_env (реестр такие имена не пропускает), как и
    переменных, меняющих источник оплаты (ТЗ WSH-4.1, п. 2.3: реестр о них только предупреждает)."""
    names = set(ENV_PASS) | {k for k in environ if k.startswith("LC_")} | set(pass_env)
    env = {k: v for k, v in environ.items() if k in names and not _secret_name(k) and not billing_env(k)}
    env["TERM"] = "dumb"
    return env


def _secret_name(name: str) -> bool:
    return name == "SSH_AUTH_SOCK" or re.search(r"TOKEN|SECRET|PASSWORD|KEY", name, re.IGNORECASE) is not None


TMP_DEFAULT = "/tmp"


def _uid() -> int:
    return os.getuid()


def tmpdir_problem(env: dict) -> str | None:
    """Временный каталог claude (<CLAUDE_CODE_TMPDIR или /tmp>/claude-<uid>, ТЗ WSH-3, решение 8): нет — ок;
    есть — должен принадлежать текущему пользователю и быть доступен на запись, иначе claude не стартует.
    env — окружение, с которым запустится claude (белый список)."""
    uid = _uid()
    p = Path(env.get("CLAUDE_CODE_TMPDIR") or TMP_DEFAULT) / f"claude-{uid}"
    try:
        st = p.lstat()
    except FileNotFoundError:
        return None
    except OSError as e:
        return f"временный каталог claude {p} недоступен ({e})"
    import stat
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != uid or not os.access(p, os.W_OK | os.X_OK):
        return (f"временный каталог claude {p} принадлежит uid {st.st_uid} (нужен {uid}) или недоступен "
                f"на запись — claude откажется стартовать; исправь: sudo rm -rf {shlex.quote(str(p))}")
    return None


def resolve_claude(claude: str, env: dict) -> str:
    exe = claude if os.path.isabs(claude) else shutil.which(claude, path=env.get("PATH"))
    if not exe or not os.access(exe, os.X_OK):
        raise CliError(f"claude не найден ({claude}): поставь Claude Code или задай [code].claude")
    return exe


def detect_flags(claude: str, cache_dir: Path, env: dict) -> dict:
    """Есть ли у установленного claude необязательные флаги; результат кэшируется по версии."""
    def run(*args: str) -> str:
        try:
            r = subprocess.run([claude, *args], capture_output=True, text=True, timeout=30, env=env,
                               stdin=subprocess.DEVNULL, check=False)
        except (OSError, subprocess.TimeoutExpired) as e:
            raise CliError(f"claude {' '.join(args)} не выполнился: {e}") from None
        return r.stdout + r.stderr

    out = run("--version").strip()
    version = out.splitlines()[0] if out else "?"
    cache = cache_dir / FLAGS_CACHE
    try:
        data = json.loads(cache.read_text(encoding="utf-8"))
        data = data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        data = {}
    hit = data.get(claude)
    if isinstance(hit, dict) and hit.get("version") == version and version != "?":
        return hit
    help_text = run("--help")
    res = {"version": version, "permission_prompts": "--permission-prompts" in help_text,
           "setting_sources": "--setting-sources" in help_text}
    data[claude] = res
    try:
        atomic_write_text(cache, json.dumps(data, ensure_ascii=False, indent=1))
    except OSError:
        pass
    return res


def write_job_files(job_dir: Path, settings_data: dict, prompt: str) -> None:
    atomic_write_text(job_dir / "settings.json", json.dumps(settings_data, ensure_ascii=False, indent=2) + "\n")
    atomic_write_text(job_dir / "mcp.json", json.dumps({"mcpServers": {}}) + "\n")
    atomic_write_text(job_dir / "prompt.txt", prompt)

"""Задачи Claude Code: хранилище, старт, снимки, отмена, resume (ТЗ WSH-2, п. 2.5); политика выбора модели,
фазы плана и исполнения одной сессии, цепочка звеньев, статистика (ТЗ WSH-3); снимок git на старте звена
и блок «Вне кода» в итоге (ТЗ WSH-3.3).

Логика не зависит от MCP и проверяется тестами напрямую. Общее с файловым сервером берётся из Hub:
журнал (`_audit`), служебные каталоги (`protected_kind`), `runtime.pid_alive`/`proc_start`.
"""
from __future__ import annotations

import contextlib
import fnmatch
import hashlib
import json
import math
import os
import secrets
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from . import claude_cli, code_policy, gitsafe, taskfile
from .core import WsError
from .registry import ProjectCode, RegistryError, billing_env
from .runtime import atomic_write_text, pid_alive, proc_start, read_json

B32 = "abcdefghijklmnopqrstuvwxyz234567"
TASKFILE_MAX = 1024 * 1024
SETTINGS_MAX = 1024 * 1024  # байт: .claude/settings*.json крупнее — отказ до старта (ТЗ WSH-4.1, 7.1)
MODELS = ("haiku", "sonnet", "opus", "fable")
EFFORTS = ("low", "medium", "high", "xhigh", "max")
LIVE = ("starting", "prefetch", "running", "cancelling", "orphaned")
# plan_ready — фаза плана завершена и ждёт code_approve/code_replan: терминально для звена, не для цепочки
TERMINAL = ("completed", "failed", "max_turns", "max_budget", "timeout", "cancelled", "lost", "plan_ready")
# сигнал отмены → пауза → эскалация (п. 2.5.4). Паузы можно укоротить env-переменными — только для тестов
CANCEL_SIGTERM_AFTER = 20
CANCEL_SIGKILL_AFTER = 10
GIT_START = "git_start.json"  # снимок git на старте звена, в каталоге звена (ТЗ WSH-3.3, п. 1.2)
COMMITS_MAX = 20
EXTERNAL_MAX = 10  # строк вызовов помощника каждого вида в блоке «Внешние действия»
DOCKER_MAX = 30
META_MAX = 30  # строк git_meta_changed и errors в блоке
HASH_MAX = 64 * 1024 * 1024  # файл в .git крупнее — по размеру и mtime, без чтения
# точки монтирования песочницы в рабочем каталоге: bwrap монтирует /dev/null на месте несуществующих защищённых путей,
# git status видит их неотслеживаемыми, git add -A падает. Хаб прячет их помеченным блоком в .git/info/exclude (ТЗ
# WSH-3.4, А4.4). Список — журнал WSH-3.1 плюс пути под .claude/, найденные на стенде WSH-3.4 (git status --ignored
# изнутри песочницы claude 2.1.291)
SANDBOX_MOUNTS = (".bash_profile", ".bashrc", ".gitconfig", ".gitmodules", ".idea", ".mcp.json", ".profile",
                  ".ripgreprc", ".vscode", ".zprofile", ".zshrc",
                  ".claude/agents", ".claude/commands", ".claude/hooks", ".claude/launch.json", ".claude/loop.md",
                  ".claude/output-styles", ".claude/routines", ".claude/scheduled_tasks.json", ".claude/settings.json",
                  ".claude/settings.local.json", ".claude/skills", ".claude/workflows")
EXCLUDE_BEGIN = "# >>> wshub-code: точки монтирования песочницы (блок пишет хаб, не править)"
EXCLUDE_END = "# <<< wshub-code"
# блок «От хаба» в конце промпта (ТЗ WSH-3.5, П1–П2)
HUB_BLOCK_MAX = 2048  # байт на весь блок
HUB_BLOCK_GATES = 10
HUB_BLOCK_HIDDEN = 20
HUB_BLOCK_CMD = 100  # символов команды гейта в строке блока
HUB_BLOCK_PATH = 200  # символов пути в строке блока (круг 2, Р8)
HIDDEN_JOB_MAX = 200  # путей sandbox_hidden в job.json
GATES_PYTHON_M = "гейты недоступны: хаб запущен как python -m wshub"
# гейт (ТЗ WSH-3.5, Г3): pid, pgid и proc_start идущего гейта — в каталоге звена; SIGTERM группе → пауза → SIGKILL
GATE_PID = "gate.pid"
GATE_KILL_AFTER = 10
META_WARNING = ("ВНИМАНИЕ: задача изменила .git/hooks или .git/config. Эти правки не видны в диффе и выполнятся "
                "твоим git — проверь их перед git-командами в этом репозитории")


def cancel_timers() -> tuple[float, float]:
    """(пауза до SIGTERM, пауза до SIGKILL), секунд. WSHUB_CODE_CANCEL_* — тестовое переопределение."""
    return (float(os.environ.get("WSHUB_CODE_CANCEL_SIGTERM_S", CANCEL_SIGTERM_AFTER)),
            float(os.environ.get("WSHUB_CODE_CANCEL_SIGKILL_S", CANCEL_SIGKILL_AFTER)))


class JobError(WsError):
    pass


# режим оплаты задачи wshub-code (ТЗ WSH-4.6, п. 2.1): подписка — по умолчанию; key — API-ключ anthropic хаба
BILLING = "subscription"
BILLINGS = ("subscription", "key")
KEY_PROVIDER = "anthropic"
LIMIT_IGNORED = "лимит из задачи не применён: на подписке лимиты задаёт только оператор в реестре"
SOURCE = {"subscription": "none", "key": "apiKeyHelper"}  # ожидаемый init.apiKeySource режима (п. 2.4.1)


def billing_mode(d: dict) -> str:
    """Режим оплаты задачи: billing.mode в job.json (решение 5.1.1) или billing строкой (строки list и stats); у задач
    до WSH-4.6 — подписка."""
    b = d.get("billing")
    mode = b.get("mode") if isinstance(b, dict) else b
    return mode if mode in BILLINGS else BILLING


def cost_shown(d: dict) -> bool:
    """Показывать ли сумму в долларах (ТЗ WSH-4.6, п. 2.6.2): на ключе — да; на подписке — нет, кроме задачи,
    остановленной не по подписке (billing_mismatch) или по отклонённому ключу (key_rejected)."""
    return billing_mode(d) == "key" or d.get("reason") in ("billing_mismatch", "key_rejected")


@dataclass(frozen=True)
class Limits:
    """Действующие лимиты расхода задачи; None — лимита нет."""
    max_turns: int | None = None
    max_budget_usd: float | None = None
    daily_budget_usd: float | None = None
    max_hours: float | None = None
    plan_max_turns: int | None = None


def effective_limits(proj: ProjectCode, billing: str) -> Limits:
    """Единственное место решения, какие лимиты действуют (WSH-4.7). На подписке — только записанное оператором
    в реестре, остальное — «нет лимита». На ключе — так же: деньги только те, что задал оператор, умолчаний нет
    (ТЗ WSH-4.6, упрощение 5.2)."""
    if billing not in BILLINGS:
        raise JobError(f"режим оплаты {billing!r} в wshub-code не поддерживается: {' или '.join(BILLINGS)}")
    return Limits(proj.max_turns, proj.max_budget_usd, proj.daily_budget_usd, proj.max_hours, proj.plan_max_turns)


def new_id(now: float | None = None) -> str:
    t = datetime.fromtimestamp(now if now is not None else time.time())
    tail = "".join(secrets.choice(B32) for _ in range(6))
    return f"{t:%Y%m%d-%H%M%S}-{tail}"


@dataclass
class Plan:
    """Проверенные параметры звена задачи: готово к записи и запуску."""
    project: str
    root: Path
    dir: Path
    task: Path
    task_rel: str
    profile: str
    prompt_name: str
    model: str
    effort: str
    max_turns: int | None  # None — лимита нет, флаг claude не передаётся (WSH-4.7)
    budget_usd: float | None
    prompt: str
    warnings: list[str]
    proj: ProjectCode
    ticket: str = ""
    size: str | None = None
    risk: str | None = None
    choice: code_policy.Choice | None = None
    phase: str | None = None  # None — без плана; plan | execute — звено цепочки (ТЗ WSH-3, 2.3)
    plan_mode: str = "no"
    budget_total: float | None = None  # бюджет всей задачи (цепочки); budget_usd — этого звена
    max_turns_total: int | None = None
    grants: dict | None = None  # разрешения задачи {push, forge, reason} (ТЗ WSH-3.4, А2)
    gates: tuple = ()  # гейты задачи профиля task; звену плана правила не даются (ТЗ WSH-3.5, Г2)
    sandbox_hidden: list[str] | None = None  # отслеживаемые файлы, скрытые песочницей (ТЗ WSH-3.5, П2)
    billing: str = BILLING  # режим оплаты; у звеньев цепочки — режим корня (ТЗ WSH-4.6, п. 2.1.1)

    @property
    def permission_mode(self) -> str:
        return "plan" if self.phase == "plan" else "dontAsk"

    def session_name(self) -> str:
        return f"{self.project}/{self.ticket}" + ("/plan" if self.phase == "plan" else "")


def billing_settings_problem(d: Path) -> tuple[str, bool] | None:
    """Ядро проверки настроек claude (ТЗ WSH-4.1, п. 2.1 и 7.1): .claude/settings.json и settings.local.json рабочего
    каталога (claude читает их только оттуда) не задают apiKeyHelper и в env — имена, меняющие источник оплаты. Файла
    нет — пропуск; пустой (0 байт или одни пробелы) — настроек нет: так остаётся точка монтирования песочницы. Не
    обычный файл (после симлинка), больше SETTINGS_MAX, не JSON-объект — хаб не может проверить, что в нём.
    Ответ: None — чисто; (текст, False) — «<файл> задаёт <ключ>»; (текст, True) — не проверить. Не бросает: её же
    повторяет надзиратель посреди звена (ревью круга 1, п. 1)."""
    def unverifiable(p: Path, why: str) -> tuple[str, bool]:
        return f"{p} {why}: хаб не может проверить, что в нём", True

    for name in claude_cli.CLAUDE_SETTINGS:
        p = d / name
        try:
            fd = os.open(p, os.O_RDONLY | os.O_NONBLOCK)  # FIFO не повесит старт: открытие не ждёт писателя
        except (FileNotFoundError, NotADirectoryError):
            continue
        except OSError as e:
            return unverifiable(p, f"не открывается ({e.strerror})")
        try:  # тип — по сырому дескриптору: fdopen на каталоге сам бросает IsADirectoryError
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                return unverifiable(p, "— не обычный файл (каталог, устройство, FIFO)")
            with os.fdopen(fd, "rb", closefd=False) as fh:
                data = fh.read(SETTINGS_MAX + 1)
        except OSError as e:
            return unverifiable(p, f"не читается ({e.strerror})")
        finally:
            os.close(fd)
        if len(data) > SETTINGS_MAX:
            return unverifiable(p, f"больше {SETTINGS_MAX // 1024} КБ")
        try:
            text = data.decode("utf-8")
            obj = json.loads(text) if text.strip() else {}
        except (ValueError, RecursionError) as e:
            return unverifiable(p, f"не разбирается как JSON ({e})")
        if not isinstance(obj, dict):
            return unverifiable(p, "— не JSON-объект")
        env = obj.get("env")
        keys = (["apiKeyHelper"] if "apiKeyHelper" in obj else []) + \
            [f"env.{k}" for k in (env if isinstance(env, dict) else {}) if billing_env(k)]
        if keys:
            return f"{p} задаёт {keys[0]}", False
    return None


def check_billing_settings(d: Path, billing: str = BILLING) -> None:
    """Проверка до старта (ТЗ WSH-4.1, п. 2.1): нарушение или непроверяемый файл — JobError. Действует в обоих
    режимах оплаты: на ключе ключ задаёт только хаб через --settings (ТЗ WSH-4.6, п. 2.4.2)."""
    problem = billing_settings_problem(d)
    if problem:
        msg, unsure = problem
        if billing == "key":
            raise JobError(f"{msg}, — хаб не может проверить, чем будет оплачена задача на ключе" if unsure
                           else f"задача на ключе: {msg} — ключ задаёт только хаб (--settings задачи)")
        raise JobError(f"{msg}, — задача могла бы пойти не по подписке" if unsure
                       else f"задача пошла бы не по подписке: {msg}")


def _order(value: str, order: tuple[str, ...], name: str) -> int:
    try:
        return order.index(value)
    except ValueError:
        raise JobError(f"{name}: одно из {', '.join(order)}") from None


class Jobs:
    def __init__(self, hub):
        self.hub = hub
        self.root = hub.state_dir / "jobs"
        self.lock_path = self.root / ".lock"

    # ---------- проверка и план ----------

    def _project(self, name: str):
        """(Workspace, ProjectCode) для проекта с code.enabled, иначе отказ. Ошибка в [code] — отказ wshub-code."""
        reg = self.hub.registry.get()
        if reg.code.error:
            raise JobError(f"секция [code] в реестре с ошибкой: {reg.code.error}")
        ws = reg.workspaces.get(name)
        if ws is None:
            raise JobError(f"проект {name} не найден в реестре")
        if name in reg.code.project_errors:
            raise JobError(f"[workspace.{name}.code] с ошибкой: {reg.code.project_errors[name]}")
        proj = reg.code.projects.get(name)
        if proj is None or not proj.enabled:
            raise JobError(f"у проекта {name} не включён запуск Claude Code (code.enabled)")
        return ws, proj, reg.code.glob

    def _dirs(self, root: Path, proj: ProjectCode) -> list[Path]:
        """Каталоги запуска из code.dirs: внутри корня, существуют, строго без пересечения со служебными
        каталогами wshub. Нарушение — ошибка реестра для wshub-code."""
        out = []
        for d in proj.dirs:
            p = (root / d).resolve()
            if not (p == root or p.is_relative_to(root)):
                raise RegistryError(f"code.dirs: {d} вне корня проекта {root}")
            if not p.is_dir():
                raise RegistryError(f"code.dirs: каталога нет — {p}")
            if self.hub.protected_kind(p) is not None:
                raise RegistryError(f"code.dirs: {p} пересекается со служебным каталогом wshub")
            # каждый dir — корень рабочего дерева git (замечание 3): иначе allowRead открывает только cwd,
            # а .git остаётся в родителе под закрытым ~/, и git у Code не работает
            if not (p / ".git").is_dir():
                raise RegistryError(f"code.dirs: {p} — не корень git-репозитория (нет каталога .git); "
                                    "каждый dir должен быть рабочим деревом git")
            out.append(p)
        return out

    def _read_extra(self, root: Path, proj: ProjectCode) -> list[str]:
        """read_extra для Read(//…) (ТЗ WSH-3.1, п. 2.2): база — компоненты пути до первого с «*», после
        resolve() — внутри корня и без пересечения со служебными каталогами wshub; правило — разрешённая база
        плюс хвост шаблона. Хвост раскрывается по ФС, один уровень на компонент, и каждое совпадение после
        resolve() проверяется так же, как база (симлинк наружу в хвосте). Нарушение — ошибка реестра для
        wshub-code. Файлы, появившиеся после старта, проверка не видит."""
        def checked(e: str, p: Path) -> Path:
            try:
                q = p.resolve()
            except (OSError, RuntimeError) as err:  # цикл симлинков и т. п.
                raise RegistryError(f"code.read_extra: {e} — путь {p} не разрешается ({err})") from None
            if not (q == root or q.is_relative_to(root)):
                via = "" if p == q else f" ({p} → {q})"
                raise RegistryError(f"code.read_extra: {e} вне корня проекта {root}{via}")
            if self.hub.protected_kind(q) is not None:
                raise RegistryError(f"code.read_extra: {e} пересекается со служебным каталогом wshub ({q})")
            return q

        out = []
        for e in proj.read_extra:
            parts = Path(e).parts
            i = next((k for k, x in enumerate(parts) if "*" in x), len(parts))
            base = checked(e, root.joinpath(*parts[:i]))
            if claude_cli.RULE_UNSAFE.search(base.as_posix()):  # иначе разрешённая база станет шаблоном в Read(…)
                raise RegistryError(f"code.read_extra: {e} ведёт в {base} — путь с символами шаблона")
            level = [base]
            for comp in parts[i:]:
                nxt = []
                for d in level:
                    if not d.is_dir():
                        continue
                    try:
                        names = sorted(c.name for c in d.iterdir())
                    except OSError as err:
                        raise RegistryError(f"code.read_extra: {e} — каталог {d} не читается ({err})") from None
                    nxt += [checked(e, d / n) for n in names if fnmatch.fnmatchcase(n, comp)]
                level = nxt
            out.append(base.joinpath(*parts[i:]).as_posix())
        return out

    def plan(self, project: str, task: str, *, model=None, effort=None, plan=None, chosen_by=None, reason=None,
             max_turns=None, budget_usd=None, note=None, prompt_override: str | None = None,
             inherit: dict | None = None, channel: str = "mcp", push: bool = False, forge: bool = False,
             billing: str | None = None) -> Plan:
        """Проверить задачу и выбрать модель/effort/план. chosen_by="operator" — аргументы model/effort/plan
        выбрал оператор (2.2.1); иначе это отступление чата. inherit — job.json родителя при resume.
        channel — откуда пришли аргументы: «cli» (флаги wshub code) или «mcp» (решение 12).
        push/forge — разрешения задачи по регламенту проекта (ТЗ WSH-3.4, А2).
        billing — режим оплаты (ТЗ WSH-4.6, п. 2.1): None — подписка или режим родителя."""
        ws, proj, glob = self._project(project)
        root = ws.path.resolve()
        dirs = self._dirs(root, proj)

        task_in = Path(task)
        task_abs = (task_in if task_in.is_absolute() else root / task_in).resolve()
        # «внутри dir» — до чтения файла (замечание 13): иначе моделью можно открыть любой читаемый файл
        container = next((d for d in dirs if task_abs == d or task_abs.is_relative_to(d)), None)
        if container is None:
            raise JobError(f"файл ТЗ {task} вне разрешённых каталогов (code.dirs) проекта {project}")
        if not task_abs.is_file():
            raise JobError(f"файл ТЗ не найден: {task}")
        try:
            size = task_abs.stat().st_size
        except OSError as e:
            raise JobError(f"файл ТЗ недоступен: {e}") from None
        if size > TASKFILE_MAX:
            raise JobError(f"файл ТЗ больше 1 МБ ({size} байт)")
        header = taskfile.parse_header(task_abs.read_text(encoding="utf-8"))

        dir_abs = (root / header["dir"]).resolve()
        if dir_abs not in dirs:
            raise JobError(f"dir из шапки ({header['dir']}) не в code.dirs проекта {project}")
        if dir_abs != container:
            raise JobError(f"файл ТЗ {task_abs} лежит вне своего dir {dir_abs}")
        task_rel = task_abs.relative_to(dir_abs).as_posix()
        profile = header["profile"]
        if profile not in proj.profiles:
            raise JobError(f"профиль {profile} не разрешён проекту (profiles = {', '.join(proj.profiles)})")
        if chosen_by not in (None, "", "operator", "chat"):
            raise JobError("chosen_by — \"operator\" (выбор оператора) или пусто")
        if channel not in ("cli", "mcp"):
            raise JobError(f"канал выбора {channel!r} не поддерживается")

        grants, gwarn = task_grants(project, proj, profile, push, forge, reason,
                                    (inherit or {}).get("grants"))
        mode = self._billing(billing, inherit, channel, reason)
        lim = effective_limits(proj, mode)
        explicit = channel == "cli"  # флаги терминала — ручной выбор оператора (ТЗ WSH-4.6, п. 2.6.1)
        asked_turns, asked_budget = max_turns, budget_usd
        max_turns, budget_usd, limit_warn = self._task_limits(lim, header, max_turns, budget_usd, inherit, explicit)
        self._check_limits(lim, max_turns, budget_usd, free_turns=explicit and bool(asked_turns),
                           free_budget=explicit and asked_budget is not None and mode == BILLING)
        if inherit is None:
            missing = taskfile.missing_for_profile(header)
            if missing:
                rows = "; ".join(code_policy.profile_rows(glob, proj, profile))
                raise JobError(f"в шапке нет {', '.join(missing)} — по ним политика выбирает модель, effort "
                               f"и нужен ли план (профиль {profile}: {rows}). {code_policy.GUIDE}")
            choice = self._choose(glob, proj, header, profile, model, effort, plan, chosen_by, reason, channel)
        else:
            choice = self._inherit(proj, inherit, model, effort, chosen_by, reason, channel)

        # промпт — только из шаблона (п. 2.3, 8): имя из override (resume) или из шапки, литерального текста нет
        prompt_name = prompt_override or header.get("prompt") or profile
        prompt, warnings, note_left = self._render(root, proj, prompt_name, header["ticket"], task_rel, note)
        gates = link_gates(proj, profile, warnings)
        hidden = self._hidden(dir_abs, glob, proj, warnings)
        prompt += hub_block(note_left, gates, "plan" if choice.plan != "no" else None, hidden, prompt)
        out = Plan(project, root, dir_abs, task_abs, task_rel, profile, prompt_name, choice.model, choice.effort,
                   max_turns, budget_usd, prompt, warnings + gwarn + limit_warn, proj, ticket=header["ticket"],
                   size=header.get("size"), risk=header.get("risk"), choice=choice, plan_mode=choice.plan,
                   budget_total=budget_usd, max_turns_total=max_turns, grants=grants, gates=gates,
                   sandbox_hidden=hidden, billing=mode)
        if choice.plan != "no":
            # шаблон исполнения проверяем на старте, а не после оплаченной фазы плана (2.6.3)
            self._render(root, proj, proj.execute_prompt, header["ticket"], task_rel,
                         None if choice.plan == "auto" else "проверка шаблона", what="исполнения")
            if choice.plan == "review":  # переделка плана возможна только при review — шаблон нужен заранее
                self._replan_template(root, proj)
            out.phase = "plan"
            out.model, out.effort = choice.plan_model, choice.plan_effort
            if budget_usd is not None:  # доля бюджета — только при заданном бюджете
                out.budget_usd = _cents(budget_usd * proj.plan_budget_share)
                if out.budget_usd <= 0:
                    raise JobError(f"бюджет фазы плана {budget_usd:g} × {proj.plan_budget_share:g} меньше цента")
            out.max_turns = _least(lim.plan_max_turns, max_turns)
        return out

    def _billing(self, billing: str | None, inherit: dict | None, channel: str, reason) -> str:
        """Режим оплаты звена (ТЗ WSH-4.6, п. 2.1): у продолжения — режим родителя, сменить его нельзя; key из чата —
        только с reason (≥ 15 символов), из терминала — выбор оператора. Разрешение проекта и ключ проверяет start."""
        if billing is not None and billing not in BILLINGS:
            raise JobError(f"billing: {' или '.join(BILLINGS)}")
        if inherit is not None:
            mode = billing_mode(inherit)
            if billing is not None and billing != mode:
                raise JobError(f"сменить режим оплаты внутри цепочки нельзя: задача {inherit.get('id')} идёт в режиме "
                               f"{mode}, а запрошен {billing}")
            return mode
        mode = billing or BILLING
        if mode == "key" and channel == "mcp" and len(code_policy.clean_reason(reason)) < code_policy.REASON_MIN:
            raise JobError(f"задача на ключе из чата — только с reason (≥ {code_policy.REASON_MIN} символов): кто "
                           "разрешил тратить ключ и когда, например «оператор в чате 20.10: разведка на ключе»")
        return mode

    def _task_limits(self, lim: Limits, header: dict, max_turns, budget_usd, inherit: dict | None,
                     explicit: bool = False):
        """(ходы, бюджет, предупреждения) задачи. Потолок не задан — лимит из задачи не применяется, а переданный
        в этом вызове или в шапке даёт одну строку LIMIT_IGNORED; унаследованный от родителя (resume) молча
        отбрасывается. Потолок задан — прежний порядок: аргумент, родитель, шапка, потолок (WSH-4.7, п. 2.3.2).
        explicit — аргументы пришли флагами терминала: это ручной выбор оператора, он применяется и без потолка
        (ТЗ WSH-4.6, п. 2.6.1); потолки проверяет _check_limits."""
        parent = inherit or {}
        asked_budget = budget_usd if budget_usd is not None else header.get("budget_usd")
        ignored = False
        turns = budget = None
        if explicit and max_turns:
            turns = int(max_turns)
        elif lim.max_turns is None:
            ignored = bool(max_turns or header.get("max_turns"))
        else:
            turns = int(max_turns or parent.get("max_turns") or header.get("max_turns") or lim.max_turns)
        if explicit and budget_usd is not None:
            budget = float(budget_usd)
        elif lim.max_budget_usd is None:
            ignored = ignored or asked_budget is not None
        else:
            budget = float(budget_usd if budget_usd is not None else
                           parent["budget_usd"] if parent.get("budget_usd") is not None else
                           header.get("budget_usd", lim.max_budget_usd))
        return turns, budget, [LIMIT_IGNORED] if ignored else []

    def _replan_template(self, root: Path, proj: ProjectCode) -> None:
        path = (root / proj.prompts / f"{REPLAN_PROMPT}.md").resolve()
        if not path.is_file():
            raise JobError(f"шаблон промпта переделки плана не найден: {proj.prompts}/{REPLAN_PROMPT}.md")
        if "{note}" not in path.read_text(encoding="utf-8"):
            raise JobError(f"в шаблоне {proj.prompts}/{REPLAN_PROMPT}.md нет {{note}} — замечание к плану обязательно")

    def _render(self, root: Path, proj: ProjectCode, name: str, ticket: str, task_rel: str, note,
                what: str = "") -> tuple[str, list[str], str | None]:
        """(промпт, предупреждения, note для блока «От хаба» — если в шаблоне нет {note}; ТЗ WSH-3.5, П3)."""
        if not taskfile.NAME_RE.match(name):
            raise JobError(f"имя шаблона промпта недопустимо: {name}")
        template_path = (root / proj.prompts / f"{name}.md").resolve()
        if not template_path.is_file():
            raise JobError(f"шаблон промпта{' ' + what if what else ''} не найден: {proj.prompts}/{name}.md")
        template = template_path.read_text(encoding="utf-8")
        prompt, warnings = taskfile.render(template, ticket=ticket, task=task_rel, note=note)
        return prompt, warnings, None if taskfile.uses_note(template) else taskfile.check_note(note)

    def _hidden(self, workdir: Path, glob, proj: ProjectCode, warnings: list[str]) -> list[str]:
        """Скрытые песочницей отслеживаемые файлы (ТЗ WSH-3.5, П2); сбой git — предупреждение, списка нет."""
        hidden, problem = sandbox_hidden(workdir, [*glob.deny_read, *proj.deny_read], dict(os.environ))
        if problem:
            warnings.append(problem)
        return hidden

    def _choose(self, glob, proj, header, profile, model, effort, plan, chosen_by, reason,
                channel) -> code_policy.Choice:
        """Источники выбора (2.2): аргументы, затем шапка, затем политика; у каждого поля — свой источник
        и канал (решение 12)."""
        arg_src = "operator" if chosen_by == "operator" else "chat"
        head_src = "operator" if header.get("choice") == "operator" else "chat"
        given, chan = {}, {}
        for f, v in (("model", model), ("effort", effort), ("plan", plan)):
            if v:
                given[f], chan[f] = (v, arg_src), channel
            elif header.get(f):
                given[f], chan[f] = (header[f], head_src), "header"
        why = reason or header.get("choice_reason")
        try:
            c = code_policy.choose(glob, proj, profile, header.get("size"), header.get("risk"), given, why)
        except code_policy.PolicyError as e:
            raise JobError(str(e)) from None
        # каналы всех полей не из политики, а не только итогового источника (уточнение решения 12)
        c.channel = _channel(chan.values())
        c.choice_fields = {f: f"{src}:{chan[f]}" for f, (_, src) in given.items()}
        return c

    def _inherit(self, proj, parent: dict, model, effort, chosen_by, reason, channel) -> code_policy.Choice:
        """resume: выбор родителя; явные model/effort — по тем же правилам источника, что у code_start."""
        base = parent.get("choice") or {}
        vals = {"model": parent.get("model"), "effort": parent.get("effort")}
        fields = {"model": "inherited", "effort": "inherited", "plan": "inherited"}
        src = "operator" if chosen_by == "operator" else "chat"
        for f, v in (("model", model), ("effort", effort)):
            if v and v != vals[f]:
                vals[f], fields[f] = v, src
        changed = [f for f in ("model", "effort") if fields[f] in ("operator", "chat")]
        source, why, chan = base.get("source") or "policy", base.get("reason"), base.get("channel")
        cf = dict(base.get("choice_fields") or {})
        if changed:
            if src == "chat":
                try:
                    code_policy._check_bounds(proj, vals["model"] if "model" in changed else None,
                                              vals["effort"] if "effort" in changed else None, "чат")
                except code_policy.PolicyError as e:
                    raise JobError(code_policy._bounds_hint(str(e), "chat")) from None
                why = code_policy.clean_reason(reason)
                if len(why) < code_policy.REASON_MIN:
                    raise JobError(f"чат задал {', '.join(changed)} в обход выбора задачи: укажи причину "
                                   f"(reason, ≥ {code_policy.REASON_MIN} символов) или не меняй их")
            source = src
            cf.update({f: f"{src}:{channel}" for f in changed})
            chan = _channel(x.split(":", 1)[1] for x in cf.values())
            why = code_policy.clean_reason(reason) or why
        return code_policy.Choice(vals["model"], vals["effort"], "no", None, None, source, base.get("row"), why,
                                  fields, chan, cf or None)

    def _check_limits(self, lim: Limits, max_turns: int | None, budget_usd: float | None,
                      free_turns: bool = False, free_budget: bool = False):
        """Бюджеты и ходы — в силе для любого источника выбора (2.2.1); модель и effort проверяет политика.
        Незаданный лимит (None) не проверяется. free_* — флаг терминала: без потолка реестра (ТЗ WSH-4.6, п. 2.6.1);
        бюджет на ключе свободным не бывает — потолок ключа действует всегда."""
        if max_turns is not None:
            if not free_turns and lim.max_turns is not None and max_turns > lim.max_turns:
                raise JobError(f"max_turns {max_turns} выше потолка {lim.max_turns}")
            if max_turns < 1:
                raise JobError("max_turns должен быть ≥ 1")
        if budget_usd is not None:
            if not free_budget and lim.max_budget_usd is not None and budget_usd > lim.max_budget_usd:
                raise JobError(f"budget_usd {budget_usd:g} выше потолка {lim.max_budget_usd:g}")
            if budget_usd <= 0:
                raise JobError("budget_usd должен быть > 0")

    # ---------- хранилище ----------

    def job_dir(self, jid: str) -> Path:
        return self.root / jid

    def read(self, jid: str) -> dict:
        d = read_json(self.job_dir(jid) / "job.json")
        if not d:
            raise JobError(f"задача {jid} не найдена")
        return d

    def write(self, jid: str, data: dict) -> None:
        atomic_write_text(self.job_dir(jid) / "job.json", json.dumps(data, ensure_ascii=False, indent=1))

    @contextlib.contextmanager
    def _flock(self):
        self.root.mkdir(parents=True, exist_ok=True)
        with open(self.lock_path, "a") as fh:
            try:
                import fcntl
                fcntl.flock(fh, fcntl.LOCK_EX)
            except ImportError:  # pragma: no cover — не POSIX
                pass
            yield

    def _live_jobs(self, project: str | None = None) -> list[dict]:
        out = []
        for jd in sorted(self.root.glob("*/job.json")) if self.root.is_dir() else []:
            d = read_json(jd)
            if not d:
                continue
            if project and d.get("project") != project:
                continue
            if self.effective_state(d) in LIVE:
                out.append(d)
        return out

    def effective_state(self, d: dict) -> str:
        """Состояние с учётом живости процессов: orphaned (надзиратель мёртв, Code жив) и lost (оба мертвы,
        итога нет) вычисляются при чтении (п. 2.5.5)."""
        state = d.get("state", "lost")
        if state in TERMINAL:
            return state
        sup_alive = _alive(d.get("sup_pid"), d.get("sup_start"))
        code_alive = _alive(d.get("pid"), d.get("proc_start"))
        if sup_alive:
            return state
        if code_alive:
            return "orphaned"
        return state if d.get("result") else "lost"

    # ---------- старт ----------

    def start(self, plan: Plan, *, parent: str | None = None, resume: str | None = None,
              chain_root: str | None = None, link: tuple[str, str] | None = None, now: float | None = None) -> dict:
        """Записать и запустить звено. link = (id фазы плана, причина) — переход из plan_ready: под тем же flock
        проверяем, что план всё ещё plan_ready и не утверждён, и отмечаем его (code_approve/replan/auto)."""
        if plan.billing == "key":
            self._check_key(plan)  # до каталога задачи: отказ не оставляет файлов
        check_billing_settings(plan.dir, plan.billing)  # до каталога задачи: отказ не оставляет файлов (WSH-4.1)
        glob = self.hub.registry.get().code.glob
        read_extra = self._read_extra(plan.root, plan.proj)  # до каталога задачи: отказ не оставляет файлов
        # claude и его флаги (вызовы подпроцесса) — до flock: не трогают общее состояние, не держат лок
        claude = claude_cli.resolve_claude(glob.claude, dict(os.environ))
        env = claude_cli.environment(dict(os.environ), glob.pass_env)
        problem = claude_cli.tmpdir_problem(env)  # решение 8: иначе claude не стартует
        if problem:
            raise JobError(problem)
        flags = claude_cli.detect_flags(claude, self.hub.state_dir, env)
        grants = plan.grants or {}
        gates = plan.gates if plan.phase != "plan" else ()  # гейты — только фазе исполнения (Г1)
        if grants.get("push") or grants.get("forge") or gates:
            env = helper_env(env, plan.dir)
        if grants.get("forge"):  # файл токена — при старте задачи с доступом к API (А1.2)
            from .helper import HelperError, read_token
            try:
                read_token(plan.proj.forge_token or "")
            except HelperError as e:
                raise JobError(f"API хостинга: {e}") from None
        git_start = start_snapshot(plan.dir, dict(os.environ))  # тоже подпроцесс — до flock
        lim = effective_limits(plan.proj, plan.billing)
        with self._flock():
            if link is not None:
                ld = self.read(link[0])
                if (self.job_dir(link[0]) / "cancel").exists():
                    raise JobError(f"план {link[0]} отменён (code_cancel) — продолжения не будет")
                if ld.get("state") != "plan_ready" or ld.get("next"):
                    raise JobError(f"план {link[0]} в состоянии {ld.get('state')}"
                                   + (f", уже продолжен задачей {ld['next']}" if ld.get("next") else "")
                                   + " — нужен plan_ready")
            live = self._live_jobs(plan.project)
            if len(live) >= plan.proj.max_parallel:
                raise JobError(f"у проекта {plan.project} уже {len(live)} живых задач "
                               f"(max_parallel = {plan.proj.max_parallel})")
            self._check_dir_free(plan.dir)
            if lim.daily_budget_usd is not None:  # не задан — дневной бюджет не считается вовсе (WSH-4.7, п. 2.3.3)
                self._check_daily_budget(plan, live, now, lim.daily_budget_usd)

            jid = new_id(now)
            jdir = self.job_dir(jid)
            jdir.mkdir(parents=True, exist_ok=False)
            choice = plan.choice.to_dict() if plan.choice else None
            data = {
                "id": jid, "project": plan.project, "root": str(plan.root), "dir": str(plan.dir),
                "task": str(plan.task), "task_rel": plan.task_rel, "profile": plan.profile,
                "prompt_name": plan.prompt_name, "model": plan.model, "effort": plan.effort,
                "max_turns": plan.max_turns, "budget_usd": plan.budget_usd, "prefetch": plan.proj.prefetch,
                "max_hours": lim.max_hours, "status_regex": plan.proj.status_regex,
                "state": "starting", "reason": None, "parent": parent, "resume": resume,
                "session_id": resume, "warnings": plan.warnings,
                "created": datetime.fromtimestamp(now or time.time()).astimezone().isoformat(timespec="seconds"),
                "created_ts": now or time.time(),
                "ticket": plan.ticket, "size": plan.size, "risk": plan.risk, "choice": choice,
                "phase": plan.phase, "plan_mode": plan.plan_mode, "budget_total": plan.budget_total,
                "max_turns_total": plan.max_turns_total, "chain_root": chain_root or jid,
                "session_name": plan.session_name(), "grants": grants,
                "sandbox_hidden": (plan.sandbox_hidden or [])[:HIDDEN_JOB_MAX],
                # режим notify — на момент старта корня; звенья и resume берут его у корня (ТЗ WSH-3.4, В3.1)
                "notify": self._root_notify(chain_root) or glob.notify,
                "billing": {"mode": plan.billing},  # режим оплаты; init и rate допишет надзиратель (ТЗ WSH-4.6)
            }
            sid = resume or str(_uuid4())
            data["session_id"] = sid if not resume else resume
            data["claude"] = claude
            data["flags"] = flags
            protected = [p for p in (self.hub.protected_data + self.hub.protected_code)]
            hook_cmd = [sys.executable, "-m", "wshub.hook"]  # лёгкий запуск хука (замечание 9)
            # ключ — командой хаба, а не значением (ТЗ WSH-4.6, п. 2.3): claude запросит его сам
            key_helper = shlex.join([*claude_cli.wshub_cmd(), "code", "key-helper"]) if plan.billing == "key" else None
            settings = claude_cli.settings(plan.profile, plan.task_rel, glob, plan.proj, protected,
                                           self.hub.protected_data, hook_cmd, Path.home(), plan.dir,
                                           phase="plan" if plan.phase == "plan" else None, read_extra=read_extra,
                                           grants=grants, gates=gates, key_helper=key_helper)
            claude_cli.write_job_files(jdir, settings, plan.prompt)
            # помощнику — задача и хаб (не секреты); агент ssh — только в каталог задачи, Code туда не видит (А3)
            env = {**env, "WSHUB_JOB": jid, "WSHUB_CONFIG": str(self.hub.registry.path),
                   "WSHUB_STATE": str(self.hub.state_dir)}
            if grants.get("push") and os.environ.get("SSH_AUTH_SOCK"):
                atomic_write_text(jdir / "ssh_auth_sock", os.environ["SSH_AUTH_SOCK"])
            # резерв — до argv: при включённой зоне без бюджета звена бюджет claude — остаток зоны (ТЗ WSH-4.8, 2.1.2)
            rec = self._key_reserve(plan, jid, jdir, data) if plan.billing == "key" else None
            budget = plan.budget_usd if plan.budget_usd is not None else data["billing"].get("cap_usd")
            try:
                argv = claude_cli.argv(claude, plan.prompt, job_dir=jdir, model=plan.model, effort=plan.effort,
                                       max_turns=plan.max_turns, budget_usd=budget, flags=flags,
                                       session_id=None if resume else sid, resume=resume,
                                       permission_mode=plan.permission_mode, name=plan.session_name())
                atomic_write_text(jdir / "argv.json", json.dumps(argv, ensure_ascii=False))
                atomic_write_text(jdir / "env.json", json.dumps(env, ensure_ascii=False))
                atomic_write_text(jdir / GIT_START, json.dumps(git_start, ensure_ascii=False, indent=1))
                try:
                    write_exclude(plan.dir)
                except OSError as e:
                    data["warnings"] = [*data["warnings"], f".git/info/exclude не записан: {e}"]
                self.write(jid, data)

                sup = self._spawn_supervisor(jid)
                data["sup_pid"] = sup
                data["sup_start"] = proc_start(sup)
                self.write(jid, data)
            except BaseException:
                if rec and rec["reserve"] and not data.get("sup_pid"):  # надзиратель не запущен — резерв снят фактом 0
                    with contextlib.suppress(Exception):
                        self._key_api().code_settle(rec, 0.0, False)
                raise
            if link is not None:
                ld = self.read(link[0])
                ld.update(state="completed", reason=link[1], next=jid)
                self.write(link[0], ld)
        c = plan.choice
        self.hub._audit_code("code_start", plan.project, jid, params={
            "model": plan.model, "effort": plan.effort, "max_turns": plan.max_turns,
            "budget_usd": plan.budget_usd, "profile": plan.profile, "phase": plan.phase, "plan": plan.plan_mode,
            "model_source": c.source if c else None, **({"channel": c.channel} if c and c.channel else {}),
            "choice_fields": c.choice_fields or {} if c else None,
            "policy_row": c.row if c else None,
            "reason": c.reason if c else None, "parent": parent,
            "grants": {k: v for k, v in grants.items() if v}, "billing": plan.billing})
        return data

    def _check_dir_free(self, workdir: Path) -> None:
        """Одна живая задача на рабочий каталог (ТЗ WSH-3.8, К1): среди живых задач всех проектов и любого профиля нет
        задачи в том же каталоге — две задачи делили бы рабочее дерево и ветку. Сравнение — по realpath. Вызывается
        под flock старта."""
        real = os.path.realpath(workdir)
        for d in self._live_jobs():
            if d.get("dir") and os.path.realpath(d["dir"]) == real:
                raise JobError(f"в каталоге {workdir} уже идёт задача {d.get('id')} ({d.get('profile')}, "
                               f"{self.effective_state(d)}) — одна живая задача на каталог; дождись её (code_wait) "
                               "или останови (code_cancel)")

    def _key_api(self):
        from .llm.core import Api
        return Api(self.hub.registry.get(), self.hub.registry.path.parent, self.hub.state_dir)

    def _check_key(self, plan: Plan) -> None:
        """Задача на ключе (ТЗ WSH-4.6, п. 2.1.4; разрешения проекта нет — упрощение 5.2): ключ anthropic есть и с
        верными правами. Значение читается только для проверки и сразу отбрасывается; в отказ — текст keys без ключа.
        Хаб запущен как python -m wshub — отказ (ТЗ WSH-4.3, п. 2.2.4): apiKeyHelper выполняется вне песочницы с
        каталогом проекта в sys.path, и ./wshub/__init__.py, созданный задачей, получил бы ключ."""
        if not claude_cli.wshub_exe():
            raise JobError("задача на ключе: wshub запущен как «python -m wshub», а не исполняемым файлом — ключ "
                           "задаче не выдаётся; поставь wshub как программу (uv tool install) и перезапусти")
        from .llm import keys
        try:
            keys.read(keys.key_path(self.hub.registry.path.parent, KEY_PROVIDER))
        except keys.KeyProblem as e:
            raise JobError(f"задача на ключе: {e}") from None

    def _key_reserve(self, plan: Plan, jid: str, jdir: Path, data: dict) -> dict:
        """Потолок дня и зона — если их включил оператор (зона — included_usd или команда credits) — под блокировкой
        журнала трат, резерв звена — строкой с job (ТЗ WSH-4.6, п. 2.5.2–2.5.4, упрощение 5.2; ТЗ WSH-4.8, п. 2.1).
        Отказ — JobError с текстом 4.2, каталог звена удаляется: отказ не оставляет файлов. Бюджет по остатку зоны —
        billing.cap_usd; журнал не читается без денежных настроек — предупреждение задачи. Ответ — строка звена для
        факта по итогу (code_settle)."""
        from .llm.core import ApiError
        billing = data["billing"]
        try:
            rec, by_zone, cap, warn = self._key_api().code_reserve(KEY_PROVIDER, plan.project, plan.model,
                                                                   plan.budget_usd, jid)
        except (ApiError, OSError) as e:
            shutil.rmtree(jdir, ignore_errors=True)
            raise JobError(f"задача на ключе: {e}") from None
        billing.update(provider=KEY_PROVIDER, ledger=rec)
        if by_zone:  # зона — в строке оплаты, только если её проверяли (упрощение 5.2.4)
            billing["zone"] = rec["zone"]
        if rec["reserve"]:
            billing["reserve_usd"] = rec["cost_usd"]
        if cap is not None:
            billing["cap_usd"] = cap
        if warn:
            data["warnings"] = [*data["warnings"], warn]
        return rec

    def _root_notify(self, root: str | None) -> str | None:
        if not root:
            return None
        try:
            return self.read(root).get("notify")
        except JobError:
            return None

    def _check_daily_budget(self, plan: Plan, live: list[dict], now: float | None, daily_usd: float):
        day = datetime.fromtimestamp(now or time.time()).date()
        spent = 0.0
        seen = set()
        for d in live:
            spent += float(d.get("budget_usd") or 0)
            seen.add(d.get("id"))
        for jd in sorted(self.root.glob("*/job.json")) if self.root.is_dir() else []:
            d = read_json(jd)
            if not d or d.get("project") != plan.project or d.get("id") in seen:
                continue
            ts = d.get("created_ts")
            if not ts or datetime.fromtimestamp(ts).date() != day:
                continue
            cost = phase_cost(d)  # стоимость звена один раз: у resume total_cost_usd накопительный (решение 6)
            spent += float(cost if cost is not None else d.get("budget_usd") or 0)
        asked = plan.budget_usd or 0  # звено без бюджета (лимит бюджета задачи не задан) входит как 0
        if spent + asked > daily_usd:
            raise JobError(f"дневной бюджет проекта исчерпан: уже {spent:g} + {asked:g} > "
                           f"{daily_usd:g} (daily_budget_usd)")

    def _spawn_supervisor(self, jid: str) -> int:
        # cwd — каталог задачи: рабочий каталог сервера мог быть удалён (ревью WSH-3.4, п. 11)
        proc = subprocess.Popen([*claude_cli.wshub_cmd(), "code-run", jid], stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
                                env={**os.environ}, cwd=self.job_dir(jid))
        return proc.pid

    # ---------- снимки, список ----------

    def snapshot(self, jid: str, now: float | None = None) -> dict:
        d = self.read(jid)
        state = self.effective_state(d)
        now = now or time.time()
        elapsed = int(_end_ts(d, state, now) - (d.get("started") or d.get("created_ts") or now))
        snap = {"id": jid, "project": d.get("project"), "state": state, "reason": d.get("reason"),
                "task": d.get("task"), "profile": d.get("profile"), "model": d.get("model"),
                "elapsed_s": max(elapsed, 0), "session_id": d.get("session_id"), "parent": d.get("parent"),
                "result": d.get("result"), "warnings": d.get("warnings") or []}
        if d.get("error"):  # текст отказа хаба (prefetch, git_config_unsafe…) — чату, а не только код (ревью, п. 10)
            snap["error"] = d["error"]
        if d.get("reason") in REASON_HINT:
            snap["reason_hint"] = REASON_HINT[d["reason"]]
        if d.get("choice"):
            snap["choice"] = d["choice"]
        if d.get("phase"):
            snap.update(phase=d["phase"], plan_mode=d.get("plan_mode"), next=d.get("next"),
                        chain_error=d.get("chain_error"))
            if d.get("chain_root") == jid:
                # корень цепочки (2.3.6): состояние и фаза — активного звена, плюс все звенья
                links = self.chain(jid)
                active = links[-1]
                chain_state = self._chain_state(active)
                end = _end_ts(active, chain_state, now)  # время цепочки: от старта корня до конца активного звена
                snap.update(own_state=state, state=chain_state, phase=active.get("phase"),
                            active=active["id"], chain=[self._link_row(x) for x in links],
                            cost_usd=_sum(phase_cost(x) for x in links),
                            elapsed_s=max(int(end - (d.get("started") or d.get("created_ts") or end)), 0))
                if active.get("error"):
                    snap["error"] = active["error"]
                else:
                    snap.pop("error", None)
                if active["id"] == jid:  # корень перечитан в chain() — причина из того же прочтения, что и состояние
                    snap["reason"] = active.get("reason")
                    snap.pop("reason_hint", None)
                    if active.get("reason") in REASON_HINT:
                        snap["reason_hint"] = REASON_HINT[active["reason"]]
        return snap

    def _link_row(self, d: dict) -> dict:
        res = d.get("result") or {}
        return {"id": d["id"], "phase": d.get("phase"), "state": self.effective_state(d), "reason": d.get("reason"),
                "model": d.get("model"), "effort": d.get("effort"), "cost_usd": phase_cost(d),
                "turns": res.get("num_turns")}

    def _chain_state(self, active: dict) -> str:
        st = self.effective_state(active)
        if st == "plan_ready" and active.get("plan_mode") == "auto" and not active.get("chain_error") \
                and _alive(active.get("sup_pid"), active.get("sup_start")):
            return "starting"  # план готов, живой надзиратель сам запускает исполнение
        if st == "completed" and active.get("next"):
            return "starting"  # звено утверждено, следующее ещё не записано
        return st

    def chain_cached(self, root: str) -> list[dict]:
        """Звенья цепочки без обхода всех задач на каждом вызове (карточка опрашивает каждые 3 с; ТЗ WSH-3.4,
        В2.1): состав цепочки меняется только с новой задачей, а она меняет mtime каталога задач. Сами job.json
        звеньев читаются каждый раз."""
        try:
            stamp = self.root.stat().st_mtime_ns
        except OSError:
            return self.chain(root)
        cache = self.__dict__.setdefault("_chain_cache", {})
        hit = cache.get(root)
        if hit and hit[0] == stamp:
            out = [read_json(self.job_dir(i) / "job.json") for i in hit[1]]
            if all(out):
                return out
        links = self.chain(root)
        cache[root] = (stamp, [x["id"] for x in links])
        return links

    def chain(self, root: str) -> list[dict]:
        """Звенья цепочки по порядку создания: фаза плана, переделки плана, исполнение, resume."""
        out = []
        for jd in self.root.glob("*/job.json") if self.root.is_dir() else []:
            d = read_json(jd)
            if d and (d.get("chain_root") == root or d.get("id") == root):
                out.append(d)
        return sorted(out, key=lambda x: (x.get("created_ts") or 0, x.get("id")))

    def list(self, project: str | None = None, limit: int = 20) -> list[dict]:
        rows = []
        for jd in sorted(self.root.glob("*/job.json"), reverse=True) if self.root.is_dir() else []:
            d = read_json(jd)
            if not d or (project and d.get("project") != project):
                continue
            rows.append({"id": d.get("id"), "project": d.get("project"), "task": d.get("task"),
                         "state": self.effective_state(d), "cost_usd": phase_cost(d), "phase": d.get("phase"),
                         "billing": billing_mode(d), "reason": d.get("reason")})  # показ суммы — cost_shown
            if len(rows) >= limit:
                break
        return rows

    # ---------- отмена ----------

    def cancel(self, jid: str) -> dict:
        d = self.read(jid)
        target = d
        if d.get("phase") and d.get("chain_root") == jid:
            target = self.chain(jid)[-1]  # корень цепочки — отменяем активное звено (2.3.6)
        tid = target["id"]
        state = self.effective_state(target)
        if state == "plan_ready":
            # цепочка стоит на плане: отмена плана — отмена цепочки (исполнения не будет)
            with self._flock():
                t = self.read(tid)
                if t.get("state") == "plan_ready" and not t.get("next"):
                    t.update(state="cancelled", reason="cancel")
                    self.write(tid, t)
            return self.snapshot(jid)
        if state in TERMINAL:
            if (self.job_dir(tid) / GATE_PID).exists():
                # звено конечное (в том числе lost), а гейт ещё идёт: гасим в фоне, состояние не трогаем (круг 2, Р7)
                self._spawn_killer(tid, gate_only=True)
            return self.snapshot(jid)
        atomic_write_text(self.job_dir(tid) / "cancel", "1")
        if state == "orphaned":
            # надзиратель мёртв — гасим группу Code сами, но в фоне: code_cancel отвечает сразу (2.7, замечание 15)
            self._spawn_killer(tid)
        return self.snapshot(jid)

    def _spawn_killer(self, jid: str, gate_only: bool = False) -> None:
        """Гасим группу Code отдельным процессом `wshub code-kill`, а не os.fork: в MCP-сервере (многопоточный
        async) fork копирует удерживаемый flock-fd и заклинил бы лок. close_fds (по умолчанию) этого не даёт.
        gate_only — только гейт звена (`code-kill <job> --gate`), состояние звена не меняется."""
        subprocess.Popen([*claude_cli.wshub_cmd(), "code-kill", jid, *(["--gate"] if gate_only else [])],
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True, env={**os.environ}, cwd=self.job_dir(jid))

    def kill_orphaned(self, jid: str) -> dict:
        """Погасить группу Code осиротевшей задачи и записать cancelled (вызывается процессом `code-kill`)."""
        self._kill_group(self.read(jid))
        with self._flock():
            d = self.read(jid)
            # raw-состояние осиротевшей задачи — «running» (надзиратель умер, не обновив); effective_state дал бы
            # «lost», поэтому смотрим на записанное состояние и не трогаем настоящие терминальные итоги
            keep = {"completed", "failed", "max_turns", "max_budget", "timeout", "cancelled"}
            if d.get("state") not in keep:
                d["state"], d["reason"] = "cancelled", "orphaned"
                self.write(jid, d)
        return d

    def _kill_group(self, d: dict) -> None:
        """SIGINT процессу Code → SIGTERM группе → SIGKILL группе; proc_start защищает от повторного pid. Затем —
        группа гейта из gate.pid (ТЗ WSH-3.5, Г3.5): гейт идёт в своём сеансе, сигнал группе Code его не задевает."""
        try:
            self._kill_code_group(d)
        finally:
            kill_gate(self.job_dir(d["id"]))

    def _kill_code_group(self, d: dict) -> None:
        pgid, pid = d.get("pgid"), d.get("pid")
        if not pgid or not _alive(pid, d.get("proc_start")):
            return
        t_term, t_kill = cancel_timers()
        for sig, group, wait in ((signal.SIGINT, False, t_term),
                                 (signal.SIGTERM, True, t_kill), (signal.SIGKILL, True, 0)):
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(pgid, sig) if group else os.kill(int(pid), sig)
            if not wait or not _alive(pid, d.get("proc_start")):
                break
            deadline = time.time() + wait
            while time.time() < deadline and _alive(pid, d.get("proc_start")):
                time.sleep(0.2)

    # ---------- resume ----------

    def resume(self, jid: str, prompt: str, *, note=None, model=None, effort=None, max_turns=None,
               budget_usd=None, chosen_by=None, reason=None, push: bool = False, forge: bool = False,
               billing: str | None = None) -> dict:
        d = self.read(jid)
        if d.get("phase") == "plan":
            # resume поднял бы dontAsk с правами профиля без утверждения плана
            raise JobError(f"{jid} — фаза плана: продолжить можно только code_approve или code_replan")
        # жив надзиратель ИЛИ Code родителя — отказ (любое состояние, кроме терминальных и lost; замечание 5):
        # иначе можно поднять второй claude на тот же session_id
        state = self.effective_state(d)
        if state not in TERMINAL and state != "lost":
            raise JobError(f"родитель {jid} в состоянии {state} — продолжить можно только завершённую "
                           "или потерянную (lost) задачу")
        sid = d.get("session_id")
        if not sid:
            raise JobError(f"у задачи {jid} нет session_id — продолжить нельзя")
        # prompt — имя шаблона (не литеральный текст из чата, замечание 6); рендер тем же taskfile.render
        plan = self.plan(d["project"], d["task"], model=model, effort=effort, chosen_by=chosen_by, reason=reason,
                         max_turns=max_turns, budget_usd=budget_usd,  # только переданное: родительское — inherit
                         note=note, prompt_override=prompt, inherit=d, push=push, forge=forge, billing=billing)
        # тот же dir и профиль, что у родителя (п. 2.5.6): задача-потомок могла править свою шапку
        if str(plan.dir) != d.get("dir") or plan.profile != d.get("profile"):
            raise JobError(f"шапка ТЗ изменилась с момента родителя (dir/profile): было "
                           f"{d.get('dir')}/{d.get('profile')}, стало {plan.dir}/{plan.profile}")
        # size/risk — тоже: resume наследует план родителя, иначе задача L/high пошла бы без плана (решение 10)
        if (plan.size, plan.risk) != (d.get("size"), d.get("risk")):
            raise JobError(f"шапка ТЗ изменилась с момента родителя (size/risk): было "
                           f"{d.get('size')}/{d.get('risk')}, стало {plan.size}/{plan.risk} — новая задача "
                           "через code_start, план выберет политика")
        return self.start(plan, parent=jid, resume=sid, chain_root=d.get("chain_root") or jid)

    # ---------- фазы плана и исполнения (ТЗ WSH-3, 2.3) ----------

    def _plan_ready(self, jid: str, action: str = "approve", auto: bool = False) -> dict:
        d = self.read(jid)
        root = d.get("chain_root") or jid
        if d.get("phase") and d.get("chain_root") == jid:
            d = self.chain(jid)[-1]  # по корню цепочки — активное звено, как code_cancel и code_plan
            jid = d["id"]
        if not auto:  # автопереход надзирателя идёт как раз в окне перехода — отказы П4 только чату
            self._auto_plan_refusal(root, d, action)
        if d.get("phase") != "plan":
            raise JobError(f"{jid} — не фаза плана (утвердить или переделать можно только план)")
        state = self.effective_state(d)
        if state != "plan_ready" or d.get("next"):
            raise JobError(f"план {jid} в состоянии {state}" + (f", уже продолжен задачей {d['next']}"
                                                                 if d.get("next") else "") + " — нужен plan_ready")
        return d

    def _auto_plan_refusal(self, root: str, d: dict, action: str) -> None:
        """plan = auto у корня цепочки (ТЗ WSH-3.5, П4): план утверждает хаб, и code_approve/code_replan из чата
        получают понятный отказ — план уже утверждён или утверждается сейчас. Автопереход не удался (chain_error,
        надзиратель мёртв) — утверждение работает, как раньше."""
        try:
            rd = d if d.get("id") == root else self.read(root)
        except JobError:
            return
        if rd.get("plan_mode") != "auto":
            return
        done = next((x for x in reversed(self.chain(root)) if x.get("phase") == "plan" and x.get("reason") == "auto"
                     and x.get("next")), None)
        waiting = d.get("phase") == "plan" and self.effective_state(d) == "plan_ready" and not d.get("next") \
            and not d.get("chain_error") and _alive(d.get("sup_pid"), d.get("sup_start"))
        if action == "replan" and (done or waiting):
            raise JobError("при plan = auto исполнение стартует само, переделать план нельзя; нужен разбор плана — "
                           "запускай с plan = review")
        if done:
            raise JobError(f"план утверждён автоматически (plan = auto), исполнение — задача {done['next']}; "
                           f"code_approve не нужен — жди итог: code_wait({root})")
        if waiting:
            raise JobError("план утверждается автоматически (plan = auto) — code_approve не нужен")

    def _remaining(self, d: dict) -> tuple[float | None, int | None]:
        """Остаток бюджета и ходов задачи после всех звеньев цепочки (стоимость звена — разностью).
        У задачи без бюджета или без потолка ходов остатка нет — None (WSH-4.7, п. 2.3.6)."""
        links = self.chain(d.get("chain_root") or d["id"])
        spent = _sum(phase_cost(x) for x in links)
        turns = sum(int((x.get("result") or {}).get("num_turns") or 0) for x in links)
        total_usd = d.get("budget_total") or d.get("budget_usd")
        total_turns = d.get("max_turns_total")  # не max_turns звена: у фазы плана это потолок plan_max_turns
        return (None if not total_usd else _cents(float(total_usd) - spent),
                None if not total_turns else int(total_turns) - turns)

    def _link_plan(self, d: dict, phase: str, note, template: str | None = None) -> Plan:
        """Звено цепочки из job.json фазы плана: те же проект, dir, ТЗ, профиль и выбор; бюджет — остаток."""
        ws, proj, glob = self._project(d["project"])
        root = ws.path.resolve()
        dir_abs = Path(d["dir"])
        if dir_abs not in self._dirs(root, proj):
            raise JobError(f"dir задачи {dir_abs} больше не в code.dirs проекта {d['project']}")
        if d["profile"] not in proj.profiles:
            raise JobError(f"профиль {d['profile']} больше не разрешён проекту (profiles = {', '.join(proj.profiles)})")
        mode = billing_mode(d)  # звено цепочки — в режиме оплаты фазы плана, сменить нельзя (ТЗ WSH-4.6, п. 2.1.1)
        lim = effective_limits(proj, mode)
        budget, turns = self._remaining(d)
        choice = code_policy.Choice(**d["choice"])
        template = template or proj.execute_prompt
        if phase == "plan":
            if budget is not None:
                budget = min(budget, _cents(float(d["budget_total"]) * proj.plan_budget_share))
            turns = _least(lim.plan_max_turns, turns)
            model, effort = choice.plan_model, choice.plan_effort
        else:
            model, effort = choice.model, choice.effort
        if budget is not None and budget <= 0:
            raise JobError(f"бюджет задачи исчерпан: остаток {budget:g} из {d.get('budget_total')}")
        if turns is not None and turns < 1:
            raise JobError(f"ходы задачи исчерпаны: из {d.get('max_turns_total')} не осталось")
        prompt, warnings, note_left = self._render(root, proj, template, d.get("ticket") or "", d["task_rel"], note)
        gates = link_gates(proj, d["profile"], warnings)
        hidden = self._hidden(dir_abs, glob, proj, warnings)
        prompt += hub_block(note_left, gates, phase, hidden, prompt)
        # разрешения — у звеньев цепочки те же (А2.2), регламент проекта перепроверяется на старте звена
        grants, gwarn = task_grants(d["project"], proj, d["profile"], False, False, None, d.get("grants"))
        return Plan(d["project"], root, dir_abs, Path(d["task"]), d["task_rel"], d["profile"], template, model,
                    effort, turns, budget, prompt, warnings + gwarn, proj, ticket=d.get("ticket") or "",
                    size=d.get("size"), risk=d.get("risk"), choice=choice, phase=phase,
                    plan_mode=d.get("plan_mode") or "no",
                    budget_total=float(d["budget_total"]) if d.get("budget_total") else None,
                    max_turns_total=int(d["max_turns_total"]) if d.get("max_turns_total") else None,
                    grants=grants, gates=gates, sandbox_hidden=hidden, billing=mode)

    def approve(self, jid: str, note=None, *, auto: bool = False) -> dict:
        """Запуск фазы исполнения после plan_ready: --resume той же сессии, dontAsk, модель исполнения,
        остаток бюджета. auto=True — переход сам, из надзирателя фазы плана (plan = auto)."""
        d = self._plan_ready(jid, auto=auto)
        plan = self._link_plan(d, "execute", note)
        return self.start(plan, parent=d["id"], resume=d["session_id"], chain_root=d.get("chain_root") or d["id"],
                          link=(d["id"], "auto" if auto else "approved"))

    def replan(self, jid: str, note: str) -> dict:
        """Новая фаза плана в той же сессии: --permission-mode plan, шаблон replan с {note}."""
        if not (taskfile.check_note(note) or "").strip():
            raise JobError("note обязателен: что исправить в плане (одна строка, ≤ 300 символов)")
        d = self._plan_ready(jid, "replan")
        ws, proj, _glob = self._project(d["project"])
        self._replan_template(ws.path.resolve(), proj)
        plan = self._link_plan(d, "plan", note, REPLAN_PROMPT)
        return self.start(plan, parent=d["id"], resume=d["session_id"], chain_root=d.get("chain_root") or d["id"],
                          link=(d["id"], "replanned"))

    def plan_text(self, jid: str) -> tuple[str, Path]:
        d = self.read(jid)
        if d.get("phase") == "plan" and d.get("chain_root") == jid:
            plans = [x for x in self.chain(jid) if x.get("phase") == "plan"]
            d = next((x for x in reversed(plans) if (self.job_dir(x["id"]) / "plan.md").is_file()), d)
        if d.get("phase") != "plan":
            raise JobError(f"{jid} — не фаза плана")
        path = self.job_dir(d["id"]) / "plan.md"
        if not path.is_file():
            raise JobError(f"плана у {d['id']} нет (состояние {self.effective_state(d)})")
        return path.read_text(encoding="utf-8", errors="replace"), path

    def result_view(self, job: str, docker_max: int = DOCKER_MAX) -> dict:
        """Итог задачи для code_result и wshub code result: у корня цепочки — все звенья, стоимость обеих фаз
        и итог активного звена; строка, чтобы открыть сессию в терминале (2.5.2). docker_max — сколько
        docker-команд блока «Вне кода» показать (MCP — 10, CLI — все 30; ТЗ WSH-3.4, Б8). Ходы и действия —
        по событиям всех звеньев, как в code_status (Б1). Файлы — только существующие (Б3)."""
        snap = self.snapshot(job)
        d = self.read(snap.get("active") or job)
        jdir = self.job_dir(d["id"])
        sid = d.get("session_id")
        links = [self.read(x["id"]) for x in snap["chain"]] if snap.get("chain") else [d]
        # «Вне кода» — один блок по всем звеньям, в итоге активного звена его не повторяем (предел ответа)
        result = {k: v for k, v in (d.get("result") or {}).items() if k != "outside_code"}
        out = {"job": job, "project": snap.get("project"), "state": snap["state"], "reason": d.get("reason"),
               "choice": snap.get("choice"), "elapsed_s": snap.get("elapsed_s"), **self.counts(links),
               **({"error": d["error"]} if d.get("error") else {}),
               "grants": d.get("grants") or {}, "outside_code": outside_view(links, docker_max),
               "external": self.external_view(links), "result": result,
               **({"reason_hint": REASON_HINT[d["reason"]]} if d.get("reason") in REASON_HINT else {}),
               "files": {"events": _existing(jdir / "events.jsonl"), "stderr": _existing(jdir / "stderr.log"),
                         "task": _existing(Path(d["task"]), "нет (файл ТЗ удалён)") if d.get("task") else None}}
        if snap.get("chain"):
            out.update(phase=snap.get("phase"), active=snap.get("active"), chain=snap["chain"],
                       cost_usd=snap.get("cost_usd"))
        plan_md = next((self.job_dir(x["id"]) / "plan.md" for x in reversed(snap.get("chain") or [d])
                        if (self.job_dir(x["id"]) / "plan.md").is_file()), None)
        if plan_md:
            out["files"]["plan"] = str(plan_md)
        if sid and d.get("dir"):
            out["open_in_terminal"] = f"cd {shlex.quote(d['dir'])} && claude --resume {sid}"
        return out

    def counts(self, links: list[dict]) -> dict:
        """Ходы и действия по events.jsonl звеньев (ТЗ WSH-3.4, Б1) — одни и те же цифры во всех ответах."""
        from . import code_events
        out = {"turns": 0, "actions": 0}
        for x in links:
            c = code_events.scan(self.job_dir(x["id"]) / "events.jsonl", x.get("dir")).counts()
            out["turns"] += c["turns"]
            out["actions"] += c["actions"]
        return out

    def external_view(self, links: list[dict]) -> dict:
        """Блок «Внешние действия» (ТЗ WSH-3.4, А3.4): вызовы помощника по external.jsonl звеньев — пуши целиком
        (их мало), вызовы API числом и последние EXTERNAL_MAX, ссылки на MR/PR; гейты (ТЗ WSH-3.5, Г4) — последние
        EXTERNAL_MAX (имя, код, длительность, таймаут), число и сколько упало."""
        rows: list[dict] = []
        for x in links:
            try:
                lines = (self.job_dir(x["id"]) / "external.jsonl").read_text(encoding="utf-8").splitlines()
            except OSError:
                continue
            for line in lines:
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if isinstance(r, dict):
                    rows.append(r)
        pushes = [r for r in rows if r.get("cmd") == "push"]
        calls = [r for r in rows if r.get("cmd") == "forge"]
        gates = [r for r in rows if r.get("cmd") == "gate"]

        def row(r: dict) -> dict:
            return {k: r[k] for k in ("ts", "args", "exit", "status", "link", "error", "duration_s", "timeout")
                    if r.get(k) is not None}

        return {"push": [row(r) for r in pushes[-EXTERNAL_MAX:]], "push_count": len(pushes),
                "forge": [row(r) for r in calls[-EXTERNAL_MAX:]], "forge_count": len(calls),
                "forge_errors": sum(1 for r in calls if r.get("exit")),
                "gate": [row(r) for r in gates[-EXTERNAL_MAX:]], "gate_count": len(gates),
                "gate_failed": sum(1 for r in gates if r.get("exit")),
                "links": list(dict.fromkeys(r["link"] for r in rows if r.get("link")))}

    # ---------- статистика (2.4.5) ----------

    def stats(self, project: str | None = None, days: int = 30, now: float | None = None) -> list[dict]:
        """По строке политики и источнику: задачи (цепочки), средние стоимость и ходы, доля completed, доля
        планов, отправленных на replan. Каждое звено считается один раз (стоимость — разностью)."""
        since = (now or time.time()) - days * 86400
        chains: dict[str, list[dict]] = {}
        for jd in self.root.glob("*/job.json") if self.root.is_dir() else []:
            d = read_json(jd)
            if not d or (project and d.get("project") != project):
                continue
            chains.setdefault(d.get("chain_root") or d.get("id"), []).append(d)
        agg: dict[tuple, dict] = {}
        for links in chains.values():
            links.sort(key=lambda x: (x.get("created_ts") or 0, x.get("id")))
            first = links[0]
            if (first.get("created_ts") or 0) < since:
                continue
            c = first.get("choice") or {}
            # режим оплаты — тоже в ключе группы: средняя сумма показывается только у задач на ключе (ТЗ WSH-4.6)
            key = (first.get("project"), c.get("row") or "—", c.get("source") or "—", c.get("channel") or "—",
                   billing_mode(first))
            a = agg.setdefault(key, {"tasks": 0, "cost": 0.0, "turns": 0, "completed": 0, "plans": 0,
                                     "replanned": 0})
            a["tasks"] += 1
            a["cost"] += _sum(phase_cost(x) for x in links)
            a["turns"] += sum(int((x.get("result") or {}).get("num_turns") or 0) for x in links)
            a["completed"] += self.effective_state(links[-1]) == "completed"
            plans = [x for x in links if x.get("phase") == "plan"]
            a["plans"] += len(plans)
            a["replanned"] += sum(x.get("reason") == "replanned" for x in plans)
        return [{"project": k[0], "row": k[1], "source": k[2], "channel": k[3], "billing": k[4], "tasks": a["tasks"],
                 "avg_cost_usd": round(a["cost"] / a["tasks"], 4), "avg_turns": round(a["turns"] / a["tasks"], 1),
                 "completed_share": round(a["completed"] / a["tasks"], 3),
                 "replan_share": round(a["replanned"] / a["plans"], 3) if a["plans"] else None}
                for k, a in sorted(agg.items())]


REPLAN_PROMPT = "replan"
# подсказки к коду причины — в снимке и итоге рядом с reason
REASON_HINT = {"no_plan": "план ищется только в ~/.claude/plans/: настройка plansDirectory в "
                          ".claude/settings*.json проекта не поддерживается"}  # решение 13


def task_grants(name: str, proj: ProjectCode, profile: str, push: bool, forge: bool, reason,
                inherited: dict | None = None) -> tuple[dict, list[str]]:
    """Разрешения задачи по регламенту проекта (ТЗ WSH-3.4, А2): no — отказ на просьбу, task — с reason
    (≥ 15 символов: кто разрешил и когда), always — само. Пуш — только профилю task. inherited — разрешения
    родителя: наследуются, пока регламент их не закрыл. Ответ — (разрешения, предупреждения)."""
    inh = inherited or {}
    why = code_policy.clean_reason(reason) if reason else ""
    out: dict = {"push": False, "forge": False, "reason": None}
    warnings: list[str] = []
    for kind, asked, mode, key, label in (("push", push, proj.push, "push", "пуш"),
                                           ("forge", forge, proj.forge_access, "forge_access", "API хостинга")):
        # разрешение, выданное само при always, не наследуется, если регламент стал task (ревью WSH-3.4)
        was = bool(inh.get(kind)) and (kind not in (inh.get("auto") or []) or mode == "always")
        if kind == "push" and profile != "task":
            if asked:
                raise JobError(f"пуш — только задаче профиля task (у задачи {profile})")
            continue
        if mode == "no":
            if asked:
                raise JobError(f"в проекте {name} {label} закрыт реестром ({key} = no)")
            if was:
                warnings.append(f"{label}: разрешение родителя снято — регламент проекта теперь {key} = no")
            continue
        if mode == "always":
            out[kind] = True
            if not asked and (not inh.get(kind) or kind in (inh.get("auto") or [])):
                out.setdefault("auto", []).append(kind)
        elif was:
            out[kind] = True
        elif asked:
            if len(why) < code_policy.REASON_MIN:
                raise JobError(f"{label} при {key} = task — только с reason (≥ {code_policy.REASON_MIN} символов): "
                               "кто разрешил и когда, например «оператор в чате 15.10: запушить и открыть MR»")
            out[kind] = True
    if out["push"] or out["forge"]:
        out["reason"] = (why if (push or forge) and why else None) or inh.get("reason")
    return out, warnings


def helper_env(env: dict, workdir: Path) -> dict:
    """PATH задачи с помощником (ревью WSH-3.4, круг 1): правила Bash(wshub push|forge *) и excludedCommands
    называют wshub по имени, и вне песочницы запустится первый wshub из PATH. Поэтому PATH начинается с каталога
    настоящего wshub хаба, а относительные элементы и каталоги внутри рабочего каталога задачи выкидываются.
    wshub хаба — не один исполняемый файл (python -m wshub) — разрешения не выдаются: JobError."""
    exe = claude_cli.wshub_exe()
    if not exe:
        raise JobError("помощник хаба: wshub запущен как «python -m wshub», а не исполняемым файлом — разрешения "
                       "push и forge не выдаются; поставь wshub как программу (uv tool install) и перезапусти")
    home = os.path.dirname(exe)
    work = os.path.realpath(workdir)
    keep = []
    for d in (env.get("PATH") or "").split(os.pathsep):
        if not d or not os.path.isabs(d):
            continue
        real = os.path.realpath(d)
        if real == work or real.startswith(work + os.sep) or d in keep or d == home:
            continue
        keep.append(d)
    return {**env, "PATH": os.pathsep.join([home, *keep])}


def link_gates(proj: ProjectCode, profile: str, warnings: list[str]) -> tuple:
    """Гейты задачи (ТЗ WSH-3.5, Г1–Г2): только профилю task. Хаб запущен как python -m wshub — правило
    Bash(wshub gate *) выпустило бы из песочницы первый wshub из PATH: гейтов нет, задача стартует с предупреждением."""
    if profile != "task" or not proj.gates:
        return ()
    if not claude_cli.wshub_exe():
        warnings.append(GATES_PYTHON_M)
        return ()
    return proj.gates


def deny_read_hit(masks, rel: str, real: str) -> bool:
    """Маски deny_read: абсолютные и ~ — по реальному пути, относительные — от рабочего каталога. Одна функция для
    помощника (--data) и списка скрытых песочницей файлов (ТЗ WSH-3.5, П2)."""
    for m in masks:
        if m == "~" or m.startswith("~/") or os.path.isabs(m):
            base = os.path.normpath(os.path.expanduser(m))
            if real == base or real.startswith(base.rstrip("/") + "/") or fnmatch.fnmatchcase(real, base):
                return True
        else:
            m = os.path.normpath(m)
            if rel == m or rel.startswith(m + "/") or fnmatch.fnmatchcase(rel, m):
                return True
    return False


def sandbox_hidden(workdir: Path, masks: list[str], environ: dict) -> tuple[list[str], str | None]:
    """Отслеживаемые файлы, которые песочница скрывает от Bash (ТЗ WSH-3.5, П2): git ls-files через gitsafe, затем те
    же маски, что получает песочница, — имя .env* (Read(**/.env*)) и deny_read [code] и проекта. В git status задачи
    такие файлы выглядят изменёнными. Ответ — (пути, None) или ([], причина сбоя git)."""
    git = shutil.which("git") or "git"
    why = "скрытые песочницей файлы не посчитаны"
    try:
        r = gitsafe.run(git, workdir, ["ls-files", "-z"], environ, 30)
    except gitsafe.GitUnsafe as e:
        return [], f"{why}: {e.reason}: {e}"[:500]
    except (OSError, subprocess.TimeoutExpired, ValueError) as e:  # ValueError — имя не в UTF-8
        return [], f"{why}: {e}"[:500]
    if r.returncode != 0:
        return [], f"{why}: git ls-files вернул {r.returncode}: {r.stderr.strip()[:300]}"
    base = os.path.realpath(workdir)
    return [rel for rel in r.stdout.split("\0") if rel and (rel.rsplit("/", 1)[-1].startswith(".env")
                                                          or deny_read_hit(masks, rel, os.path.join(base, rel)))], None


def hub_block(note: str | None, gates: tuple, phase: str | None, hidden: list[str] | None, prompt: str = "") -> str:
    """Блок «От хаба» в конце промпта (ТЗ WSH-3.5, П1): замечание оператора, если шаблон его не подставил; гейты
    (у фазы плана — какие будут в исполнении); скрытые песочницей файлы. Только непустые строки, весь блок ≤ 2 КБ:
    лишнее — «и ещё N». Ответ — текст, который дописать к промпту (prompt — чтобы начать с новой строки), или ""."""
    hidden = hidden or []

    def build(ng: int, nh: int) -> str:
        lines = []
        if note and note.strip():  # замечание из одних пробелов — строки нет (круг 2, Р11)
            lines.append(f"- Замечание оператора: {note}")
        if gates:
            items = [f"{g.name} (timeout {(g.timeout_s + 60) * 1000} мс) — {_shown(shlex.join(g.cmd), HUB_BLOCK_CMD)}"
                     for g in gates[:ng]]
            listed = "; ".join(items) + (f"; и ещё {len(gates) - ng}" if len(gates) > ng else "")
            lines.append(f"- Гейты будут в фазе исполнения: {listed}." if phase == "plan" else
                         "- Гейты — проверки вне песочницы, только командой `wshub gate <имя>` целиком, Bash с timeout "
                         f"не меньше указанного: {listed}. Полный вывод — `--out <файл>`.")
        if hidden:
            shown = ", ".join(_shown_path(h) for h in hidden[:nh]) + \
                (f" (и ещё {len(hidden) - nh})" if len(hidden) > nh else "") if nh else \
                f"скрыто файлов: {len(hidden)}, список — `sandbox_hidden` в job.json"
            lines.append(f"- Песочница скрывает отслеживаемые файлы: {shown}. В `git status` задачи они выглядят "
                         "изменёнными — это не правка задачи: не восстанавливай их и не добавляй в коммит. "
                         "`git add -A` и `git add .` на них падают — добавляй файлы в индекс явными путями.")
        if not lines:
            return ""
        return ("" if not prompt or prompt.endswith("\n") else "\n") + \
            "\n---\nОт хаба wshub (добавлено к шаблону):\n" + "\n".join(lines) + "\n"

    ng, nh = min(len(gates), HUB_BLOCK_GATES), min(len(hidden), HUB_BLOCK_HIDDEN)
    text = build(ng, nh)
    # сокращаем списки: пути до одного, гейты до одного, затем пути до нуля — строка «скрыто файлов: N» без путей
    # (круг 3, Р17): тогда хвост инструкции не срезает предохранитель по байтам
    while len(text.encode("utf-8")) > HUB_BLOCK_MAX and (nh > 0 or ng > 1):
        nh, ng = (nh - 1, ng) if nh > 1 or (nh == 1 and ng <= 1) else (nh, ng - 1)
        text = build(ng, nh)
    if len(text.encode("utf-8")) > HUB_BLOCK_MAX:  # последний предохранитель (note из 4-байтовых символов)
        text = text.encode("utf-8")[:HUB_BLOCK_MAX - 4].decode("utf-8", "ignore") + "…\n"
    return text


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit - 1] + "…"


_QUOTE = {"\\": "\\\\", '"': '\\"', "\n": "\\n", "\t": "\\t", "\r": "\\r"}
_LINE_SEPS = "\x85\N{LINE SEPARATOR}\N{PARAGRAPH SEPARATOR}"  # U+0085, U+2028, U+2029


def _shown_path(p: str) -> str:
    """Путь в строке блока (круг 2, Р3 и Р8): не длиннее HUB_BLOCK_PATH, экранирование — _shown."""
    return _shown(p, HUB_BLOCK_PATH)


def _shown(text: str, limit: int) -> str:
    """Текст в строке блока — путь или команда гейта (круг 2, Р3; круг 3, Р16): не длиннее limit; с управляющими
    символами (\\x00–\\x1f, \\x7f, U+0085, U+2028, U+2029) — в двойных кавычках с экранированием, как git
    core.quotePath: перевод строки в имени файла или в команде не подделает строку блока."""
    if not any(ord(c) < 0x20 or c in "\x7f" + _LINE_SEPS for c in text):
        return _clip(text, limit)
    out = "".join(_QUOTE.get(c) or (f"\\x{ord(c):02x}" if ord(c) < 0x20 or c == "\x7f" else
                                    f"\\u{ord(c):04x}" if c in _LINE_SEPS else c) for c in text)
    return '"' + _clip(out, limit) + '"'


def gate_group_alive(pgid: int, proc: Path = Path("/proc")) -> bool:
    """Есть ли в группе процессы, кроме зомби (их снимает родитель, сигнал им не нужен). /proc/<pid>/stat — байтами:
    comm, обрезанный посреди многобайтового символа, не UTF-8 (круг 3, Р13)."""
    if proc.is_dir():
        group = str(pgid).encode()
        for p in proc.iterdir():
            if not p.name.isdigit():
                continue
            try:
                fields = (p / "stat").read_bytes().rsplit(b")", 1)[-1].split()
            except OSError:
                continue
            if len(fields) > 2 and fields[2] == group and fields[0] != b"Z":
                return True
        return False
    try:  # без /proc (macOS)
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def stop_gate_group(pgid: int, alive=None) -> None:
    """SIGTERM группе гейта → до GATE_KILL_AFTER с → SIGKILL группе (ТЗ WSH-3.5, Г3.5). alive() — идёт ли ещё
    группа (помощник заодно снимает зомби лидера); по умолчанию — есть ли в группе процессы."""
    alive = alive or (lambda: gate_group_alive(pgid))
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(pgid, signal.SIGTERM)
    deadline = time.time() + GATE_KILL_AFTER
    while time.time() < deadline and alive():
        time.sleep(0.1)
    if alive():
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(pgid, signal.SIGKILL)


def kill_gate(jdir: Path) -> None:
    """Погасить гейт звена по gate.pid (ТЗ WSH-3.5, Г3.5) — при остановке задачи и в code-kill: помощник мог умереть
    по SIGKILL, не погасив гейт. Защита proc_start, как у _kill_group: pid лидера жив с другим временем старта —
    переиспользован, группу не трогаем. Лидер уже вышел, а в группе остались его потомки — гасим их: номер группы не
    выдаётся новому процессу, пока в ней кто-то есть."""
    p = jdir / GATE_PID
    info = read_json(p)
    if not info:
        return
    kill_gate_group(info)
    with contextlib.suppress(OSError):
        p.unlink()


def kill_gate_group(info: dict) -> bool:
    """Погасить группу гейта по записи {pid, pgid, proc_start} (gate.pid звена или .holder лока проекта) с защитой
    proc_start. Нет времени старта в записи — переиспользование pid не проверить, группу не трогаем (круг 3, Т9).
    True — группа шла и погашена."""
    pid, pgid, started = info.get("pid"), info.get("pgid"), info.get("proc_start")
    if not (isinstance(pid, int) and isinstance(pgid, int) and pgid > 1 and pgid != os.getpgrp()
            and isinstance(started, str)):
        return False
    now = proc_start(pid)
    if (now is not None and now != started) or not gate_group_alive(pgid):
        return False
    stop_gate_group(pgid)
    return True


def write_exclude(repo: Path) -> None:
    """Помеченный блок с точками монтирования песочницы в .git/info/exclude (ТЗ WSH-3.4, А4.4): заменяется, а не
    дублируется; чужие строки не трогаются. .git/info — каталог, exclude — обычный файл или его нет (симлинк —
    отказ: запись не уходит по ссылке, подложенной задачей)."""
    info = repo / ".git" / "info"
    if info.is_symlink() or (info.exists() and not info.is_dir()):
        raise OSError(f"{info} — не каталог")
    path = info / "exclude"
    old = ""
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise OSError(f"{path} — не обычный файл")
    if path.exists():
        try:
            old = path.read_bytes().decode("utf-8")
        except UnicodeDecodeError:
            raise OSError(f"{path} — не UTF-8, блок не пишу") from None
    lines, skip = [], False
    for line in old.splitlines():
        if line == EXCLUDE_BEGIN:
            skip = True
        elif skip and line == EXCLUDE_END:
            skip = False
        elif not skip:
            lines.append(line)
    while lines and not lines[-1].strip():
        lines.pop()
    block = [EXCLUDE_BEGIN, *(f"/{m}" for m in SANDBOX_MOUNTS), EXCLUDE_END]
    text = "\n".join([*lines, *([""] if lines else []), *block]) + "\n"
    if text != old:
        info.mkdir(parents=True, exist_ok=True)
        atomic_write_text(path, text)


def git_meta(repo: Path) -> dict:
    """Исполняемая часть .git, которой нет в диффе (ТЗ WSH-3.3, п. 1.2): .git/hooks — обычный файл по sha256,
    симлинк по цели; .git/config — так же (обычно sha256). Файлы читает хаб сам, без git.
    hooks_dir — что такое сам .git/hooks (каталог, симлинк, нет); hooks = None — каталог не прочитан;
    config = None — файла нет. Имена и цели — без суррогатов (не-UTF-8 имя иначе роняет запись JSON)."""
    g = repo / ".git"
    out: dict = {"hooks_dir": None, "hooks": {}, "config": None}
    errors = []
    try:
        st = os.lstat(g / "hooks")
        out["hooks_dir"] = {"symlink": _text(os.readlink(g / "hooks"))} if stat.S_ISLNK(st.st_mode) else \
            {"type": "dir" if stat.S_ISDIR(st.st_mode) else "not_dir"}
        entries = sorted(os.scandir(g / "hooks"), key=lambda e: e.name)
    except FileNotFoundError:
        entries = []
    except OSError as e:
        entries, out["hooks"] = [], None
        errors.append(f".git/hooks не прочитан: {e}")
    for e in entries:
        try:
            if e.is_symlink() or e.is_file(follow_symlinks=False):  # каталоги и прочее хуком не запускаются
                out["hooks"][_text(e.name)] = _file_id(Path(e.path))
        except FileNotFoundError:
            pass
        except OSError as err:  # lstat не прошёл — запись есть, но что это, не узнать: считаем её отдельным видом
            out["hooks"][_text(e.name)] = {"type": "unreadable"}
            errors.append(f".git/hooks/{_text(e.name)} не прочитан: {err}")
    try:
        out["config"] = _file_id(g / "config")
    except FileNotFoundError:
        pass
    except OSError as e:
        out["config"] = {"type": "unreadable"}
        errors.append(f".git/config не прочитан: {e}")
    if errors:
        out["meta_errors"] = [_text(x) for x in errors]
    return out


def start_snapshot(cwd: Path, environ: dict) -> dict:
    """Снимок на старте звена: полный HEAD и ветка через gitsafe, git_meta и ключи .git/config. Отказ gitsafe
    (небезопасный ключ, worktree) задачу не останавливает — пишется в error. Пустой репозиторий (ветка без
    коммитов) — empty: коммиты в конце считаются от корня (ТЗ WSH-3.4, Б6)."""
    git = shutil.which("git") or "git"
    out: dict = {"head": None, "branch": None, "ts": time.time(), **git_meta(cwd)}
    salt = secrets.token_hex(8)
    keys = config_keys(git, cwd, environ, salt)
    if keys is not None:
        out.update(config_salt=salt, config_keys=keys)
    try:
        out.update(head_state(git, cwd, environ))
    except gitsafe.GitUnsafe as e:
        out["error"] = f"{e.reason}: {e}"
    except (OSError, subprocess.TimeoutExpired) as e:
        out["error"] = str(e)
    return out


def head_state(git: str, cwd: Path, environ: dict) -> dict:
    """HEAD и ветка: {head, branch} (branch = None — HEAD отсоединён), {head: None, empty: True} — ветка без
    коммитов, {head: None, error} — HEAD не читается."""
    r = gitsafe.run(git, cwd, ["rev-parse", "--verify", "-q", "HEAD"], environ, 30)
    b = gitsafe.run(git, cwd, ["symbolic-ref", "-q", "--short", "HEAD"], environ, 30)
    branch = b.stdout.strip() if b.returncode == 0 and b.stdout.strip() else None
    if r.returncode == 0:
        return {"head": r.stdout.strip(), "branch": branch}
    if branch is not None:
        return {"head": None, "branch": branch, "empty": True}
    return {"head": None, "branch": None,
            "error": f"git rev-parse HEAD вернул {r.returncode}: {r.stderr.strip()[:300]}"}


def config_keys(git: str, repo: Path, environ: dict, salt: str) -> dict | None:
    """Ключи .git/config без значений (ТЗ WSH-3.4, Б7): ключ → хэш значений с солью снимка — так видно, что ключ
    изменился, а значения (в конфигурации бывают учётные данные) не хранятся. Читает git config --file …
    --list --no-includes в чистом окружении (gitsafe.env): чтение ничего не запускает. Файл не обычный (симлинк,
    спецфайл) или не читается — None, сравнение идёт по sha256 файла."""
    path = repo / ".git" / "config"
    try:
        if not stat.S_ISREG(os.lstat(path).st_mode):
            return None
        r = subprocess.run([git, "config", "--file", str(path), "--list", "--no-includes"], capture_output=True,
                           text=True, errors="replace", timeout=gitsafe.CONFIG_TIMEOUT, env=gitsafe.env(environ),
                           stdin=subprocess.DEVNULL, check=False, cwd="/")
    except (OSError, subprocess.TimeoutExpired):
        return None
    if r.returncode != 0:
        return None
    values: dict[str, list[str]] = {}
    for line in r.stdout.splitlines():
        key, _, value = line.partition("=")
        values.setdefault(_text(key), []).append(value)
    return {k: hashlib.sha256((salt + "\x00" + "\x00".join(v)).encode()).hexdigest()[:16] for k, v in values.items()}


def meta_changes(start: dict, now: dict) -> list[str]:
    """Что задача сделала с .git/hooks и .git/config по сравнению со снимком на старте."""
    out = []
    a, b = start.get("hooks"), now.get("hooks")
    if "hooks_dir" in start and start.get("hooks_dir") != now.get("hooks_dir"):
        out.append(".git/hooks — изменён")  # стал симлинком, исчез, появился
    if a is None or b is None:
        out.append(".git/hooks — не прочитан")  # сравнить нельзя: хук git пользователя всё равно исполнит
    else:
        for name in sorted(set(a) | set(b)):
            if name not in a:
                out.append(f".git/hooks/{name} — новый")
            elif name not in b:
                out.append(f".git/hooks/{name} — удалён")
            elif a[name] != b[name]:
                out.append(f".git/hooks/{name} — изменён")
    if "config" in start and start.get("config") != now.get("config"):
        a, b = start.get("config_keys"), now.get("config_keys")
        if isinstance(a, dict) and isinstance(b, dict):
            rows = [(k, "добавлен") for k in b if k not in a] + [(k, "удалён") for k in a if k not in b] + \
                [(k, "изменён") for k in a if k in b and a[k] != b[k]]
            out += [f".git/config — {what} ключ {k}" + (" (опасный)" if gitsafe.unsafe_key(k) else "")
                    for k, what in sorted(rows)]
            if not rows:
                out.append(".git/config — изменён, ключи те же")
        else:
            out.append(".git/config — изменён")
    return out


def outside_view(links: list[dict], docker_max: int = DOCKER_MAX) -> dict:
    """Блок «Вне кода» по звеньям (у корня цепочки — по всем): HEAD и ветка на старте первого звена и в конце
    последнего, строки «HEAD ушёл», коммиты (новые сверху), docker-вызовы по порядку (не больше docker_max),
    изменения .git/hooks и .git/config. Звено без блока (идёт, не дошло до итога, старое) — в no_data."""
    out: dict = {"commits": [], "commits_count": 0, "docker": [], "docker_count": 0, "git_meta_changed": []}
    errors, no_data, moved = [], [], []
    for x in links:
        oc = (x.get("result") or {}).get("outside_code")
        if not isinstance(oc, dict):
            no_data.append(x.get("id"))
            continue
        for k in ("head_start", "branch_start"):
            if k in oc and k not in out:
                out[k] = oc[k]
        for k in ("head_end", "branch_end"):
            if k in oc:
                out[k] = oc[k]
        if oc.get("head_moved"):
            moved.append(oc["head_moved"])
        out["commits"] = list(oc.get("commits") or []) + out["commits"]
        out["commits_count"] += int(oc.get("commits_count") or 0)
        out["docker"] += list(oc.get("docker") or [])
        out["docker_count"] += int(oc.get("docker_count") or 0)
        out["git_meta_changed"] += [m for m in oc.get("git_meta_changed") or [] if m not in out["git_meta_changed"]]
        errors += [f"{x.get('id')}: {e}" for e in oc.get("errors") or []]
    out["commits"] = out["commits"][:COMMITS_MAX]
    out["docker"] = out["docker"][:min(docker_max, DOCKER_MAX)]
    if moved:
        out["head_moved"] = moved
    out["git_meta_changed"] = clip_list(out["git_meta_changed"])
    if errors:
        out["errors"] = clip_list(errors)
    if no_data:
        out["no_data"] = no_data
    return out


def clip_list(items: list[str], limit: int = META_MAX) -> list[str]:
    """Не больше limit строк (предел ответа 20 КБ и строки журнала) плюс «…ещё N»."""
    return items if len(items) <= limit else [*items[:limit], f"…ещё {len(items) - limit}"]


def meta_warning(view: dict) -> str | None:
    """Первая строка ответа code_result / wshub code result, если задача тронула .git/hooks или .git/config."""
    return META_WARNING if (view.get("outside_code") or {}).get("git_meta_changed") else None


def _file_id(path: Path) -> dict:
    """{"symlink": цель} | {"sha256": …} обычного файла | {"type": …} прочего. Сначала lstat: спецфайл не
    открывается, симлинк не проходится; открытие — без следования по симлинку и без блокировки (подмена между
    lstat и open). Файл в .git могла подложить задача (через docker). Нечитаемый или крупный файл — по размеру
    и mtime: так его появление и смена тоже видны."""
    st = os.lstat(path)
    if stat.S_ISLNK(st.st_mode):
        return {"symlink": _text(os.readlink(path))}
    if not stat.S_ISREG(st.st_mode):
        return {"type": "not_regular"}
    meta = {"size": st.st_size, "mtime_ns": st.st_mtime_ns}
    if st.st_size > HASH_MAX:
        return {"type": "large", **meta}
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except PermissionError:
        return {"type": "unreadable", **meta}
    with os.fdopen(fd, "rb") as fh:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return {"type": "not_regular"}
        h = hashlib.sha256()
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
        return {"sha256": h.hexdigest()}


def _text(name: str) -> str:
    """Имя с не-UTF-8 байтами (суррогаты из os.fsdecode) — с экранированием: JSON в UTF-8 его иначе не запишет."""
    return os.fsencode(name).decode("utf-8", "backslashreplace")


def _end_ts(d: dict, state: str, now: float) -> float:
    """Конец отсчёта времени звена (ТЗ WSH-3.4, Б2): у завершённого — момент завершения (finished_ts, у старых
    задач — finished), а не текущее время."""
    if state not in TERMINAL:
        return now
    if isinstance(d.get("finished_ts"), (int, float)):
        return float(d["finished_ts"])
    try:
        return datetime.fromisoformat(d["finished"]).timestamp()
    except (KeyError, TypeError, ValueError):
        return now


def _existing(path: Path, missing: str = "нет (задача не дошла до запуска claude)") -> str:
    """Путь для code_result: только существующий файл (ТЗ WSH-3.4, Б3)."""
    return str(path) if path.exists() else missing


def _channel(chans) -> str | None:
    """Каналы через «+» в порядке cli, mcp, header (решение 12)."""
    got = set(chans)
    return "+".join(c for c in code_policy.CHANNELS if c in got) or None


def phase_cost(d: dict) -> float | None:
    """Стоимость звена: phase_cost_usd из итога (разность накопительного total_cost_usd с предыдущим звеном
    сессии, решение 6), для старых задач — total_cost_usd. Нет итога — None."""
    res = d.get("result") or {}
    for k in ("phase_cost_usd", "total_cost_usd"):
        if isinstance(res.get(k), (int, float)):
            return float(res[k])
    return None


def _sum(values) -> float:
    return round(sum(v for v in values if v is not None), 6)


def _least(*values):
    """Меньшее из заданных значений; ни одного нет — None."""
    got = [v for v in values if v is not None]
    return min(got) if got else None


def _cents(v: float) -> float:
    """Бюджет звена — вниз до цента: --max-budget-usd не превышает остаток."""
    return math.floor(round(v * 100, 6)) / 100


def _alive(pid, started) -> bool:
    return bool(pid) and pid_alive(int(pid), started)


def _uuid4() -> str:
    import uuid
    return str(uuid.uuid4())

"""Политика выбора модели, effort и плана для задач wshub-code (ТЗ WSH-3, п. 2.1–2.2).

Источники выбора: оператор — безусловно (пол и потолок реестра на него не действуют); политика — если
оператор не выбирал; чат — отступление от политики, только в пределах пола и потолка и только с причиной.
Встроенная таблица — стартовые значения; оператор переопределяет её строками [[code.policy]] (глобально)
и [[workspace.<имя>.code.policy]] (для проекта). Числа здесь и только здесь — калибруются по code_stats.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass

from .core import WsError
from .registry import EFFORTS, MODELS, CodeGlobal, PolicyRow, ProjectCode

REASON_MIN = 15
FIELDS = ("model", "effort", "plan")
SOURCES = ("operator", "policy", "chat")
CHANNELS = ("cli", "mcp", "header")  # откуда пришли поля выбора: флаги терминала, аргументы MCP, шапка ТЗ
ORIGIN_RU = {"project": "проекта", "global": "глобальная", "builtin": "встроенная"}

BUILTIN: tuple[PolicyRow, ...] = (
    PolicyRow("recon", "S", "*", "sonnet", "medium", "no"),
    PolicyRow("recon", "M", "*", "sonnet", "high", "no"),
    PolicyRow("recon", "L", "*", "opus", "medium", "no"),
    PolicyRow("review", "*", "*", "opus", "medium", "no"),
    PolicyRow("task", "S", "low", "sonnet", "medium", "no"),
    PolicyRow("task", "S", "high", "sonnet", "high", "auto", "opus", "high"),
    PolicyRow("task", "M", "low", "sonnet", "high", "auto", "opus", "high"),
    PolicyRow("task", "M", "high", "opus", "high", "review", "opus", "high"),
    PolicyRow("task", "L", "low", "opus", "high", "review", "opus", "high"),
    PolicyRow("task", "L", "high", "opus", "high", "review", "opus", "high"),
)

GUIDE = """\
Модель, effort и план выбирает политика по шапке ТЗ: profile, size, risk.
size: S — правка в одном слое, ≤ 3 файлов, рядом эталон; M — несколько файлов или слоёв, без смены
контрактов; L — контракт API, схема БД, перенос ответственности или больше одного сервиса.
risk: high — безопасность, деньги, данные пользователей, необратимые миграции; иначе low.
plan: no — без плана; auto — Code сначала строит план, затем исполняет; review — план, остановка,
решение архитектора (code_plan → code_approve или code_replan).
Отступить от политики (model/effort/plan) чат может только в пределах потолков проекта и только с причиной
(reason, ≥ 15 символов); понизить план ниже строки политики — только оператор. Выбор оператора
(chosen_by: "operator" или choice: operator в шапке) исполняется как есть — передавай его, только когда
так сказал оператор."""


class PolicyError(WsError):
    pass


@dataclass
class Choice:
    model: str
    effort: str
    plan: str
    plan_model: str | None
    plan_effort: str | None
    source: str  # operator | policy | chat
    row: str | None  # «task/M/low (встроенная)» — строка политики, по которой выбрано
    reason: str | None
    fields: dict  # поле → источник: operator | policy | chat
    channel: str | None = None  # cli | mcp | header, несколько — через «+»; у источника policy — None
    choice_fields: dict | None = None  # поле не из политики → «источник:канал», например «chat:mcp»

    def to_dict(self) -> dict:
        out = asdict(self)
        if out["channel"] is None:  # у источника policy поля нет (решение 12)
            del out["channel"]
        return out

    def line(self) -> str:
        """Строка для ответа code_start (2.4.4)."""
        plan = self.plan if self.plan == "no" else f"{self.plan} (план: {self.plan_model}/{self.plan_effort})"
        out = (f"модель/effort/план: {self.model}/{self.effort}/{plan}, источник: {self.source}"
               f"{f' ({self.channel})' if self.channel else ''}, "
               f"строка политики: {self.row or '—'}")
        if self.source == "chat":
            out += f"; чат отступил от политики: {self.reason}"
        return out


def layers(glob: CodeGlobal, proj: ProjectCode) -> list[tuple[PolicyRow, ...]]:
    """Слои от сильного к слабому: строки проекта, глобальные, встроенные."""
    return [proj.policy, glob.policy, BUILTIN]


def effective(glob: CodeGlobal, proj: ProjectCode) -> list[dict]:
    """Действующая политика проекта для code_projects: все слои в порядке силы."""
    return [{**asdict(r), "origin": ORIGIN_RU[r.origin]} for rows in layers(glob, proj) for r in rows]


def _row_label(r: PolicyRow) -> str:
    return f"{r.key} ({ORIGIN_RU[r.origin]})"


def match(glob: CodeGlobal, proj: ProjectCode, profile: str, size: str | None, risk: str | None):
    """Самая точная строка в самом сильном слое, где есть совпадение. «*» слабее значения; при равной
    точности size весомее risk. Нет size/risk в шапке — подходят только строки с «*»."""
    for rows in layers(glob, proj):
        cands = [r for r in rows if r.profile == profile and r.size in ("*", size) and r.risk in ("*", risk)]
        if cands:
            return max(cands, key=lambda r: 2 * (r.size != "*") + (r.risk != "*"))
    return None


def profile_rows(glob: CodeGlobal, proj: ProjectCode, profile: str) -> list[str]:
    """Строки политики профиля — для текста отказа (2.4.1)."""
    out = []
    for rows in layers(glob, proj):
        for r in rows:
            if r.profile == profile:
                plan = r.plan if r.plan == "no" else f"{r.plan} (план {r.plan_model}/{r.plan_effort})"
                out.append(f"{_row_label(r)}: {r.model}/{r.effort}, план {plan}")
    return out


def _order(value: str, order: tuple[str, ...], name: str) -> int:
    try:
        return order.index(value)
    except ValueError:
        raise PolicyError(f"{name}: одно из {', '.join(order)}") from None


def _check_bounds(proj: ProjectCode, model: str | None, effort: str | None, what: str) -> None:
    """Пол и потолок реестра; None — поле не проверяется (выбор оператора)."""
    if model is not None:
        if _order(model, MODELS, "model") < _order(proj.model_min, MODELS, "model_min"):
            raise PolicyError(f"{what}: модель {model} ниже model_min ({proj.model_min})")
        if _order(model, MODELS, "model") > _order(proj.model_max, MODELS, "model_max"):
            raise PolicyError(f"{what}: модель {model} выше model_max ({proj.model_max})")
    if effort is not None and _order(effort, EFFORTS, "effort") > _order(proj.effort_max, EFFORTS, "effort_max"):
        raise PolicyError(f"{what}: effort {effort} выше effort_max ({proj.effort_max})")


def choose(glob: CodeGlobal, proj: ProjectCode, profile: str, size: str | None, risk: str | None,
           given: dict, reason: str | None) -> Choice:
    """given: поле → (значение, источник «operator» | «chat»). Остальные поля — из строки политики."""
    from .registry import PLAN_MODES
    for f, (v, src) in given.items():
        if f not in FIELDS or src not in ("operator", "chat"):
            raise PolicyError(f"выбор {f} от {src} не поддерживается")
        order = {"model": MODELS, "effort": EFFORTS, "plan": PLAN_MODES}[f]
        _order(v, order, f)
    row = match(glob, proj, profile, size, risk)
    if row is None and not all(f in given for f in FIELDS):
        raise PolicyError(f"нет строки политики для {profile}/{size or '*'}/{risk or '*'}: добавь строку "
                          "[[workspace.<проект>.code.policy]] или [[code.policy]] с этими profile, size, risk")
    vals = {f: given[f][0] if f in given else getattr(row, f) for f in FIELDS}
    fields = {f: given[f][1] if f in given else "policy" for f in FIELDS}
    plan_model = row.plan_model if row is not None and row.plan != "no" else None
    plan_effort = row.plan_effort if row is not None and row.plan != "no" else None
    plan_from_row = plan_model is not None
    if vals["plan"] != "no" and plan_model is None:  # план включили сверх строки — фаза плана на итоговых
        plan_model, plan_effort = vals["model"], vals["effort"]
    if vals["plan"] == "no":
        plan_model = plan_effort = None
    label = _row_label(row) if row is not None else None

    # пол и потолок: на выбор оператора не действуют (2.2.1); политика и чат — в пределах
    for f in ("model", "effort"):
        if fields[f] == "operator":
            continue
        what = "чат" if fields[f] == "chat" else f"строка политики {label}"
        try:
            _check_bounds(proj, vals[f] if f == "model" else None, vals[f] if f == "effort" else None, what)
        except PolicyError as ex:
            raise PolicyError(_bounds_hint(str(ex), fields[f])) from None
    if plan_from_row and vals["plan"] != "no":  # модель фазы плана — из строки политики
        # оператор снял пол и потолок для модели/effort задачи — и для фазы плана (критерий 6.3)
        try:
            _check_bounds(proj, None if fields["model"] == "operator" else plan_model,
                          None if fields["effort"] == "operator" else plan_effort,
                          f"фаза плана, строка политики {label}")
        except PolicyError as ex:
            raise PolicyError(_bounds_hint(str(ex), "policy")) from None

    # план понижает только оператор (решение 10): строки нет — понижать не от чего
    if row is not None and fields["plan"] == "chat" and PLAN_MODES.index(vals["plan"]) < PLAN_MODES.index(row.plan):
        raise PolicyError(f"понизить план ниже политики (строка {label}: plan {row.plan}) может только оператор: "
                          "`choice: operator` в шапке, `chosen_by: \"operator\"` или флаг `--plan` в терминале")
    chat = [f for f in FIELDS if fields[f] == "chat"]
    if chat:
        r = clean_reason(reason)
        if len(r) < REASON_MIN:
            raise PolicyError(f"чат задал {', '.join(chat)} в обход политики: укажи причину отступления от "
                              f"политики (reason, ≥ {REASON_MIN} символов) или оставь выбор политике"
                              + (f" (строка {label}: {row.model}/{row.effort}, план {row.plan})" if row else ""))
        source, reason = "chat", r
    elif "operator" in fields.values():
        source, reason = "operator", clean_reason(reason) or None
    else:
        source, reason = "policy", None
    return Choice(vals["model"], vals["effort"], vals["plan"], plan_model, plan_effort, source, label, reason,
                  fields)


def clean_reason(reason: str | None) -> str:
    """Причина без непечатаемых символов (U+200B и т. п.) и краевых пробелов — длину считаем по ней."""
    return "".join(ch for ch in (reason or "") if ch.isprintable()).strip()


def _bounds_hint(msg: str, src: str) -> str:
    if src == "chat":
        return f"{msg} — чат отступает от политики только в пределах потолков проекта"
    return (f"{msg} — подними потолок проекта, задай строку [[workspace.<проект>.code.policy]] "
            "или выбор оператора")

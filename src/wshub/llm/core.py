"""Ядро API хаба (ТЗ WSH-4.2): барьеры реестра, caps провайдера, выбор модели, оценка цены, зоны и потолки трат —
всё до запроса; ключ читается только в момент запроса. Форматов конкретного API ядро не знает."""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path

from ..registry import ApiProvider, Registry
from . import keys, ledger
from .provider import Provider
from .providers import PROVIDERS
from .types import TIERS, Request, Response, Usage


class ApiError(Exception):
    pass


def now_local() -> datetime:
    return datetime.now().astimezone()


def usd(v: float) -> str:
    """Суммы хаба — оценка: до цента и с «≈» (решение оператора, ТЗ WSH-4.2, 0́)."""
    return f"≈${v:.2f}"


CENT = 0.01  # задача Code на ключе без бюджета проходит проверку зон и дня, если остался хотя бы цент (ТЗ WSH-4.6)
HIDDEN = "<ключ скрыт>"
SCRUB_MIN = 8  # куски ключа от стольких символов вырезаются из текста ошибки


def scrub(text: str, key: str) -> str:
    """Ключ и любые его куски от SCRUB_MIN символов (обрезанный, в repr) — вон из текста
    (ревью WSH-4.2, находка 4)."""
    for n in range(len(key), min(SCRUB_MIN, len(key)) - 1, -1):
        for i in range(len(key) - n + 1):
            if key[i:i + n] in text:
                text = text.replace(key[i:i + n], HIDDEN)
    return text


def ddmm(d) -> str:
    return d.strftime("%d.%m") if d else "—"


@dataclass(frozen=True)
class Plan:
    """Проверенный запрос до отправки: модель выбрана, цена оценена, зона определена."""
    provider: Provider
    request: Request  # с выбранной моделью и уровнем
    estimate: Usage  # вход — оценка провайдера, выход — max_tokens
    estimate_usd: float
    zone: str | None  # included | purchased; None — запрос был бы отклонён (refusal)
    refusal: str | None
    zones: ledger.Zones | None  # None — зоны не проверяются (оператор их не задал, ledger.zones_set)
    skipped: int  # нечитаемых строк журнала
    rough: bool = False  # вход оценён грубо (символы / 4): нет ключа или нет count_tokens


class Api:
    def __init__(self, reg: Registry, config_dir: Path, state_dir: Path, now=now_local):
        self.reg = reg
        self.config_dir = Path(config_dir)
        self.ledger_path = ledger.ledger_path(state_dir)
        self.now = now

    def check_section(self) -> None:
        if self.reg.api.error:
            raise ApiError(f"секция [api] в реестре с ошибкой: {self.reg.api.error}")

    def gate(self, project: str) -> None:
        """Барьеры реестра — до чтения файлов проекта."""
        api = self.reg.api
        self.check_section()
        if not api.enabled:
            raise ApiError("API хаба выключен: в реестре [api] enabled = false (или секции [api] нет)")
        if project not in self.reg.workspaces:
            raise ApiError(f"проекта {project} нет в реестре")
        if project in api.project_errors:
            raise ApiError(f"[workspace.{project}.api] с ошибкой: {api.project_errors[project]}")
        if not api.projects.get(project, False):
            raise ApiError(f"проект {project} в API не отправляется: нужен [workspace.{project}.api] enabled = true")

    def provider(self, name: str) -> tuple[Provider, ApiProvider]:
        self.check_section()
        mod = PROVIDERS.get(name)
        if mod is None:
            raise ApiError(f"неизвестный провайдер {name}; есть: {', '.join(PROVIDERS)}")
        conf = self.reg.api.providers.get(name)
        if conf is None:
            raise ApiError(f"провайдер {name} не настроен: нет секции [api.providers.{name}] в реестре")
        return mod.create(conf), conf

    def read_ledger(self) -> ledger.Ledger:
        try:
            return ledger.read(self.ledger_path)
        except ledger.LedgerError as e:
            raise ApiError(str(e)) from None

    def zones(self, name: str, led: ledger.Ledger | None = None) -> ledger.Zones:
        _prov, conf = self.provider(name)
        return ledger.zones(led or self.read_ledger(), name, conf, self.now().date())

    def plan(self, req: Request, name: str, choice: str | None = None) -> Plan:
        self.gate(req.project)
        prov, conf = self.provider(name)
        need = [c for c, used in (("schema", req.schema is not None), ("effort", req.effort is not None),
                                  ("cache", req.cache != "none")) if used and c not in prov.caps]
        if need:
            raise ApiError(f"провайдер {name} не поддерживает {', '.join(need)} — запрос не отправлен")
        own = prov.models()
        tiers = {**own, **conf.models}
        if req.tier and req.model:
            raise ApiError("уровень (--tier) и модель (--model) — что-то одно")
        if req.tier:
            if req.tier not in TIERS or not tiers.get(req.tier):
                raise ApiError(f"уровень {req.tier}: одно из {', '.join(t for t in TIERS if tiers.get(t))}")
            model, tier = tiers[req.tier], req.tier
        elif req.model:
            model = req.model
            tier = next((t for t in reversed(TIERS) if tiers.get(t) == model), None)
            # модель вне уровней (с учётом переопределений) — только явным выбором оператора, как strong
            # (ревью WSH-4.2, решение 1; ТЗ WSH-4.3, п. 2.2.1)
            if model not in tiers.values() and choice != "operator":
                levels = ", ".join(f"{t} — {tiers[t]}" for t in TIERS if tiers.get(t))
                raise ApiError(f"модель {model} не из уровней провайдера {name} ({levels}); другая модель — только "
                               "явным выбором оператора: --choice operator")
        else:
            raise ApiError("нужен уровень (--tier) или модель (--model)")
        # сильная — и по уровню, и по модели: strong из реестра или собственная strong провайдера, даже если
        # переопределение уровней её убрало или поставило на другой уровень (ревью WSH-4.2, находка 2)
        strong = tier == "strong" or model in {tiers.get("strong"), own.get("strong")} - {None}
        if strong and conf.strong_requires_choice and choice != "operator":
            raise ApiError(f"сильная модель ({model}) — только явным выбором оператора: --choice operator "
                           f"(strong_requires_choice в [api.providers.{name}])")
        req = replace(req, model=model, tier=tier)
        inp, rough = self._estimate(name, prov, req)
        try:
            est = replace(inp, output=req.max_tokens)
            est_usd = prov.price(model, est)
        except KeyError:
            raise ApiError(f"цена модели {model} у провайдера {name} неизвестна — запрос не отправлен") from None
        led = self.read_ledger()
        z = ledger.zones(led, name, conf, self.now().date()) if ledger.zones_set(led, name, conf) else None
        zone, refusal = self._zone(name, est_usd, z, led)
        return Plan(prov, req, est, est_usd, zone, refusal, z, led.skipped, rough)

    def _estimate(self, name: str, prov: Provider, req: Request) -> tuple[Usage, bool]:
        """Оценка входа (ТЗ WSH-4.3, п. 2.1.4): при count_tokens и ключе — точная, ключ передаётся провайдеру;
        ключа нет (или он с ошибкой — отказ скажет запрос) — грубая. Сбой оценки — отказ без ключа в тексте."""
        key = None
        if "count_tokens" in prov.caps:
            try:
                key = keys.read(keys.key_path(self.config_dir, name))
            except keys.KeyProblem:
                key = None
        if key is None:
            return prov.estimate(req), True
        error = None
        try:
            est = prov.estimate(req, key=key)
        except Exception as e:  # сбой провайдера — понятный отказ, ключ вырезан
            error = scrub(f"{type(e).__name__}: {e}", key)
        del key
        if error is not None:  # вне except: без __context__ с исходным текстом
            raise ApiError(f"провайдер {name}: оценка входа: {error}")
        return est, False

    def _zone(self, name: str, est: float, z: ledger.Zones | None, led: ledger.Ledger,
              per_call: bool = True, day: bool = True) -> tuple[str | None, str | None]:
        """Потолки, затем зона: включённая, если оценка помещается в остаток; иначе купленная — только по
        действующему разрешению оператора (allow). Для задач Code на ключе (ТЗ WSH-4.6, решения 5.1.5 и 5.2):
        per_call=False — без max_usd_per_call (это потолок одного запроса API); day=False — daily_usd в реестре не
        записан; z=None — зоны не проверяются (оператор их не задал), строка идёт во включённую зону. Потолок, не
        записанный в реестре, не проверяется (ТЗ WSH-4.3, п. 2.2.2)."""
        api = self.reg.api
        if per_call and api.max_usd_per_call is not None and est > api.max_usd_per_call:
            return None, (f"оценка запроса {usd(est)} выше потолка max_usd_per_call = ${api.max_usd_per_call:.2f}; "
                          "уменьши --max-tokens или вход")
        today = ledger.spent_today(led, self.now())
        if day and api.daily_usd is not None and today + est > api.daily_usd:
            return None, (f"дневной потолок daily_usd = ${api.daily_usd:.2f}: сегодня уже {usd(today)}, "
                          f"запрос {usd(est)}")
        if z is None:
            return "included", None
        if est <= z.included_left:
            return "included", None
        if z.allow_usd is not None and est <= z.purchased_left:
            return "purchased", None
        msg = (f"включённые кредиты исчерпаны ({usd(z.included_left)}); купленные — только по разрешению "
               f"оператора: wshub api allow {name} --usd <сумма> --until ДД.ММ")
        if z.allow_usd is not None:
            msg += (f" (действующее разрешение: осталось {usd(z.purchased_left)} из ${z.allow_usd:.2f}, "
                    f"запрос {usd(est)})")
        return None, msg

    def run(self, plan: Plan) -> Response:
        """Один запрос по проверенному плану. Зона и потолки проверяются заново под блокировкой журнала до записи
        строки: параллельный запрос или команда оператора между plan и run не дают потратить сверх остатка
        (ревью WSH-4.2, находки 3 и 18). Ключ — только здесь; в журнал, ответ и исключения не попадает."""
        if plan.zone is None:
            raise ApiError(f"{plan.refusal} — запрос не отправлен")
        name = plan.provider.name
        with ledger.locked(self.ledger_path):
            _prov, conf = self.provider(name)
            led = self.read_ledger()
            z = ledger.zones(led, name, conf, self.now().date()) if ledger.zones_set(led, name, conf) else None
            zone, refusal = self._zone(name, plan.estimate_usd, z, led)
            if zone is None:
                raise ApiError(f"{refusal} — запрос не отправлен")
            return self._send(plan, zone)

    def code_reserve(self, name: str, project: str, model: str, budget: float | None, job: str) -> tuple[dict, bool]:
        """Старт звена задачи Code на ключе (ТЗ WSH-4.6, п. 2.5.2–2.5.4 и упрощение 5.2): проверяется только то, что
        оператор записал в реестр, — потолок дня, если задан [api] daily_usd, и зоны, если у провайдера задан
        included_usd. Проверка и строка-резерв с job — под блокировкой журнала; не помещается — ApiError с текстом 4.2.
        Нечего проверять — резерва нет. Ответ — (строка звена, проверялись ли зоны); reserve в строке — записан ли
        резерв; по итогу её заменит или дополнит строка с фактом (code_settle). Без бюджета звена проверка требует
        хотя бы цента остатка."""
        self.check_section()
        conf = self.reg.api.providers.get(name)
        by_zone, day = conf is not None and conf.included_set, self.reg.api.daily_set
        rec = {"kind": "run", "provider": name, "model": model, "tier": None, "project": project, "template": "code",
               "zone": "included", "job": job, "usage": Usage().as_dict()}
        if not by_zone and not day:
            return {**rec, "ts": ledger.stamp(self.now()), "cost_usd": 0.0, "reserve": False}, False
        with ledger.locked(self.ledger_path):
            led = self.read_ledger()
            z = ledger.zones(led, name, conf, self.now().date()) if by_zone else None
            zone, refusal = self._zone(name, CENT if budget is None else budget, z, led, per_call=False, day=day)
            if zone is None:
                raise ApiError(f"{refusal} — задача не запущена")
            rec.update(ts=ledger.stamp(self.now()), zone=zone, cost_usd=float(budget or 0.0), reserve=True)
            ledger.append(self.ledger_path, rec)
            return rec, by_zone

    def code_settle(self, rec: dict, cost: float, ok: bool | None) -> None:
        """Факт звена вместо резерва: строка того же job (ТЗ WSH-4.6, п. 2.5.4)."""
        with ledger.locked(self.ledger_path):
            ledger.append(self.ledger_path, {**rec, "ts": ledger.stamp(self.now()), "cost_usd": float(cost),
                                             "reserve": False, "ok": ok})

    def _send(self, plan: Plan, zone: str) -> Response:
        name, req = plan.provider.name, plan.request
        try:
            key = keys.read(keys.key_path(self.config_dir, name))
        except keys.KeyProblem as e:
            raise ApiError(str(e)) from None
        rec = {"ts": ledger.stamp(self.now()), "kind": "run", "provider": name, "model": req.model,
               "tier": req.tier, "project": req.project, "template": req.template, "zone": zone}
        error = None
        try:
            resp = plan.provider.run(req, key)
        except Exception as e:  # любой сбой провайдера — в журнал и в понятный отказ
            error = scrub(f"{type(e).__name__}: {e}", key)
        del key
        if error is not None:  # исключение поднимаем вне except: без __context__ с исходным текстом
            ledger.append(self.ledger_path, {**rec, "usage": Usage().as_dict(), "cost_usd": 0.0, "ok": False,
                                             "error": error})
            raise ApiError(f"провайдер {name}: {error}")
        try:
            cost = plan.provider.price(resp.model, resp.usage)
        except KeyError:  # модель ответа не из таблицы цен — считаем по оценке до запроса
            cost = plan.estimate_usd
        ledger.append(self.ledger_path, {**rec, "model": resp.model, "usage": resp.usage.as_dict(),
                                         "cost_usd": cost, "ok": True})
        return replace(resp, cost_usd=cost)

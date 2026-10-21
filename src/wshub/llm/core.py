"""Ядро API хаба (ТЗ WSH-4.2): барьеры реестра, caps провайдера, выбор модели, оценка цены, зоны и потолки трат —
всё до запроса; ключ читается только в момент запроса и для точной оценки входа (count_tokens, ТЗ WSH-4.3).
Форматов конкретного API ядро не знает."""
from __future__ import annotations

import json
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path

from ..registry import ApiProvider, Registry
from . import batches, keys, ledger
from .provider import Provider, rough_tokens
from .providers import PROVIDERS
from .types import TIERS, BatchItem, Request, Response, Usage


class ApiError(Exception):
    pass


class PaidError(ApiError):
    """HTTP-ответ получен (запрос оплачен), но не разобран: битый JSON по схеме, не JSON-объект, usage. Ядро пишет
    в журнал стоимость по usage, а без него — оценку до запроса (ТЗ WSH-4.4, п. 2.3а.1). preview — начало ответа:
    в тексте исключения есть, в журнал вызовов сервера не идёт (там msg). cost_usd — стоимость, записанная ядром в
    журнал трат (у исключения, которое поднимает ядро после записи)."""

    def __init__(self, msg: str, usage: Usage | None = None, model: str | None = None, preview: str = "",
                 cost_usd: float | None = None):
        super().__init__(msg)
        self.msg, self.usage, self.model, self.preview, self.cost_usd = msg, usage, model, preview, cost_usd

    def __str__(self) -> str:
        return f"{self.msg}: {self.preview}" if self.preview else self.msg


def now_local() -> datetime:
    return datetime.now().astimezone()


def usd(v: float) -> str:
    """Суммы хаба — оценка: до цента и с «≈» (решение оператора, ТЗ WSH-4.2, 0́)."""
    return f"≈${v:.2f}"


CENT = 0.01  # задача Code на ключе без бюджета проходит проверку зон и дня, если остался хотя бы цент (ТЗ WSH-4.6)
BATCH_PRICE = 0.5  # пакет: −50 % ко входу и выходу (pricing, 2026-10-20); к кэшу — допущение PoC WSH-4.0 (ТЗ WSH-4.5)
BATCH_MAX = 1000  # запросов в пакете хаба
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


def _cache_split(inp: Usage, req: Request) -> Usage:
    """Оценка с записью в кэш (ТЗ WSH-4.4, п. 2.3а.2): при cache = short | long системная часть входа идёт в
    cache_write (long — ещё и в cache_write_1h) и оценивается по цене записи. Её размер — грубо, символы / 4, не больше
    всего входа (решение Р2)."""
    if req.cache == "none" or not req.system:
        return inp
    w = min(rough_tokens(req.system), inp.input)
    return replace(inp, input=inp.input - w, cache_write=inp.cache_write + w,
                   cache_write_1h=inp.cache_write_1h + (w if req.cache == "long" else 0))


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
    strong: bool = False  # сильная модель (по уровню или по модели) — сервер требует reason (ТЗ WSH-4.4, Р1)
    off_tier: bool = False  # модель вне уровней провайдера (с учётом переопределений)


class Api:
    def __init__(self, reg: Registry, config_dir: Path, state_dir: Path, now=now_local):
        self.reg = reg
        self.config_dir = Path(config_dir)
        self.state_dir = Path(state_dir)
        self.ledger_path = ledger.ledger_path(state_dir)
        self.now = now

    def check_section(self) -> None:
        if self.reg.api.error:
            raise ApiError(f"секция [api] в реестре с ошибкой: {self.reg.api.error}")

    def gate(self, project: str) -> None:
        """Барьеры реестра — до чтения файлов проекта. API и проект включены по умолчанию, закрывает их только явный
        enabled = false (ТЗ WSH-4.4, п. 2.3б)."""
        api = self.reg.api
        self.check_section()
        if not api.enabled:
            raise ApiError("API хаба выключен: в реестре [api] enabled = false")
        if project not in self.reg.workspaces:
            raise ApiError(f"проекта {project} нет в реестре")
        if project in api.project_errors:
            raise ApiError(f"[workspace.{project}.api] с ошибкой: {api.project_errors[project]}")
        if not api.projects.get(project, True):
            raise ApiError(f"проект {project} закрыт от отправки в API: [workspace.{project}.api] enabled = false")

    def key_problem(self, name: str) -> str | None:
        """Почему ключа провайдера нет или он негоден; None — ключ есть и права верны. Значение отбрасывается."""
        try:
            keys.read(keys.key_path(self.config_dir, name))
        except keys.KeyProblem as e:
            return str(e)
        except OSError as e:  # каталог ключей не читается (нет прав) — ключа для хаба нет, а не падение
            return f"ключ {name} не читается: {e.strerror or type(e).__name__}"
        return None

    def provider(self, name: str) -> tuple[Provider, ApiProvider]:
        """Провайдер из словаря. Без секции [api.providers.<имя>] — умолчания модуля и без зон, но только при годном
        ключе (ТЗ WSH-4.4, п. 2.3б.1); секция нужна лишь для переопределений."""
        self.check_section()
        mod = PROVIDERS.get(name)
        if mod is None:
            raise ApiError(f"неизвестный провайдер {name}; есть: {', '.join(PROVIDERS)}")
        conf = self.reg.api.providers.get(name)
        if conf is None:
            bad = self.key_problem(name)
            if bad:
                raise ApiError(f"провайдер {name}: нет секции [api.providers.{name}] в реестре и нет годного ключа "
                               f"({bad})")
            conf = ApiProvider()
        return mod.create(conf), conf

    def available(self) -> dict[str, ApiProvider]:
        """Доступные провайдеры: с секцией в реестре — и провайдеры словаря без секции, но с годным ключом."""
        self.check_section()
        out = dict(self.reg.api.providers)
        for name in PROVIDERS:
            if name not in out and self.key_problem(name) is None:
                out[name] = ApiProvider()
        return out

    def read_ledger(self) -> ledger.Ledger:
        try:
            return ledger.read(self.ledger_path)
        except ledger.LedgerError as e:
            raise ApiError(str(e)) from None

    def zones(self, name: str, led: ledger.Ledger | None = None) -> ledger.Zones:
        _prov, conf = self.provider(name)
        return ledger.zones(led or self.read_ledger(), name, conf, self.now().date())

    def plan(self, req: Request, name: str, choice: str | None = None, batch: bool = False,
             exact: bool = True) -> Plan:
        """batch — запрос пакета: оценка со скидкой BATCH_PRICE; exact=False — вход грубо, без count_tokens."""
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
        try:
            prov.price(model, Usage())  # цена неизвестна — отказ до оценки входа (count_tokens — запрос с ключом)
        except KeyError:
            raise ApiError(f"цена модели {model} у провайдера {name} неизвестна — запрос не отправлен") from None
        inp, rough = self._estimate(name, prov, req) if exact else (prov.estimate(req), True)
        est = replace(_cache_split(inp, req), output=req.max_tokens)
        est_usd = prov.price(model, est) * (BATCH_PRICE if batch else 1.0)
        led = self.read_ledger()
        z = ledger.zones(led, name, conf, self.now().date()) if ledger.zones_set(led, name, conf) else None
        zone, refusal = self._zone(name, est_usd, z, led)
        return Plan(prov, req, est, est_usd, zone, refusal, z, led.skipped, rough, strong=strong,
                    off_tier=model not in tiers.values())

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
        error, paid = None, None
        try:
            resp = plan.provider.run(req, key)
        except PaidError as e:  # ответ получен и оплачен, но не разобран (ТЗ WSH-4.4, п. 2.3а.1)
            # подпись ApiError — как у прочих отказов провайдера: текст ошибки CLI 4.3 не меняется (ревью, п. 10)
            error = scrub(f"ApiError: {e}", key)
            paid = (e.usage, e.model or req.model, scrub(f"ApiError: {e.msg}", key), scrub(e.preview, key))
        except Exception as e:  # любой сбой провайдера — в журнал и в понятный отказ
            error = scrub(f"{type(e).__name__}: {e}", key)
        del key
        if paid is not None:  # стоимость — по usage ответа; usage нет или модель вне таблицы — оценка до запроса
            usage, model, msg, preview = paid
            try:
                cost = plan.provider.price(model, usage) if usage is not None else plan.estimate_usd
            except KeyError:
                cost = plan.estimate_usd
            ledger.append(self.ledger_path, {**rec, "model": model, "usage": (usage or Usage()).as_dict(),
                                             "cost_usd": cost, "ok": False, "error": error})
            raise PaidError(f"провайдер {name}: {msg}", model=model, preview=preview, cost_usd=cost)
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

    # ---------- пакеты (ТЗ WSH-4.5) ----------

    def batch_provider(self, name: str) -> Provider:
        prov, _conf = self.provider(name)
        if "batch" not in prov.caps:
            raise ApiError(f"провайдер {name} не поддерживает batch — пакет не отправлен")
        return prov

    def money_checks(self, name: str) -> bool:
        """Задана ли хоть одна денежная проверка: max_usd_per_call, daily_usd или зоны провайдера (решение Р2)."""
        _prov, conf = self.provider(name)
        api = self.reg.api
        return (api.max_usd_per_call is not None or api.daily_usd is not None
                or ledger.zones_set(self.read_ledger(), name, conf))

    def batch_plan(self, requests: list[Request], name: str, choice: str | None = None) -> list[Plan]:
        """Каждый запрос пакета — те же проверки, что у run (выбор модели, choice для strong, потолок одного запроса —
        по оценке со скидкой). count_tokens — только при денежной проверке (решение Р2), иначе вход грубо, без сети."""
        self.batch_provider(name)
        if not requests:
            raise ApiError("пакет пуст: нет входов")
        check_batch_size(len(requests))
        exact = self.money_checks(name)
        return [self.plan(r, name, choice, batch=True, exact=exact) for r in requests]

    def _keyed(self, name: str, what: str, call):
        """Вызов провайдера с ключом: ключ — только здесь; ошибка — ApiError без ключа и без цепочки исключений."""
        try:
            key = keys.read(keys.key_path(self.config_dir, name))
        except keys.KeyProblem as e:
            raise ApiError(str(e)) from None
        error = None
        try:
            out = call(key)
        except Exception as e:
            error = scrub(f"{type(e).__name__}: {e}", key)
        del key
        if error is not None:
            raise ApiError(f"провайдер {name}: {what}: {error}")
        return out

    def submit(self, plans: list[Plan], ids: list[str], meta: dict) -> dict:
        """Отправка пакета по проверенным планам (ТЗ WSH-4.5, п. 2.1.2; решение Р1): потолок дня и зоны — на сумму
        оценок, заново под блокировкой журнала до отправки. Резерва в журнале нет: траты — строками results. Ответ —
        запись учёта; meta — project, out_dir, inputs {custom_id: {input, file}}."""
        for cid, p in zip(ids, plans, strict=True):
            if p.zone is None:
                raise ApiError(f"{cid}: {p.refusal} — пакет не отправлен")
        prov, first = plans[0].provider, plans[0].request
        name, total = prov.name, sum(p.estimate_usd for p in plans)
        with ledger.locked(self.ledger_path):
            _prov, conf = self.provider(name)
            led = self.read_ledger()
            z = ledger.zones(led, name, conf, self.now().date()) if ledger.zones_set(led, name, conf) else None
            zone, refusal = self._zone(name, total, z, led, per_call=False)
            if zone is None:
                raise ApiError(f"пакет: {refusal} — пакет не отправлен")
            pairs = [(cid, p.request) for cid, p in zip(ids, plans, strict=True)]
            bid = self._keyed(name, "пакет", lambda key: prov.batch_submit(pairs, key))
        try:
            batches.check_id(bid)
        except batches.BatchError:
            raise ApiError(f"провайдер {name}: пакет создан, но id не годится для учёта: {str(bid)[:80]!r}") from None
        inputs = {cid: {**meta["inputs"][cid], "estimate_usd": p.estimate_usd}
                  for cid, p in zip(ids, plans, strict=True)}
        rec = {"id": bid, "provider": name, "project": first.project, "model": first.model, "tier": first.tier,
               "schema": first.schema is not None, "requests": len(plans), "inputs": inputs,
               "created": ledger.stamp(self.now()), "status": "in_progress", "counts": {}, "ended_at": None,
               "expires_at": None, "out_dir": meta.get("out_dir"), "zone": zone, "estimate_usd": total,
               "rough": any(p.rough for p in plans)}
        try:
            batches.save(self.state_dir, rec)
        except OSError as e:
            raise ApiError(f"пакет {bid} создан у провайдера, но учёт не записан ({e.strerror or type(e).__name__}); "
                           "результаты хаб не разложит — отмени пакет в консоли провайдера") from None
        return rec

    def _refresh(self, rec: dict, state: dict) -> dict:
        rec.update(status=state.get("status", "?"), counts=state.get("counts", {}), ended_at=state.get("ended_at"),
                   expires_at=state.get("expires_at"), checked=ledger.stamp(self.now()))
        batches.save(self.state_dir, rec)
        return rec

    def batch_status(self, batch_id: str) -> dict:
        rec = batches.load(self.state_dir, batch_id)
        prov = self.batch_provider(rec["provider"])
        return self._refresh(rec, self._keyed(rec["provider"], "состояние пакета",
                                              lambda key: prov.batch_status(batch_id, key)))

    def batch_list(self) -> tuple[list[dict], int]:
        """Пакеты учёта; незаконченные — с состоянием от провайдера, сбой — в поле error записи."""
        recs, bad = batches.listing(self.state_dir)
        for rec in recs:
            if rec.get("status") != "ended":
                try:
                    rec.update(self.batch_status(rec["id"]))
                except (ApiError, keys.KeyProblem, OSError, ValueError) as e:
                    rec["error"] = str(e)
        return recs, bad

    def batch_cancel(self, batch_id: str) -> dict:
        rec = batches.load(self.state_dir, batch_id)
        prov = self.batch_provider(rec["provider"])
        return self._refresh(rec, self._keyed(rec["provider"], "отмена пакета",
                                              lambda key: prov.batch_cancel(batch_id, key)))

    def batch_results(self, batch_id: str) -> tuple[dict, list[dict]]:
        """Результаты законченного пакета (п. 2.1.4): разбор по custom_id, цена со скидкой, строка журнала на каждый
        результат. Журнал — источник истины (решение Р6): под блокировкой пишутся только custom_id, которых в строках
        этого пакета ещё нет, — повторная выборка строк не дублирует. Ответ — (учёт, результаты: custom_id, ok,
        response, error, cost_usd, logged)."""
        rec = batches.load(self.state_dir, batch_id)
        name = rec["provider"]
        prov = self.batch_provider(name)
        rec = self._refresh(rec, self._keyed(name, "состояние пакета", lambda key: prov.batch_status(batch_id, key)))
        if rec["status"] != "ended":
            raise ApiError(f"пакет {batch_id} ещё не закончен ({rec['status']}, {batch_counts(rec)}); "
                           "результаты — после ended, ожидание до 24 ч")

        def fetch(key: str) -> list[BatchItem]:
            return [replace(i, error=scrub(i.error, key)) if i.error else i
                    for i in prov.batch_results(batch_id, key)]
        items = [self._batch_item(prov, rec, i) for i in self._keyed(name, "результаты пакета", fetch)]
        with ledger.locked(self.ledger_path):
            done = {r.get("custom_id") for r in self.read_ledger().rows if r.get("batch") == batch_id}
            base = {"kind": "run", "provider": name, "tier": rec.get("tier"), "project": rec["project"],
                    "template": None, "batch": batch_id, "zone": rec.get("zone") or "included"}
            for it in items:
                it["logged"] = it["custom_id"] not in done
                if not it["logged"]:
                    continue
                row = {"ts": ledger.stamp(self.now()), **base, "custom_id": it["custom_id"], "model": it["model"],
                       "usage": it["usage"].as_dict(), "cost_usd": it["cost_usd"], "ok": it["ok"]}
                if it["error"]:
                    row["error"] = it["error"]
                ledger.append(self.ledger_path, row)
                done.add(it["custom_id"])
        return rec, items

    def _batch_item(self, prov: Provider, rec: dict, item: BatchItem) -> dict:
        """Результат → ok, ответ, цена со скидкой (решение Р7): ответ по схеме не разобран — ошибка, цена по usage;
        оплачен, но usage не разобран — оценка запроса из учёта; не оплачен (errored, canceled, expired) — $0."""
        est = (rec["inputs"].get(item.custom_id) or {}).get("estimate_usd", 0.0)
        resp, error, usage = item.response, item.error, Usage()
        model = resp.model if resp else (item.model or rec.get("model"))
        if resp is not None:
            usage = resp.usage
            if rec.get("schema"):
                try:
                    resp = replace(resp, data=json.loads(resp.text))
                except ValueError:
                    resp, error = None, "ответ не разобран как JSON по схеме"
            try:
                cost = prov.price(model, usage) * BATCH_PRICE
            except KeyError:  # модель ответа не из таблицы цен — оценка запроса
                cost = est
        else:
            cost = est if item.model is not None else 0.0
        if resp is not None:
            resp = replace(resp, cost_usd=cost)
        return {"custom_id": item.custom_id, "ok": resp is not None, "response": resp, "error": error,
                "model": model, "usage": usage, "cost_usd": float(cost)}


def check_batch_size(n: int) -> None:
    if n > BATCH_MAX:
        raise ApiError(f"пакет — до {BATCH_MAX} запросов, а входов больше ({n}): раздели на несколько пакетов")


def batch_counts(rec: dict) -> str:
    c = rec.get("counts") or {}
    names = (("succeeded", "успешно"), ("errored", "с ошибкой"), ("processing", "в работе"),
             ("canceled", "отменено"), ("expired", "истекло"))
    return ", ".join(f"{ru} {c[k]}" for k, ru in names if c.get(k)) or "счётчиков нет"

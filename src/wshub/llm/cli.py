"""wshub api …: ключи, запрос к модели, траты, остаток и разрешения оператора (ТЗ WSH-4.2, п. 2.5)."""
from __future__ import annotations

import getpass
import json
import math
import re
import secrets
import sys
import warnings
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

from ..core import Hub, Session, WsError, _is_file
from ..policy import Policy
from ..registry import Registry, RegistryError, RegistryFile
from . import keys, ledger
from .core import Api, ApiError, Plan, ddmm, now_local, usd
from .providers import PROVIDERS
from .types import Request, Response

OUT_MAX = 8 * 1024  # длиннее — ответ в файл, на экран путь
ZONE_RU = {"included": "включённая", "purchased": "купленная"}
USAGE = """\
wshub api                         — MCP-сервер wshub-api (stdio); его запускает Claude Desktop
wshub api key set <провайдер>     — задать ключ API (скрытый ввод), файл с правами 0600
wshub api key check <провайдер>   — есть ли ключ и верны ли права (значение не выводится)
wshub api run --provider P --project X (--tier T | --model M) [--system FILE] --input FILE [--schema FILE]
              [--max-tokens N] [--effort E] [--choice operator] [--estimate] [--out FILE]
wshub api stats [--days N]        — траты за цикл (или за N дней): провайдеры, зоны, модели, проекты
wshub api credits <провайдер> --left USD --until ДД.ММ[.ГГГГ]   — остаток включённых кредитов, новый цикл
wshub api allow <провайдер> --usd USD --until ДД.ММ[.ГГГГ]      — разрешить купленную зону
wshub api allow <провайдер> --revoke                            — снять разрешение"""


class _Usage(Exception):
    pass


def api_main(rest: list[str], config: Path, state: Path, now=now_local) -> int:
    if not rest:  # без аргументов — MCP-сервер wshub-api, как wshub code для wshub-code (ТЗ WSH-4.4, п. 2.1.1)
        from ..api_server import serve
        return serve(Path(config), Path(state))
    if rest[0] in ("-h", "--help", "help"):
        print(USAGE)
        return 0
    sub, args = rest[0], rest[1:]
    handlers = {"key": _key, "run": _run, "stats": _stats, "credits": _credits, "allow": _allow}
    if sub not in handlers:
        print(USAGE, file=sys.stderr)
        return 2
    try:
        return handlers[sub](Path(config), Path(state), now, args)
    except _Usage as e:
        print(f"wshub api {sub}: {e}\n{USAGE}", file=sys.stderr)
        return 2
    except (ApiError, keys.KeyProblem, WsError, RegistryError, OSError, ValueError) as e:
        print(f"wshub api: отказ: {e}", file=sys.stderr)  # без трассировки
        return 1


def _parse(args: list[str], valued: set[str], flags: tuple[str, ...] = ()) -> tuple[list[str], dict]:
    pos, opts, i = [], {}, 0
    while i < len(args):
        a = args[i]
        if a in flags:
            opts[a[2:]] = True
            i += 1
        elif a in valued:
            if i + 1 >= len(args):
                raise _Usage(f"после {a} нужно значение")
            opts[a[2:]] = args[i + 1]
            i += 2
        elif a.startswith("--"):
            raise _Usage(f"неизвестный параметр {a}")
        else:
            pos.append(a)
            i += 1
    return pos, opts


def _int(v: str, what: str) -> int:
    if not re.fullmatch(r"[0-9]+", v) or int(v) < 1:
        raise _Usage(f"{what}: целое число больше 0")
    return int(v)


def _money(v: str, what: str, zero: bool = False) -> float:
    try:
        x = float(v)
    except ValueError:
        x = math.nan
    if not math.isfinite(x) or x < 0 or (x == 0 and not zero):
        raise _Usage(f"{what}: сумма в USD{', ≥ 0' if zero else ' больше 0'}, например 9.99")
    return x


def parse_until(s: str, today: date) -> date:
    """ДД.ММ[.ГГГГ]; без года — ближайшая будущая дата (сегодня включительно). Срок действует весь этот день."""
    m = re.fullmatch(r"(\d{1,2})\.(\d{1,2})(?:\.(\d{4}))?", s)
    if not m:
        raise _Usage(f"--until {s}: дата ДД.ММ или ДД.ММ.ГГГГ")
    d, mo = int(m[1]), int(m[2])
    try:
        if m[3]:
            dt = date(int(m[3]), mo, d)
        else:
            dt = date(today.year, mo, d)
            if dt < today:
                dt = date(today.year + 1, mo, d)
    except ValueError:
        raise _Usage(f"--until {s}: нет такой даты") from None
    if dt < today:
        raise ApiError(f"--until {s}: дата уже прошла")
    return dt


def _api(config: Path, state: Path, now, reg: Registry | None = None) -> Api:
    return Api(reg or RegistryFile(config).get(), config.parent, state, now)


def _burn(z: ledger.Zones | None) -> list[str]:
    if z is None or not z.burn:
        return []
    return [f"⚠ сгорит: {usd(z.included_left)} включённых кредитов {z.provider} до {ddmm(z.until)} "
            f"(дней осталось: {z.days_left})"]


def _skipped(led_skipped: int) -> list[str]:
    return [f"журнал: пропущено строк {led_skipped}"] if led_skipped else []


# ---------- key ----------

def _key(config: Path, state: Path, now, args: list[str]) -> int:
    if len(args) != 2 or args[0] not in ("set", "check"):
        raise _Usage("key set <провайдер> | key check <провайдер>")
    op, name = args
    if name not in PROVIDERS:  # имя идёт в путь — только из словаря провайдеров
        raise ApiError(f"неизвестный провайдер {name}; есть: {', '.join(PROVIDERS)}")
    path = keys.key_path(config.parent, name)
    if op == "check":
        bad = keys.problems(path)
        if not bad:
            try:
                keys.read(path)  # пустой или не текстовый ключ — тот же отказ, что при запросе; значение не выводится
            except keys.KeyProblem as e:
                bad = [str(e)]
        if bad:
            print("\n".join(bad))
            return 1
        print(f"ключ есть, права верны: {path}")
        return 0
    with warnings.catch_warnings():  # без терминала getpass вводит с эхом — тогда отказ, а не открытый ввод
        warnings.simplefilter("error", getpass.GetPassWarning)
        try:
            value = getpass.getpass(f"Ключ {name} (ввод не отображается): ")
        except getpass.GetPassWarning:
            raise ApiError("нет терминала для скрытого ввода — ключ не записан; "
                           "запусти команду в терминале") from None
    if not value.strip():
        raise ApiError("пустой ввод — ключ не записан")
    keys.write(path, value)
    del value
    print(f"ключ записан: {path} (права 0600)")
    return 0


# ---------- run ----------

RUN_VALUED = {"--provider", "--project", "--tier", "--model", "--system", "--input", "--schema", "--max-tokens",
              "--effort", "--out", "--choice"}


def _session(hub: Hub, reg: Registry, project: str) -> Session:
    """Файлы --input / --system / --schema читаются по политике проекта: путь внутри корня, deny-маски действуют."""
    ws = reg.workspaces[project]
    root = ws.path.resolve()
    why = hub.workspace_conflict(root, "ro")
    if why:
        raise ApiError(why)
    if not root.is_dir():
        raise ApiError(f"корень проекта {project} не найден: {ws.path}")
    return Session(ws, root, "ro", Policy(ws.deny), reg.max_read_kb * 1024)


def _read(hub: Hub, s: Session, path: str) -> str:
    real, rel = hub._resolve(s, path)  # вне корня, симлинк наружу, deny — WsError
    if not _is_file(real):
        raise ApiError(f"нет такого файла в проекте: {path}")
    if real.stat().st_size > s.max_read:
        raise ApiError(f"файл {rel} больше max_read_kb = {s.max_read // 1024} КБ")
    return real.read_text(encoding="utf-8")


def _out_path(state: Path, now: datetime) -> Path:
    return state / "api" / "out" / f"{now:%Y%m%d-%H%M%S}-{secrets.token_hex(3)}.txt"


NO_ZONES = "зоны не проверяются: не заданы ни included_usd, ни команда credits"


def _cycle(z: ledger.Zones) -> str:
    return f"цикл {usd(z.included_spent)} из ${z.included_total:.2f}, до {ddmm(z.until)}"


def _run(config: Path, state: Path, now, args: list[str]) -> int:
    pos, o = _parse(args, RUN_VALUED, ("--estimate",))
    if pos:
        raise _Usage(f"лишние аргументы: {' '.join(pos)}")
    for k in ("provider", "project", "input"):
        if k not in o:
            raise _Usage(f"нужен --{k}")
    if "choice" in o and o["choice"] != "operator":
        raise _Usage("--choice: только operator")
    max_tokens = _int(o.get("max-tokens", "1024"), "--max-tokens")
    hub = Hub(config, state)
    reg = hub.registry.get()
    api = _api(config, state, now, reg)
    api.gate(o["project"])  # выключенный API и проект с api.enabled = false — отказ до чтения файлов
    api.provider(o["provider"])
    s = _session(hub, reg, o["project"])
    text = _read(hub, s, o["input"])
    system = _read(hub, s, o["system"]) if "system" in o else ""
    schema = None
    if "schema" in o:
        schema = json.loads(_read(hub, s, o["schema"]))
        if not isinstance(schema, dict):
            raise ApiError("--schema: ожидается JSON-объект схемы")
    req = Request(system, ({"role": "user", "text": text},), o["project"], tier=o.get("tier"),
                  model=o.get("model"), schema=schema, effort=o.get("effort"), max_tokens=max_tokens)
    plan = api.plan(req, o["provider"], o.get("choice"))
    if o.get("estimate"):
        print("\n".join(estimate_lines(plan)))
        return 0
    resp = api.run(plan)
    body = answer_body(resp)
    if "out" in o or len(body.encode("utf-8")) > OUT_MAX:
        path = Path(o["out"]) if "out" in o else _out_path(state, now())
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
        print(f"ответ: {path}")
    else:
        print(body)
    print("\n".join(run_lines(api, plan, resp)))
    return 0


def answer_body(resp: Response) -> str:
    """Ответ модели для вывода: JSON по схеме (data) с отступами или текст."""
    return json.dumps(resp.data, ensure_ascii=False, indent=2) if resp.data is not None else resp.text


def estimate_lines(plan: Plan) -> list[str]:
    """Оценка без запроса: модель, вход, выход, сумма, зона или причина отказа, цикл; общий вывод CLI --estimate и
    api_estimate сервера wshub-api."""
    return [f"оценка: {plan.request.model} · вход ≈{plan.estimate.input}{' (грубо)' if plan.rough else ''} · "
            f"выход до {plan.estimate.output} · {usd(plan.estimate_usd)}",
            f"зона: {ZONE_RU[plan.zone]}" if plan.zone else f"запрос был бы отклонён: {plan.refusal}",
            _cycle(plan.zones) if plan.zones is not None else NO_ZONES,
            *_skipped(plan.skipped), *_burn(plan.zones)]


def run_lines(api: Api, plan: Plan, resp: Response) -> list[str]:
    """Строка «API: …» после запроса и предупреждения (журнал, «сгорит»); общий вывод CLI run и api_run."""
    z = api.zones(plan.provider.name) if plan.zones is not None else None
    u = resp.usage
    line = (f"API: {resp.model} · вход {u.input + u.cache_read + u.cache_write} (из кэша {u.cache_read}) · "
            f"выход {u.output} · {usd(resp.cost_usd)}" + (f" · {_cycle(z)}" if z is not None else ""))
    if plan.zone == "purchased" and z is not None and z.allow_usd is not None:
        line += f" · купленная зона {usd(z.purchased_spent)} из ${z.allow_usd:.2f}, до {ddmm(z.allow_until)}"
    return [line, *_skipped(plan.skipped), *_burn(z)]


# ---------- stats, credits, allow ----------

def _stats(config: Path, state: Path, now, args: list[str]) -> int:
    pos, o = _parse(args, {"--days"})
    if pos:
        raise _Usage(f"лишние аргументы: {' '.join(pos)}")
    days = _int(o["days"], "--days") if "days" in o else None
    print("\n".join(stats_lines(_api(config, state, now), days)))
    return 0


def stats_lines(api: Api, days: int | None = None) -> list[str]:
    """Траты за цикл (или за days дней) по доступным провайдерам: с секцией в реестре или с ключом (ТЗ WSH-4.4,
    п. 2.3б); общий вывод CLI stats и api_stats."""
    provs = api.available()
    led = api.read_ledger()
    t = api.now()
    daily = api.reg.api.daily_usd
    out = [f"Траты API хаба (оценка) {f'за {days} дн.' if days else 'за цикл'}; сегодня "
           f"{usd(ledger.spent_today(led, t))} " + (f"из ${daily:.2f} (daily_usd)" if daily is not None
                                                   else "(daily_usd не задан)")]
    if not provs:
        out.append("провайдеров нет: ни секций [api.providers.<имя>] в реестре, ни ключей "
                   "(wshub api key set <провайдер>)")
    for name, conf in provs.items():
        z = ledger.zones(led, name, conf, t.date())
        if not ledger.zones_set(led, name, conf):
            out.append(f"{name}: {NO_ZONES}")
        else:
            out.append(f"{name}: включённые {usd(z.included_spent)} из ${z.included_total:.2f}, "
                       f"остаток {usd(z.included_left)}, до {ddmm(z.until)}")
            out.append("  купленные: " + (f"{usd(z.purchased_spent)} из ${z.allow_usd:.2f} по разрешению до "
                                          f"{ddmm(z.allow_until)}" if z.allow_usd is not None
                                          else f"нет разрешения (wshub api allow {name} --usd … --until …)"))
        if days:
            since = t - timedelta(days=days)
            rows = [r for r in led.rows if datetime.fromisoformat(r["ts"]) >= since]
        else:
            rows = list(led.rows[z.cycle_from:])
        rows = [r for r in rows if r["kind"] == "run" and r["provider"] == name]
        if not rows:
            out.append("  запросов нет")
        for title, field, names in (("по зонам", "zone", ZONE_RU), ("по моделям", "model", None),
                                    ("по проектам", "project", None)):
            if not rows:
                break
            sums: dict[str, list] = defaultdict(lambda: [0.0, 0])
            for r in rows:
                s = sums[str(r.get(field))]
                s[0] += r["cost_usd"]
                s[1] += 1
            parts = [f"{(names or {}).get(k, k)} {usd(v[0])} ({v[1]})" for k, v in sorted(sums.items())]
            out.append(f"  {title}: " + " · ".join(parts))
        out += ["  " + b for b in _burn(z)]
    out += _skipped(led.skipped)
    return out


def _credits(config: Path, state: Path, now, args: list[str]) -> int:
    pos, o = _parse(args, {"--left", "--until"})
    if len(pos) != 1 or "left" not in o or "until" not in o:
        raise _Usage("credits <провайдер> --left USD --until ДД.ММ[.ГГГГ]")
    name = pos[0]
    api = _api(config, state, now)
    api.provider(name)
    left = _money(o["left"], "--left", zero=True)
    t = now()
    until = parse_until(o["until"], t.date())
    ledger.append(api.ledger_path, {"ts": ledger.stamp(t), "kind": "credits", "provider": name,
                                    "left_usd": left, "until": until.isoformat()})
    print(f"{name}: остаток включённых кредитов ${left:.2f} до {until:%d.%m.%Y} — с этой минуты новый цикл")
    return 0


def _allow(config: Path, state: Path, now, args: list[str]) -> int:
    pos, o = _parse(args, {"--usd", "--until"}, ("--revoke",))
    revoke = o.get("revoke", False)
    if len(pos) != 1 or (revoke and ("usd" in o or "until" in o)) or (not revoke and ("usd" not in o
                                                                                       or "until" not in o)):
        raise _Usage("allow <провайдер> --usd USD --until ДД.ММ[.ГГГГ] | allow <провайдер> --revoke")
    name = pos[0]
    api = _api(config, state, now)
    api.provider(name)
    t = now()
    if revoke:
        ledger.append(api.ledger_path, {"ts": ledger.stamp(t), "kind": "revoke", "provider": name})
        print(f"{name}: разрешение на купленные кредиты снято")
        return 0
    amount = _money(o["usd"], "--usd")
    until = parse_until(o["until"], t.date())
    ledger.append(api.ledger_path, {"ts": ledger.stamp(t), "kind": "allow", "provider": name, "usd": amount,
                                    "until": until.isoformat()})
    print(f"{name}: разрешено потратить купленных кредитов до ${amount:.2f} по {until:%d.%m.%Y} включительно")
    return 0

"""wshub api …: ключи, запрос к модели, траты, остаток и разрешения оператора (ТЗ WSH-4.2, п. 2.5)."""
from __future__ import annotations

import getpass
import json
import math
import os
import re
import secrets
import sys
import warnings
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

from ..core import Hub, Session, Skipped, WsError, _is_file
from ..policy import Policy, glob_match
from ..registry import Registry, RegistryError, RegistryFile
from . import batches, keys, ledger
from .core import Api, ApiError, Plan, batch_counts, check_batch_size, ddmm, now_local, usd
from .providers import PROVIDERS
from .types import CACHE, Request, Response

OUT_MAX = 8 * 1024  # длиннее — ответ в файл, на экран путь
ZONE_RU = {"included": "включённая", "purchased": "купленная"}
USAGE = """\
wshub api                         — MCP-сервер wshub-api (stdio); его запускает Claude Desktop
wshub api key set <провайдер>     — задать ключ API (скрытый ввод), файл с правами 0600
wshub api key check <провайдер>   — есть ли ключ и верны ли права (значение не выводится)
wshub api run --provider P --project X (--tier T | --model M) [--system FILE] --input FILE [--schema FILE]
              [--max-tokens N] [--effort E] [--choice operator] [--estimate] [--out FILE]
wshub api batch submit --provider P --project X (--tier T | --model M) [--system FILE]
              (--inputs FILE… | --inputs-glob 'МАСКА') [--schema FILE] [--max-tokens N] [--effort E]
              [--cache none|short|long] [--choice operator] [--out-dir DIR]   — пакет: −50 %, ответ до 24 ч
wshub api batch status [ID]       — состояние пакета; без ID — список пакетов
wshub api batch results ID        — ответы законченного пакета: файлы, сводка, траты
wshub api batch cancel ID         — отменить пакет
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
    handlers = {"key": _key, "run": _run, "batch": _batch, "stats": _stats, "credits": _credits, "allow": _allow}
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
    api_estimate сервера wshub-api. Вход — весь, с записью в кэш (её часть — в скобках; без cache её нет)."""
    e = plan.estimate
    cached = f" (запись в кэш {e.cache_write})" if e.cache_write else ""
    return [f"оценка: {plan.request.model} · вход ≈{e.input + e.cache_write + e.cache_read}{cached}"
            f"{' (грубо)' if plan.rough else ''} · выход до {e.output} · {usd(plan.estimate_usd)}",
            f"зона: {ZONE_RU[plan.zone]}" if plan.zone else f"запрос был бы отклонён: {plan.refusal}",
            _cycle(plan.zones) if plan.zones is not None else NO_ZONES,
            *_skipped(plan.skipped), *_burn(plan.zones), *([plan.unread] if plan.unread else []), *plan.warnings]


def run_lines(api: Api, plan: Plan, resp: Response) -> list[str]:
    """Строка «API: …» после запроса и предупреждения (журнал, «сгорит», [api.warn]); общий вывод CLI run и
    api_run."""
    try:  # ответ уже оплачен: журнал перестал читаться — строка без цикла, предупреждения всё равно выводятся
        z = api.zones(plan.provider.name) if plan.zones is not None else None
    except ApiError:
        z = None
    u = resp.usage
    line = (f"API: {resp.model} · вход {u.input + u.cache_read + u.cache_write} (из кэша {u.cache_read}) · "
            f"выход {u.output} · {usd(resp.cost_usd)}" + (f" · {_cycle(z)}" if z is not None else ""))
    if plan.zone == "purchased" and z is not None and z.allow_usd is not None:
        line += f" · купленная зона {usd(z.purchased_spent)} из ${z.allow_usd:.2f}, до {ddmm(z.allow_until)}"
    warn = api.warnings([plan.provider.name], plan.estimate_usd)
    if plan.unread and plan.unread not in warn:  # журнал не читался на оценке — запись после ответа могла его починить
        warn.insert(0, plan.unread)
    return [line, *_skipped(plan.skipped), *_burn(z), *resp.warnings, *warn]


# ---------- batch (ТЗ WSH-4.5) ----------

BATCH_VALUED = {"--provider", "--project", "--tier", "--model", "--system", "--inputs-glob", "--schema",
                "--max-tokens", "--effort", "--cache", "--out-dir", "--choice"}
BATCH_OPS = ("submit", "status", "results", "cancel")


def _split_inputs(args: list[str]) -> tuple[list[str], list[str] | None]:
    """--inputs берёт значения до следующего параметра «--…»."""
    rest, inputs, i = [], None, 0
    while i < len(args):
        if args[i] == "--inputs":
            if inputs is not None:
                raise _Usage("--inputs — один раз, файлы через пробел")
            inputs, i = [], i + 1
            while i < len(args) and not args[i].startswith("--"):
                inputs.append(args[i])
                i += 1
            if not inputs:
                raise _Usage("после --inputs нужны файлы")
        else:
            rest.append(args[i])
            i += 1
    return rest, inputs


def _batch(config: Path, state: Path, now, args: list[str]) -> int:
    if not args or args[0] not in BATCH_OPS:
        raise _Usage("batch submit … | batch status [ID] | batch results ID | batch cancel ID")
    op, args = args[0], args[1:]
    if op != "submit":
        pos, _o = _parse(args, set())
        if len(pos) > 1 or (op != "status" and len(pos) != 1):
            raise _Usage(f"batch {op} {'[ID]' if op == 'status' else 'ID'}")
        hub = Hub(config, state)
        api = _api(config, state, now, hub.registry.get())
        if op == "status":
            print("\n".join(batch_status_lines(api, pos[0] if pos else None)))
        elif op == "cancel":
            print("\n".join(batch_cancel_lines(api, api.batch_cancel(pos[0]))))
        else:
            rec, items = api.batch_results(pos[0])
            print("\n".join(batch_result_lines(api, rec, items) + write_answers(hub, rec, items, state)))
        return 0
    rest, inputs = _split_inputs(args)
    pos, o = _parse(rest, BATCH_VALUED)
    if pos:
        raise _Usage(f"лишние аргументы: {' '.join(pos)}")
    for k in ("provider", "project"):
        if k not in o:
            raise _Usage(f"нужен --{k}")
    if (inputs is None) == ("inputs-glob" not in o):
        raise _Usage("вход пакета — --inputs ФАЙЛ… или --inputs-glob 'МАСКА', что-то одно")
    if "choice" in o and o["choice"] != "operator":
        raise _Usage("--choice: только operator")
    if o.get("cache", "none") not in CACHE:
        raise _Usage(f"--cache: одно из {', '.join(CACHE)}")
    max_tokens = _int(o.get("max-tokens", "1024"), "--max-tokens")
    hub = Hub(config, state)
    reg = hub.registry.get()
    api = _api(config, state, now, reg)
    api.gate(o["project"])  # выключенный API и закрытый проект — отказ до чтения файлов
    api.batch_provider(o["provider"])
    s = _session(hub, reg, o["project"])
    system = _read(hub, s, o["system"]) if "system" in o else ""
    schema = None
    if "schema" in o:
        schema = json.loads(_read(hub, s, o["schema"]))
        if not isinstance(schema, dict):
            raise ApiError("--schema: ожидается JSON-объект схемы")
    plans, ids, meta = batch_prepare(api, hub, reg, o["project"], o["provider"], inputs, o.get("inputs-glob", ""),
                                     None, system, schema, o.get("tier"), o.get("model"), o.get("effort"),
                                     max_tokens, o.get("cache", "none"), o.get("choice"), o.get("out-dir", ""))
    print("\n".join(batch_submit_lines(api, api.submit(plans, ids, meta))))
    return 0


def _glob_files(hub: Hub, s: Session, pattern: str) -> list[str]:
    """Файлы проекта по маске (как find): без .git, deny и симлинков-каталогов; не больше BATCH_MAX."""
    hits = []
    for base, _dirs, files in hub._walk(s, s.root, Skipped(s)):
        for f in files:
            rel = hub._rel(s, base / f)
            # симлинк и не обычный файл (FIFO, сокет) пропускаются, а не валят весь пакет (ревью, п. 12)
            if glob_match(pattern, rel) and not s.policy.denied(rel) and _is_file(base / f) \
                    and not (base / f).is_symlink():
                hits.append(rel)
                check_batch_size(len(hits))
    if not hits:
        raise ApiError(f"по маске {pattern} в проекте нет файлов")
    return sorted(hits)


def _out_dir_check(hub: Hub, reg: Registry, project: str, out_dir: str, names: list[str]) -> None:
    """out_dir пакета — проверки out_file сервера для каждого файла ответа до отправки (решение Р3)."""
    from ..api_server import _out_target
    for n in names:
        try:
            target, _rel = _out_target(hub, reg, project, f"{out_dir.rstrip('/')}/{n}")
        except WsError as e:
            raise WsError("out_dir: " + str(e).removeprefix("out_file: ")) from None
        if os.path.lexists(target.parent) and not target.parent.is_dir():  # ревью, п. 9
            raise WsError(f"out_dir: {out_dir} — не каталог")


def batch_prepare(api: Api, hub: Hub, reg: Registry, project: str, provider: str, files: list[str] | None,
                  glob: str, texts: list[str] | None, system: str, schema: dict | None, tier: str | None,
                  model: str | None, effort: str | None, max_tokens: int, cache: str, choice: str | None,
                  out_dir: str, check=None) -> tuple[list[Plan], list[str], dict]:
    """Общий путь CLI batch submit и api_batch_submit: входы (файлы по политике проекта, маска или тексты), имена
    custom_id и файлов ответа (Р4, Р5), out_dir — до чтения входов, проверки ядра для каждого запроса; check — по
    первому плану (reason сервера)."""
    api.batch_provider(provider)
    if sum(bool(x) for x in (files, glob, texts)) != 1:
        raise ApiError("вход пакета — ровно одно из: список файлов, маска файлов или (из чата) список текстов")
    s = _session(hub, reg, project)
    if texts:
        check_batch_size(len(texts))
        names, briefs, paths = [f"r{i}" for i in range(1, len(texts) + 1)], [t[:batches.BRIEF] for t in texts], []
    else:
        if files:
            check_batch_size(len(files))
        rels = [hub._resolve(s, p)[1] for p in files] if files else _glob_files(hub, s, glob)
        names, briefs, paths = [Path(r).stem for r in rels], rels, rels
    ids, fnames = batches.custom_ids(names), batches.file_names(names)
    if out_dir:
        ext = ".json" if schema is not None else ".md"
        _out_dir_check(hub, reg, project, out_dir, [f + ext for f in fnames])
    bodies = texts or [_read(hub, s, p) for p in paths]
    reqs = [Request(system, ({"role": "user", "text": t},), project, tier=tier or None, model=model or None,
                    schema=schema, effort=effort or None, max_tokens=max_tokens, cache=cache) for t in bodies]
    plans = api.batch_plan(reqs, provider, choice or None, check)
    inputs = {cid: {"input": b, "file": f} for cid, b, f in zip(ids, briefs, fnames, strict=True)}
    return plans, ids, {"project": project, "out_dir": out_dir or None, "inputs": inputs}


def _batch_warn(api: Api, rec: dict, est: float | None = None) -> list[str]:
    """Предупреждения пакета: журнал (резерв или строки results не записаны) и [api.warn] (ТЗ WSH-4.8)."""
    own = list(rec.get("warnings", ()))
    warn = api.warnings([rec.get("provider")], est, "пакета")
    return own + [w for w in warn if not any(x.startswith(w) for x in own)]  # «журнал не читается» — один раз


def batch_submit_lines(api: Api, rec: dict) -> list[str]:
    where = (f"в {rec['out_dir']} проекта {rec['project']}" if rec.get("out_dir")
             else f"в ~/.local/state/wshub/api/out/{rec['id']}/")
    return [f"пакет {rec['id']} отправлен: запросов {rec['requests']} · {rec['model']} · оценка "
            f"{usd(rec['estimate_usd'])} со скидкой 50 %" + (" (вход грубо)" if rec.get("rough") else ""),
            f"ответы — {where}; ожидание до 24 ч",
            f"состояние — batch status {rec['id']}; результаты — batch results {rec['id']}",
            *_batch_warn(api, rec, rec["estimate_usd"])]


def batch_cancel_lines(api: Api, rec: dict) -> list[str]:
    return [f"отмена запрошена: {batch_line(rec)}", *_batch_warn(api, rec)]


def batch_line(rec: dict) -> str:
    line = (f"{rec['id']} · {rec.get('status')} ({batch_counts(rec)}) · {rec.get('provider')}/{rec.get('model')} · "
            f"проект {rec.get('project')} · запросов {rec.get('requests')} · создан {str(rec.get('created'))[:16]}")
    if rec.get("status") != "ended" and rec.get("expires_at"):
        line += f" · истекает {str(rec['expires_at'])[:16]}"
    return line + (f" · не обновлён: {rec['error']}" if rec.get("error") else "")


def batch_status_lines(api: Api, batch_id: str | None) -> list[str]:
    """Один пакет (состояние от провайдера) или список учёта; общий вывод CLI batch status и api_batch_status."""
    if batch_id:
        rec = api.batch_status(batch_id)
        return [batch_line(rec), *_batch_warn(api, rec)]
    recs, bad = api.batch_list()
    out = [batch_line(r) for r in recs] or ["пакетов нет"]
    names = sorted({r["provider"] for r in recs if isinstance(r.get("provider"), str)})
    return out + ([f"учёт пакетов: нечитаемых файлов {bad}"] if bad else []) + api.warnings(names)


def batch_result_lines(api: Api, rec: dict, items: list[dict]) -> list[str]:
    """Сводка results: сколько успешно, с ошибкой, цена со скидкой, строки журнала; ошибки — по custom_id."""
    ok = sum(1 for i in items if i["ok"])
    new = sum(1 for i in items if i["logged"])
    failed = sum(1 for i in items if i.get("ledger_failed"))
    old = sum(1 for i in items if not i.get("foreign")) - new - failed
    out = [f"пакет {rec['id']}: успешно {ok}, с ошибкой {len(items) - ok} · "
           f"{usd(sum(i['cost_usd'] for i in items))} со скидкой 50 %",
           f"журнал трат: записано строк {new}" + (f", уже были {old} — не дублируются" if old else "")
           + (f", не записано {failed}" if failed else "")]
    for i in items:
        if not i["ok"]:
            src = (rec["inputs"].get(i["custom_id"]) or {}).get("input", "")
            out.append(f"  ✗ {i['custom_id']}" + (f" ({src})" if src else "") + f": {i['error']}")
    return out + _batch_warn(api, rec)


def _same(p: Path, data: bytes) -> bool:
    try:
        return p.is_file() and not p.is_symlink() and p.stat().st_size == len(data) and p.read_bytes() == data
    except OSError:
        return False


def _save_new(path: Path, data: bytes) -> str:
    """Ответ — в каталог состояния без перезаписи: тот же ответ уже лежит — «записан ранее», другой — рядом под новым
    именем."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if _same(path, data):
            return f"записан ранее: {path}"
        p = path if not os.path.lexists(path) else path.with_name(f"{path.stem}.{secrets.token_hex(3)}{path.suffix}")
        with open(p, "xb") as fh:
            fh.write(data)
    except OSError as e:
        return f"не сохранён — ошибка записи в {path.parent}: {e.strerror or type(e).__name__}"
    return f"сохранён: {p}"


def write_answers(hub: Hub, rec: dict, items: list[dict], state: Path, check=None) -> list[str]:
    """Успешные ответы — по файлу на custom_id (п. 2.2.3): в out_dir проекта (проверка out_file заново по свежему
    реестру, без перезаписи) или в ~/.local/state/wshub/api/out/<id>/. Тот же ответ уже записан — «записан ранее»
    (решение Р6); файл другой или запись в проект не прошла — ответ в каталог состояния. check — проверка перед
    записью в проект (блокировка из панели — у сервера)."""
    from ..api_server import _out_target, _write_new
    ext = ".json" if rec.get("schema") else ".md"
    fallback = Path(state) / "api" / "out" / rec["id"]
    oks = [i for i in items if i["ok"]]
    if not oks:
        return []
    out = [f"ответы: {rec['out_dir']} в проекте {rec['project']}" if rec.get("out_dir") else f"ответы: {fallback}"]
    for it in oks:
        cid = it["custom_id"]
        name = ((rec["inputs"].get(cid) or {}).get("file") or cid) + ext
        data = answer_body(it["response"]).encode("utf-8")
        if not rec.get("out_dir"):
            out.append(f"  {cid}: {_save_new(fallback / name, data)}")
            continue
        rel = f"{rec['out_dir'].rstrip('/')}/{name}"
        try:
            if check is not None:
                check()
            target, rel = _out_target(hub, hub.registry.get(), rec["project"], rel, exist_ok=True)
            if _same(target, data):
                out.append(f"  {cid} → {rel} (записан ранее)")
                continue
            _write_new(target, data)
            out.append(f"  {cid} → {rel}")
            continue
        except (WsError, RegistryError, OSError) as e:
            why = ("файл уже есть — перезапись запрещена" if isinstance(e, FileExistsError)
                   else str(e).removeprefix("out_file: "))
        out.append(f"  {cid} → {rel} не записан: {why}; ответ {_save_new(fallback / name, data)}")
    return out


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
    return out + api.warn_lines(led, provs, None)


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

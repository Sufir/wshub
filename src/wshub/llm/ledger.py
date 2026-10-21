"""Журнал трат API (ТЗ WSH-4.2, п. 2.3): ~/.local/state/wshub/api/ledger.jsonl. Строка на запрос (kind = run) и на
команды оператора credits / allow / revoke. Суммы — оценка хаба по таблице цен провайдера, а не списание у провайдера.

Нечитаемая строка (оборванная запись) пропускается и считается; отказ — только если файл не читается целиком или
при ненулевом размере не разбирается ни одна строка (решение архитектора, ТЗ WSH-4.2, п. 6.1)."""
from __future__ import annotations

import fcntl
import json
import os
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

ZONES = ("included", "purchased")
NO_SPEND = "; до тех пор API хаба не тратит"  # хвост отказа — только когда журнал нужен проверкам (ТЗ WSH-4.8, 5.1.3)
BURN_DAYS = 7  # до credits_until меньше стольких дней …
BURN_USD = 10.0  # … а включённый остаток больше — «сгорит»


class LedgerError(Exception):
    pass


def ledger_path(state_dir: Path) -> Path:
    return Path(state_dir) / "api" / "ledger.jsonl"


def _num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and v >= 0


def _valid(r) -> bool:
    if not isinstance(r, dict) or not isinstance(r.get("provider"), str) or not isinstance(r.get("ts"), str):
        return False
    if datetime.fromisoformat(r["ts"]).tzinfo is None:  # ValueError — строка тоже пропускается
        return False
    kind = r.get("kind")
    if kind == "run":
        return _num(r.get("cost_usd")) and r.get("zone") in ZONES
    if kind in ("credits", "allow"):
        date.fromisoformat(r.get("until") if isinstance(r.get("until"), str) else "")
        return _num(r.get("left_usd" if kind == "credits" else "usd"))
    return kind == "revoke"


@dataclass(frozen=True)
class Ledger:
    rows: tuple[dict, ...]  # в порядке записи
    skipped: int  # нечитаемых строк пропущено


def read(path: Path) -> Ledger:
    try:
        data = Path(path).read_bytes()
    except FileNotFoundError:
        return Ledger((), 0)
    except OSError as e:
        raise LedgerError(f"журнал трат {path} не читается: {e.strerror or e}") from None
    rows, skipped = [], 0
    for line in data.splitlines():
        if not line.strip():
            continue
        try:
            r = json.loads(line)
            ok = _valid(r)
        except (ValueError, TypeError):
            ok = False
        if ok:
            rows.append(r)
        else:
            skipped += 1
    if data.strip() and not rows:
        raise LedgerError(f"журнал трат {path}: не разбирается ни одна строка — исправь или перенеси файл{NO_SPEND}")
    # звено задачи Code пишет резерв на старте и факт по итогу (ТЗ WSH-4.6, п. 2.5.4): из строк одного job
    # действует последняя — прежние в зоны, потолок дня и stats не входят
    last = {r["job"]: i for i, r in enumerate(rows) if isinstance(r.get("job"), str)}
    rows = [r for i, r in enumerate(rows) if not isinstance(r.get("job"), str) or last[r["job"]] == i]
    # резерв пакета на submit (ТЗ WSH-4.8, п. 2.1.4) действует, пока в журнале нет строк факта этого пакета (results)
    facts = {r["batch"] for r in rows if isinstance(r.get("batch"), str) and r.get("reserve") is not True}
    rows = [r for r in rows if not (r.get("reserve") is True and isinstance(r.get("batch"), str)
                                    and r["batch"] in facts)]
    return Ledger(tuple(rows), skipped)


def append(path: Path, rec: dict) -> None:
    """Одна строка одним write в режиме O_APPEND под flock; файл создаётся с правами 0600. Если прошлая запись
    оборвалась без перевода строки, сначала дописывается «\\n» — иначе новая строка склеилась бы с обрывком
    и пропала вместе с ним (ревью WSH-4.2, находка 1)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    line = (json.dumps(rec, ensure_ascii=False) + "\n").encode("utf-8")
    fd = os.open(path, os.O_RDWR | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        size = os.fstat(fd).st_size
        if size and os.pread(fd, 1, size - 1) != b"\n":
            line = b"\n" + line
        if os.write(fd, line) != len(line):
            raise LedgerError(f"журнал трат {path}: строка записана не целиком")
    finally:
        os.close(fd)


@contextmanager
def locked(path: Path):
    """Исключительная блокировка журнала на «проверка зоны → запрос → запись» (рядом лежит <журнал>.lock):
    параллельные запросы не проходят проверку на одном и том же остатке."""
    lock = Path(path).with_suffix(".lock")
    lock.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def stamp(now: datetime) -> str:
    return now.isoformat(timespec="seconds")


@dataclass(frozen=True)
class Zones:
    provider: str
    included_total: float  # из реестра или из последней команды credits
    included_spent: float  # потрачено в текущем цикле
    included_left: float  # после until — 0
    until: date | None
    cycle_from: int  # индекс первой строки цикла в журнале
    allow_usd: float | None  # действующее разрешение на купленную зону; None — нет
    allow_until: date | None
    purchased_spent: float
    purchased_left: float
    days_left: int | None

    @property
    def burn(self) -> bool:
        return self.days_left is not None and 0 <= self.days_left < BURN_DAYS and self.included_left > BURN_USD


def zones_set(led: Ledger, provider: str, conf) -> bool:
    """Проверять ли зоны провайдера: только если их задал оператор — included_usd в реестре или команда credits
    в журнале (ТЗ WSH-4.3, п. 2.2.2 и решение 5.1). Иначе зон нет, строки идут во включённую."""
    return conf.included_set or any(r["kind"] == "credits" and r["provider"] == provider for r in led.rows)


def zones(led: Ledger, provider: str, conf, today: date) -> Zones:
    """Зоны провайдера. Цикл — от последней команды credits (или от начала журнала) до until. Команда credits
    сильнее реестра. Купленная зона — только при действующем allow; её траты считаются от команды allow."""
    total, until, start = float(conf.included_usd), conf.credits_until, 0
    allow = None  # (usd, until, индекс)
    for i, r in enumerate(led.rows):
        if r["provider"] != provider:
            continue
        if r["kind"] == "credits":
            total, until, start = float(r["left_usd"]), date.fromisoformat(r["until"]), i + 1
        elif r["kind"] == "allow":
            allow = (float(r["usd"]), date.fromisoformat(r["until"]), i + 1)
        elif r["kind"] == "revoke":
            allow = None

    def spent(zone: str, since: int) -> float:
        return sum(r["cost_usd"] for r in led.rows[since:]
                   if r["kind"] == "run" and r["provider"] == provider and r["zone"] == zone)

    inc_spent = spent("included", start)
    active = until is not None and today <= until
    inc_left = max(0.0, total - inc_spent) if active else 0.0
    if allow is not None and today > allow[1]:
        allow = None
    pur_spent = spent("purchased", allow[2]) if allow else 0.0
    pur_left = max(0.0, allow[0] - pur_spent) if allow else 0.0
    return Zones(provider, total, inc_spent, inc_left, until, start, allow[0] if allow else None,
                 allow[1] if allow else None, pur_spent, pur_left, (until - today).days if until else None)


def spent_today(led: Ledger, now: datetime) -> float:
    """Траты всех провайдеров за текущие местные сутки — для потолка daily_usd."""
    today = now.date()
    return sum(r["cost_usd"] for r in led.rows
               if r["kind"] == "run" and _local_date(r["ts"], now) == today)


def reserved_today(led: Ledger, now: datetime) -> float:
    """Из трат текущих суток — действующие резервы (задачи Code и пакеты до итога, ТЗ WSH-4.8): для текста ⚠."""
    today = now.date()
    return sum(r["cost_usd"] for r in led.rows
               if r["kind"] == "run" and r.get("reserve") is True and _local_date(r["ts"], now) == today)


def _local_date(ts: str, now: datetime) -> date:
    t = datetime.fromisoformat(ts)
    return (t.astimezone(now.tzinfo) if t.tzinfo and now.tzinfo else t).date()

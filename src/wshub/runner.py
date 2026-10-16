"""Надзиратель задачи Claude Code: `wshub code-run <id>` (ТЗ WSH-2, п. 2.5.3).

Запускается в своём сеансе (start_new_session). prefetch силами хаба, запуск Code в собственной группе
процессов, слежение за отменой и пределом времени, разбор итога, снимок git, запись result.json и job.json.
Фаза плана (ТЗ WSH-3, 2.3): копия плана в plan.md, состояние plan_ready, при plan = auto — запуск исполнения.
Блок «Вне кода» (ТЗ WSH-3.3): коммиты звена, изменения .git/hooks и .git/config, вызовы docker.
Страж подписки (ТЗ WSH-4.1): сверка init и rate_limit_event в потоке, поле billing, остановка не по подписке и на
overage, причина subscription_limit, строка оплаты для ответов.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import signal
import subprocess
import time
from datetime import datetime
from pathlib import Path

from . import code_events, gitsafe
from .jobs import (
    COMMITS_MAX,
    DOCKER_MAX,
    GIT_START,
    JobError,
    Jobs,
    cancel_timers,
    clip_list,
    config_keys,
    git_meta,
    head_state,
    meta_changes,
)
from .runtime import atomic_write_text, proc_start, read_json

PREFETCH_TIMEOUT = 120
POLL = 1.0
RESULT_MAX = 8 * 1024
STATUS_LINES = 200
PLAN_MAX = 200 * 1024  # байт: копия плана в каталоге задачи
DOCKER_CMD_MAX = 200  # символов команды в блоке «Вне кода»
# docker как команда: в начале (строки) или после ;, &&, ||, | (ТЗ WSH-3.3, п. 1.3)
DOCKER_RE = re.compile(r"(?:^|;|&&|\|\|?)\s*(?:docker|docker-compose)(?=\s|$)", re.MULTILINE)
# окна лимита подписки в строке оплаты (ТЗ WSH-4.1, п. 2.6); прочие окна — под своим именем, после этих
WINDOWS = {"five_hour": "5 ч", "seven_day": "неделя"}
BILLING_HINT = {
    "billing_mismatch": "claude сообщил источник оплаты не «none» (не подписка) — задача остановлена; проверь "
                        "apiKeyHelper и env в .claude/settings*.json рабочего каталога",
    "overage": "задача начала тратить extra usage сверх подписки (isUsingOverage) и остановлена; разрешить — "
               "[code] allow_overage = true в реестре",
}


def _iso(ts) -> str | None:
    """Время по часам хоста в формате поля created в job.json."""
    if not isinstance(ts, (int, float)) or isinstance(ts, bool):
        return None
    try:
        return datetime.fromtimestamp(ts).astimezone().isoformat(timespec="seconds")
    except (OverflowError, OSError, ValueError):
        return None


def _short(ts) -> str | None:
    """Время сброса в строке оплаты: «15.10 10:20» по часам хоста (resetsAt — unix-секунды, факт 3)."""
    if not isinstance(ts, (int, float)) or isinstance(ts, bool):
        return None
    try:
        return datetime.fromtimestamp(ts).strftime("%d.%m %H:%M")
    except (OverflowError, OSError, ValueError):
        return None


def billing_line(billing: dict | None, reason: str | None = None) -> str:
    """Строка оплаты (ТЗ WSH-4.1, п. 2.6): окна из unifiedWindows (utilization × 100), сброс, overage; «близко к
    лимиту» при allowed_warning; три причины остановки — хвостом строки."""
    b = billing or {}
    if reason == "billing_mismatch" or ("source" in b and b["source"] != "none"):
        src = b.get("source")
        return (f"оплата: НЕ подписка (apiKeySource: {src if src is not None else 'поля нет'})"
                + (" · задача остановлена" if reason == "billing_mismatch" else ""))
    rate = b.get("rate") if isinstance(b.get("rate"), dict) else None
    if not rate:
        line = "оплата: подписка (данных о лимитах ещё нет)"
    else:
        wins = rate.get("unifiedWindows") if isinstance(rate.get("unifiedWindows"), dict) else {}
        parts = ["оплата: подписка"]
        for k in [*(k for k in WINDOWS if k in wins), *sorted(k for k in wins if k not in WINDOWS)]:
            w = wins[k]
            u = w.get("utilization") if isinstance(w, dict) else None
            # json.loads пропускает Infinity и NaN — на них round() падает
            if not isinstance(u, (int, float)) or isinstance(u, bool) or not math.isfinite(u):
                continue
            t = _short(w.get("resetsAt"))
            parts.append(f"{WINDOWS.get(k, k)} — {round(u * 100)} %" + (f", сброс {t}" if t else ""))
        ov = _overage(rate)
        if ov:
            parts.append(ov)
        line = " · ".join(parts)
        if rate.get("status") == "allowed_warning":
            line = "близко к лимиту · " + line
    if reason == "overage":
        line += " · задача остановлена: overage"
    elif reason == "subscription_limit":
        t = _short((rate or {}).get("resetsAt"))
        line += " · лимит подписки исчерпан" + (f", сброс {t}" if t else "")
    return line


def _overage(rate: dict) -> str | None:
    if rate.get("isUsingOverage"):
        return "overage: используется"
    st = rate.get("overageStatus")
    if st == "rejected":
        why = rate.get("overageDisabledReason")
        return "overage: выкл" + (f" ({why})" if why else "")
    return "overage: вкл" if isinstance(st, str) else None


def billing_hint(reason: str | None, billing: dict | None) -> str | None:
    """reason_hint трёх причин стража подписки; у subscription_limit — когда продолжать."""
    if reason == "subscription_limit":
        rate = (billing or {}).get("rate")
        t = _short(rate.get("resetsAt")) if isinstance(rate, dict) else None
        return f"лимит подписки исчерпан; после {t or 'сброса лимита'} продолжи: code_resume"
    return BILLING_HINT.get(reason)


def apply_billing(jobs: Jobs, view: dict, model: bool = False) -> dict:
    """Строка оплаты, подсказка трёх причин и (model) фактическая модель в снимке или итоге — по активному звену
    (code_status, code_wait, code_result, wshub code status/result)."""
    try:
        d = jobs.read(view.get("active") or view.get("id") or view.get("job"))
    except JobError:
        return view
    b = d.get("billing") or {}
    view["billing"] = billing_line(b, d.get("reason"))
    hint = billing_hint(d.get("reason"), b)
    if hint:
        view["reason_hint"] = hint
    if model:
        view["model_line"] = f"модель: {d.get('model')} → {b.get('model') or 'неизвестна (init не было)'}"
    return view


def _rate_brief(rate) -> dict | None:
    """Сжатый rate_limit_info для журнала: окно, его utilization, status, isUsingOverage."""
    if not isinstance(rate, dict):
        return None
    wins = rate.get("unifiedWindows") if isinstance(rate.get("unifiedWindows"), dict) else {}
    w = wins.get(rate.get("rateLimitType")) if isinstance(rate.get("rateLimitType"), str) else None
    return {"window": rate.get("rateLimitType"), "utilization": w.get("utilization") if isinstance(w, dict) else None,
            "status": rate.get("status"), "isUsingOverage": rate.get("isUsingOverage")}


class _Stream:
    """events.jsonl по мере записи (ТЗ WSH-4.1, п. 2.4): новые целые строки с последнего прочитанного байта."""

    def __init__(self, path: Path):
        self.path = path
        self.offset = 0

    def read(self) -> list[dict]:
        try:
            with self.path.open("rb") as fh:
                fh.seek(self.offset)
                data = fh.read()
        except OSError:
            return []
        end = data.rfind(b"\n") + 1
        self.offset += end
        out = []
        for raw in data[:end].splitlines():
            try:
                e = json.loads(raw)
            except (ValueError, RecursionError):  # глубокая вложенность не должна ронять надзирателя
                continue
            if isinstance(e, dict):
                out.append(e)
        return out


def _clip_bytes(text: str, limit: int) -> str:
    """Обрезка по БАЙТАМ UTF-8 (замечание 17): кириллица — 2 байта, символьный предел ломал бы лимит."""
    b = (text or "").encode("utf-8")
    return text if len(b) <= limit else b[:limit].decode("utf-8", "ignore")


def _map_state(subtype: str | None, is_error: bool) -> tuple[str, str | None]:
    if subtype == "success":
        return "completed", None
    if subtype and "max_turns" in subtype:
        return "max_turns", subtype
    if subtype and ("budget" in subtype or "cost" in subtype):
        return "max_budget", subtype
    return "failed", subtype or ("is_error" if is_error else "no_result")


def _tail_result(events_path: Path) -> dict | None:
    last = None
    try:
        for line in events_path.read_text(encoding="utf-8").splitlines():
            try:
                e = json.loads(line)
            except ValueError:
                continue
            if isinstance(e, dict) and e.get("type") == "result":
                last = e
    except OSError:
        return None
    return last


PLAN_TOOLS = ("Write", "Edit", "MultiEdit")


def _plan_source(events_path: Path, plans_dir: Path, since: float | None = None) -> Path | None:
    """Файл плана (решение 7): путь из последнего успешного вызова Write (или Edit того же файла — переделка
    плана в той же сессии) внутрь ~/.claude/plans/ в потоке событий. Путь пишет модель — принимаем только .md,
    который после resolve() лежит прямо в каталоге планов, вызов с парным tool_result без ошибки и файл,
    изменённый не раньше старта звена (иначе — чужой или прошлый план)."""
    plans = plans_dir.resolve()
    uses: list[tuple[str, Path]] = []
    ok: set[str] = set()
    try:
        lines = events_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    for line in lines:
        try:
            e = json.loads(line)
        except ValueError:
            continue
        msg = e.get("message") if isinstance(e, dict) else None
        content = msg.get("content") if isinstance(msg, dict) else None
        for c in content if isinstance(content, list) else []:
            if not isinstance(c, dict):
                continue
            if c.get("type") == "tool_result" and not c.get("is_error") and isinstance(c.get("tool_use_id"), str):
                ok.add(c["tool_use_id"])
            if not (e.get("type") == "assistant" and c.get("type") == "tool_use" and c.get("name") in PLAN_TOOLS):
                continue
            fp = (c.get("input") or {}).get("file_path") if isinstance(c.get("input"), dict) else None
            if not isinstance(fp, str) or not os.path.isabs(fp) or not isinstance(c.get("id"), str):
                continue
            p = Path(fp).resolve()
            if p.suffix == ".md" and p.parent == plans:
                uses.append((c["id"], p))
    good = [p for uid, p in uses if uid in ok]
    if not good or not good[-1].is_file():
        return None
    if since is not None and good[-1].stat().st_mtime < since - 1:
        return None
    return good[-1]


def _git_snapshot(git: str, cwd: Path, environ: dict) -> dict:
    out: dict = {}
    try:
        r = gitsafe.run(git, cwd, ["status", "--short", "--ignore-submodules=all"], environ, 30)
        out["status"] = "\n".join(r.stdout.splitlines()[:STATUS_LINES])
        r = gitsafe.run(git, cwd, ["log", "-1", "--oneline"], environ, 30)
        out["head"] = r.stdout.strip()
    except gitsafe.GitUnsafe as e:
        out["error"] = f"{e.reason}: {e}"
    except (OSError, subprocess.TimeoutExpired) as e:
        out["error"] = str(e)
    return out


def _docker_calls(events_path: Path) -> tuple[list[str], int]:
    """Bash-вызовы docker из потока событий: первые DOCKER_MAX, команда ≤ DOCKER_CMD_MAX символов, «— отказ»
    (права или хук) или «— ошибка» (прочий is_error) по парному tool_result (ТЗ WSH-3.4, Б5); плюс общее число."""
    uses: list[tuple[str | None, str]] = []
    results: dict[str, dict] = {}
    denied: set[str] = set()
    try:
        lines = events_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return [], 0
    for line in lines:
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if not isinstance(e, dict):
            continue
        if e.get("type") == "system" and e.get("subtype") == "permission_denied":
            denied.add(e.get("tool_use_id"))
        if e.get("type") == "result":
            denied |= {d.get("tool_use_id") for d in e.get("permission_denials") or [] if isinstance(d, dict)}
        msg = e.get("message")
        content = msg.get("content") if isinstance(msg, dict) else None
        for c in content if isinstance(content, list) else []:
            if not isinstance(c, dict):
                continue
            if c.get("type") == "tool_result" and isinstance(c.get("tool_use_id"), str):
                results[c["tool_use_id"]] = c
            if e.get("type") == "assistant" and c.get("type") == "tool_use" and c.get("name") == "Bash":
                cmd = (c.get("input") or {}).get("command") if isinstance(c.get("input"), dict) else None
                if isinstance(cmd, str) and DOCKER_RE.search(cmd):
                    uses.append((c.get("id") if isinstance(c.get("id"), str) else None, cmd))
    out = []
    for uid, cmd in uses[:DOCKER_MAX]:
        m = code_events.mark(results[uid], denied) if uid in results else None
        out.append(cmd[:DOCKER_CMD_MAX] + (f" — {m}" if m else ""))
    return out, len(uses)


def _outside_code(git: str, cwd: Path, environ: dict, jdir: Path) -> dict:
    """Что звено сделало вне диффа: коммиты от HEAD на старте, .git/hooks и .git/config против снимка на
    старте (git_start.json), вызовы docker. Каждая часть — отдельно: сбой одной пишется в errors и не стирает
    остальные (прежде всего git_meta_changed — повод для предупреждения)."""
    out: dict = {"commits": [], "commits_count": 0, "git_meta_changed": [], "docker": [], "docker_count": 0}
    errors: list[str] = []
    start = read_json(jdir / GIT_START)
    if not start:
        errors.append("снимка git на старте звена нет")
    if start.get("error"):
        errors.append(f"снимок на старте: {start['error']}")
    if start:
        try:
            now = git_meta(cwd)
            if start.get("config_salt"):
                now["config_keys"] = config_keys(git, cwd, environ, start["config_salt"])
            out["git_meta_changed"] = clip_list(_drop_push_upstream(meta_changes(start, now), jdir))
            errors += [f"на старте: {m}" for m in start.get("meta_errors") or []]
            errors += [f"в конце: {m}" for m in now.get("meta_errors") or []]
        except Exception as e:  # noqa: BLE001
            out["git_meta_changed"] = [".git/hooks и .git/config — не проверены"]
            errors.append(f".git не проверен: {e}")
    if start and (start.get("head") or start.get("empty")):
        try:
            _commits(git, cwd, environ, start, out, errors)
        except gitsafe.GitUnsafe as e:
            errors.append(f"коммиты: {e.reason}: {e}")
        except Exception as e:  # noqa: BLE001 — OSError, TimeoutExpired, декодирование вывода
            errors.append(f"коммиты: {e}")
    elif start:
        errors.append("коммиты: HEAD на старте неизвестен")
    try:
        out["docker"], out["docker_count"] = _docker_calls(jdir / "events.jsonl")
    except Exception as e:  # noqa: BLE001
        errors.append(f"docker: {e}")
    if errors:
        out["errors"] = clip_list([x[:500] for x in errors])
    return out


def _drop_push_upstream(lines: list[str], jdir: Path) -> list[str]:
    """Ключи branch.<ветка>.remote и .merge, которые поставил сам wshub push -u этого звена (успешный пуш ветки в
    external.jsonl), — не правка задачи: без них предупреждение про .git было бы после каждого пуша (стенд WSH-3.4)."""
    pushed = set()
    try:
        for line in (jdir / "external.jsonl").read_text(encoding="utf-8").splitlines():
            r = json.loads(line)
            if isinstance(r, dict) and r.get("cmd") == "push" and r.get("exit") == 0 and len(r.get("args") or []) > 1:
                pushed.add(r["args"][1])
    except (OSError, ValueError):
        return lines
    own = {f".git/config — {what} ключ branch.{b}.{k}" for b in pushed for k in ("remote", "merge")
           for what in ("добавлен", "изменён")}
    return [x for x in lines if x not in own]


def _commits(git: str, cwd: Path, environ: dict, start: dict, out: dict, errors: list[str]) -> None:
    """Коммиты звена (ТЗ WSH-3.4, Б6): HEAD и ветка на старте и в конце. Старт — предок конца: коммиты
    старт..конец; пустой репозиторий на старте — все коммиты от корня. Старт не предок конца (reset назад,
    другая ветка) — строка «HEAD ушёл» без коммитов. Ветка сменилась, но старт — предок: коммиты старт..конец,
    сделанные после старта звена; более старые — чужие (ветка уже была), тогда тоже «HEAD ушёл»."""
    end = head_state(git, cwd, environ)
    out.update(head_start=start.get("head"), head_end=end.get("head"), branch_start=start.get("branch"),
               branch_end=end.get("branch"))
    if end.get("error"):
        errors.append(f"коммиты: {end['error']}")
        return
    head, last = start.get("head"), end.get("head")
    if not last:
        return  # в конце коммитов нет (пустой репозиторий так и остался пустым)
    moved = (f"HEAD ушёл: {start.get('branch') or '(отсоединён)'} {(head or '—')[:9]} → "
             f"{end.get('branch') or '(отсоединён)'} {last[:9]}")
    if head:
        r = gitsafe.run(git, cwd, ["merge-base", "--is-ancestor", head, last], environ, 30)
        if r.returncode != 0:
            if r.returncode != 1:
                errors.append(f"git merge-base вернул {r.returncode}: {r.stderr.strip()[:300]}")
            out["head_moved"] = moved
            return
    r = gitsafe.run(git, cwd, ["log", "--format=%ct %h %s", f"{head}..{last}" if head else last], environ, 30)
    if r.returncode != 0:
        errors.append(f"git log вернул {r.returncode}: {r.stderr.strip()[:300]}")
        return
    rows = [line.split(" ", 1) for line in r.stdout.splitlines() if " " in line]
    commits = [c for _t, c in rows]
    if start.get("branch") != end.get("branch") and isinstance(start.get("ts"), (int, float)):
        since = int(start["ts"]) - 1  # время коммита — в секундах
        own = [c for t, c in rows if t.isdigit() and int(t) >= since]
        if len(own) != len(commits):
            out["head_moved"] = moved
        commits = own
    out["commits"], out["commits_count"] = commits[:COMMITS_MAX], len(commits)


def _find_status(text: str, regex: str | None) -> str | None:
    if not regex:
        return None
    import re
    try:
        m = re.search(regex, text)
    except re.error:
        return None
    return m.group(1) if m and m.groups() else (m.group(0) if m else None)


class Supervisor:
    def __init__(self, hub, jid: str):
        self.hub = hub
        self.jobs = Jobs(hub)
        self.jid = jid
        self.jdir = self.jobs.job_dir(jid)
        self.d = self.jobs.read(jid)
        self.stop: str | None = None  # billing_mismatch | overage — остановка стражем подписки (ТЗ WSH-4.1)

    def _save(self, **kw):
        self.d.update(kw)
        self.jobs.write(self.jid, self.d)

    def _cancelled(self) -> bool:
        return (self.jdir / "cancel").exists()

    def run(self) -> None:
        # перечитываем job.json под тем же flock, что держит старт: так гарантированно видим sup_pid/sup_start,
        # записанные родителем после Popen (иначе наш _save затёр бы их → ложный orphaned/lost, замечание 20)
        with self.jobs._flock():
            self.d = self.jobs.read(self.jid)
        git = None
        environ = dict(os.environ)
        cwd = Path(self.d["dir"])
        if self.d.get("prefetch"):
            self._save(state="prefetch")
            git = self._git()
            if not self._prefetch(git, cwd, environ):
                return
        self._run_code(cwd, environ)
        self._finish(git or self._git(), cwd, environ)

    def _git(self) -> str:
        import shutil
        return shutil.which("git") or "git"

    def _prefetch(self, git: str, cwd: Path, environ: dict) -> bool:
        try:
            r = gitsafe.run(git, cwd, ["fetch", "--all", "--prune", "--no-recurse-submodules"], environ,
                            PREFETCH_TIMEOUT, ssh_agent=True)
            (self.jdir / "prefetch.log").write_text(r.stdout + r.stderr, encoding="utf-8")
            if r.returncode != 0:
                self._fail("prefetch", f"git fetch вернул {r.returncode}")
                return False
        except gitsafe.GitUnsafe as e:
            self._fail(e.reason, str(e))
            return False
        except (OSError, subprocess.TimeoutExpired) as e:
            self._fail("prefetch", str(e))
            return False
        return True

    def _fail(self, reason: str, message: str) -> None:
        self.d.update(state="failed", reason=reason, error=message, finished_ts=time.time(),
                      finished=datetime.now().astimezone().isoformat(timespec="seconds"))
        self._journal()  # журнал до записи терминального состояния (замечание 10)
        self.jobs.write(self.jid, self.d)

    def _run_code(self, cwd: Path, environ: dict):
        argv = json.loads((self.jdir / "argv.json").read_text(encoding="utf-8"))
        env = json.loads((self.jdir / "env.json").read_text(encoding="utf-8"))
        events = (self.jdir / "events.jsonl").open("w", encoding="utf-8")
        stderr = (self.jdir / "stderr.log").open("w", encoding="utf-8")
        proc = subprocess.Popen(argv, cwd=os.fspath(cwd), env=env, stdin=subprocess.DEVNULL,
                                stdout=events, stderr=stderr, start_new_session=True)
        pgid = os.getpgid(proc.pid)
        self._save(state="running", pid=proc.pid, pgid=pgid, proc_start=proc_start(proc.pid),
                   started=time.time())
        max_s = float(self.d.get("max_hours") or 4) * 3600
        escalated = None
        stream = _Stream(self.jdir / "events.jsonl")
        while proc.poll() is None:
            time.sleep(POLL)
            self._watch(stream)
            over = time.time() - self.d["started"] > max_s
            if (self._cancelled() or over or self.stop) and escalated is None:
                # остановка стражем — тем же путём, что code_cancel (ТЗ WSH-4.1, п. 2.4)
                self._save(state="cancelling", reason=self.stop or ("timeout" if over else "cancel"))
                escalated = self._escalate(proc)
        proc.wait()
        events.close()
        stderr.close()
        self._watch(stream)  # хвост потока: claude мог выйти раньше очередного опроса

    def _watch(self, stream: _Stream) -> None:
        """Сверка оплаты по потоку (ТЗ WSH-4.1, п. 2.4): init — источник и фактическая модель, rate_limit_event —
        последний rate_limit_info и счётчик. apiKeySource не "none" или поля нет — billing_mismatch; isUsingOverage
        без [code] allow_overage — overage. Первая причина остаётся."""
        changed = False
        for e in stream.read():
            if e.get("type") not in ("system", "rate_limit_event"):
                continue
            b = self.d.setdefault("billing", {})
            if e.get("type") == "system" and e.get("subtype") == "init":
                src = e.get("apiKeySource")
                if b.get("source", "none") == "none":  # несовпадение не затирается следующим init
                    b["source"] = src
                b["model"] = e.get("model")
                changed = True
                if src != "none":
                    self.stop = self.stop or "billing_mismatch"
            elif e.get("type") == "rate_limit_event":
                b["rate_events"] = int(b.get("rate_events") or 0) + 1
                info = e.get("rate_limit_info")
                if isinstance(info, dict):
                    b.update(rate=info, rate_at=_iso(time.time()), rate_ts=time.time())
                    if info.get("isUsingOverage") and not self._allow_overage():
                        self.stop = self.stop or "overage"
                changed = True
        if changed:
            try:
                self._save()
            except OSError:  # сбой записи (диск) не ломает цикл: остановка по self.stop идёт, billing допишет _finish
                pass

    def _allow_overage(self) -> bool:
        try:
            return self.hub.registry.get().code.glob.allow_overage is True
        except Exception:  # noqa: BLE001 — реестр не читается: overage не разрешён
            return False

    def _escalate(self, proc) -> bool:
        """SIGINT процессу Code → пауза → SIGTERM его группе → пауза → SIGKILL группе (п. 2.5.4)."""
        pid, pgid = proc.pid, self.d["pgid"]
        t_term, t_kill = cancel_timers()
        steps = ((signal.SIGINT, False, t_term), (signal.SIGTERM, True, t_kill), (signal.SIGKILL, True, 0))
        for sig, group, wait in steps:
            try:
                os.killpg(pgid, sig) if group else os.kill(pid, sig)
            except (ProcessLookupError, PermissionError):
                return True
            if not wait:
                break
            deadline = time.time() + wait
            while time.time() < deadline:
                if proc.poll() is not None:
                    return True
                time.sleep(0.2)
        return True

    def _finish(self, git: str, cwd: Path, environ: dict):
        res_event = _tail_result(self.jdir / "events.jsonl") or {}
        denials = res_event.get("permission_denials") or []
        result = {
            "subtype": res_event.get("subtype"),
            "session_id": res_event.get("session_id") or self.d.get("session_id"),
            "total_cost_usd": res_event.get("total_cost_usd"),
            "num_turns": res_event.get("num_turns"),
            "duration_ms": res_event.get("duration_ms"),
            "is_error": res_event.get("is_error"),
            "permission_denials_count": len(denials),
            "permission_denials": denials[:10],
            "result": _clip_bytes(res_event.get("result") or "", RESULT_MAX),
            "git": _git_snapshot(git, cwd, environ),
        }
        try:
            result["outside_code"] = _outside_code(git, cwd, environ, self.jdir)
        except Exception as e:  # noqa: BLE001 — блок прозрачности не должен ронять итог звена
            result["outside_code"] = {"commits": [], "commits_count": 0, "git_meta_changed": [], "docker": [],
                                      "docker_count": 0, "errors": [f"блок не собран: {e}"[:500]]}
        billing = self.d.get("billing") or {}
        rate = billing.get("rate") if isinstance(billing.get("rate"), dict) else {}
        if self.stop:  # страж подписки важнее отмены и таймаута: деньги (ТЗ WSH-4.1, решения плана)
            state, reason = "failed", self.stop
            if self.stop == "billing_mismatch":
                result["billing_source"] = billing.get("source")  # фактический источник; None — поля нет
        elif self.d.get("state") == "cancelling":
            state, reason = ("timeout", "max_hours") if self.d.get("reason") == "timeout" else ("cancelled", "cancel")
        elif not res_event:
            state, reason = "failed", "no_result"
        elif res_event.get("is_error") and rate.get("status") == "rejected":  # лимит подписки (п. 2.5, факт 9)
            state, reason = "failed", "subscription_limit"
            result["resets_at"] = _iso(rate.get("resetsAt"))
        else:
            state, reason = _map_state(res_event.get("subtype"), res_event.get("is_error"))
        result["session_id"] = result["session_id"] or self.d.get("session_id")
        result["taskfile_status"] = self._taskfile_status(cwd)
        result["phase_cost_usd"] = self._phase_cost(result.get("total_cost_usd"))
        if self.d.get("phase") == "plan" and state == "completed":
            if self._cancelled():  # отмена пришла после выхода claude — план не идёт дальше (ни в plan_ready)
                state, reason = "cancelled", "cancel"
            else:
                state, reason = self._save_plan(result)
        atomic_write_text(self.jdir / "result.json", json.dumps(result, ensure_ascii=False, indent=1))
        # журнал пишем до терминального состояния в job.json: читатель, увидевший терминал, уже видит строку
        self.d.update(state=state, reason=reason, session_id=result["session_id"], result=result,
                      finished_ts=time.time(), finished=datetime.now().astimezone().isoformat(timespec="seconds"))
        self._journal()
        self.jobs.write(self.jid, self.d)
        if state == "plan_ready" and self.d.get("plan_mode") == "auto":
            self._auto_execute()

    def _phase_cost(self, total) -> float | None:
        """Стоимость звена (решение 6): после --resume total_cost_usd накопительный за сессию (факт PoC 0.3.3),
        поэтому вычитаем накопленное к концу предыдущего звена той же сессии."""
        if not isinstance(total, (int, float)):
            return None
        prev = None
        parent, sid, hops = self.d.get("parent"), self.d.get("session_id"), 0
        # ближайший предок той же сессии с числовым итогом: у звена без итога (lost, no_result) его нет
        while self.d.get("resume") and parent and hops < 100:
            try:
                pd = self.jobs.read(parent)
            except JobError:
                break
            if pd.get("session_id") != sid:
                break
            prev = (pd.get("result") or {}).get("total_cost_usd")
            if isinstance(prev, (int, float)):
                break
            prev, parent, hops = None, pd.get("parent"), hops + 1
        if isinstance(prev, (int, float)) and 0 <= prev <= total:
            return round(total - prev, 6)
        return float(total)

    def _save_plan(self, result: dict) -> tuple[str, str | None]:
        """Копия плана в plan.md каталога задачи (≤ 200 КБ). Нет вызова Write в каталог планов — no_plan."""
        try:
            env = json.loads((self.jdir / "env.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            env = {}
        base = Path(env["CLAUDE_CONFIG_DIR"]) if env.get("CLAUDE_CONFIG_DIR") else \
            Path(env.get("HOME") or Path.home()) / ".claude"
        src = _plan_source(self.jdir / "events.jsonl", base / "plans", self.d.get("started"))
        if src is None:
            return "failed", "no_plan"
        try:
            with src.open("rb") as fh:
                data = fh.read(PLAN_MAX + 1)
        except OSError:
            return "failed", "no_plan"
        if len(data) > PLAN_MAX:
            data = data[:PLAN_MAX]
            self.d.setdefault("warnings", []).append("план длиннее 200 КБ — в plan.md обрезан")
        atomic_write_text(self.jdir / "plan.md", data.decode("utf-8", "ignore"))
        result.update(plan_file=str(src), plan_bytes=len(data))
        return "plan_ready", None

    def _auto_execute(self) -> None:
        """plan = auto (2.3.4): исполнение тем же путём, что code_approve — под тем же flock, с проверками
        max_parallel и дневного бюджета. Не вышло — план остаётся plan_ready с причиной, решает человек."""
        try:
            child = self.jobs.approve(self.jid, auto=True)
            self.hub._audit_code("code_approve", self.d.get("project"), self.jid, params={"auto": True,
                                                                                         "next": child["id"]})
        except Exception as e:  # noqa: BLE001 — любой отказ оставляет план ждать решения
            with self.jobs._flock():
                d = self.jobs.read(self.jid)
                if d.get("state") == "plan_ready" and not d.get("next"):
                    d["chain_error"] = str(e)[:500]
                    self.jobs.write(self.jid, d)
            self.hub._audit_code("code_approve", self.d.get("project"), self.jid, status="error",
                                 error=str(e), params={"auto": True})

    def _taskfile_status(self, cwd: Path) -> str | None:
        """Статус из ФАЙЛА ТЗ (а не из текста Code): тот же путь, внутри dir, ≤ 1 МБ (замечание 8)."""
        regex = self.d.get("status_regex")
        task = self.d.get("task")
        if not regex or not task:
            return None
        p = Path(task)
        try:
            if not p.resolve().is_relative_to(cwd.resolve()) or p.stat().st_size > 1024 * 1024:
                return None
            return _find_status(p.read_text(encoding="utf-8", errors="replace"), regex)
        except OSError:
            return None

    def _journal(self):
        res = self.d.get("result") or {}
        oc = res.get("outside_code") or {}
        b = self.d.get("billing") or {}
        sha = hashlib.sha256((self.d.get("prompt") or "").encode()).hexdigest()
        try:
            prompt = (self.jdir / "prompt.txt").read_text(encoding="utf-8")
            sha = hashlib.sha256(prompt.encode()).hexdigest()
        except OSError:
            pass
        self.hub._audit_code("code_finish", self.d.get("project"), self.jid, prompt_sha=sha,
                             status="ok" if self.d.get("state") in ("completed", "plan_ready") else "error",
                             error=self.d.get("error"), params={
                                 "state": self.d.get("state"), "reason": self.d.get("reason"),
                                 "cost_usd": res.get("phase_cost_usd", res.get("total_cost_usd")),
                                 "session_cost_usd": res.get("total_cost_usd"), "num_turns": res.get("num_turns"),
                                 "session_id": res.get("session_id"), "phase": self.d.get("phase"),
                                 "docker_count": oc.get("docker_count"), "commits_count": oc.get("commits_count"),
                                 "git_meta_changed": oc.get("git_meta_changed"),
                                 "billing_source": b.get("source"), "billing_model": b.get("model"),
                                 "rate_events": b.get("rate_events") or 0, "rate": _rate_brief(b.get("rate"))})


def run_job(hub, jid: str) -> int:
    # POSIX сохраняет SIG_IGN при exec: если сервер игнорировал SIGCHLD, надзиратель унаследовал бы игнор и
    # его subprocess (git fetch, claude) получали бы ECHILD и код 0. Возвращаем штатное поведение.
    try:
        signal.signal(signal.SIGCHLD, signal.SIG_DFL)
    except (AttributeError, ValueError, OSError):  # pragma: no cover — не POSIX
        pass
    try:
        Supervisor(hub, jid).run()
        return 0
    except Exception as e:  # noqa: BLE001 — надзиратель не должен падать молча
        try:
            jobs = Jobs(hub)
            d = jobs.read(jid)
            d.update(state="failed", reason="supervisor", error=str(e)[:500])
            jobs.write(jid, d)
        except Exception:  # noqa: BLE001
            pass
        return 1

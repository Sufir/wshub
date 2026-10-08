"""Журнал и копии: служебные чтения панели не пишутся, ротация журнала, чтение через архивы,
срок хранения копий, лимиты в реестре."""
import json
import os
from datetime import datetime

import pytest
from test_panel_ops import make_panel

from wshub import doctor, housekeeping
from wshub.core import WsError
from wshub.registry import Limits, RegistryError, parse
from wshub.registry_edit import revision

DAY = 86400


def journal(env) -> list[dict]:
    p = env.state / "audit.jsonl"
    return [json.loads(line) for line in p.read_text().splitlines()] if p.exists() else []


def set_limits(env, **lim):
    env.write_registry()
    with env.cfg.open("a", encoding="utf-8") as f:
        f.write("\n[limits]\n" + "".join(f"{k} = {v}\n" for k, v in lim.items()))
    env._mtime += 1_000_000_000  # mtime на ФС грубый — двигаем сами, как Env.write_registry
    os.utime(env.cfg, ns=(env._mtime, env._mtime))


# ---------- шум ----------

def test_panel_reads_not_logged(env):
    panel = make_panel(env)
    token = env.open("payload", "rw")
    env.hub.write(token, "a.txt", "v2\n")  # есть копия — для списка копий и diff
    before = journal(env)
    assert [r["tool"] for r in before] == ["workspace_open", "write"]

    env.hub.panel_data()  # инструмент panel
    key = panel.data()["key"]
    panel.browse(key, "")
    panel.browse(key, str(env.tmp))
    panel.brief_check(key, str(env.proj), "x.md")
    panel.mask_preview(key, str(env.proj), ["*.txt"])
    panel.audit(key)
    panel.audit(key, only_changes=True)
    items = panel.backups(key, "payload")["items"]
    panel.backups(key, "")
    panel.diff(key, "payload", items[0]["id"])
    assert journal(env) == before


def test_panel_changes_logged(env):
    panel = make_panel(env)
    token = env.open("payload", "rw")
    env.hub.write(token, "a.txt", "v2\n")
    d = panel.data()
    panel.save_workspace(d["nonce"], name="newp", path=str(env.outside), mode="ro", description="", brief="",
                         deny=[], rev=d["rev"], create=True)
    d = panel.data()
    panel.save_limits(d["nonce"], values={"journal_max_mb": 7, "journal_keep_files": 5, "backup_keep_days": 30,
                                          "backup_keep_per_file": 10}, rev=d["rev"])
    d = panel.data()
    panel.delete_workspace(d["nonce"], name="newp", rev=d["rev"])
    item = panel.backups(panel.data()["key"], "payload")["items"][0]
    panel.restore(panel.data()["nonce"], "payload", item["id"])
    hid = env.hub.handles[token].hid
    panel.revoke(panel.data()["nonce"], hid)
    with pytest.raises(WsError):
        panel.revoke("bad", hid)
    tools = [(r["tool"], r["status"]) for r in journal(env)]
    assert tools == [("workspace_open", "ok"), ("write", "ok"), ("panel_save", "ok"), ("panel_limits", "ok"),
                     ("panel_delete", "ok"), ("panel_restore", "ok"), ("panel_revoke", "ok"),
                     ("panel_revoke", "error")]


def test_audit_filters(env):
    panel = make_panel(env)
    token = env.open("payload", "rw")
    env.hub.write(token, "a.txt", "v2\n")
    env.hub.read(token, "a.txt")
    with pytest.raises(WsError):
        env.hub.read(token, ".env")
    key = panel.data()["key"]
    assert [r["tool"] for r in panel.audit(key)["records"]] == ["read", "read", "write", "workspace_open"]
    assert [r["tool"] for r in panel.audit(key, only_changes=True)["records"]] == ["write"]
    assert [r["path"] for r in panel.audit(key, only_errors=True)["records"]] == [".env"]
    assert [r["tool"] for r in panel.audit(key, tool="read", ws="payload")["records"]] == ["read", "read"]
    assert panel.audit(key, ws="rt")["records"] == []


# ---------- ротация ----------

def test_rotation_by_size_and_keep(env):
    set_limits(env, journal_max_mb=1, journal_keep_files=2)
    t = [datetime(2026, 10, 1, 12, 0, 0).timestamp()]
    env.hub.clock = lambda: t[0]
    pad = "x" * (400 * 1024)
    names = []
    for i in range(12):  # 3 записи по 400 КБ > 1 МБ → ротация на каждой третьей
        env.hub._append_journal({"tool": "read", "n": i, "pad": pad})
        t[0] += 1
        names.append(sorted(p.name for p in env.state.glob("audit-*.jsonl")))
    arch = housekeeping.archives(env.state)
    assert [p.name for p in arch] == ["audit-20261001-120011.jsonl", "audit-20261001-120008.jsonl"]
    assert names[2] == ["audit-20261001-120002.jsonl"]  # первая ротация — на третьей записи
    assert not (env.state / "audit.jsonl").exists() or (env.state / "audit.jsonl").stat().st_size <= 1024 * 1024
    for p in arch:
        assert p.stat().st_size > 1024 * 1024
    assert [json.loads(x)["n"] for x in arch[0].read_text().splitlines()] == [9, 10, 11]


def test_rotation_same_second_and_small_file(env):
    s = env.state
    s.mkdir()
    (s / "audit.jsonl").write_text("x" * 10)
    now = datetime(2026, 10, 1, 12, 0, 0).timestamp()
    assert housekeeping.rotate_if_needed(s, 100, 5, now) is None
    for _ in range(3):
        (s / "audit.jsonl").write_text("x" * 200)
        housekeeping.rotate_if_needed(s, 100, 5, now)
    assert [p.name for p in housekeeping.archives(s)] == [
        "audit-20261001-120000-2.jsonl", "audit-20261001-120000-1.jsonl", "audit-20261001-120000.jsonl"]


def test_read_across_archive_boundary(env):
    panel = make_panel(env)
    set_limits(env, journal_max_mb=1, journal_keep_files=5)
    t = [datetime(2026, 10, 1, 12, 0, 0).timestamp()]
    env.hub.clock = lambda: t[0]
    pad = "y" * 3000
    for i in range(500):  # ~1,5 МБ: одна ротация
        env.hub._append_journal({"tool": "write" if i % 7 == 0 else "read", "n": i, "pad": pad})
        t[0] += 1
    assert len(housekeeping.archives(env.state)) == 1 and (env.state / "audit.jsonl").exists()
    cur_first = json.loads((env.state / "audit.jsonl").read_text().splitlines()[0])["n"]
    assert 0 < cur_first < 499
    key = panel.data()["key"]

    r = panel.audit(key)
    assert r["files"] == 2
    seen = [x["n"] for x in r["records"]]
    # пока листаем — журнал ещё раз уходит в архив: курсор (inode) переживает переименование
    for i in range(500, 900):
        env.hub._append_journal({"tool": "read", "n": i, "pad": pad})
        t[0] += 1
    assert len(housekeeping.archives(env.state)) == 2
    while r["cursor"]:
        r = panel.audit(key, cursor=r["cursor"])
        seen += [x["n"] for x in r["records"]]
    assert seen == list(range(499, -1, -1))  # через границу архива, без пропусков и повторов

    ch = []
    r = {"cursor": ""}
    while True:
        r = panel.audit(key, cursor=r["cursor"], only_changes=True, limit=10)
        ch += [x["n"] for x in r["records"]]
        if not r["cursor"]:
            break
    assert ch == [n for n in range(899, -1, -1) if n < 500 and n % 7 == 0]


def test_cursor_bad(env):
    panel = make_panel(env)
    with pytest.raises(Exception, match="курсор"):
        panel.audit(panel.data()["key"], cursor="nonsense")


# ---------- копии ----------

def make_copy(base, project, ts, rel, text="x", seq=0):
    stamp = datetime.fromtimestamp(ts).strftime("%Y%m%d-%H%M%S")
    f = base / project / stamp / rel
    if seq:
        f = f.with_name(f"{f.name}.{seq}")
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(text)
    return f


def test_backup_retention_rule(env):
    base = env.hub.backup_dir
    now = datetime(2026, 10, 7, 12, 0, 0).timestamp()
    # a.txt: 15 копий, по одной в день, от 0 до 14×5 дней назад
    a = [make_copy(base, "payload", now - i * 5 * DAY, "docs/a.txt", f"a{i}") for i in range(15)]
    # b.txt: единственная копия, очень старая
    b = make_copy(base, "payload", now - 400 * DAY, "b.txt")
    # c.txt: 12 старых копий — 10 новейших остаются, хоть они и старые
    c = [make_copy(base, "other", now - (100 + i) * DAY, "c.txt") for i in range(12)]
    # d.txt: две копии за одну секунду — x и x.1 (x.1 новее)
    d0 = make_copy(base, "payload", now - 50 * DAY, "d.txt")
    d1 = make_copy(base, "payload", now - 50 * DAY, "d.txt", seq=1)
    junk = base / "payload" / "not-a-stamp" / "z.txt"
    junk.parent.mkdir(parents=True)
    junk.write_text("z")

    res = housekeeping.cleanup_backups(base, keep_days=30, keep_per_file=10, now=now)
    # a: копии 10..14 (50..70 дней) — у каждой ≥ 10 более новых и они старше 30 дней
    assert all(p.exists() for p in a[:10]) and not any(p.exists() for p in a[10:])
    assert b.exists()  # последняя копия файла не удаляется никогда
    assert all(p.exists() for p in c[:10]) and not any(p.exists() for p in c[10:])
    assert d0.exists() and d1.exists()
    assert junk.exists()
    assert len(res["deleted"]) == 7 and res["freed"] > 0
    assert not a[12].parent.parent.exists()  # опустевшие папки копий убраны

    res = housekeeping.cleanup_backups(base, keep_days=30, keep_per_file=1, now=now)
    assert b.exists() and a[0].exists() and d1.exists() and not d0.exists()
    assert all(p.exists() for p in a[1:7])  # моложе 30 дней (a[6] — ровно 30) — остаются
    assert not any(p.exists() for p in a[7:10])


def test_cleanup_once_a_day_and_logged(env):
    base = env.hub.backup_dir
    t = [datetime(2026, 10, 7, 12, 0, 0).timestamp()]
    env.hub.clock = lambda: t[0]
    old = [make_copy(base, "payload", t[0] - (40 + i) * DAY, "a.txt") for i in range(13)]
    set_limits(env, backup_keep_days=30, backup_keep_per_file=10)

    res = env.hub.maybe_cleanup()  # старт сервера
    assert len(res["deleted"]) == 3 and not any(p.exists() for p in old[10:])
    recs = journal(env)
    assert len(recs) == 1 and recs[0]["tool"] == "backup_cleanup" and recs[0]["deleted"] == 3
    assert sorted(recs[0]["files"]) == sorted(f"payload/{p.relative_to(base / 'payload').as_posix()}"
                                              for p in old[10:])

    extra = make_copy(base, "payload", t[0] - 60 * DAY, "a.txt", "older")
    t[0] += 3600
    assert env.hub.maybe_cleanup() is None  # в этом процессе — рано
    other = type(env.hub)(env.cfg, env.state, clock=lambda: t[0])  # другой процесс: метка общая
    assert other.maybe_cleanup() is None
    assert extra.exists() and len(journal(env)) == 1

    t[0] += DAY
    res = env.hub.maybe_cleanup()
    assert res["deleted"] == [f"payload/{extra.relative_to(base / 'payload').as_posix()}"]
    t[0] += 2 * DAY
    assert env.hub.maybe_cleanup() == {"deleted": [], "freed": 0}
    assert [r["tool"] for r in journal(env)] == ["backup_cleanup", "backup_cleanup"]  # пустая — без записи


def test_write_triggers_daily_cleanup(env):
    base = env.hub.backup_dir
    t = [datetime(2026, 10, 7, 12, 0, 0).timestamp()]
    env.hub.clock = lambda: t[0]
    env.hub.maybe_cleanup()
    env.hub._next_cleanup = 0  # будто процесс работает больше суток
    t[0] += DAY + 1
    old = [make_copy(base, "payload", t[0] - (40 + i) * DAY, "a.txt") for i in range(11)]
    token = env.open("payload", "rw")
    env.hub.write(token, "a.txt", "new\n")  # новая копия a.txt → у old[9], old[10] ≥ 10 более новых
    assert not old[10].exists() and not old[9].exists() and old[8].exists()
    assert [r["tool"] for r in journal(env)] == ["workspace_open", "backup_cleanup", "write"]


# ---------- лимиты ----------

def test_limits_parse():
    assert parse("").limits == Limits(5, 5, 30, 10)
    assert parse("[limits]\njournal_max_mb = 2\n").limits == Limits(2, 5, 30, 10)
    for bad in ("journal_max_mb = 0", "journal_max_mb = -1", "journal_max_mb = 1.5", 'journal_max_mb = "5"',
                "journal_max_mb = true"):
        with pytest.raises(RegistryError):
            parse(f"[limits]\n{bad}\n")
    reg = parse("[limits]\nunknown = 1\njournal_max_mb = 2\n")
    assert reg.limits == Limits(2, 5, 30, 10) and reg.warnings == ("неизвестный ключ limits.unknown — пропущен",)


def test_limits_default_when_registry_broken(env):
    env.cfg.write_text("[[[", encoding="utf-8")
    assert env.hub.limits() == Limits()


def test_save_limits_form(env):
    panel = make_panel(env)
    env.cfg.write_text("# шапка\n" + env.cfg.read_text(), encoding="utf-8")
    good = {"journal_max_mb": "8", "journal_keep_files": 3, "backup_keep_days": "30", "backup_keep_per_file": 10}
    for k, bad in (("journal_max_mb", "0"), ("journal_keep_files", "abc"), ("backup_keep_days", "1.5"),
                   ("backup_keep_per_file", ""), ("journal_max_mb", -3), ("journal_max_mb", True)):
        d = panel.data()
        before = env.cfg.read_bytes()
        with pytest.raises(Exception, match="целое число больше 0"):
            panel.save_limits(d["nonce"], values={**good, k: bad}, rev=d["rev"])
        assert env.cfg.read_bytes() == before
    d = panel.data()
    ch = panel.save_limits(d["nonce"], values=good, rev=d["rev"])["changes"]
    assert ch == ["лимиты: размер файла журнала, МБ: 5 → 8", "лимиты: архивов журнала хранить: 5 → 3"]
    text = env.cfg.read_text()
    assert text.startswith("# шапка") and "[limits]" in text
    assert env.hub.limits() == Limits(8, 3, 30, 10)
    assert len(list((env.state / "registry-history").glob("*.toml"))) == 1
    d = panel.data()
    assert d["storage"]["limits"] == {"journal_max_mb": 8, "journal_keep_files": 3, "backup_keep_days": 30,
                                      "backup_keep_per_file": 10}
    assert panel.save_limits(d["nonce"], values=good, rev=d["rev"])["changes"] == ["изменений нет"]
    d = panel.data()
    with pytest.raises(Exception, match="реестр изменился"):
        panel.save_limits(d["nonce"], values=good, rev=revision(b"old"))


def test_doctor_storage(env):
    from test_panel_ops import Ctx
    set_limits(env, journal_max_mb=2)
    token = env.open("payload", "rw")
    env.hub.write(token, "a.txt", "v2\n")
    (env.state / "audit-20261001-120000.jsonl").write_text("{}\n")
    ctx = Ctx(config=env.cfg, state=env.state, home=env.tmp / "home", win_users=env.tmp / "nowin",
              proc=env.tmp / "proc", protected_overlap=env.hub.protected_overlap,
              workspace_conflict=env.hub.workspace_conflict)
    res = doctor.run(ctx)
    checks = {c["id"]: c for c in res["checks"]}
    assert "из 2 МБ" in checks["audit"]["detail"][0]
    assert checks["audit"]["detail"][1].startswith("архивов: 1 из 5")
    assert "файлов 1" in checks["backup"]["detail"][0] and "старше 30 дн." in checks["backup"]["detail"][1]
    assert checks["limits"]["status"] == "ok"
    assert "journal_max_mb = 2 (размер файла журнала, МБ)" in checks["limits"]["detail"]
    assert res["storage"]["journal"]["archives"] == 1 and res["storage"]["backup"]["files"] == 1
    env.write_registry()
    checks = {c["id"]: c for c in doctor.run(ctx)["checks"]}
    assert checks["limits"]["status"] == "info" and "по умолчанию" in checks["limits"]["detail"][-1]

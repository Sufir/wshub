"""ТЗ WSH-3.7, часть В: хэндлы в handles.json каталога состояния — переживают перезапуск сервера и действуют в
другом процессе того же хаба; на диске — sha256 токена, не токен."""
import hashlib
import json
import stat
import threading

import pytest
from test_panel_ops import make_panel

from wshub import core, runtime
from wshub.core import BLOCKED, REOPEN, REVOKED, Hub, WsError
from wshub.runtime import Runtime, handle_key

TOKEN = "QzXwVyRuMnLpKjHgFtSdWqEe"  # 24 знака, как у token_urlsafe(18): подстроки легко искать в байтах файла


def _rows(env) -> dict:
    return json.loads((env.state / "handles.json").read_text())["handles"]


def _journal(env) -> list[dict]:
    return [json.loads(x) for x in (env.state / "audit.jsonl").read_text().splitlines()]


def _clocked(env, t) -> Hub:
    return Hub(env.cfg, env.state, clock=lambda: t[0])


# ---------- В1. файл ----------

def test_file_has_hash_not_token(env, monkeypatch):
    """Тест 11: ключ — полный sha256, в байтах файла и журнала нет токена и его подстрок длиннее 4 знаков."""
    monkeypatch.setattr(core.secrets, "token_urlsafe", lambda n: TOKEN)
    assert env.open("payload", "rw") == TOKEN
    rows = _rows(env)
    key = hashlib.sha256(TOKEN.encode()).hexdigest()
    assert list(rows) == [key] and len(key) == 64
    r = rows[key]
    assert r == {"name": "payload", "mode": "rw", "root": str(env.proj.resolve()), "expires": r["expires"],
                 "hid": key[:16], "prefix": TOKEN[:4], "opened": r["opened"]}
    assert r["expires"] - r["opened"] == 8 * 3600
    Hub(env.cfg, env.state).ls(TOKEN, ".")  # восстановление и вызов тоже ничего с токеном не пишут
    files = [f for f in env.state.rglob("*") if f.is_file()]
    assert env.state / "handles.json" in files and env.state / "audit.jsonl" in files
    for path in files:  # весь каталог состояния (ревью субагента: не только два файла)
        data = path.read_bytes()
        assert all(TOKEN[i:i + 5].encode() not in data for i in range(len(TOKEN) - 4)), path


def test_file_mode_0600(env):
    """Тест 11: права 0600 — у нового файла и после перезаписи файла 0644."""
    env.open()
    f = env.state / "handles.json"
    assert stat.S_IMODE(f.stat().st_mode) == 0o600
    f.chmod(0o644)
    env.open()
    assert stat.S_IMODE(f.stat().st_mode) == 0o600 and len(_rows(env)) == 2


# ---------- В3. восстановление и проверки ----------

def test_expired_dropped(env):
    """Тест 12: истёкший хэндл у второго Hub — REOPEN, записи нет; истёкшую чужую запись убирает следующая запись."""
    t = [1_000_000.0]
    a = _clocked(env, t)
    ws = a.workspace_open("payload").splitlines()[0].removeprefix("ws: ")
    t[0] += 8 * 3600
    b = _clocked(env, t)
    with pytest.raises(WsError, match=REOPEN):
        b.ls(ws, ".")
    assert handle_key(ws) not in _rows(env)

    t[0] = 2_000_000.0
    ws1 = a.workspace_open("payload").splitlines()[0].removeprefix("ws: ")
    t[0] += 8 * 3600
    ws2 = a.workspace_open("rt").splitlines()[0].removeprefix("ws: ")  # никто не трогал ws1 — убрала запись
    assert list(_rows(env)) == [handle_key(ws2)] and handle_key(ws1) not in _rows(env)


def test_revoke_removes_record(env):
    """Тест 13, В2.4: отзыв удаляет запись, даже если процесс с хэндлом к нему не обращался; восстановленный
    хэндл отзыв тоже видит."""
    a = Hub(env.cfg, env.state)
    ws = a.workspace_open("payload").splitlines()[0].removeprefix("ws: ")
    b = Hub(env.cfg, env.state)
    assert b.ls(ws, ".")  # восстановлен в b
    assert a.runtime.revoke(a.handles[ws].hid) is None
    assert handle_key(ws) not in _rows(env)
    for hub in (a, b):
        with pytest.raises(WsError, match=REVOKED):
            hub.ls(ws, ".")
    with pytest.raises(WsError, match=REOPEN):  # новый процесс: записи нет
        Hub(env.cfg, env.state).ls(ws, ".")


def test_revoke_file_failure_still_revoked(env, monkeypatch):
    """Тест 13, В2.4: запись не удалилась — отзыв всё равно записан; хэндл, восстановленный из файла, отказывает
    REVOKED, и запись уходит. Причина — в журнал вызова панели."""
    ws = env.open()
    hid = env.hub.handles[ws].hid
    real = Runtime._drop_handles

    def fail(self, match):
        raise OSError("лок не взят")

    monkeypatch.setattr(Runtime, "_drop_handles", fail)
    p = make_panel(env)
    p.revoke(p.data()["nonce"], hid)
    [rec] = [x for x in _journal(env) if x["tool"] == "panel_revoke"]
    assert rec["status"] == "ok" and rec["handles_file_error"] == "лок не взят"
    assert handle_key(ws) in _rows(env) and hid in env.hub.runtime.revoked()
    monkeypatch.setattr(Runtime, "_drop_handles", real)
    b = Hub(env.cfg, env.state)
    with pytest.raises(WsError, match=REVOKED):
        b.ls(ws, ".")
    assert handle_key(ws) not in _rows(env) and b.handles == {}


def test_block_applies_to_restored(env):
    """Тест 13: запрет проекта — восстановленный хэндл отказывает BLOCKED, запись уходит."""
    ws = env.open()
    env.hub.runtime.block("payload")
    b = Hub(env.cfg, env.state)
    with pytest.raises(WsError) as e:
        b.ls(ws, ".")
    assert str(e.value) == BLOCKED and b.handles == {} and handle_key(ws) not in _rows(env)


@pytest.mark.parametrize("change", ["removed", "moved"])
def test_registry_change_drops_restored(env, change):
    """Тест 13: проект убран из реестра или сменил путь — REOPEN, запись уходит."""
    ws = env.open()
    if change == "removed":
        del env.workspaces["payload"]
    else:
        env.workspaces["payload"]["path"] = str(env.outside)
    env.write_registry()
    b = Hub(env.cfg, env.state)
    with pytest.raises(WsError, match=REOPEN):
        b.ls(ws, ".")
    assert handle_key(ws) not in _rows(env) and b.handles == {}


def test_rw_not_above_registry(env):
    """Тест 14: rw-хэндл, проект в реестре стал ro — второй Hub даёт сессию ro."""
    ws = env.open("payload", "rw")
    env.workspaces["payload"]["mode"] = "ro"
    env.write_registry()
    b = Hub(env.cfg, env.state)
    assert "hello" in b.read(ws, "a.txt")
    with pytest.raises(WsError, match="только для чтения"):
        b.write(ws, "new.txt", "x")
    assert not (env.proj / "new.txt").exists()


# ---------- битый файл, сбой записи ----------

@pytest.mark.parametrize("bad", ["not json", "array", "no_root", "expires_str", "nan", "infinity", "huge_int",
                                 "deep"])
def test_broken_file(env, bad):
    """Тест 15: битый файл или запись — REOPEN без исключения; следующий workspace_open заменяет файл, хэндлы этого
    процесса работают."""
    ws = env.open()
    f = env.state / "handles.json"
    good = _rows(env)[handle_key(ws)]
    text = {"not json": "{oops", "array": "[1, 2]",
            "no_root": json.dumps({"handles": {handle_key(ws): {k: v for k, v in good.items() if k != "root"}}}),
            "expires_str": json.dumps({"handles": {handle_key(ws): {**good, "expires": "завтра"}}}),
            # ревью субагента, п. 4–6: NaN и Infinity из JSON, целое вне float, вложенность глубже рекурсии
            "nan": json.dumps({"handles": {handle_key(ws): {**good, "expires": float("nan")}}}),
            "infinity": json.dumps({"handles": {handle_key(ws): {**good, "expires": float("inf")}}}),
            "huge_int": json.dumps({"handles": {handle_key(ws): good}}).replace(
                json.dumps(good["expires"]), "1" + "0" * 400),
            "deep": "[" * 100_000 + "]" * 100_000}[bad]
    f.write_text(text)
    b = Hub(env.cfg, env.state)
    with pytest.raises(WsError, match=REOPEN):
        b.ls(ws, ".")
    ws2 = env.open("rt")
    assert list(_rows(env)) == [handle_key(ws2)]  # файл переписан целиком, негодная запись выброшена
    assert env.hub.ls(ws, ".") and Hub(env.cfg, env.state).ls(ws2, ".")
    rec = [x for x in _journal(env) if x["tool"] == "workspace_open"][-1]
    assert (rec.get("handles_file") == "был повреждён — заменён") is (bad in ("not json", "array", "deep"))


@pytest.mark.parametrize("exc", [OSError, ValueError])
def test_write_failure_keeps_handle(env, monkeypatch, exc):
    """Тест 16: сбой записи — хэндл выдан и работает в этом процессе, строка в ответе, причина в журнале.
    ValueError — путь, который не кодируется в UTF-8 (ревью субагента, п. 4)."""
    def fail(path, text):
        raise exc("нет места на диске")

    monkeypatch.setattr(runtime, "atomic_write_text", fail)
    out = env.hub.workspace_open("payload")
    ws = out.splitlines()[0].removeprefix("ws: ")
    assert ("хэндл не сохранён на диск (нет места на диске): после перезапуска сервера открой проект заново"
            in out.splitlines())
    assert env.hub.ls(ws, ".")
    rec = [x for x in _journal(env) if x["tool"] == "workspace_open"][-1]
    assert rec["status"] == "ok" and rec["handle_not_saved"] == "нет места на диске"
    with pytest.raises(WsError, match=REOPEN):
        Hub(env.cfg, env.state).ls(ws, ".")


def test_saved_line_absent_normally(env):
    """В4.1: формат ответа прежний, когда запись удалась."""
    assert "не сохранён" not in env.hub.workspace_open("payload")


# ---------- лок ----------

def test_concurrent_opens_all_saved(env):
    """Тест 17: два «процесса» (Hub в двух потоках) открывают хэндлы одновременно — в файле все."""
    hubs = [Hub(env.cfg, env.state), Hub(env.cfg, env.state)]
    gate = threading.Barrier(2)
    got: list[list[str]] = [[], []]
    errors = []

    def run(i):
        try:
            gate.wait()
            for _ in range(30):
                got[i].append(hubs[i].workspace_open("payload").splitlines()[0].removeprefix("ws: "))
        except Exception as e:  # noqa: BLE001 — ошибку потока показывает assert ниже
            errors.append(e)

    threads = [threading.Thread(target=run, args=(i,)) for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert set(_rows(env)) == {handle_key(w) for w in got[0] + got[1]} and len(_rows(env)) == 60


# ---------- по ревью субагента ----------

def test_restore_respects_self_protection(env, code_dir):
    """П. 3: служебные каталоги у процессов свои — запись rw на код wshub этот процесс не восстанавливает
    (workspace_open он бы такой не выдал); ro на тот же каталог — восстанавливает, как и выдал бы."""
    env.workspaces["code"] = {"path": str(code_dir), "mode": "rw"}
    env.write_registry()
    now = env.hub.clock()
    for tok, mode in (("RwCodeTokenAbcdefghijklm", "rw"), ("RoCodeTokenAbcdefghijklm", "ro")):
        env.hub.runtime.save_handle(handle_key(tok), {
            "name": "code", "mode": mode, "root": str(code_dir.resolve()), "expires": now + 3600,
            "hid": handle_key(tok)[:16], "prefix": tok[:4], "opened": now}, now)
    with pytest.raises(WsError, match=REOPEN):
        env.hub.ls("RwCodeTokenAbcdefghijklm", ".")
    assert "x.txt" in env.hub.ls("RoCodeTokenAbcdefghijklm", ".")


def test_restored_hid_from_token(env):
    """П. 6: hid восстановленного хэндла — из токена, не из записи: отзыв по настоящему hid действует."""
    ws = env.open()
    f = env.state / "handles.json"
    rows = _rows(env)
    rows[handle_key(ws)]["hid"] = "0" * 16
    f.write_text(json.dumps({"handles": rows}))
    b = Hub(env.cfg, env.state)
    assert b.ls(ws, ".") and b.handles[ws].hid == handle_key(ws)[:16]
    b.runtime.revoke(handle_key(ws)[:16])
    with pytest.raises(WsError, match=REVOKED):
        b.ls(ws, ".")


def test_file_reread_only_when_changed(env, monkeypatch):
    """В3.2: handles.json перечитывается, только если изменился."""
    calls = []
    real = Runtime._handle_rows

    def counted(self):
        calls.append(1)
        return real(self)

    env.open()
    b = Hub(env.cfg, env.state)
    monkeypatch.setattr(Runtime, "_handle_rows", counted)
    for _ in range(3):
        with pytest.raises(WsError, match=REOPEN):
            b.ls("unknownTokenAbcdefghijkl", ".")
    assert len(calls) == 1
    ws = env.hub.workspace_open("rt").splitlines()[0].removeprefix("ws: ")  # запись — тоже чтение под локом
    n = len(calls)
    assert b.ls(ws, ".") and len(calls) == n + 1


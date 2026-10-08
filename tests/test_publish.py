"""publish: перевалка на «диск Windows» (во временной папке), имена NTFS, лимиты, очистка, журнал, панель, CLI."""
import hashlib
import json
import os
import subprocess
import sys
import time

import pytest
from test_panel_ops import make_panel

from wshub import doctor, outbox
from wshub.core import BLOCKED, NO_OUTBOX, REVOKED, Hub, WsError
from wshub.outbox import ntfs_name, path_problems, unique_names, win_path
from wshub.registry import Outbox, parse
from wshub.server import INSTRUCTIONS, PUBLISH_DOC, main

MB = 1024 * 1024


def set_outbox(env, text=None, **kw):
    """Реестр env + секция [outbox]; по умолчанию путь — <tmp>/mnt/c/Users/me/ClaudeOutbox."""
    env.write_registry()
    if text is None:
        vals = {"path": f'"{env.box}"', **{k: str(v) for k, v in kw.items()}}
        text = "[outbox]\n" + "".join(f"{k} = {v}\n" for k, v in vals.items())
    with env.cfg.open("a", encoding="utf-8") as f:
        f.write("\n" + text)
    env._mtime += 1_000_000_000
    os.utime(env.cfg, ns=(env._mtime, env._mtime))


@pytest.fixture
def box(env):
    env.mnt = env.tmp / "mnt"
    env.box = env.mnt / "c" / "Users" / "me" / "ClaudeOutbox"
    env.box.mkdir(parents=True)
    env.hub.mnt_root = env.mnt
    set_outbox(env)
    return env.box


def stages(box):
    return sorted(p for p in box.iterdir() if outbox.STAGE_RE.match(p.name))


def journal(env):
    return [json.loads(line) for line in (env.state / "audit.jsonl").read_text().splitlines()]


def age(path, seconds):
    t = time.time() - seconds
    os.utime(path, (t, t))


# ---------- основной путь ----------

def test_publish_copies_without_content(env, box):
    ws = env.open("payload")
    out = env.hub.publish_files(ws, ["a.txt"])
    [st] = stages(box)
    assert (st / "a.txt").read_bytes() == b"hello\nworld\n"
    lines = out.splitlines()
    assert lines[0] == "перевалка C:\\Users\\me\\ClaudeOutbox; копии удаляются через 15 мин"
    assert lines[1] == f"C:\\Users\\me\\ClaudeOutbox\\{st.name}\\a.txt — 12 байт"
    assert len(lines) == 2 and "hello" not in out
    assert not list(box.rglob("*.partial"))


def test_ro_allowed(env, box):
    ws = env.open("rt", "ro")
    assert "a.txt — 12 байт" in env.hub.publish_files(ws, ["a.txt"])


def test_refusals_like_read_and_partial_success(env, box):
    (env.proj / "sub").mkdir()
    (env.proj / "secrets").mkdir()
    (env.proj / "secrets" / "k.txt").write_text("k")
    (env.proj / "out.txt").symlink_to(env.outside / "secret.txt")
    (env.proj / "env-link").symlink_to(env.proj / ".env")
    (env.proj / "in-link.txt").symlink_to(env.proj / "a.txt")
    os.mkfifo(env.proj / "pipe")
    set_outbox(env, max_files_per_call=20)
    ws = env.open("payload")
    bad = {"../outside/secret.txt": "вне проекта", str(env.outside / "secret.txt"): "вне проекта",
           "../Payload2/x.txt": "вне проекта", "out.txt": "вне проекта", ".env": "deny", "env-link": "deny",
           "secrets/k.txt": "deny", "sub": "не обычный файл", "pipe": "не обычный файл", "missing.txt": "файла нет",
           ".": "не обычный файл"}
    out = env.hub.publish_files(ws, ["a.txt", *bad, "in-link.txt"])
    lines = out.splitlines()
    assert lines[1].endswith("\\a.txt — 12 байт") and lines[2].endswith("\\in-link.txt — 12 байт")
    refused = lines[3:]
    assert len(refused) == len(bad)
    for line, (p, why) in zip(refused, bad.items(), strict=True):
        assert line.startswith(f"отказ: {p} — ") and why in line
    [st] = stages(box)
    assert sorted(p.name for p in st.iterdir()) == ["a.txt", "in-link.txt"]
    assert "TOPSECRET" not in out and "supersecret" not in out


def test_all_refused_is_error_and_leaves_nothing(env, box):
    ws = env.open("payload")
    with pytest.raises(WsError, match="ни один файл не скопирован.*\nотказ: .env — доступ запрещён"):
        env.hub.publish_files(ws, [".env"])
    assert stages(box) == []


def test_revoked_and_blocked(env, box):
    ws = env.open("payload")
    env.hub.runtime.revoke(env.hub.handles[ws].hid)
    with pytest.raises(WsError, match=REVOKED):
        env.hub.publish_files(ws, ["a.txt"])
    ws = env.open("rt")
    env.hub.runtime.block("rt")
    with pytest.raises(WsError, match=BLOCKED):
        env.hub.publish_files(ws, ["a.txt"])
    with pytest.raises(WsError, match="workspace_open заново"):
        env.hub.publish_files("stale", ["a.txt"])
    assert stages(box) == []


def test_limits_size_and_count(env, box):
    set_outbox(env, max_file_mb=1, max_files_per_call=2)
    (env.proj / "big.bin").write_bytes(b"x" * (MB + 1))
    (env.proj / "ok.bin").write_bytes(b"x" * MB)
    ws = env.open("payload")
    out = env.hub.publish_files(ws, ["ok.bin", "big.bin"])
    assert "ok.bin — 1048576 байт" in out and "отказ: big.bin — файл 1048577 байт — больше лимита 1 МБ" in out
    with pytest.raises(WsError, match="не больше 2 файлов за вызов, передано 3"):
        env.hub.publish_files(ws, ["a.txt", "a.txt", "a.txt"])
    with pytest.raises(WsError, match="непустой список"):
        env.hub.publish_files(ws, [])


# ---------- имена NTFS ----------

@pytest.mark.parametrize("src, dst", [
    ('a<b>c:d"e|f?g*h.txt', "a_b_c_d_e_f_g_h.txt"),
    ("tab\there\x01.md", "tab_here_.md"),
    ("back\\slash.txt", "back_slash.txt"),
    ("name. . ", "name"),
    ("dots...", "dots"),
    ("...", "_"),
    ("CON", "_CON"),
    ("con.txt", "_con.txt"),
    ("Nul.tar.gz", "_Nul.tar.gz"),
    ("COM1.log", "_COM1.log"),
    ("lpt9", "_lpt9"),
    ("COM10.txt", "COM10.txt"),
    ("CONSOLE.txt", "CONSOLE.txt"),
    ("aux .txt", "_aux .txt"),
    ("обычное имя.md", "обычное имя.md"),
])
def test_ntfs_name(src, dst):
    assert ntfs_name(src) == dst


def test_ntfs_long_name_keeps_extension():
    n = ntfs_name("я" * 300 + ".md")
    assert len(n) == 120 and n.endswith(".md") and n.startswith("я")
    n = ntfs_name("x" * 300)
    assert n == "x" * 120
    n = ntfs_name("a" * 118 + ". b.txt")
    assert len(n) <= 120 and n.endswith(".txt") and not n[:-4].endswith((".", " "))


def test_unique_names():
    assert unique_names(["a.txt", "A.TXT", "a.txt", "b"]) == ["a.txt", "A (2).TXT", "a (3).txt", "b"]
    long = "z" * 117 + ".md"
    [_x, second] = unique_names([long, long])
    assert len(second) == 120 and second.endswith(" (2).md")


def test_names_in_stage(env, box):
    (env.proj / "d1").mkdir()
    (env.proj / "d2").mkdir()
    (env.proj / "d1" / "Same.md").write_text("1")
    (env.proj / "d2" / "same.md").write_text("2")
    (env.proj / "AUX.txt").write_text("x")
    (env.proj / "q?.txt").write_text("x")
    ws = env.open("payload")
    out = env.hub.publish_files(ws, ["d1/Same.md", "d2/same.md", "AUX.txt", "q?.txt"])
    [st] = stages(box)
    assert sorted(p.name for p in st.iterdir()) == ["Same.md", "_AUX.txt", "q_.txt", "same (2).md"]
    assert (st / "same (2).md").read_text() == "2" and "\\same (2).md — 1 байт" in out


# ---------- .partial ----------

def test_no_partial_after_error(env, box, monkeypatch):
    (env.proj / "b.txt").write_text("b")
    real = os.rename

    def rename(src, dst):
        if str(dst).endswith("b.txt"):
            raise PermissionError(13, "Permission denied")
        return real(src, dst)
    monkeypatch.setattr("wshub.core.os.rename", rename)
    ws = env.open("payload")
    out = env.hub.publish_files(ws, ["a.txt", "b.txt"])
    assert "\\a.txt — 12 байт" in out and "отказ: b.txt — нет прав на чтение или запись копии: b.txt" in out
    [st] = stages(box)
    assert [p.name for p in st.iterdir()] == ["a.txt"]
    with pytest.raises(WsError, match="ни один файл"):
        env.hub.publish_files(ws, ["b.txt"])
    assert len(stages(box)) == 1  # пустой каталог неудачного вызова удалён
    assert not list(box.rglob("*.partial"))


def test_file_grows_during_copy(env, box, monkeypatch):
    set_outbox(env, max_file_mb=1)
    (env.proj / "grow.bin").write_bytes(b"x" * 100)
    ws = env.open("payload")
    real_check = env.hub._publish_check

    def check(s, path, max_bytes):
        res = real_check(s, path, max_bytes)
        (env.proj / "grow.bin").write_bytes(b"x" * (MB + 10))  # файл вырос после проверки
        return res
    monkeypatch.setattr(env.hub, "_publish_check", check)
    with pytest.raises(WsError, match="вырос"):
        env.hub.publish_files(ws, ["grow.bin"])
    assert not list(box.rglob("*.partial")) and stages(box) == []


# ---------- настройка [outbox] ----------

def test_not_configured(env):
    ws = env.open("payload")
    with pytest.raises(WsError) as e:
        env.hub.publish_files(ws, ["a.txt"])
    assert str(e.value) == NO_OUTBOX and "wshub outbox set" in NO_OUTBOX


def test_section_parsed_tolerantly(env, box):
    reg = parse('[outbox]\npath = "/mnt/c/x/y"\nttl_minutes = 5\nextra = 1\n')
    assert reg.outbox == Outbox(present=True, path=reg.outbox.path, ttl_minutes=5)
    assert reg.warnings == ("неизвестный ключ outbox.extra — пропущен",)
    for bad, msg in (('path = "rel"', "абсолютный"), ("ttl_minutes = 0\npath = \"/mnt/c/a/b\"", "ttl_minutes"),
                     ("max_file_mb = \"5\"", "path не задан")):
        assert msg in parse(f"[outbox]\n{bad}\n").outbox.error
    assert parse("outbox = 5").outbox.error
    set_outbox(env, text='[outbox]\npath = "relative"\n')
    ws = env.open("payload")  # ошибка в [outbox] не ломает остальные инструменты
    assert "hello" in env.hub.read(ws, "a.txt")
    with pytest.raises(WsError, match="ошибка в секции \\[outbox\\].*абсолютный"):
        env.hub.publish_files(ws, ["a.txt"])


def test_path_problems(env, box):
    mnt = env.mnt
    roots = {"payload": env.proj}
    assert path_problems(str(box), roots, mnt) == []
    (mnt / "c" / "proj" / "inbox").mkdir(parents=True)
    (mnt / "c" / "Users" / "me" / "file").write_text("x")
    (mnt / "c" / "Users" / "me" / "link").symlink_to(box)
    other = env.tmp / "notwin"
    other.mkdir()
    cases = {
        "": "не задан",
        "relative/x": "абсолютный",
        str(mnt / "c" / "Users" / "me" / "missing"): "нет — создай",
        str(mnt / "c" / "Users" / "me" / "file"): "не каталог",
        str(mnt / "c" / "Users" / "me" / "link"): "симлинк",
        str(other): "не на диске Windows",
        str(mnt / "c"): "корень диска",
        str(mnt / "c" / "Users"): "домашняя папка Windows",
        str(mnt / "c" / "users" / "me"): "домашняя папка Windows",
        str(mnt / "c" / "proj" / "inbox"): "внутри проекта wsl",
    }
    for p, why in cases.items():
        errs = path_problems(p, {**roots, "wsl": mnt / "c" / "proj"}, mnt)
        assert errs and any(why in e for e in errs), (p, errs)
    assert win_path(mnt / "d" / "A B" / "x", mnt) == "D:\\A B\\x"
    assert outbox.from_windows("C:\\Users\\me\\Box") == "/mnt/c/Users/me/Box"


def test_bad_path_refuses_publish(env, box):
    env.workspaces["inbox"] = {"path": str(box.parent), "mode": "ro"}
    set_outbox(env)
    ws = env.open("payload")
    with pytest.raises(WsError, match="не годится.*внутри проекта inbox"):
        env.hub.publish_files(ws, ["a.txt"])


# ---------- очистка ----------

def make_stage(box, name, size=0, older=0):
    d = box / name
    d.mkdir()
    if size:
        (d / "f.bin").write_bytes(b"x" * size)
    age(d, older)
    return d


def test_cleanup_ttl_only_own_dirs(env, box):
    old = make_stage(box, "20260101-000000-aaaaaa", 10, older=16 * 60)
    fresh = make_stage(box, "20260101-000001-bbbbbb", 10, older=60)
    foreign = box / "20260101-000000-AAAAAA"  # не наш шаблон (заглавные)
    foreign.mkdir()
    age(foreign, 99999)
    (box / "мой файл.txt").write_text("чужой")
    age(box / "мой файл.txt", 99999)
    (box / "20260101-000000-cccccc").write_text("файл с нашим именем, но не каталог")
    ws = env.open("payload")
    env.hub.publish_files(ws, ["a.txt"])
    assert not old.exists() and fresh.exists() and foreign.exists()
    assert (box / "мой файл.txt").exists() and (box / "20260101-000000-cccccc").is_file()
    mark = outbox.last_cleanup(env.state)
    assert mark["deleted"] == 1 and mark["freed"] == 10


def test_cleanup_at_start(env, box):
    old = make_stage(box, "20260101-000000-aaaaaa", 1, older=3600)
    assert env.hub.outbox_cleanup()["deleted"] == 1 and not old.exists()


def test_max_total_evicts_oldest(env, box):
    set_outbox(env, max_total_mb=1)
    a = make_stage(box, "20260101-000000-aaaaaa", 400 * 1024, older=120)
    b = make_stage(box, "20260101-000001-bbbbbb", 400 * 1024, older=60)
    (env.proj / "c.bin").write_bytes(b"x" * 300 * 1024)
    ws = env.open("payload")
    env.hub.publish_files(ws, ["c.bin"])
    assert not a.exists() and b.exists() and len(stages(box)) == 2
    (env.proj / "huge.bin").write_bytes(b"x" * (MB + 1))
    with pytest.raises(WsError, match="перевалка переполнена.*max_total_mb = 1"):
        env.hub.publish_files(ws, ["huge.bin"])
    assert len(stages(box)) == 0  # всё вытеснено, но всё равно не влезло
    rec = journal(env)[-1]
    assert rec["tool"] == "publish" and rec["status"] == "error" and "переполнена" in rec["error"]


@pytest.mark.nonroot
def test_busy_file_skipped_then_retried(env, box):
    busy = make_stage(box, "20260101-000000-aaaaaa", 10, older=3600)
    os.chmod(busy, 0o555)  # как занятый файл в Windows: удалить содержимое нельзя
    try:
        res = env.hub.outbox_cleanup()
        assert res["busy"] == 1 and res["deleted"] == 0 and busy.exists()
        ws = env.open("payload")
        assert "a.txt" in env.hub.publish_files(ws, ["a.txt"])  # занятый каталог не мешает
    finally:
        os.chmod(busy, 0o755)
    age(busy, 3600)
    assert env.hub.outbox_cleanup()["deleted"] == 1 and not busy.exists()


@pytest.mark.nonroot
def test_busy_counts_against_total(env, box):
    set_outbox(env, max_total_mb=1)
    busy = make_stage(box, "20260101-000000-aaaaaa", 900 * 1024, older=3600)
    os.chmod(busy, 0o555)
    try:
        (env.proj / "c.bin").write_bytes(b"x" * 200 * 1024)
        ws = env.open("payload")
        with pytest.raises(WsError, match="переполнена.*занятых каталогов: 1"):
            env.hub.publish_files(ws, ["c.bin"])
    finally:
        os.chmod(busy, 0o755)


SCRIPT = """
import sys
from pathlib import Path
from wshub.core import Hub
hub = Hub(Path(sys.argv[1]), Path(sys.argv[2]))
hub.mnt_root = Path(sys.argv[3])
ws = hub.workspace_open("payload").splitlines()[0].removeprefix("ws: ")
for _ in range(5):
    print(hub.publish_files(ws, ["a.txt", "big.bin"]).splitlines()[1])
"""


def test_two_processes_in_parallel(env, box):
    (env.proj / "big.bin").write_bytes(os.urandom(2 * MB))
    old = make_stage(box, "20260101-000000-aaaaaa", 10, older=3600)
    args = [sys.executable, "-c", SCRIPT, str(env.cfg), str(env.state), str(env.mnt)]
    procs = [subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for _ in range(2)]
    outs = [p.communicate(timeout=60) for p in procs]
    for p, (_out, err) in zip(procs, outs, strict=True):
        assert p.returncode == 0, err
    dirs = stages(box)
    assert len(dirs) == 10 and not old.exists()
    big = (env.proj / "big.bin").read_bytes()
    for d in dirs:
        assert (d / "a.txt").read_text() == "hello\nworld\n" and (d / "big.bin").read_bytes() == big
    assert not list(box.rglob("*.partial"))
    recs = [r for r in journal(env) if r["tool"] == "publish"]
    assert len(recs) == 20 and all(r["status"] == "ok" for r in recs)


# ---------- журнал ----------

def test_journal(env, box):
    ws = env.open("payload")
    env.hub.publish_files(ws, ["a.txt", ".env"])
    [st] = stages(box)
    recs = [r for r in journal(env) if r["tool"] == "publish"]
    ok, err = recs[-1], recs[-2]
    assert ok["status"] == "ok" and ok["ws"] == "payload" and ok["path"] == "a.txt" and ok["size"] == 12
    assert ok["sha256"] == hashlib.sha256(b"hello\nworld\n").hexdigest()
    assert ok["outbox_id"] == st.name
    assert err["status"] == "error" and err["path"] == ".env" and "deny" in err["error"]
    p = make_panel(env)
    key = p.data()["key"]
    assert [r["path"] for r in p.audit(key, tool="publish")["records"]] == ["a.txt", ".env"]
    assert all(r["tool"] != "publish" for r in p.audit(key, only_changes=True)["records"])
    assert "publish" in p.audit(key)["tools"]


# ---------- панель ----------

def test_panel_clean_now(env, box):
    a = make_stage(box, "20260101-000000-aaaaaa", 10)
    b = make_stage(box, "20260101-000001-bbbbbb", 10)
    (box / "чужой.txt").write_text("x")
    p = make_panel(env)
    p.doctor_ctx.mnt_root = env.mnt
    d = p.data()
    assert d["outbox"]["dirs"] == 2 and d["outbox"]["win_path"] == "C:\\Users\\me\\ClaudeOutbox"
    with pytest.raises(WsError, match="одноразовый код"):
        p.outbox_clean("")
    with pytest.raises(WsError, match="недействителен"):
        p.outbox_clean("guess")
    assert a.exists()
    res = p.outbox_clean(d["nonce"])
    assert "удалено каталогов 2, освобождено 20 байт" in res["changes"][0]
    assert not a.exists() and not b.exists() and (box / "чужой.txt").exists()
    with pytest.raises(WsError, match="недействителен или уже использован"):
        p.outbox_clean(d["nonce"])
    recs = [r for r in journal(env) if r["tool"] == "panel_outbox_clean"]
    assert [r["status"] for r in recs] == ["error", "error", "ok", "error"]
    key = p.data()["key"]
    assert any(r["tool"] == "panel_outbox_clean" for r in p.audit(key, only_changes=True)["records"])


def test_panel_save_outbox(env, box):
    env.write_registry()  # без секции
    p = make_panel(env)
    p.doctor_ctx.mnt_root = env.mnt
    d = p.data()
    assert not d["outbox"]["present"] and d["outbox"]["path"] is None
    for vals, why in (({"path": ""}, "путь не задан"), ({"path": str(env.mnt / "c")}, "корень диска"),
                      ({"path": str(box), "ttl_minutes": "0"}, "целое число больше 0"),
                      ({"path": str(box), "max_total_mb": "abc"}, "целое число больше 0")):
        d = p.data()
        before = env.cfg.read_bytes()
        with pytest.raises(WsError, match=why):
            p.save_outbox(d["nonce"], values=vals, rev=d["rev"])
        assert env.cfg.read_bytes() == before
    d = p.data()
    ch = p.save_outbox(d["nonce"], values={"path": str(box), "ttl_minutes": "15", "max_file_mb": "50",
                                           "max_total_mb": "500", "max_files_per_call": "10"}, rev=d["rev"])["changes"]
    assert ch == ["добавлена секция [outbox]", f"перевалка: путь: (нет) → {box}"]
    d = p.data()
    assert d["outbox"]["present"] and d["outbox"]["values"]["ttl_minutes"] == 15
    ch = p.save_outbox(d["nonce"], values={"path": str(box), "ttl_minutes": 30}, rev=d["rev"])["changes"]
    assert ch == ["перевалка: копии в перевалке живут, минут: 15 → 30"]
    ws = env.open("payload")
    assert "удаляются через 30 мин" in env.hub.publish_files(ws, ["a.txt"])


def test_panel_html_has_outbox_block():
    from importlib import resources
    html = resources.files("wshub").joinpath("panel.html").read_text(encoding="utf-8")
    for s in ('id="outbox-clean"', "panel_outbox_clean", "panel_save_outbox", 'id="o-path"',
              'autocomplete="off" spellcheck="false"', '$("o-" + k).disabled = !v.path'):
        assert s in html


# ---------- CLI и doctor ----------

def test_cli_set_and_clean(env, monkeypatch, capsys):
    env.mnt = env.tmp / "mnt"
    env.box = env.mnt / "c" / "Users" / "me" / "ClaudeOutbox"
    env.box.mkdir(parents=True)
    monkeypatch.setattr("wshub.outbox.MNT", env.mnt)
    monkeypatch.setattr("wshub.core.outbox.MNT", env.mnt)
    monkeypatch.setenv("WSHUB_CONFIG", str(env.cfg))
    monkeypatch.setenv("WSHUB_STATE", str(env.state))
    monkeypatch.setattr(Hub, "__init__", _hub_init_with(env.mnt))
    with pytest.raises(SystemExit) as e:
        main(["outbox", "set", str(env.mnt / "c")])
    assert e.value.code == 1 and "корень диска" in capsys.readouterr().err
    with pytest.raises(SystemExit) as e:
        main(["outbox", "set", str(env.box)])
    out = capsys.readouterr().out
    assert e.value.code == 0 and "добавлена секция [outbox]" in out and "C:\\Users\\me\\ClaudeOutbox" in out
    reg = parse(env.cfg.read_text())
    assert reg.outbox.path == env.box and reg.outbox.ttl_minutes == 15 and reg.outbox.max_files_per_call == 10
    assert "max_total_mb = 500" in env.cfg.read_text()
    make_stage(env.box, "20260101-000000-aaaaaa", 5)
    with pytest.raises(SystemExit) as e:
        main(["outbox", "clean"])
    assert e.value.code == 0 and "удалено каталогов 1" in capsys.readouterr().out
    assert stages(env.box) == []
    tools = [r["tool"] for r in journal(env)]
    assert tools[-2:] == ["outbox_set", "outbox_clean"]


def _hub_init_with(mnt):
    orig = Hub.__init__

    def init(self, *a, **kw):
        orig(self, *a, **kw)
        self.mnt_root = mnt
    return init


def test_doctor_outbox(env, box):
    ctx = make_panel(env).doctor_ctx
    ctx.mnt_root = env.mnt
    make_stage(box, "20260101-000000-aaaaaa", 2048, older=600)

    def check():
        return {c["id"]: c for c in doctor.run(ctx)["checks"]}["outbox"]
    c = check()
    assert c["status"] == "ok", c
    assert c["detail"][0] == f"{box} → C:\\Users\\me\\ClaudeOutbox"
    assert any("каталогов wshub: 1, 2.0 КБ из 500 МБ, самый старый — 10 мин назад" in x for x in c["detail"])
    assert not list(box.glob(".wshub-probe-*"))
    env.write_registry()
    c = check()
    assert c["status"] == "info" and "секции [outbox]" in c["detail"][0] and "wshub outbox set" in c["fix"]
    set_outbox(env, text=f'[outbox]\npath = "{box.parent}"\n')
    c = check()
    assert c["status"] == "fail" and any("домашняя папка" in x for x in c["detail"])
    cloud = env.mnt / "c" / "Users" / "me" / "OneDrive" / "Box"
    cloud.mkdir(parents=True)
    set_outbox(env, text=f'[outbox]\npath = "{cloud}"\n')
    c = check()
    assert c["status"] == "warn" and "onedrive" in c["fix"].lower() + " ".join(c["detail"]).lower()


@pytest.mark.nonroot
def test_doctor_outbox_readonly(env, box):
    ctx = make_panel(env).doctor_ctx
    ctx.mnt_root = env.mnt
    os.chmod(box, 0o555)
    try:
        c = {c["id"]: c for c in doctor.run(ctx)["checks"]}["outbox"]
        assert c["status"] == "fail" and any("запись" in x for x in c["detail"])
    finally:
        os.chmod(box, 0o755)


# ---------- описание для модели ----------

def test_tool_description_and_instructions():
    assert len(PUBLISH_DOC.split()) <= 60
    for s in ("device_request_folder_access", "device_stage_files", 'SendUserFile(display="render")', "read",
              "не возвращает"):
        assert s in PUBLISH_DOC
    assert "Показать файл человеку — publish, не read + запись копии." in INSTRUCTIONS

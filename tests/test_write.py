"""Запись: режимы, симлинки, копии, .git, edit, права, журнал."""
import json
import os
import stat

import pytest

from wshub.core import WsError


def test_ro_refuses(env):
    ws = env.open("payload", "ro")
    with pytest.raises(WsError, match="только для чтения"):
        env.hub.write(ws, "a.txt", "x")
    with pytest.raises(WsError, match="только для чтения"):
        env.hub.edit(ws, "a.txt", "hello", "bye")
    assert (env.proj / "a.txt").read_text() == "hello\nworld\n"


def test_mode_above_registry_refused(env):
    with pytest.raises(WsError, match="только для чтения"):
        env.hub.workspace_open("rt", "rw")
    with pytest.raises(WsError, match="mode"):
        env.hub.workspace_open("payload", "admin")


def test_registry_downgrade_applies_to_open_handle(env):
    ws = env.open("payload", "rw")
    env.workspaces["payload"]["mode"] = "ro"
    env.write_registry()
    with pytest.raises(WsError, match="только для чтения"):
        env.hub.write(ws, "a.txt", "x")


def test_symlink_final_component_refused(env):
    (env.proj / "alias.txt").symlink_to(env.proj / "a.txt")
    ws = env.open(mode="rw")
    with pytest.raises(WsError, match="симлинк"):
        env.hub.write(ws, "alias.txt", "x")
    with pytest.raises(WsError, match="симлинк"):
        env.hub.edit(ws, "alias.txt", "hello", "x")
    assert (env.proj / "a.txt").read_text() == "hello\nworld\n"
    assert (env.proj / "alias.txt").is_symlink()


def test_backup_on_overwrite(env):
    os.chmod(env.proj / "a.txt", 0o640)
    ws = env.open(mode="rw")
    out = env.hub.write(ws, "a.txt", "new\n")
    bpath = out.split("копия: ")[1].strip()
    assert bpath.startswith(str(env.state / "backup" / "payload"))
    assert bpath.endswith("/a.txt")
    assert open(bpath).read() == "hello\nworld\n"
    assert (env.proj / "a.txt").read_text() == "new\n"
    assert stat.S_IMODE((env.proj / "a.txt").stat().st_mode) == 0o640
    # второй раз в ту же секунду — отдельная копия
    out2 = env.hub.write(ws, "a.txt", "newer\n")
    b2 = out2.split("копия: ")[1].strip()
    assert b2 != bpath and open(b2).read() == "new\n"
    assert not [p for p in env.proj.iterdir() if "wshub-tmp" in p.name]


def test_new_file_no_backup(env):
    ws = env.open(mode="rw")
    out = env.hub.write(ws, "dir/sub/new.md", "# new\n")
    assert "новый файл, копия не нужна" in out
    assert (env.proj / "dir/sub/new.md").read_text() == "# new\n"
    assert not (env.state / "backup").exists()


def test_git_refused(env):
    (env.proj / ".git").mkdir()
    (env.proj / ".git" / "config").write_text("[core]\n")
    ws = env.open(mode="rw")
    for p in (".git/config", ".git/hooks/pre-commit", "sub/.GIT/x", ".git"):
        with pytest.raises(WsError, match=r"\.git|каталог"):
            env.hub.write(ws, p, "x")
    with pytest.raises(WsError, match=r"\.git"):
        env.hub.edit(ws, ".git/config", "core", "evil")
    assert (env.proj / ".git" / "config").read_text() == "[core]\n"
    assert not (env.proj / ".git" / "hooks").exists()


def test_write_denied_name(env):
    ws = env.open(mode="rw")
    for p in (".env", "server.key", "secrets/new.txt", "x/.Env.prod"):
        with pytest.raises(WsError, match="deny"):
            env.hub.write(ws, p, "x")
    assert (env.proj / ".env").read_text() == "TOKEN=supersecret\n"


def test_edit_exactly_one(env):
    (env.proj / "e.txt").write_bytes(b"one\r\ntwo\r\ntwo\r\n")
    ws = env.open(mode="rw")
    with pytest.raises(WsError, match="2 раз"):
        env.hub.edit(ws, "e.txt", "two", "x")
    with pytest.raises(WsError, match="0 раз"):
        env.hub.edit(ws, "e.txt", "three", "x")
    with pytest.raises(WsError, match="пуст"):
        env.hub.edit(ws, "e.txt", "", "x")
    out = env.hub.edit(ws, "e.txt", "one", "ONE")
    assert "копия:" in out
    assert (env.proj / "e.txt").read_bytes() == b"ONE\r\ntwo\r\ntwo\r\n"  # CRLF сохранены
    with pytest.raises(WsError, match="файла нет"):
        env.hub.edit(ws, "missing.txt", "a", "b")


def test_audit_log(env):
    ws = env.open(mode="rw")
    env.hub.write(ws, "a.txt", "SECRET-CONTENT-123\n")
    env.hub.edit(ws, "a.txt", "SECRET-CONTENT-123", "OTHER-CONTENT-456")
    with pytest.raises(WsError):
        env.hub.read(ws, ".env")
    env.hub.read(ws, "a.txt")
    raw = (env.state / "audit.jsonl").read_text()
    assert "SECRET-CONTENT" not in raw and "OTHER-CONTENT" not in raw and "supersecret" not in raw
    assert ws not in raw  # хэндл в журнал не пишем
    recs = [json.loads(line) for line in raw.splitlines()]
    w = [r for r in recs if r["tool"] == "write"][0]
    assert w["ws"] == "payload" and w["path"] == "a.txt" and w["status"] == "ok"
    assert len(w["sha256_before"]) == 64 and len(w["sha256_after"]) == 64 and w["size"] == 19
    assert w["backup"]
    e = [r for r in recs if r["tool"] == "edit"][0]
    assert e["sha256_before"] == w["sha256_after"]
    err = [r for r in recs if r["tool"] == "read" and r["status"] == "error"][0]
    assert err["path"] == ".env" and "deny" in err["error"]
    assert all("ts" in r for r in recs)

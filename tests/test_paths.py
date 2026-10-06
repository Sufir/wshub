"""Обход корня: ../, абсолютные пути, симлинки наружу, соседняя папка с тем же префиксом, NUL, потоки NTFS."""
import os

import pytest

from wshub import core
from wshub.core import WsError


def test_dotdot_refused(env):
    ws = env.open()
    with pytest.raises(WsError, match="вне проекта"):
        env.hub.read(ws, "../outside/secret.txt")
    with pytest.raises(WsError, match="вне проекта"):
        env.hub.ls(ws, "..")


def test_absolute_outside_refused_inside_ok(env):
    ws = env.open()
    with pytest.raises(WsError, match="вне проекта"):
        env.hub.read(ws, str(env.outside / "secret.txt"))
    with pytest.raises(WsError, match="вне проекта"):
        env.hub.read(ws, "/etc/passwd")
    assert "hello" in env.hub.read(ws, str(env.proj / "a.txt"))


def test_symlink_file_outside(env):
    (env.proj / "lnk.txt").symlink_to(env.outside / "secret.txt")
    ws = env.open(mode="rw")
    with pytest.raises(WsError, match="вне проекта"):
        env.hub.read(ws, "lnk.txt")
    with pytest.raises(WsError):
        env.hub.write(ws, "lnk.txt", "x")
    assert (env.outside / "secret.txt").read_text() == "TOPSECRET outside\n"
    assert "lnk.txt -> (вне проекта)" in env.hub.ls(ws, ".")


def test_symlink_dir_outside(env, engine):
    (env.proj / "lnkdir").symlink_to(env.outside)
    ws = env.open(mode="rw")
    with pytest.raises(WsError, match="вне проекта"):
        env.hub.read(ws, "lnkdir/secret.txt")
    with pytest.raises(WsError, match="вне проекта"):
        env.hub.ls(ws, "lnkdir")
    with pytest.raises(WsError, match="вне проекта"):
        env.hub.write(ws, "lnkdir/new.txt", "x")
    assert not (env.outside / "new.txt").exists()
    assert "TOPSECRET" not in env.hub.grep(ws, "TOPSECRET")
    assert "secret.txt" not in env.hub.tree(ws, ".", 4)
    assert "secret.txt" not in env.hub.find(ws, "*.txt")


def test_sibling_with_same_prefix(env, engine):
    ws = env.open(mode="rw")
    for p in ("../Payload2/x.txt", str(env.sibling / "x.txt")):
        with pytest.raises(WsError, match="вне проекта"):
            env.hub.read(ws, p)
    with pytest.raises(WsError, match="вне проекта"):
        env.hub.write(ws, "../Payload2/new.txt", "x")
    (env.proj / "sib").symlink_to(env.sibling)
    with pytest.raises(WsError, match="вне проекта"):
        env.hub.read(ws, "sib/x.txt")
    assert "TOPSECRET" not in env.hub.grep(ws, "TOPSECRET")


def test_symlink_inside_is_ok(env):
    (env.proj / "alias.txt").symlink_to(env.proj / "a.txt")
    ws = env.open()
    assert "hello" in env.hub.read(ws, "alias.txt")


def test_nul_refused(env):
    ws = env.open(mode="rw")
    with pytest.raises(WsError, match="NUL"):
        env.hub.read(ws, "a.txt\x00.png")
    with pytest.raises(WsError, match="NUL"):
        env.hub.write(ws, "b\x00.txt", "x")


def test_colon_under_mnt(env, monkeypatch):
    (env.proj / "a:b.txt").write_text("colon ok\n")
    ws = env.open(mode="rw")
    assert "colon ok" in env.hub.read(ws, "a:b.txt")  # вне /mnt «:» допустим
    monkeypatch.setattr(core, "NTFS_ROOT", env.tmp)  # делаем вид, что проект лежит под /mnt
    with pytest.raises(WsError, match="NTFS"):
        env.hub.read(ws, "a.txt:Zone.Identifier")
    with pytest.raises(WsError, match="NTFS"):
        env.hub.write(ws, "a.txt:stream", "x")


def test_root_via_symlink(env):
    """Корень проекта задан через симлинк — проверки идут от resolve() корня."""
    link = env.tmp / "PayloadLink"
    link.symlink_to(env.proj)
    env.workspaces["viaLink"] = {"path": str(link), "mode": "ro"}
    env.write_registry()
    ws = env.open("viaLink")
    assert "hello" in env.hub.read(ws, "a.txt")
    with pytest.raises(WsError, match="вне проекта"):
        env.hub.read(ws, "../outside/secret.txt")
    assert os.path.exists(env.proj / "a.txt")

"""Хэндлы (истёкший, неизвестный, смена реестра), самозащита, workspace_open и BRIEF."""
from pathlib import Path

import pytest

import wshub
from wshub.core import REOPEN, Hub, WsError


def test_unknown_handle(env):
    for bad in ("nope", "", None):
        with pytest.raises(WsError, match="вызови workspace_open заново"):
            env.hub.ls(bad, ".")


def test_expired_handle(env):
    t = [1_000_000.0]
    env.hub.clock = lambda: t[0]
    ws = env.open()
    t[0] += 8 * 3600 - 1
    assert "a.txt" in env.hub.ls(ws, ".")
    t[0] += 2
    with pytest.raises(WsError, match="вызови workspace_open заново"):
        env.hub.ls(ws, ".")
    assert ws not in env.hub.handles
    ws2 = env.open()
    assert ws2 != ws and "a.txt" in env.hub.ls(ws2, ".")


def test_handle_dropped_when_workspace_removed_or_moved(env, tmp_path):
    ws = env.open()
    del env.workspaces["payload"]
    env.write_registry()
    with pytest.raises(WsError, match=REOPEN):
        env.hub.ls(ws, ".")
    env.workspaces["payload"] = {"path": str(env.proj), "mode": "rw"}
    env.write_registry()
    ws = env.open()
    env.workspaces["payload"]["path"] = str(env.outside)
    env.write_registry()
    with pytest.raises(WsError, match=REOPEN):
        env.hub.ls(ws, ".")


def test_handles_are_per_process(env):
    ws = env.open()
    other = Hub(env.cfg, env.state)  # «перезапуск сервера»
    with pytest.raises(WsError, match=REOPEN):
        other.ls(ws, ".")


REPO = Path(wshub.__file__).resolve().parents[2]  # корень репозитория — только открыть и прочитать


@pytest.mark.parametrize("which", ["state_parent", "config_dir", "state_dir", "inside_state"])
def test_self_protection(env, which):
    target = {
        "state_parent": env.tmp,  # проект, внутри которого лежат state и реестр
        "config_dir": env.cfg.parent,
        "state_dir": env.state,
        "inside_state": env.state / "backup",
    }[which]
    target.mkdir(parents=True, exist_ok=True)
    env.workspaces["bad"] = {"path": str(target), "mode": "rw"}
    env.write_registry()
    for mode in ("ro", "rw"):
        with pytest.raises(WsError, match="служебными данными wshub"):
            env.hub.workspace_open("bad", mode)
    assert not env.hub.handles


def test_self_protection_repo_ro(env):
    """Код wshub в ro открывается и читается; запись проверяется на поддельном каталоге (test_code_rw_by_hand)."""
    env.workspaces["self"] = {"path": str(REPO), "mode": "ro"}
    env.write_registry()
    ws = env.open("self")
    assert "[project]" in env.hub.read(ws, "pyproject.toml")
    with pytest.raises(WsError, match="rw недоступен"):
        env.hub.workspace_open("self", "rw")


def test_self_protection_uv_tool_dir(env, code_dir):
    env.workspaces["tools"] = {"path": str(code_dir.parent), "mode": "ro"}
    env.workspaces["tools_rw"] = {"path": str(code_dir.parent), "mode": "rw"}
    env.write_registry()
    env.open("tools")
    with pytest.raises(WsError, match="доступен только режим ro"):
        env.hub.workspace_open("tools_rw", "rw")


def test_code_rw_by_hand(env, code_dir):
    """rw поверх кода (реестр записан руками): rw — отказ, ro открывается, запись — отказ."""
    env.workspaces["code"] = {"path": str(code_dir), "mode": "rw"}
    env.write_registry()
    with pytest.raises(WsError, match="кодом wshub"):
        env.hub.workspace_open("code", "rw")
    ws = env.open("code")
    assert "code" in env.hub.read(ws, "x.txt")
    with pytest.raises(WsError, match="только для чтения"):
        env.hub.write(ws, "x.txt", "y")
    assert (code_dir / "x.txt").read_text() == "code\n"


@pytest.mark.parametrize("which", ["home", "tmp"])
def test_root_with_data_and_code(env, code_dir, which):
    """Корень, который содержит данные (и, возможно, код), — отказ в любом режиме: побеждают данные."""
    target = Path.home() if which == "home" else env.tmp
    env.workspaces["wide"] = {"path": str(target), "mode": "rw"}
    env.write_registry()
    for mode in ("ro", "rw"):
        with pytest.raises(WsError, match="служебными данными wshub"):
            env.hub.workspace_open("wide", mode)


def test_symlink_to_protected(env, code_dir):
    links = env.tmp / "links"
    links.mkdir()
    (links / "code").symlink_to(code_dir)
    (links / "state").symlink_to(env.state)
    env.state.mkdir(exist_ok=True)
    env.workspaces["lcode"] = {"path": str(links / "code"), "mode": "rw"}
    env.workspaces["lstate"] = {"path": str(links / "state"), "mode": "ro"}
    env.write_registry()
    env.open("lcode")
    with pytest.raises(WsError, match="кодом wshub"):
        env.hub.workspace_open("lcode", "rw")
    with pytest.raises(WsError, match="служебными данными wshub"):
        env.hub.workspace_open("lstate")


def test_workspaces_list(env):
    out = env.hub.workspaces_list()
    assert "payload [rw] — тестовый rw" in out and "rt [ro] — тестовый ro" in out


def test_open_unknown_and_missing_dir(env):
    with pytest.raises(WsError, match="нет проекта"):
        env.hub.workspace_open("zzz")
    env.workspaces["gone"] = {"path": str(env.tmp / "gone"), "mode": "ro"}
    env.write_registry()
    with pytest.raises(WsError, match="не найдена"):
        env.hub.workspace_open("gone")


def test_open_summary_and_brief(env):
    out = env.hub.workspace_open("payload", "rw")
    assert out.startswith("ws: ")
    assert "действует до" in out and "8 ч" in out
    assert "**/secrets/**" in out and "512 КБ" in out
    assert "BRIEF (.agents/BRIEF.md): файла нет." in out
    assert "BRIEF: в реестре не задан." in env.hub.workspace_open("rt")

    (env.proj / ".agents").mkdir()
    (env.proj / ".agents" / "BRIEF.md").write_text("# Проект\nправила\n")
    out = env.hub.workspace_open("payload")
    assert "=== содержимое файла .agents/BRIEF.md ===\n# Проект\nправила" in out

    (env.proj / ".agents" / "BRIEF.md").write_text("x" * 20000)
    out = env.hub.workspace_open("payload")
    assert "BRIEF обрезан до 16 КБ" in out and "x" * 16384 in out and "x" * 16385 not in out


def test_brief_cannot_point_outside_or_to_secret(env):
    env.workspaces["payload"]["brief"] = "../outside/secret.txt"
    env.write_registry()
    out = env.hub.workspace_open("payload")
    assert "TOPSECRET" not in out and "недоступен" in out
    env.workspaces["payload"]["brief"] = ".env"
    env.write_registry()
    out = env.hub.workspace_open("payload")
    assert "supersecret" not in out and "deny" in out

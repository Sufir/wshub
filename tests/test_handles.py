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


@pytest.mark.parametrize("which", ["state_parent", "config_dir", "state_dir", "repo", "inside_state"])
def test_self_protection(env, which):
    target = {
        "state_parent": env.tmp,  # проект, внутри которого лежат state и реестр
        "config_dir": env.cfg.parent,
        "state_dir": env.state,
        "repo": Path(wshub.__file__).resolve().parents[2],  # /home/sufir/wshub — только попытка открыть
        "inside_state": env.state / "backup",
    }[which]
    target.mkdir(parents=True, exist_ok=True)
    env.workspaces["bad"] = {"path": str(target), "mode": "rw"}
    env.write_registry()
    with pytest.raises(WsError, match="служебным каталогом"):
        env.hub.workspace_open("bad", "ro")
    assert not env.hub.handles


def test_self_protection_uv_tool_dir(env, monkeypatch, tmp_path):
    tools = tmp_path / "uvtools"
    (tools / "wshub").mkdir(parents=True)
    monkeypatch.setenv("UV_TOOL_DIR", str(tools))
    hub = Hub(env.cfg, env.state)
    env.workspaces["bad"] = {"path": str(tools), "mode": "ro"}
    env.write_registry()
    with pytest.raises(WsError, match="служебным каталогом"):
        hub.workspace_open("bad")


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

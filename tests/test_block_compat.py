"""Запрет проекта из панели (blocked.json) и совместимость реестра: неизвестные разделы и ключи пропускаются."""
import asyncio
import json
import os
import sys
from importlib import resources
from pathlib import Path

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from test_panel_ops import make_panel

from wshub import doctor
from wshub.core import BLOCKED, REVOKED, Hub, WsError
from wshub.registry import RegistryError, parse


def other_process(env) -> Hub:
    """«Другой процесс» в том же state: свой Hub со своими хэндлами и кэшем."""
    h = Hub(env.cfg, env.state)
    h.runtime.enabled = True
    h.runtime.pid = os.getpid()
    return h


def journal(env) -> list[dict]:
    return [json.loads(line) for line in (env.state / "audit.jsonl").read_text().splitlines()]


# ---------- запрет ----------

def test_revoke_without_block_allows_reopen(env):
    a = other_process(env)
    a.workspace_open("payload")
    p = make_panel(env, proc=Path("/proc"))
    d = p.data()
    [s] = d["sessions"]
    p.revoke(d["nonce"], s["hid"])  # флажок по умолчанию снят
    assert p.data()["blocked"] == []
    assert a.workspace_open("payload").startswith("ws: ")


def test_block_survives_new_handle_and_other_processes(env):
    a = other_process(env)
    ws = a.workspace_open("payload").splitlines()[0].removeprefix("ws: ")
    b = Hub(env.cfg, env.state)  # процесс, где хэндл открыт до запрета
    ws_b = b.workspace_open("payload").splitlines()[0].removeprefix("ws: ")
    assert b.ls(ws_b, ".")
    p = make_panel(env, proc=Path("/proc"))
    d = p.data()
    [s] = [s for s in d["sessions"] if s["prefix"] == ws[:4]]
    res = p.revoke(d["nonce"], s["hid"], block=True)
    assert any("заблокирован" in c for c in res["changes"])

    with pytest.raises(WsError, match="отозван"):
        a.ls(ws, ".")
    for hub in (a, b, Hub(env.cfg, env.state)):  # новый хэндл не выдаётся ни в одном процессе
        with pytest.raises(WsError) as e:
            hub.workspace_open("payload", "rw")
        assert str(e.value) == BLOCKED
    with pytest.raises(WsError) as e:  # старый хэндл другого процесса тоже отказывает
        b.ls(ws_b, ".")
    assert str(e.value) == BLOCKED and b.handles == {}
    assert b.workspace_open("rt").startswith("ws: ")  # другие проекты не задеты
    assert "заблокирован" in b.workspaces_list().splitlines()[0]

    d = p.data()
    assert [x["name"] for x in d["blocked"]] == ["payload"] and d["blocked_error"] is None
    assert json.loads((env.state / "blocked.json").read_text())["projects"].keys() == {"payload"}
    recs = journal(env)
    assert [r["tool"] for r in recs if r["tool"] in ("panel_revoke", "panel_block")] == ["panel_revoke", "panel_block"]
    assert [r for r in recs if r["tool"] == "panel_block"][0]["ws"] == "payload"
    assert any(r["tool"] == "workspace_open" and r["status"] == "error" and r["error"] == BLOCKED for r in recs)


def test_unblock(env):
    a = other_process(env)
    a.workspace_open("payload")
    p = make_panel(env, proc=Path("/proc"))
    d = p.data()
    p.revoke(d["nonce"], d["sessions"][0]["hid"], block=True)
    with pytest.raises(WsError):
        a.workspace_open("payload")
    for bad in (None, "", "guess"):  # без одноразового кода — отказ, запрет на месте
        with pytest.raises(WsError, match="одноразовый код|недействителен"):
            p.unblock(bad, "payload")
    assert [x["name"] for x in p.data()["blocked"]] == ["payload"]
    d = p.data()
    with pytest.raises(WsError, match="уже использован"):
        p.unblock(d["nonce"], "payload") and p.unblock(d["nonce"], "payload")
    assert p.data()["blocked"] == []
    with pytest.raises(WsError, match="не заблокирован"):
        p.unblock(p.data()["nonce"], "payload")
    assert a.workspace_open("payload").startswith("ws: ")
    recs = [r for r in journal(env) if r["tool"] == "panel_unblock"]
    assert [r["status"] for r in recs] == ["error"] * 3 + ["ok", "error", "error"]
    assert recs[3]["ws"] == "payload" and "снят запрет" in recs[3]["changes"][0]


def test_block_between_processes(env):
    """Запрет из панели этого процесса действует на отдельный процесс сервера (stdio) и снимается."""
    params = StdioServerParameters(command=sys.executable, args=["-m", "wshub"],
                                   env={**os.environ, "WSHUB_CONFIG": str(env.cfg), "WSHUB_STATE": str(env.state)})
    p = make_panel(env, proc=Path("/proc"))

    async def go():
        async with stdio_client(params) as (r, w), ClientSession(r, w) as s:
            await s.initialize()
            res = await s.call_tool("workspace_open", {"name": "payload"})
            ws = res.content[0].text.splitlines()[0].removeprefix("ws: ")
            d = p.data()
            [h] = d["sessions"]
            p.revoke(d["nonce"], h["hid"], block=True)
            res = await s.call_tool("ls", {"ws": ws})
            assert res.isError and REVOKED in res.content[0].text
            res = await s.call_tool("workspace_open", {"name": "payload"})
            assert res.isError and BLOCKED in res.content[0].text
            p.unblock(p.data()["nonce"], "payload")
            res = await s.call_tool("workspace_open", {"name": "payload"})
            assert not res.isError
            ws2 = res.content[0].text.splitlines()[0].removeprefix("ws: ")
            assert not (await s.call_tool("ls", {"ws": ws2})).isError

    asyncio.run(go())


def test_broken_blocked_file_fails_closed(env):
    env.state.mkdir(exist_ok=True)
    (env.state / "blocked.json").write_text("{не json")
    with pytest.raises(WsError, match="повреждён"):
        env.hub.workspace_open("rt")
    assert "повреждён" in make_panel(env).data()["blocked_error"]
    with pytest.raises(OSError, match="повреждён"):
        env.hub.runtime.block("rt")
    (env.state / "blocked.json").unlink()
    assert env.hub.workspace_open("rt").startswith("ws: ")


def test_panel_html_revoke_checkbox():
    html = resources.files("wshub").joinpath("panel.html").read_text(encoding="utf-8")
    assert "Запретить открывать этот проект, пока я не сниму запрет" in html
    assert 'el("input", { type: "checkbox", id: "revoke-block" })' in html  # без checked: по умолчанию снят
    assert "block: block.checked" in html and '"panel_unblock"' in html and "Снять запрет" in html


# ---------- совместимость реестра ----------

FUTURE = """
[server]
log_level = "debug"

[defaults]
deny = [".env"]
color = "red"

[limits]
journal_max_mb = 3
future_limit = 7

[workspace.payload]
path = "{path}"
mode = "rw"
tags = ["x"]
"""


def test_unknown_section_and_key_are_skipped(env):
    env.cfg.write_text(FUTURE.format(path=env.proj), encoding="utf-8")
    reg = parse(env.cfg.read_text())
    assert reg.limits.journal_max_mb == 3 and reg.workspaces["payload"].deny == (".env",)
    assert reg.warnings == ("неизвестный ключ server — пропущен", "неизвестный ключ defaults.color — пропущен",
                            "неизвестный ключ limits.future_limit — пропущен",
                            "неизвестный ключ workspace.payload.tags — пропущен")
    hub = Hub(env.cfg, env.state)
    ws = hub.workspace_open("payload", "rw").splitlines()[0].removeprefix("ws: ")
    assert "hello" in hub.read(ws, "a.txt")


def test_unknown_keys_in_doctor_and_overview(env):
    env.cfg.write_text(FUTURE.format(path=env.proj), encoding="utf-8")
    p = make_panel(env)
    d = p.data()
    assert d["registry_error"] is None and [w["name"] for w in d["workspaces"]] == ["payload"]
    [reg] = [c for c in d["doctor"] if c["id"] == "registry"]  # вкладка «Обзор» рисует эти проверки
    assert reg["status"] == "warn"
    assert "неизвестный ключ server — пропущен" in reg["detail"]
    assert "неизвестный ключ workspace.payload.tags — пропущен" in reg["detail"]
    text = doctor.format_report(doctor.run(p.doctor_ctx))
    assert "[ВНИМ] Реестр" in text and "неизвестный ключ defaults.color — пропущен" in text
    # правка из панели не теряет неизвестные ключи
    p.save_workspace(d["nonce"], name="payload", path=str(env.proj), mode="ro", description="", brief="",
                     deny=[], rev=d["rev"], create=False)
    after = env.cfg.read_text()
    assert "tags" in after and "[server]" in after and "future_limit" in after


@pytest.mark.parametrize("bad", [
    "[[[",
    "[defaults]\nmax_read_kb = 0",
    "[defaults]\nttl_hours = \"8\"",
    "[limits]\njournal_max_mb = 0",
    "[workspace.x]\npath = \"relative\"",
    "[workspace.x]\npath = \"/tmp\"\nmode = \"rwx\"",
    "defaults = 5",
    "workspace = 5",
])
def test_bad_values_of_known_keys_still_fail(bad):
    with pytest.raises(RegistryError):
        parse(bad)

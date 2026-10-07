"""Каркас MCP Apps: инструменты панели, их _meta и ресурс ui://wshub/panel."""
import asyncio
import json

from mcp.shared.memory import create_connected_server_and_client_session

from wshub.server import APP_MIME, PANEL_URI, build_server


def run(env, fn):
    async def go():
        async with create_connected_server_and_client_session(build_server(env.hub)._mcp_server) as s:
            return await fn(s)
    return asyncio.run(go())


def test_tools_and_meta(env):
    async def fn(s):
        return {t.name: t for t in (await s.list_tools()).tools}
    tools = run(env, fn)
    panel, data = tools["panel"], tools["panel_data"]
    assert panel.meta["ui"]["resourceUri"] == PANEL_URI
    assert panel.meta["ui/resourceUri"] == PANEL_URI
    assert "visibility" not in panel.meta["ui"]  # по умолчанию ["model", "app"] — модель видит
    assert data.meta == {"ui": {"visibility": ["app"]}}


def test_resource(env):
    async def fn(s):
        listed = {str(r.uri): r for r in (await s.list_resources()).resources}
        return listed, await s.read_resource(PANEL_URI)
    listed, res = run(env, fn)
    assert listed[PANEL_URI].mimeType == APP_MIME
    c = res.contents[0]
    assert c.mimeType == APP_MIME
    assert c.text.startswith("<!doctype html>") and "Обновить" in c.text
    # без внешних загрузок: ни src=, ни href= на http(s), ни import
    for bad in ('src="http', "src='http", 'href="http', "import ", "@import"):
        assert bad not in c.text


def test_panel_summary_and_data(env):
    env.open("payload")

    async def fn(s):
        return await s.call_tool("panel", {}), await s.call_tool("panel_data", {})
    summary, data = run(env, fn)
    text = summary.content[0].text
    assert "Проектов: 2" in text and "payload [rw]" in text and "Открытых хэндлов: 1" in text
    d = json.loads(data.content[0].text)
    assert d["open_handles"] == 1
    assert {(w["name"], w["mode"], w["path"]) for w in d["workspaces"]} == {
        ("payload", "rw", str(env.proj)), ("rt", "ro", str(env.proj))}


def test_panel_data_readonly(env):
    before = env.cfg.read_bytes()
    env.hub.panel_data()
    assert env.cfg.read_bytes() == before and env.hub.handles == {}

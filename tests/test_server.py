"""Сквозной тест: сервер запускается как процесс и отвечает по MCP через stdio."""
import asyncio
import os
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

TOOLS = {"workspaces_list", "workspace_open", "ls", "tree", "find", "grep", "read", "extract", "write", "edit",
         "panel", "panel_data", "panel_browse", "panel_brief_check", "panel_mask_preview", "panel_audit",
         "panel_backups", "panel_backup_diff", "panel_save_workspace", "panel_delete_workspace", "panel_save_limits",
         "panel_revoke", "panel_unblock", "panel_restore", "publish", "panel_save_outbox", "panel_outbox_clean"}


def test_stdio_roundtrip(env):
    params = StdioServerParameters(
        command=sys.executable, args=["-m", "wshub"],
        env={**os.environ, "WSHUB_CONFIG": str(env.cfg), "WSHUB_STATE": str(env.state)})

    async def run():
        async with stdio_client(params) as (r, w), ClientSession(r, w) as s:
            init = await s.initialize()
            assert "workspace_open" in (init.instructions or "")
            tools = {t.name for t in (await s.list_tools()).tools}
            assert tools == TOOLS
            res = await s.call_tool("workspaces_list", {})
            assert "payload [rw]" in res.content[0].text
            res = await s.call_tool("workspace_open", {"name": "payload"})
            ws = res.content[0].text.splitlines()[0].removeprefix("ws: ")
            res = await s.call_tool("read", {"ws": ws, "path": "a.txt"})
            assert not res.isError and "1\thello" in res.content[0].text
            res = await s.call_tool("read", {"ws": "stale", "path": "a.txt"})
            assert res.isError and "вызови workspace_open заново" in res.content[0].text
            res = await s.call_tool("write", {"ws": ws, "path": "a.txt", "content": "x"})
            assert res.isError and "только для чтения" in res.content[0].text

    asyncio.run(run())
    assert (env.proj / "a.txt").read_text() == "hello\nworld\n"

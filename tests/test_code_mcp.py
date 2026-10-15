"""MCP-сервер wshub-code: ровно инструменты из 2.7, лимит ответа, потолок code_wait (ТЗ WSH-2, п. 5.13)."""
from __future__ import annotations

import asyncio
import time

import pytest

from wshub import code_server
from wshub.core import Hub


class _Code:
    def __init__(self, tmp_path):
        self.cfg = tmp_path / "cfg" / "workspaces.toml"
        self.cfg.parent.mkdir(parents=True)
        self.state = tmp_path / "state"
        self.root = tmp_path / "proj"
        (self.root / "prompts").mkdir(parents=True)
        self.write()

    def write(self, max_wait_s=40):
        self.cfg.write_text(
            "[defaults]\ndeny = []\nmax_read_kb = 512\nttl_hours = 8\n\n"
            f"[code]\nmax_wait_s = {max_wait_s}\n\n"
            f'[workspace.proj]\npath = "{self.root}"\nmode = "rw"\n\n'
            '[workspace.proj.code]\nenabled = true\ndirs = ["."]\nprompts = "prompts"\n', encoding="utf-8")
        self.hub = Hub(self.cfg, self.state)


@pytest.fixture
def code(tmp_path):
    return _Code(tmp_path)


def _tool_names(mcp) -> set[str]:
    return {t.name for t in asyncio.run(mcp.list_tools())}


def test_registers_exactly_27_tools(code):
    mcp = code_server.build_code_server(code.hub)
    assert _tool_names(mcp) == {"code_projects", "code_start", "code_status", "code_wait", "code_result",
                                "code_cancel", "code_resume", "code_jobs",
                                "code_plan", "code_approve", "code_replan", "code_stats",  # WSH-3
                                "code_card_data", "code_card_cancel", "code_card_ack"}  # WSH-3.4: карточка


def test_answer_trimmed_to_20kb_bytes():
    # предел в БАЙТАХ (замечание 17): и для ASCII, и для кириллицы результат ≤ ANSWER_MAX байт, JSON не рвётся
    for s in ("x" * 30000, "я" * 30000):
        out = code_server._trim(s)
        assert len(out.encode("utf-8")) <= code_server.ANSWER_MAX
        assert out.endswith("…")
    assert code_server._trim("коротко") == "коротко"


def test_code_projects_lists_enabled(code):
    mcp = code_server.build_code_server(code.hub)
    tools = {t.name: t for t in asyncio.run(mcp.list_tools())}
    out = asyncio.run(mcp.call_tool("code_projects", {}))
    content = out[0] if isinstance(out, tuple) else out
    text = content[0].text
    assert "proj" in text and "code_projects" in tools


def test_code_wait_capped(code, monkeypatch):
    code.write(max_wait_s="1")
    monkeypatch.setattr("wshub.jobs.Jobs.snapshot", lambda self, jid, now=None: {"id": jid, "state": "running"})
    mcp = code_server.build_code_server(code.hub)
    start = time.time()
    asyncio.run(mcp.call_tool("code_wait", {"job": "x", "seconds": 30}))
    assert time.time() - start < 3  # не дольше max_wait_s = 1 (плюс накладные)

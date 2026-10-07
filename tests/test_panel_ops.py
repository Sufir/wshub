"""Панель: коды nonce/key, видимость инструментов, правка реестра, отзыв хэндлов, копии, обход каталогов."""
import asyncio
import json
import os
import sys
from pathlib import Path

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.shared.memory import create_connected_server_and_client_session

from wshub.core import REVOKED, Hub, WsError
from wshub.doctor import Ctx
from wshub.panel import Panel
from wshub.registry_edit import revision
from wshub.server import APP_ONLY_META, MUTATING, build_server

BASE_TOOLS = {"workspaces_list", "workspace_open", "ls", "tree", "find", "grep", "read", "extract", "write", "edit",
              "panel"}


def make_panel(env, hub=None, proc=None) -> Panel:
    hub = hub or env.hub
    fake_proc = env.tmp / "proc"
    fake_proc.mkdir(exist_ok=True)
    ctx = Ctx(config=env.cfg, state=env.state, home=env.tmp / "home", win_users=env.tmp / "nowin",
              proc=proc or fake_proc, protected_overlap=hub.protected_overlap)
    return Panel(hub, roots=[env.tmp], doctor_ctx=ctx)


@pytest.fixture
def panel(env):
    return make_panel(env)


def save(panel, nonce=None, **kw):
    d = panel.data()
    args = dict(name="newp", path=str(panel.hub.registry.path.parent.parent / "outside"), mode="ro",
                description="", brief="", deny=[], rev=d["rev"], create=True)
    args.update(kw)
    return panel.save_workspace(d["nonce"] if nonce is None else nonce, **args)


# ---------- коды ----------

def test_mutation_requires_nonce(env, panel):
    before = env.cfg.read_bytes()
    rev = revision(before)
    for bad in (None, "", "guess"):
        with pytest.raises(WsError, match="одноразовый код|недействителен"):
            panel.save_workspace(bad, name="x", path=str(env.outside), mode="ro", description="", brief="",
                                 deny=[], rev=rev, create=True)
    with pytest.raises(WsError, match="одноразовый код|недействителен"):
        panel.delete_workspace("", name="rt", rev=rev)
    with pytest.raises(WsError, match="одноразовый код|недействителен"):
        panel.revoke(None, "0" * 16)
    with pytest.raises(WsError, match="одноразовый код|недействителен"):
        panel.restore("x", "payload", "x/a.txt")
    assert env.cfg.read_bytes() == before
    recs = [json.loads(line) for line in (env.state / "audit.jsonl").read_text().splitlines()]
    assert {r["tool"] for r in recs} == {"panel_save", "panel_delete", "panel_revoke", "panel_restore"}
    assert all(r["status"] == "error" for r in recs)


def test_nonce_expired_and_single_use(env, panel):
    t = [1_000_000.0]
    env.hub.clock = lambda: t[0]
    d = panel.data()
    t[0] += 601
    with pytest.raises(WsError, match="истёк"):
        save(panel, nonce=d["nonce"], rev=d["rev"])
    d = panel.data()
    save(panel, nonce=d["nonce"], rev=d["rev"])
    with pytest.raises(WsError, match="недействителен или уже использован"):
        panel.delete_workspace(d["nonce"], name="newp", rev=panel.data()["rev"])


def test_read_tools_require_key(env, panel):
    for call in (lambda k: panel.browse(k, str(env.tmp)), lambda k: panel.audit(k),
                 lambda k: panel.backups(k), lambda k: panel.mask_preview(k, str(env.proj), ["*.txt"])):
        with pytest.raises(WsError, match="ключ панели"):
            call("nope")
    t = [1_000_000.0]
    env.hub.clock = lambda: t[0]
    key = panel.data()["key"]
    t[0] += 500
    panel.audit(key)  # использование продлевает срок
    t[0] += 500
    panel.audit(key)
    t[0] += 601
    with pytest.raises(WsError, match="истёк"):
        panel.audit(key)


def test_data_has_no_tokens(env, panel):
    env.hub.runtime.enabled = True
    out = env.hub.workspace_open("payload")
    token = out.splitlines()[0].removeprefix("ws: ")
    d = panel.data()
    assert token not in json.dumps(d)
    run = (env.state / "run" / f"{os.getpid()}.json").read_text()
    assert token not in run and json.loads(run)["handles"][0]["prefix"] == token[:4]


# ---------- видимость ----------

def test_visibility_of_all_tools(env):
    async def go():
        async with create_connected_server_and_client_session(build_server(env.hub)._mcp_server) as s:
            return {t.name: t for t in (await s.list_tools()).tools}
    tools = asyncio.run(go())
    app_only = {n for n, t in tools.items() if (t.meta or {}).get("ui", {}).get("visibility") == ["app"]}
    assert set(tools) - app_only == BASE_TOOLS  # модель видит только их
    assert MUTATING <= app_only
    assert all(n.startswith("panel_") for n in app_only)
    for n in MUTATING:
        assert tools[n].meta == APP_ONLY_META
        assert "nonce" in tools[n].inputSchema["required"]
    for n in app_only - MUTATING - {"panel_data"}:
        assert "key" in tools[n].inputSchema["required"]


# ---------- реестр ----------

COMMENTED = '''# Шапка реестра — должна остаться
[defaults]
deny = [".env"]  # общий deny

[workspace.payload]
path = "{proj}"
mode = "rw"
deny = [
  # раскомментировать, если нужно:
  # "users*.csv",
]

# соседний проект
[workspace.rt]
path = "{proj}"
mode = "ro"
'''


def test_save_keeps_comments_and_history(env, panel):
    env.cfg.write_text(COMMENTED.format(proj=env.proj), encoding="utf-8")
    d = panel.data()
    ch = panel.save_workspace(d["nonce"], name="payload", path=str(env.proj), mode="rw", description="Описание",
                              brief="", deny=["*.log"], rev=d["rev"], create=False)["changes"]
    assert any("описание" in c for c in ch) and any("deny +*.log" in c for c in ch)
    text = env.cfg.read_text()
    for keep in ("# Шапка реестра — должна остаться", "# общий deny", "# раскомментировать, если нужно:",
                 '# "users*.csv",', "# соседний проект"):
        assert keep in text
    assert text.index('# "users*.csv"') < text.index('"*.log"') < text.index("[workspace.rt]")
    hist = list((env.state / "registry-history").glob("*.toml"))
    assert len(hist) == 1 and hist[0].read_text() == COMMENTED.format(proj=env.proj)

    d = panel.data()
    panel.save_workspace(d["nonce"], name="payload", path=str(env.proj), mode="rw", description="Описание",
                         brief="", deny=[], rev=d["rev"], create=False)
    text = env.cfg.read_text()
    assert '# "users*.csv",' in text and "*.log" not in text  # массив с комментариями остался

    save(panel, name="new-one", path=str(env.outside), mode="ro", deny=["a*"])
    d = panel.data()
    assert [w["name"] for w in d["workspaces"]] == ["payload", "rt", "new-one"]
    panel.delete_workspace(d["nonce"], name="rt", rev=d["rev"])
    text = env.cfg.read_text()
    assert "[workspace.rt]" not in text and "# Шапка реестра" in text
    assert len(list((env.state / "registry-history").glob("*.toml"))) == 4


@pytest.mark.parametrize("kw, err", [
    ({"name": "bad_name"}, "латиница"),
    ({"name": "Кириллица"}, "латиница"),
    ({"name": "-x"}, "латиница"),
    ({"name": "PAYLOAD"}, "уже есть"),
    ({"name": ""}, "имя не задано"),
    ({"mode": ""}, "режим не выбран"),
    ({"path": ""}, "путь не задан"),
    ({"path": "relative/dir"}, "абсолютным"),
    ({"path": "/nonexistent/wshub-test"}, "нет — проверь"),
    ({"path": "FILE"}, "не каталог"),
    ({"path": "STATE"}, "служебным каталогом"),
    ({"path": "CFGDIR"}, "служебным каталогом"),
    ({"deny": ["ok*", ""]}, "пустая маска"),
    ({"deny": ["a\\b"]}, "косая черта"),
    ({"deny": ["[abc"]}, "незакрытая"),
    ({"deny": ["../x"]}, "«..»"),
    ({"deny": ["x*", "X*"]}, "повторяется"),
    ({"brief": "../x.md"}, "BRIEF"),
    ({"description": "a\nb"}, "одна строка"),
])
def test_save_validation(env, panel, kw, err):
    if kw.get("path") == "FILE":
        kw["path"] = str(env.proj / "a.txt")
    elif kw.get("path") == "STATE":
        env.state.mkdir(exist_ok=True)
        kw["path"] = str(env.state)
    elif kw.get("path") == "CFGDIR":
        kw["path"] = str(env.cfg.parent)
    before = env.cfg.read_bytes()
    with pytest.raises(WsError, match=err):
        save(panel, **kw)
    assert env.cfg.read_bytes() == before
    assert not (env.state / "registry-history").exists()


def test_save_rejects_stale_revision(env, panel):
    d = panel.data()
    env.workspaces["rt"]["description"] = "правка вручную"
    env.write_registry()
    with pytest.raises(WsError, match="изменился после загрузки"):
        panel.save_workspace(d["nonce"], name="x", path=str(env.outside), mode="ro", description="", brief="",
                             deny=[], rev=d["rev"], create=True)
    assert "правка вручную" in env.cfg.read_text()


def test_other_process_picks_up_changes(env, panel):
    """Правка из панели одного процесса видна другому без перезапуска (mtime/inode реестра)."""
    other = Hub(env.cfg, env.state)
    ws = other.workspace_open("rt").splitlines()[0].removeprefix("ws: ")
    assert "newp" not in other.workspaces_list()
    save(panel, name="newp", path=str(env.outside), mode="ro")
    assert "newp [ro]" in other.workspaces_list()
    d = panel.data()
    panel.save_workspace(d["nonce"], name="rt", path=str(env.outside), mode="ro", description="", brief="",
                         deny=[], rev=d["rev"], create=False)
    with pytest.raises(WsError, match="workspace_open заново"):  # путь сменился — хэндл больше не действует
        other.ls(ws, ".")
    d = panel.data()
    panel.delete_workspace(d["nonce"], name="newp", rev=d["rev"])
    assert "newp" not in other.workspaces_list()


def test_first_project_without_registry(env, panel):
    env.cfg.unlink()
    d = panel.data()
    assert d["registry_error"] is None and d["workspaces"] == []
    panel.save_workspace(d["nonce"], name="first", path=str(env.outside), mode="rw", description="", brief="",
                         deny=[], rev=d["rev"], create=True)
    assert "first [rw]" in env.hub.workspaces_list()


# ---------- отзыв ----------

def test_revoke_in_process(env, panel):
    a = Hub(env.cfg, env.state)  # «другой процесс» в том же state
    a.runtime.enabled = True
    a.runtime.pid = os.getpid()
    ws = a.workspace_open("payload").splitlines()[0].removeprefix("ws: ")
    p = make_panel(env, proc=Path("/proc"))
    d = p.data()
    [s] = [s for s in d["sessions"] if s["name"] == "payload"]
    assert s["prefix"] == ws[:4] and not s["revoked"]
    p.revoke(d["nonce"], s["hid"])
    assert [s["revoked"] for s in p.data()["sessions"]] == [True]
    with pytest.raises(WsError, match="отозван"):
        a.ls(ws, ".")
    assert a.handles == {}
    d = p.data()
    assert d["sessions"] == []  # процесс обработал отзыв и обновил run-файл
    with pytest.raises(WsError, match="не найден"):
        p.revoke(d["nonce"], s["hid"])


def test_revoke_between_processes(env):
    """Хэндл открыт в отдельном процессе сервера (stdio), отзыв — из панели в этом процессе."""
    params = StdioServerParameters(command=sys.executable, args=["-m", "wshub"],
                                   env={**os.environ, "WSHUB_CONFIG": str(env.cfg), "WSHUB_STATE": str(env.state)})
    p = make_panel(env, proc=Path("/proc"))

    async def go():
        async with stdio_client(params) as (r, w), ClientSession(r, w) as s:
            await s.initialize()
            res = await s.call_tool("workspace_open", {"name": "payload"})
            ws = res.content[0].text.splitlines()[0].removeprefix("ws: ")
            assert not (await s.call_tool("ls", {"ws": ws})).isError
            d = p.data()
            procs = d["processes"]["registered"]
            assert len(procs) == 1 and procs[0]["pid"] != os.getpid() and procs[0]["handles"] == 1
            assert procs[0]["last_call"]["tool"] == "ls"
            [h] = d["sessions"]
            assert h["prefix"] == ws[:4] and h["pid"] == procs[0]["pid"]
            p.revoke(d["nonce"], h["hid"])
            res = await s.call_tool("ls", {"ws": ws})
            assert res.isError and REVOKED in res.content[0].text
            res = await s.call_tool("workspace_open", {"name": "payload"})  # новый хэндл не задет
            ws2 = res.content[0].text.splitlines()[0].removeprefix("ws: ")
            assert not (await s.call_tool("ls", {"ws": ws2})).isError
            return procs[0]["pid"]

    asyncio.run(go())
    recs = [json.loads(line) for line in (env.state / "audit.jsonl").read_text().splitlines()]
    assert any(r["tool"] == "panel_revoke" and r["status"] == "ok" for r in recs)


# ---------- копии ----------

def test_backup_list_diff_restore(env, panel):
    t = [1_700_000_000.0]
    env.hub.clock = lambda: t[0]
    ws = env.open("payload", "rw")
    env.hub.write(ws, "a.txt", "v2\n")          # копия v1 = hello\nworld
    t[0] += 1
    env.hub.write(ws, "a.txt", "v3\n")          # копия v2
    key = panel.data()["key"]
    b = panel.backups(key, "payload")
    assert "payload" in b["projects"] and b["in_registry"]
    assert [i["rel"] for i in b["items"]] == ["a.txt", "a.txt"]
    oldest = b["items"][-1]
    diff = panel.diff(key, "payload", oldest["id"])
    assert not diff["same"] and "-hello" in diff["diff"] and "+v3" in diff["diff"]

    t[0] += 1
    d = panel.data()
    res = panel.restore(d["nonce"], "payload", oldest["id"])
    assert "восстановлен" in res["changes"][0]
    assert (env.proj / "a.txt").read_text() == "hello\nworld\n"
    b = panel.backups(d["key"], "payload")
    assert len(b["items"]) == 3
    newest = panel.diff(d["key"], "payload", b["items"][0]["id"])  # копия текущей версии перед восстановлением
    assert "-v3" in newest["diff"]
    rec = json.loads((env.state / "audit.jsonl").read_text().splitlines()[-1])
    assert rec["tool"] == "panel_restore" and rec["status"] == "ok" and rec["path"] == "a.txt"
    assert rec["backup"] and rec["restored_from"].endswith(oldest["id"])
    d = panel.data()
    with pytest.raises(WsError, match="совпадает"):
        panel.restore(d["nonce"], "payload", oldest["id"])


def test_backup_under_deny_not_opened(env, panel):
    stamp = env.state / "backup" / "payload" / "20260101-000000"
    (stamp / "secrets").mkdir(parents=True)
    (stamp / ".env").write_text("TOKEN=old\n")
    (stamp / "secrets" / "k.txt").write_text("old\n")
    key = panel.data()["key"]
    items = {i["rel"]: i for i in panel.backups(key, "payload")["items"]}
    assert items[".env"]["denied"] and items["secrets/k.txt"]["denied"]
    for i in items.values():
        with pytest.raises(WsError, match="deny"):
            panel.diff(key, "payload", i["id"])
        d = panel.data()
        with pytest.raises(WsError, match="deny"):
            panel.restore(d["nonce"], "payload", i["id"])
    assert (env.proj / ".env").read_text() == "TOKEN=supersecret\n"


def test_backup_path_escape(env, panel):
    key = panel.data()["key"]
    (env.state / "backup" / "payload").mkdir(parents=True)
    for bad in ("../../cfg/workspaces.toml", "/etc/passwd", "", "../rt/x"):
        with pytest.raises(WsError):
            panel.diff(key, "payload", bad)
    with pytest.raises(WsError, match="проект"):
        panel.backups(key, "../x")


# ---------- обход каталогов ----------

def test_browse_only_inside_roots(env, tmp_path):
    root = tmp_path / "root"
    (root / "a" / "b").mkdir(parents=True)
    (root / "file.txt").write_text("x")
    (tmp_path / "root2").mkdir()  # тот же префикс, но не внутри
    (root / "out").symlink_to(env.outside)
    p = make_panel(env)
    p.roots = [root, tmp_path / "missing"]
    key = p.data()["key"]
    top = p.browse(key, "")
    assert [d["path"] for d in top["dirs"]] == [str(root)]
    r = p.browse(key, str(root))
    assert [d["name"] for d in r["dirs"]] == ["a", "out"] and r["parent"] is None
    r = p.browse(key, str(root / "a"))
    assert r["parent"] == str(root) and [d["name"] for d in r["dirs"]] == ["b"]
    for bad in (str(tmp_path), str(tmp_path / "root2"), str(root / ".." / "root2"), str(root / "out"), "/",
                "relative", str(root / "a" / ".." / ".." )):
        with pytest.raises(WsError, match="вне разрешённых корней|абсолютный"):
            p.browse(key, bad)
    with pytest.raises(WsError, match="не каталог"):
        p.browse(key, str(root / "file.txt"))
    with pytest.raises(WsError, match="вне разрешённых"):
        p.mask_preview(key, str(env.outside), ["*"])


def test_mask_preview(env, panel):
    (env.proj / "logs").mkdir()
    for i in range(60):
        (env.proj / "logs" / f"x{i}.log").write_text("")
    (env.proj / "secrets").mkdir()
    (env.proj / "secrets" / "k.txt").write_text("")
    key = panel.data()["key"]
    r = panel.mask_preview(key, str(env.proj), ["*.log", "**/secrets/**", "secrets", "nomatch*", "[bad"])
    m = {x["mask"]: x for x in r["masks"]}
    assert m["*.log"]["count"] == 60 and len(m["*.log"]["sample"]) == 50
    assert m["**/secrets/**"]["sample"] == ["secrets/"]  # каталог целиком — одна строка
    assert m["secrets"]["count"] == 1
    assert m["nomatch*"]["count"] == 0
    assert m["[bad"]["error"] and "незакрытая" in m["[bad"]["error"]


def test_brief_check(env, panel):
    key = panel.data()["key"]
    assert panel.brief_check(key, str(env.proj), ".agents/BRIEF.md")["status"] == "missing"
    (env.proj / ".agents").mkdir()
    (env.proj / ".agents" / "BRIEF.md").write_text("x")
    assert panel.brief_check(key, str(env.proj), ".agents/BRIEF.md")["status"] == "ok"
    assert panel.brief_check(key, str(env.proj), "../outside/secret.txt")["status"] == "bad"
    (env.proj / "link.md").symlink_to(env.outside / "secret.txt")
    assert panel.brief_check(key, str(env.proj), "link.md")["status"] == "bad"


def test_audit_tail(env, panel):
    env.state.mkdir(exist_ok=True)
    with (env.state / "audit.jsonl").open("w") as f:
        for i in range(700):
            f.write(json.dumps({"tool": "read", "n": i, "pad": "x" * 200}) + "\n")
        f.write("not json\n")
    r = panel.audit(panel.data()["key"])
    assert len(r["records"]) == 499 and r["records"][0]["n"] == 699 and r["records"][-1]["n"] == 201

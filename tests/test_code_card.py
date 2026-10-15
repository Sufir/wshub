"""wshub-code, ТЗ WSH-3.4, часть В: карточка задачи ui://wshub-code/job, скрытые инструменты code_card_*,
режим notify, тихий code_wait, названия инструментов. claude не вызывается — tests/fake_claude.py."""
from __future__ import annotations

import asyncio
import json
import re
import time

import pytest
from test_code import make_code, registry_text, wait_terminal
from test_code_plan import _wait_state

from wshub import code_server
from wshub.registry import parse

CARD_TOOLS = ("code_card_data", "code_card_cancel", "code_card_ack")


@pytest.fixture
def code(tmp_path, monkeypatch):
    return make_code(tmp_path, monkeypatch)


class _Srv:
    """Один сервер на тест: ключи карточки живут в процессе сервера."""

    def __init__(self, code):
        self.mcp = code_server.build_code_server(code.hub)

    def raw(self, name, args):
        out = asyncio.run(self.mcp.call_tool(name, args))
        return out[0] if isinstance(out, tuple) else out

    def text(self, name, args) -> str:
        return self.raw(name, args)[0].text

    def obj(self, name, args) -> dict:
        return json.loads(self.text(name, args))


def _notify(code, mode):
    code.cfg.write_text(registry_text(code.root, claude=f'"{code.root.parent / "fake_claude.py"}"')
                        .replace("[code]\n", f'[code]\nnotify = "{mode}"\n'), encoding="utf-8")
    from wshub.core import Hub
    from wshub.jobs import Jobs
    code.hub = Hub(code.cfg, code.state)
    code.jobs = Jobs(code.hub)


# ---------- реестр, ресурс, привязка ----------

@pytest.mark.parametrize("line,msg", [('notify = "push"', "code.notify"), ("max_wait_s = 56", "code.max_wait_s")])
def test_registry_notify_and_wait_cap(code, line, msg):
    reg = parse(registry_text(code.root).replace("[code]\n", f"[code]\n{line}\n"))
    assert reg.code.error and msg in reg.code.error


def test_registry_defaults(code):
    g = parse(registry_text(code.root)).code.glob
    assert (g.notify, g.max_wait_s) == ("poll", 50)
    assert parse(registry_text(code.root).replace("[code]\n", "[code]\nmax_wait_s = 55\n")).code.glob.max_wait_s == 55


def test_resource_and_bindings(code):
    mcp = code_server.build_code_server(code.hub)
    res = {str(r.uri): r for r in asyncio.run(mcp.list_resources())}
    r = res["ui://wshub-code/job"]
    assert r.mimeType == "text/html;profile=mcp-app"
    html = asyncio.run(mcp.read_resource("ui://wshub-code/job"))
    body = list(html)[0].content
    assert "<script" in body and not re.search(r"""(src|href)\s*=\s*["']?https?://""", body)
    assert "innerHTML" not in body and "insertAdjacentHTML" not in body and "outerHTML" not in body
    tools = {t.name: t for t in asyncio.run(mcp.list_tools())}
    with_ui = {n for n, t in tools.items() if (t.meta or {}).get("ui", {}).get("resourceUri")}
    assert with_ui == {"code_start", "code_resume"}
    for n in with_ui:
        assert tools[n].meta["ui/resourceUri"] == "ui://wshub-code/job"
    for n in CARD_TOOLS:
        assert tools[n].meta == {"ui": {"visibility": ["app"]}}


def test_titles(code):
    tools = {t.name: t for t in asyncio.run(code_server.build_code_server(code.hub).list_tools())}
    assert tools["code_start"].title == "Code: запуск" and tools["code_wait"].title == "Code: ожидание"
    assert tools["code_approve"].title == "Code: утвердить план" and tools["code_stats"].title == "Code: статистика"
    assert all(tools[n].title for n in code_server.TITLES)


# ---------- next и INSTRUCTIONS ----------

def test_next_by_mode(code):
    srv = _Srv(code)
    out = srv.obj("code_start", {"project": "proj", "task": code.taskfile()})
    assert out["next"] == "Следи: code_wait(job)."
    wait_terminal(code.jobs, out["job"])
    assert code.jobs.read(out["job"])["notify"] == "poll"
    _notify(code, "card")
    srv = _Srv(code)
    res = srv.obj("code_resume", {"job": out["job"], "prompt": "cont", "note": "x"})
    assert res["next"] == "Следи: code_wait(job)."  # режим — у корня, на момент его старта
    wait_terminal(code.jobs, res["job"])
    out = srv.obj("code_start", {"project": "proj", "task": code.taskfile()})
    assert out["next"] == ("Заверши ход. Карточка задачи напишет в чат, когда понадобится решение по плану или "
                           "будет итог.")
    wait_terminal(code.jobs, out["job"])
    res = srv.obj("code_resume", {"job": out["job"], "prompt": "cont", "note": "x"})
    assert res["next"].startswith("Заверши ход.")
    wait_terminal(code.jobs, res["job"])
    assert "notify" in code_server.INSTRUCTIONS and "card" in code_server.INSTRUCTIONS


def test_notify_inherited_from_root(code):
    _notify(code, "card")
    root = code.jobs.start(code.jobs.plan("proj", code.taskfile(size="L")))["id"]
    _wait_state(code.jobs, root, ("plan_ready",))
    _notify(code, "poll")
    child = code.jobs.approve(root)
    assert child["notify"] == "card"
    _wait_state(code.jobs, root, ("completed", "failed"))


# ---------- code_card_data ----------

def test_card_data_offset_args_marks_end(code, monkeypatch):
    monkeypatch.setenv("FAKE_DOCKER", "1")
    monkeypatch.setenv("FAKE_GIT_META", "1")
    srv = _Srv(code)
    jid = srv.obj("code_start", {"project": "proj", "task": code.taskfile()})["job"]
    wait_terminal(code.jobs, jid)
    d = srv.obj("code_card_data", {"job": jid})
    assert d["key"] and d["state"] == "completed" and d["project"] == "proj" and d["task"] == "TASK.md"
    tools = [(a["tool"], a["arg"], a["mark"]) for a in d["actions"]]
    assert ("Read", "x", None) in tools
    assert ("Bash", "cd app && docker compose run --rm app pytest -v tests/x.py::t", "отказ") in tools
    assert ("Bash", "docker compose run --rm app false", "ошибка") in tools
    assert d["offset"] == (code.jobs.job_dir(jid) / "events.jsonl").stat().st_size
    again = srv.obj("code_card_data", {"job": jid, "offset": d["offset"]})
    assert again["actions"] == [] and again["offset"] == d["offset"]  # offset учитывается
    assert (d["turns"], d["actions_count"]) == (srv.obj("code_status", {"job": jid})["turns"],
                                                 srv.obj("code_status", {"job": jid})["actions"])
    end = d["end"]
    assert end["warning"].startswith("ВНИМАНИЕ") and end["outside"]["docker_count"] == 3
    assert end["result_head"] == "готово" and end["external"]["push_count"] == 0
    log = srv.obj("code_card_data", {"job": jid, "offset": d["offset"], "log": True})
    assert len(log["log"]) == len(d["actions"]) and log["actions"] == []


def test_card_data_chain_offset_spans_links(code):
    srv = _Srv(code)
    root = srv.obj("code_start", {"project": "proj", "task": code.taskfile(size="L")})["job"]
    _wait_state(code.jobs, root, ("plan_ready",))
    d1 = srv.obj("code_card_data", {"job": root})
    assert d1["state"] == "plan_ready" and d1["plan_mode"] == "review"
    assert [p["label"] for p in d1["phases"]] == ["план", "исполнение"] and d1["phases"][0]["active"]
    code.jobs.approve(root)
    _wait_state(code.jobs, root, ("completed", "failed"))
    d2 = srv.obj("code_card_data", {"job": root, "offset": d1["offset"]})
    assert d2["state"] == "completed" and d2["actions"] and d2["offset"] > d1["offset"]
    assert all(a["tool"] != "Write" for a in d2["actions"])  # Write плана — в первом звене, второй раз не пришёл
    assert d2["phases"][0]["done"] and d2["phases"][1]["done"] and not d2["phases"][1]["active"]


def test_card_data_text_is_data(code, monkeypatch, tmp_path):
    """Аргумент с HTML приходит строкой как есть: карточка выводит его textContent (проверка ресурса выше)."""
    ev = tmp_path / "e.jsonl"
    srv = _Srv(code)
    jid = srv.obj("code_start", {"project": "proj", "task": code.taskfile()})["job"]
    wait_terminal(code.jobs, jid)
    p = code.jobs.job_dir(jid) / "events.jsonl"
    with p.open("a") as fh:
        fh.write(json.dumps({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": "h1", "name": "Bash", "input": {"command": "<img src=x onerror=alert(1)>"}}]}})
                 + "\n")
    d = srv.obj("code_card_data", {"job": jid})
    assert d["actions"][-1]["arg"] == "<img src=x onerror=alert(1)>"
    del ev


def test_card_data_speed_500_jobs(code):
    """В2.1: один вызов — не дольше 50 мс при 500 задачах в каталоге (с запасом на медленный CI — 0,5 с)."""
    srv = _Srv(code)
    jid = srv.obj("code_start", {"project": "proj", "task": code.taskfile(size="L")})["job"]
    _wait_state(code.jobs, jid, ("plan_ready",))
    for i in range(500):
        jd = code.jobs.root / f"20200101-000000-{i:06d}"
        jd.mkdir()
        (jd / "job.json").write_text(json.dumps({"id": jd.name, "project": "other", "state": "completed"}))
    srv.obj("code_card_data", {"job": jid})  # прогрев кэша
    t0 = time.perf_counter()
    for _ in range(10):
        srv.obj("code_card_data", {"job": jid})
    assert (time.perf_counter() - t0) / 10 < 0.5


# ---------- code_card_cancel, code_card_ack ----------

def test_card_cancel_needs_key(code, monkeypatch):
    monkeypatch.setenv("FAKE_SLEEP", "30")
    srv = _Srv(code)
    jid = srv.obj("code_start", {"project": "proj", "task": code.taskfile()})["job"]
    for bad in ("", "nope"):
        with pytest.raises(Exception, match="ключ"):
            srv.obj("code_card_cancel", {"job": jid, "key": bad})
    other = srv.obj("code_card_data", {"job": jid})["key"]
    assert srv.obj("code_card_cancel", {"job": jid, "key": other})["state"] in ("starting", "running", "cancelling",
                                                                                 "cancelled")
    assert wait_terminal(code.jobs, jid, timeout=60)["state"] == "cancelled"
    lines = [json.loads(x) for x in code.hub.audit_path.read_text().splitlines()]
    assert [x["status"] for x in lines if x["tool"] == "code_card_cancel"] == ["error", "error", "ok"]
    assert not [x for x in lines if x["tool"] == "code_card_data"]  # чтение в журнал не пишет


def test_card_key_bound_to_job(code):
    srv = _Srv(code)
    a = srv.obj("code_start", {"project": "proj", "task": code.taskfile()})["job"]
    wait_terminal(code.jobs, a)
    key_a = srv.obj("code_card_data", {"job": a})["key"]
    b = srv.obj("code_start", {"project": "proj", "task": code.taskfile("T2.md")})["job"]
    wait_terminal(code.jobs, b)
    with pytest.raises(Exception, match="ключ"):
        srv.obj("code_card_ack", {"job": b, "state": "completed", "key": key_a})


def test_card_ack_first_once(code):
    _notify(code, "card")
    srv = _Srv(code)
    jid = srv.obj("code_start", {"project": "proj", "task": code.taskfile()})["job"]
    wait_terminal(code.jobs, jid)
    key = srv.obj("code_card_data", {"job": jid})["key"]
    assert srv.obj("code_card_ack", {"job": jid, "state": "completed", "key": key}) == {"first": True}
    assert srv.obj("code_card_ack", {"job": jid, "state": "completed", "key": key}) == {"first": False}
    srv2 = _Srv(code)  # переоткрытый чат: новая карточка, новый ключ — второго сообщения нет
    key2 = srv2.obj("code_card_data", {"job": jid})["key"]
    assert srv2.obj("code_card_ack", {"job": jid, "state": "completed", "key": key2}) == {"first": False}
    # состояние, которого у задачи нет, — не повод писать в чат
    assert srv.obj("code_card_ack", {"job": jid, "state": "failed", "key": key}) == {"first": False}
    lines = [json.loads(x) for x in code.hub.audit_path.read_text().splitlines()]
    assert len([x for x in lines if x["tool"] == "code_card_ack"]) == 4


def test_card_ack_poll_mode_false(code):
    srv = _Srv(code)
    jid = srv.obj("code_start", {"project": "proj", "task": code.taskfile()})["job"]
    wait_terminal(code.jobs, jid)
    key = srv.obj("code_card_data", {"job": jid})["key"]
    assert srv.obj("code_card_ack", {"job": jid, "state": "completed", "key": key}) == {"first": False}
    assert not list(code.jobs.job_dir(jid).glob("notified-*"))


def test_card_ack_shared_by_root_and_resume(code):
    """Ревью WSH-3.4: корень с фазами и его продолжение (code_resume) видят одно звено — одно сообщение на двоих;
    у нового итога продолжения — своё."""
    _notify(code, "card")
    srv = _Srv(code)
    root = srv.obj("code_start", {"project": "proj", "task": code.taskfile(size="L")})["job"]
    _wait_state(code.jobs, root, ("plan_ready",))
    code.jobs.approve(root)
    _wait_state(code.jobs, root, ("completed",))
    kr = srv.obj("code_card_data", {"job": root})["key"]
    assert srv.obj("code_card_ack", {"job": root, "state": "completed", "key": kr}) == {"first": True}
    execute = code.jobs.chain(root)[1]["id"]
    child = srv.obj("code_resume", {"job": execute, "prompt": "cont", "note": "x"})["job"]
    assert code.jobs.read(child)["chain_root"] == root
    wait_terminal(code.jobs, child)
    kc = srv.obj("code_card_data", {"job": child})["key"]
    assert srv.obj("code_card_ack", {"job": child, "state": "completed", "key": kc}) == {"first": True}
    kr = srv.obj("code_card_data", {"job": root})["key"]  # карточка корня теперь видит то же звено
    assert srv.obj("code_card_ack", {"job": root, "state": "completed", "key": kr}) == {"first": False}
    assert sorted(p.name for p in code.jobs.job_dir(root).glob("notified-*")) == sorted(
        [f"notified-completed-{execute}", f"notified-completed-{child}"])


def test_card_ack_plan_ready_each_replan(code):
    """Переделка плана — новый plan_ready: сообщение о нём тоже одно, но своё."""
    _notify(code, "card")
    srv = _Srv(code)
    root = srv.obj("code_start", {"project": "proj", "task": code.taskfile(size="L")})["job"]
    _wait_state(code.jobs, root, ("plan_ready",))
    key = srv.obj("code_card_data", {"job": root})["key"]
    assert srv.obj("code_card_ack", {"job": root, "state": "plan_ready", "key": key}) == {"first": True}
    assert srv.obj("code_card_ack", {"job": root, "state": "plan_ready", "key": key}) == {"first": False}
    code.jobs.replan(root, "раздели на два шага")
    time.sleep(0.3)
    _wait_state(code.jobs, root, ("plan_ready",))
    assert srv.obj("code_card_ack", {"job": root, "state": "plan_ready", "key": key}) == {"first": True}
    code.jobs.cancel(root)


# ---------- тихий code_wait ----------

def test_quiet_wait_one_line(code, monkeypatch):
    monkeypatch.setenv("FAKE_SLEEP", "20")
    code.cfg.write_text(code.cfg.read_text().replace("[code]\n", "[code]\nmax_wait_s = 1\n"), encoding="utf-8")
    from wshub.core import Hub
    from wshub.jobs import Jobs
    code.hub, code.jobs = Hub(code.cfg, code.state), Jobs(Hub(code.cfg, code.state))
    srv = _Srv(code)
    jid = srv.obj("code_start", {"project": "proj", "task": code.taskfile()})["job"]
    from test_code import _wait_running
    _wait_running(code.jobs, jid)
    srv.text("code_wait", {"job": jid, "seconds": 1})  # мог захватить смену состояния
    line = srv.text("code_wait", {"job": jid, "seconds": 1})
    assert "\n" not in line and len(line.encode()) <= 200, line
    assert re.match(r"^running · \d+:\d\d · ходов \d+ · .* · ждал 1/1 с$", line), line
    code.jobs.cancel(jid)
    full = srv.text("code_wait", {"job": jid, "seconds": 1})
    for _ in range(40):
        if full.startswith("{"):
            break
        full = srv.text("code_wait", {"job": jid, "seconds": 1})
    snap = json.loads(full)  # смена состояния — полный снимок
    assert snap["state"] in ("cancelling", "cancelled") and "waited_s" in snap
    wait_terminal(code.jobs, jid, timeout=60)

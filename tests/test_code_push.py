"""wshub-code, ТЗ WSH-3.4, часть А: регламент пуша и API хостинга в реестре, разрешения задачи, помощник
`wshub push` (стенд — bare-репозиторий) и `wshub forge` (стенд — локальный HTTP-сервер), settings задачи,
блок в .git/info/exclude, текст отказа git_config_unsafe. Настоящий claude не вызывается."""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from test_code import git, make_code, wait_terminal

from wshub import claude_cli, code_server, gitsafe, helper
from wshub import jobs as jobs_mod
from wshub.forges import PRESETS
from wshub.jobs import JobError
from wshub.registry import CodeGlobal, ProjectCode, parse
from wshub.runtime import proc_start

TOKEN = "glpat-SeCrEt-0123456789"
REASON = "оператор в чате 15.10: запушить и открыть MR"


@pytest.fixture
def code(tmp_path, monkeypatch):
    return make_code(tmp_path, monkeypatch)


@pytest.fixture(autouse=True)
def hub_wshub(tmp_path, monkeypatch):
    """wshub хаба — исполняемый файл (иначе разрешения push/forge не выдаются, ревью WSH-3.4, круг 1)."""
    exe = tmp_path / "hubbin" / "wshub"
    exe.parent.mkdir()
    exe.write_text("#!/bin/sh\nexit 0\n")
    exe.chmod(0o755)
    monkeypatch.setattr(claude_cli, "wshub_exe", lambda: str(exe))
    return exe


def _call(code, name, args) -> str:
    out = asyncio.run(code_server.build_code_server(code.hub).call_tool(name, args))
    content = out[0] if isinstance(out, tuple) else out
    return content[0].text


def _token_file(tmp_path, mode=0o600, text=TOKEN) -> Path:
    p = tmp_path / "secrets" / "forge.token"
    p.parent.mkdir(exist_ok=True)
    p.write_text(text + "\n")
    os.chmod(p, mode)
    return p


# ---------- 1. реестр ----------

def test_registry_defaults(code):
    pc = parse(code.cfg.read_text()).code.projects["proj"]
    assert (pc.push, pc.push_protect, pc.forge, pc.forge_access, pc.forge_write, pc.forge_url, pc.forge_token) == \
        ("no", ("main", "master"), None, "no", False, None, None)


@pytest.mark.parametrize("over,msg", [
    ({"push": '"yes"'}, "push: одно из no, task, always"),
    ({"forge_access": '"read"'}, "forge_access: одно из no, task, always"),
    ({"forge": '"bitbucket"'}, "forge: одно из gitlab, github"),
    ({"forge_access": '"task"', "forge_token": '"~/t"'}, "без forge"),
    ({"forge": '"github"', "forge_access": '"task"'}, "без forge_token"),
    ({"forge": '"gitlab"'}, "нужен forge_url"),
    ({"forge": '"github"', "forge_url": '"ftp://x"'}, "forge_url"),
    ({"forge": '"github"', "forge_url": '"http://example.com"'}, "forge_url"),
    ({"forge_write": '"yes"'}, "forge_write: true или false"),
    ({"push_protect": '"main"'}, "push_protect"),
    ({"forge_token": '"rel/token"'}, "forge_token"),
])
def test_registry_errors(code, over, msg):
    code.write(**over)
    reg = parse(code.cfg.read_text())
    assert msg in reg.code.project_errors["proj"]
    assert reg.workspaces["proj"]  # ошибка — секции проекта, реестр жив


def test_registry_examples(code):
    """Оба примера из А1: GitLab по разрешению задачи и GitHub с PR через API."""
    code.write(push='"task"', push_protect='["master", "dev"]', forge='"gitlab"', forge_access='"task"',
               forge_url='"https://gitlab.example.com"', forge_token='"~/.config/team-service/gitlab.token"')
    pc = parse(code.cfg.read_text()).code.projects["proj"]
    assert (pc.push, pc.push_protect, pc.forge, pc.forge_url) == ("task", ("master", "dev"), "gitlab",
                                                                  "https://gitlab.example.com")
    code.write(push='"always"', forge='"github"', forge_access='"always"', forge_write="true",
               forge_token='"~/.config/pet-project/github.token"')
    pc = parse(code.cfg.read_text()).code.projects["proj"]
    from wshub.registry import forge_url
    assert pc.push_protect == ("main", "master") and forge_url(pc) == "https://api.github.com"
    assert pc.forge_write is True


def test_code_projects_shows_regulation_not_token(code, tmp_path):
    tok = _token_file(tmp_path)
    code.write(push='"task"', forge='"github"', forge_access='"task"', forge_token=f'"{tok}"')
    out = json.loads(_call(code, "code_projects", {}))[0]
    assert {k: out[k] for k in ("push", "push_protect", "forge", "forge_access", "forge_write", "forge_url",
                                "forge_token")} == {
        "push": "task", "push_protect": ["main", "master"], "forge": "github", "forge_access": "task",
        "forge_write": False, "forge_url": "https://api.github.com", "forge_token": str(tok)}
    assert TOKEN not in _call(code, "code_projects", {})


# ---------- 2. разрешения ----------

def test_grant_refused_when_registry_no(code):
    t = code.taskfile()
    with pytest.raises(JobError, match=r"в проекте proj пуш закрыт реестром \(push = no\)"):
        code.jobs.plan("proj", t, push=True, reason=REASON)
    with pytest.raises(JobError, match=r"API хостинга закрыт реестром \(forge_access = no\)"):
        code.jobs.plan("proj", t, forge=True, reason=REASON)
    assert _call(code, "code_start", {"project": "proj", "task": t, "push": True, "reason": REASON}) == \
        "отказ: в проекте proj пуш закрыт реестром (push = no)"


def test_grant_task_needs_reason(code):
    code.write(push='"task"')
    t = code.taskfile()
    with pytest.raises(JobError, match="reason"):
        code.jobs.plan("proj", t, push=True)
    with pytest.raises(JobError, match="reason"):
        code.jobs.plan("proj", t, push=True, reason="коротко")
    p = code.jobs.plan("proj", t, push=True, reason=REASON)
    assert p.grants == {"push": True, "forge": False, "reason": REASON}
    assert code.jobs.plan("proj", t).grants == {"push": False, "forge": False, "reason": None}  # без просьбы — нет


def test_grant_always_without_reason(code, tmp_path):
    code.write(push='"always"', forge='"github"', forge_access='"always"', forge_token=f'"{_token_file(tmp_path)}"')
    assert code.jobs.plan("proj", code.taskfile()).grants == {"push": True, "forge": True, "reason": None,
                                                              "auto": ["push", "forge"]}
    # пуш — только профилю task; API — любому
    g = code.jobs.plan("proj", code.taskfile("R.md", profile="recon")).grants
    assert (g["push"], g["forge"]) == (False, True)
    with pytest.raises(JobError, match="только задаче профиля task"):
        code.jobs.plan("proj", code.taskfile("R.md", profile="recon"), push=True, reason=REASON)


def test_grant_written_inherited_by_resume_and_added(code, tmp_path):
    code.write(push='"task"', forge='"github"', forge_access='"task"', forge_token=f'"{_token_file(tmp_path)}"')
    out = json.loads(_call(code, "code_start", {"project": "proj", "task": code.taskfile(), "forge": True,
                                                "reason": REASON}))
    jid = out["job"]
    assert out["grants"].startswith("API хостинга")
    wait_terminal(code.jobs, jid)
    assert code.jobs.read(jid)["grants"] == {"push": False, "forge": True, "reason": REASON}
    # типичный сценарий: задача принята, оператор говорит «запушь»
    out = json.loads(_call(code, "code_resume", {"job": jid, "prompt": "cont", "note": "запушь", "push": True,
                                                 "reason": "оператор в чате 15.10: запушь ветку"}))
    child = code.jobs.read(out["job"])
    assert child["grants"] == {"push": True, "forge": True, "reason": "оператор в чате 15.10: запушь ветку"}
    wait_terminal(code.jobs, out["job"])
    # resume без просьб — наследует оба
    again = code.jobs.resume(out["job"], "cont", note="ещё")
    assert again["grants"]["push"] and again["grants"]["forge"]
    wait_terminal(code.jobs, again["id"])
    # журнал: строки code_start с разрешениями и reason
    lines = [json.loads(x) for x in code.hub.audit_path.read_text().splitlines()]
    starts = [x for x in lines if x.get("tool") == "code_start" and x.get("status") == "ok"]
    assert starts[0]["params"]["grants"] == {"forge": True, "reason": REASON}
    assert starts[1]["params"]["grants"]["push"] is True


def test_grant_inherited_through_plan_approve(code):
    code.write(push='"task"')
    root = code.jobs.start(code.jobs.plan("proj", code.taskfile(size="L"), push=True, reason=REASON))["id"]
    from test_code_plan import _wait_state
    _wait_state(code.jobs, root, ("plan_ready",))
    plan_settings = json.loads((code.jobs.job_dir(root) / "settings.json").read_text())
    assert "Bash(wshub push *)" not in plan_settings["permissions"]["allow"]  # фаза плана — без помощника
    child = code.jobs.approve(root)
    assert child["grants"] == {"push": True, "forge": False, "reason": REASON}
    s = json.loads((code.jobs.job_dir(child["id"]) / "settings.json").read_text())
    assert "Bash(wshub push *)" in s["permissions"]["allow"]
    wait_terminal(code.jobs, child["id"])


def test_grant_dropped_when_registry_closed(code):
    code.write(push='"task"')
    jid = code.jobs.start(code.jobs.plan("proj", code.taskfile(), push=True, reason=REASON))["id"]
    wait_terminal(code.jobs, jid)
    code.write(push='"no"')
    child = code.jobs.resume(jid, "cont", note="x")
    assert child["grants"]["push"] is False and any("push = no" in w for w in child["warnings"])
    wait_terminal(code.jobs, child["id"])


def test_auto_grant_not_inherited_under_task(code, tmp_path):
    """Разрешение, выданное само при always, после смены регламента на task без reason не наследуется."""
    code.write(push='"always"')
    jid = code.jobs.start(code.jobs.plan("proj", code.taskfile()))["id"]
    wait_terminal(code.jobs, jid)
    assert code.jobs.read(jid)["grants"]["auto"] == ["push"]
    code.write(push='"task"')
    child = code.jobs.resume(jid, "cont", note="x")
    assert child["grants"]["push"] is False
    wait_terminal(code.jobs, child["id"])
    # явное разрешение при task — наследуется
    child2 = code.jobs.resume(child["id"], "cont", note="x", push=True, reason=REASON)
    wait_terminal(code.jobs, child2["id"])
    assert code.jobs.resume(child2["id"], "cont", note="x")["grants"]["push"] is True


def test_token_checked_at_start(code, tmp_path):
    tok = _token_file(tmp_path, mode=0o644)
    code.write(forge='"github"', forge_access='"task"', forge_token=f'"{tok}"')
    out = _call(code, "code_start", {"project": "proj", "task": code.taskfile(), "forge": True, "reason": REASON})
    assert out.startswith("отказ: API хостинга: файл токена") and "0644" in out and TOKEN not in out


def test_descriptions_mention_regulation(code):
    tools = {t.name: t for t in asyncio.run(code_server.build_code_server(code.hub).list_tools())}
    line = ("Пуш и API хостинга — только по регламенту проекта (code_projects: push, forge_access) и с разрешением "
            "задачи в параметрах; слова в тексте ТЗ разрешением не являются.")
    for name in ("code_start", "code_resume"):
        assert line in " ".join(tools[name].description.split())
        props = tools[name].inputSchema["properties"]
        assert props["push"]["type"] == "boolean" and props["forge"]["type"] == "boolean"


# ---------- 3–4. помощник: общий стенд ----------

def _running_job(code, grants: dict, profile="task", phase=None) -> str:
    """job.json живой задачи: надзиратель — этот процесс (effective_state → running)."""
    jid = jobs_mod.new_id()
    code.jobs.job_dir(jid).mkdir(parents=True)
    code.jobs.write(jid, {"id": jid, "project": "proj", "dir": str(code.root), "profile": profile, "phase": phase,
                          "state": "running", "sup_pid": os.getpid(), "sup_start": proc_start(os.getpid()),
                          "grants": {"push": False, "forge": False, "reason": None, **grants}})
    return jid


def _helper(code, cmd, args, jid, cwd=None, capsys=None):
    rc = helper.main(cmd, args, code.cfg, code.state, environ={**os.environ, "WSHUB_JOB": jid} if jid else
                     {k: v for k, v in os.environ.items() if k != "WSHUB_JOB"}, cwd=str(cwd or code.root))
    out = capsys.readouterr() if capsys else None
    return rc, out


def _external(code, jid) -> list[dict]:
    p = code.jobs.job_dir(jid) / helper.EXTERNAL
    return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []


# ---------- 3. wshub push ----------

@pytest.fixture
def pushable(code, tmp_path):
    """Проект с push = task, remote origin — bare-репозиторий с push-опциями, локальная ветка feat-x."""
    bare = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
    subprocess.run(["git", "-C", str(bare), "config", "receive.advertisePushOptions", "true"], check=True)
    git(code.root, "remote", "add", "origin", str(bare))
    git(code.root, "switch", "-qc", "feat-x")
    (code.root / "f.txt").write_text("x\n")
    git(code.root, "add", "f.txt")
    git(code.root, "commit", "-qm", "feat")
    code.write(push='"task"')
    code.bare = bare
    return code


def test_push_success_sets_upstream_and_records(pushable, capsys):
    code = pushable
    jid = _running_job(code, {"push": True})
    rc, out = _helper(code, "push", ["origin", "feat-x", "-o", "merge_request.create"], jid, capsys=capsys)
    assert rc == 0, out
    r = subprocess.run(["git", "-C", str(code.root), "rev-parse", "--abbrev-ref", "feat-x@{u}"],
                       capture_output=True, text=True)
    assert r.stdout.strip() == "origin/feat-x"
    rows = _external(code, jid)
    assert rows[-1]["cmd"] == "push" and rows[-1]["exit"] == 0
    assert rows[-1]["args"] == ["origin", "feat-x", "-o", "merge_request.create"]
    view = code.jobs.external_view([code.jobs.read(jid)])
    assert view["push_count"] == 1 and view["forge_count"] == 0
    audit = [json.loads(x) for x in code.hub.audit_path.read_text().splitlines()]
    assert any(x["tool"] == "wshub_push" and x["job"] == jid and x["status"] == "ok" for x in audit)


@pytest.mark.parametrize("args,grants,msg", [
    (["origin", "feat-x"], {}, "нет разрешения на пуш"),
    (["origin", "main"], {"push": True}, "защищена регламентом"),
    (["origin", "master"], {"push": True}, "защищена регламентом"),
    (["/tmp/other.git", "feat-x"], {"push": True}, "имя remote из git remote, а не адрес"),
    (["git@host:o/r.git", "feat-x"], {"push": True}, "а не адрес"),
    (["origin", "feat-x:main"], {"push": True}, "src:dst"),
    (["origin", "feat-x", "-o", "merge_request.merge_when_pipeline_succeeds"], {"push": True}, "мерж — действие"),
    (["origin", "feat-x", "-o", "AUTO_MERGE=1"], {"push": True}, "мерж — действие"),
    (["origin", "feat-x", "--force"], {"push": True}, "только -o"),
    (["origin", "+feat-x"], {"push": True}, "без +"),
    (["upstream", "feat-x"], {"push": True}, "remote upstream нет"),
    (["origin", "nope"], {"push": True}, "локальной ветки nope нет"),
    (["origin"], {"push": True}, "нужно: wshub push"),
])
def test_push_refusals(pushable, capsys, args, grants, msg):
    code = pushable
    jid = _running_job(code, grants)
    rc, out = _helper(code, "push", args, jid, capsys=capsys)
    assert rc != 0 and msg in out.err, out.err
    assert _external(code, jid)[-1]["exit"] == 1
    r = subprocess.run(["git", "-C", str(code.bare), "branch", "--list"], capture_output=True, text=True)
    assert r.stdout == ""  # ничего не ушло


def test_push_refused_without_job_env(pushable, capsys):
    rc, out = _helper(pushable, "push", ["origin", "feat-x"], None, capsys=capsys)
    assert rc == 1 and "нет WSHUB_JOB" in out.err


def test_push_refused_job_not_alive(pushable, capsys):
    code = pushable
    jid = _running_job(code, {"push": True})
    d = code.jobs.read(jid)
    d["state"] = "completed"
    code.jobs.write(jid, d)
    rc, out = _helper(code, "push", ["origin", "feat-x"], jid, capsys=capsys)
    assert rc == 1 and "не идёт (состояние completed)" in out.err


def test_push_refused_cwd_outside(pushable, capsys, tmp_path):
    code = pushable
    jid = _running_job(code, {"push": True})
    rc, out = _helper(code, "push", ["origin", "feat-x"], jid, cwd=tmp_path, capsys=capsys)
    assert rc == 1 and "вне каталога задачи" in out.err


def test_push_refused_registry_closed_or_profile(pushable, capsys):
    code = pushable
    jid = _running_job(code, {"push": True}, profile="review")
    rc, out = _helper(code, "push", ["origin", "feat-x"], jid, capsys=capsys)
    assert rc == 1 and "только задаче профиля task" in out.err
    code.write(push='"no"')
    jid = _running_job(code, {"push": True})
    rc, out = _helper(code, "push", ["origin", "feat-x"], jid, capsys=capsys)
    assert rc == 1 and "push = no" in out.err


def test_push_protect_patterns(pushable, capsys):
    code = pushable
    code.write(push='"task"', push_protect='["release/*"]')
    git(code.root, "branch", "release/1")
    jid = _running_job(code, {"push": True})
    rc, out = _helper(code, "push", ["origin", "release/1"], jid, capsys=capsys)
    assert rc == 1 and "release/*" in out.err
    git(code.root, "branch", "main")  # main теперь не защищён: список проекта заменяет умолчание
    rc, out = _helper(code, "push", ["origin", "main"], jid, capsys=capsys)
    assert rc == 0, out.err


def test_push_uses_saved_ssh_agent_only(pushable, capsys, monkeypatch):
    """SSH_AUTH_SOCK — из каталога задачи, не из окружения Code; в env.json claude его нет."""
    code = pushable
    jid = _running_job(code, {"push": True})
    (code.jobs.job_dir(jid) / helper.SSH_SOCK).write_text("/run/agent.sock")
    seen = {}

    def fake_run(git_, cwd, args, environ, timeout, ssh_agent=False, push=False, **kw):
        if args[:1] == ["push"]:
            seen.update(env=environ, push=push, args=args)
            return subprocess.CompletedProcess(args, 0, "", "")
        return real(git_, cwd, args, environ, timeout, ssh_agent=ssh_agent, **kw)

    real = gitsafe.run
    monkeypatch.setattr(gitsafe, "run", fake_run)
    environ = {**os.environ, "WSHUB_JOB": jid, "SSH_AUTH_SOCK": "/code/own.sock"}
    assert helper.main("push", ["origin", "feat-x"], code.cfg, code.state, environ=environ, cwd=str(code.root)) == 0
    assert seen["env"]["SSH_AUTH_SOCK"] == "/run/agent.sock" and seen["push"] is True
    assert seen["args"] == ["push", "-u", "--no-follow-tags", "--recurse-submodules=no", "origin",
                            "refs/heads/feat-x:refs/heads/feat-x"]
    assert "core.sshCommand=ssh" not in gitsafe.PUSH_OVERRIDES and "credential.helper=" not in gitsafe.PUSH_OVERRIDES
    assert "core.hooksPath=/dev/null" in gitsafe.PUSH_OVERRIDES


def test_start_saves_ssh_agent_and_env(code, monkeypatch, tmp_path):
    code.write(push='"task"')
    monkeypatch.setenv("SSH_AUTH_SOCK", "/run/agent.sock")
    monkeypatch.setenv("FAKE_REC", str(tmp_path / "rec.json"))
    jid = code.jobs.start(code.jobs.plan("proj", code.taskfile(), push=True, reason=REASON))["id"]
    wait_terminal(code.jobs, jid)
    assert (code.jobs.job_dir(jid) / helper.SSH_SOCK).read_text() == "/run/agent.sock"
    env = json.loads((tmp_path / "rec.json").read_text())["env"]
    assert "SSH_AUTH_SOCK" not in env and env["WSHUB_JOB"] == jid
    assert env["PATH"].split(":")[0] == str(Path(claude_cli.wshub_exe()).parent)  # настоящий wshub — первым
    assert env["WSHUB_STATE"] == str(code.state) and env["WSHUB_CONFIG"] == str(code.cfg)


# ---------- 4. wshub forge ----------

class _Api:
    """Локальный HTTP-сервер: пишет запросы, отвечает по таблице путь → (код, тело)."""

    def __init__(self):
        self.requests: list[dict] = []
        self.routes: dict[str, tuple[int, bytes]] = {}
        api = self

        class H(BaseHTTPRequestHandler):
            def _do(self):
                n = int(self.headers.get("Content-Length") or 0)
                api.requests.append({"method": self.command, "path": self.path, "headers": dict(self.headers),
                                     "body": self.rfile.read(n) if n else b""})
                code, body = api.routes.get(self.path, (200, b'{"ok": true}'))
                self.send_response(code)
                if code in (301, 302):
                    self.send_header("Location", "http://127.0.0.1:1/steal")
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = _do

            def log_message(self, *a):
                pass

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


@pytest.fixture
def api():
    a = _Api()
    yield a
    a.close()


def _forge_code(code, tmp_path, api, kind, write=True, mode=0o600):
    tok = _token_file(tmp_path, mode=mode)
    code.write(forge=f'"{kind}"', forge_access='"task"', forge_write="true" if write else "false",
               forge_url=f'"{api.url}"', forge_token=f'"{tok}"')
    return _running_job(code, {"forge": True}, profile="recon")


@pytest.mark.parametrize("kind,path,sent,header", [
    ("gitlab", "/projects/1/merge_requests/2", "/api/v4/projects/1/merge_requests/2", ("Private-Token", TOKEN)),
    ("gitlab", "/api/v4/projects/1", "/api/v4/projects/1", ("Private-Token", TOKEN)),
    ("github", "/repos/o/r/pulls/2", "/repos/o/r/pulls/2", ("Authorization", f"Bearer {TOKEN}")),
])
def test_forge_get_header_and_prefix(code, tmp_path, api, capsys, kind, path, sent, header):
    jid = _forge_code(code, tmp_path, api, kind, write=False)
    rc, out = _helper(code, "forge", ["GET", path], jid, capsys=capsys)
    assert rc == 0, out.err
    req = api.requests[-1]
    assert req["path"] == sent and req["headers"][header[0]] == header[1]
    if kind == "github":
        assert req["headers"]["Accept"] == "application/vnd.github+json"
    assert json.loads(out.out) == {"ok": True}
    row = _external(code, jid)[-1]
    assert row["cmd"] == "forge" and row["args"] == ["GET", path] and row["status"] == 200
    assert TOKEN not in json.dumps(_external(code, jid)) and TOKEN not in code.hub.audit_path.read_text()


@pytest.mark.parametrize("kind", ["gitlab", "github"])
def test_forge_write_needs_forge_write_and_delete_never(code, tmp_path, api, capsys, kind):
    jid = _forge_code(code, tmp_path, api, kind, write=False)
    rc, out = _helper(code, "forge", ["POST", "/projects/1/merge_requests"], jid, capsys=capsys)
    assert rc == 1 and "forge_write = false" in out.err
    jid = _forge_code(code, tmp_path, api, kind, write=True)
    rc, out = _helper(code, "forge", ["DELETE", "/projects/1/merge_requests/2"], jid, capsys=capsys)
    assert rc == 1 and "DELETE" in out.err
    assert api.requests == []


# каждый пункт пола (А3.3 и сверх ТЗ) — отказ записи при любом регламенте
FLOOR = [
    ("gitlab", "/projects/1/merge_requests/2/merge"),
    ("gitlab", "/projects/1/merge_requests/2/approve"),
    ("gitlab", "/projects/1/merge_requests/2/unapprove"),
    ("gitlab", "/projects/1/merge_requests/2/rebase"),
    ("gitlab", "/projects/1/repository/commits"),
    ("gitlab", "/projects/1/repository/files/src%2Fa.py"),
    ("gitlab", "/projects/1/repository/branches"),
    ("gitlab", "/projects/1/repository/branches/feat"),
    ("gitlab", "/projects/1/repository/tags"),
    ("gitlab", "/projects/1/repository/submodules/lib"),
    ("gitlab", "/projects/1/protected_branches"),
    ("gitlab", "/projects/1/protected_tags"),
    ("gitlab", "/projects/1/merge_requests/2/%6Derge"),  # %6D — «m»
    ("gitlab", "/projects/1/merge_requests/2/%256Derge"),  # двойное кодирование
    ("gitlab", "/projects/1/MERGE_REQUESTS/2/MERGE"),
    ("github", "/repos/o/r/pulls/2/merge"),
    ("github", "/repos/o/r/merges"),
    ("github", "/repos/o/r/pulls/2/reviews"),
    ("github", "/repos/o/r/pulls/2/reviews/9/events"),
    ("github", "/repos/o/r/pulls/2/update-branch"),
    ("github", "/repos/o/r/contents/README.md"),
    ("github", "/repos/o/r/git/refs"),
    ("github", "/repos/o/r/git/refs/heads/main"),
    ("github", "/repos/o/r/branches/main/protection"),
    ("github", "/repos/o/r/rulesets"),
    ("github", "/graphql"),
    # суффикс формата GitLab (Grape): …/merge.json — тот же мерж
    ("gitlab", "/projects/1/merge_requests/2/merge.json"),
    ("gitlab", "/projects/1/merge_requests/2/approve.json"),
    ("gitlab", "/projects/1/protected_branches.json"),
    ("gitlab", "/projects/1/repository/commits.json"),
    ("github", "/repos/o/r/pulls/2/merge.json"),
    # ревью WSH-3.4: мерж поездом, правила одобрения, релизы (теги), учётные данные
    ("gitlab", "/projects/1/merge_trains/merge_requests/2"),
    ("gitlab", "/projects/1/merge_requests/2/approvals"),
    ("gitlab", "/projects/1/merge_requests/2/approval_rules"),
    ("gitlab", "/projects/1/approval_rules"),
    ("gitlab", "/projects/1/releases"),
    ("gitlab", "/projects/1/access_tokens"),
    ("gitlab", "/projects/1/deploy_keys"),
    ("gitlab", "/projects/1/deploy_tokens"),
    ("gitlab", "/personal_access_tokens/self/rotate"),
    ("gitlab", "/user/keys"),
    ("github", "/repos/o/r/merge-upstream"),
    ("github", "/repos/o/r/releases"),
    ("github", "/repos/o/r/keys"),
    ("github", "/user/keys"),
    ("github", "/user/gpg_keys"),
]


@pytest.mark.parametrize("kind,path", FLOOR)
@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH"])
def test_forge_floor(code, tmp_path, api, capsys, kind, path, method):
    jid = _forge_code(code, tmp_path, api, kind)
    rc, out = _helper(code, "forge", [method, path], jid, capsys=capsys)
    assert rc == 1 and "при любом регламенте" in out.err, out.err
    assert api.requests == []


@pytest.mark.parametrize("kind,path", [
    ("gitlab", "/projects/1/merge_requests"),
    ("gitlab", "/projects/1/merge_requests/2/notes"),
    ("gitlab", "/projects/1/merge_requests/2/discussions"),
    ("github", "/repos/o/r/pulls"),
    ("github", "/repos/o/r/pulls/2/comments"),
    ("github", "/repos/o/r/issues/2/comments"),
])
def test_forge_write_allowed_paths(code, tmp_path, api, capsys, kind, path):
    jid = _forge_code(code, tmp_path, api, kind)
    (code.root / "body.json").write_text('{"title": "t"}')
    rc, out = _helper(code, "forge", ["POST", path, "--data", "body.json"], jid, capsys=capsys)
    assert rc == 0, out.err
    assert api.requests[-1]["method"] == "POST" and api.requests[-1]["body"] == b'{"title": "t"}'
    assert api.requests[-1]["headers"]["Content-Type"] == "application/json"


def test_forge_get_floor_paths_read(code, tmp_path, api, capsys):
    """Пол — для записи: чтение файла репозитория через API — обычный GET."""
    jid = _forge_code(code, tmp_path, api, "gitlab")
    rc, out = _helper(code, "forge", ["GET", "/projects/1/repository/files/a.py?ref=main"], jid, capsys=capsys)
    assert rc == 0 and api.requests[-1]["path"] == "/api/v4/projects/1/repository/files/a.py?ref=main"


def test_forge_redacts_token(code, tmp_path, api, capsys):
    jid = _forge_code(code, tmp_path, api, "gitlab")
    api.routes["/api/v4/user"] = (200, json.dumps({"echo": TOKEN, "n": 1}).encode())
    rc, out = _helper(code, "forge", ["GET", "/user"], jid, capsys=capsys)
    assert rc == 0 and TOKEN not in out.out and json.loads(out.out)["echo"] == "***"
    api.routes["/api/v4/bad"] = (401, f"bad token {TOKEN}".encode())
    rc, out = _helper(code, "forge", ["GET", "/bad"], jid, capsys=capsys)
    assert rc == 1 and "HTTP 401" in out.err and TOKEN not in out.err and "***" in out.err
    (code.root / "o").mkdir()
    rc, out = _helper(code, "forge", ["GET", "/user", "--out", "o/u.json"], jid, capsys=capsys)
    assert rc == 0 and TOKEN not in (code.root / "o" / "u.json").read_text()


def test_forge_big_answer_needs_out(code, tmp_path, api, capsys):
    jid = _forge_code(code, tmp_path, api, "github")
    big = json.dumps({"x": "y" * 30000}).encode()
    api.routes["/big"] = (200, big)
    rc, out = _helper(code, "forge", ["GET", "/big"], jid, capsys=capsys)
    assert rc == 1 and "больше 20 КБ" in out.err and out.out == ""
    rc, out = _helper(code, "forge", ["GET", "/big", "--out", "big.json"], jid, capsys=capsys)
    assert rc == 0 and (code.root / "big.json").read_bytes() == big


@pytest.mark.parametrize("out_arg,msg", [
    ("../x.json", "только внутри рабочего каталога"),
    ("/tmp/x.json", "только внутри рабочего каталога"),
    (".git/x.json", "в .git"),
    ("~/x.json", "явный путь"),
    ("$HOME/x.json", "явный путь"),
    ("lnk/x.json", "только внутри рабочего каталога"),
    # помощник пишет вне песочницы — её запреты повторяет сам (ревью WSH-3.4)
    (".claude/settings.json", "защищённый путь песочницы"),
    (".claude/x.md", "защищённый путь песочницы"),
    (".mcp.json", "защищённый путь песочницы"),
    (".vscode/tasks.json", "защищённый путь песочницы"),
    (".gitmodules", "защищённый путь песочницы"),
    ("docker-compose.override.yml", "deny_edit"),
])
def test_forge_out_outside_refused(code, tmp_path, api, capsys, out_arg, msg):
    (code.root / "lnk").symlink_to(tmp_path)
    jid = _forge_code(code, tmp_path, api, "github")
    code.write(forge='"github"', forge_access='"task"', forge_url=f'"{api.url}"',
               forge_token=f'"{tmp_path / "secrets" / "forge.token"}"', deny_edit='["docker-compose*.yml"]')
    rc, out = _helper(code, "forge", ["GET", "/x", "--out", out_arg], jid, capsys=capsys)
    assert rc == 1 and msg in out.err, out.err
    assert api.requests == []


def test_forge_data_outside_refused(code, tmp_path, api, capsys):
    (tmp_path / "d.json").write_text("{}")
    jid = _forge_code(code, tmp_path, api, "github")
    rc, out = _helper(code, "forge", ["POST", "/repos/o/r/pulls", "--data", str(tmp_path / "d.json")], jid,
                      capsys=capsys)
    assert rc == 1 and "только внутри рабочего каталога" in out.err
    (code.root / ".env.json").write_text("{}")
    rc, out = _helper(code, "forge", ["POST", "/repos/o/r/pulls", "--data", ".env.json"], jid, capsys=capsys)
    assert rc == 1 and ".env* — нельзя" in out.err
    (code.root / "bad.json").write_text("{не json")
    rc, out = _helper(code, "forge", ["POST", "/repos/o/r/pulls", "--data", "bad.json"], jid, capsys=capsys)
    assert rc == 1 and "не JSON" in out.err


def test_forge_token_mode_refused(code, tmp_path, api, capsys):
    jid = _forge_code(code, tmp_path, api, "github", mode=0o644)
    rc, out = _helper(code, "forge", ["GET", "/user"], jid, capsys=capsys)
    assert rc == 1 and "0644" in out.err and "chmod 600" in out.err and TOKEN not in out.err
    assert api.requests == []


def test_forge_refuses_redirect(code, tmp_path, api, capsys):
    """urllib переслал бы заголовок с токеном по Location — перенаправление не выполняется."""
    jid = _forge_code(code, tmp_path, api, "gitlab")
    api.routes["/api/v4/moved"] = (302, b"")
    rc, out = _helper(code, "forge", ["GET", "/moved"], jid, capsys=capsys)
    assert rc == 1 and "перенаправлением" in out.err and len(api.requests) == 1


@pytest.mark.parametrize("path", ["projects/1", "//evil.example/x", "/a/../merge_requests/1/merge",
                                  "/a/%2e%2e/b", "/a b", "/a#b", "http://x/y"])
def test_forge_bad_paths(code, tmp_path, api, capsys, path):
    jid = _forge_code(code, tmp_path, api, "gitlab")
    rc, out = _helper(code, "forge", ["GET", path], jid, capsys=capsys)
    assert rc == 1 and api.requests == []


def test_forge_without_grant_or_closed(code, tmp_path, api, capsys):
    _forge_code(code, tmp_path, api, "github")
    jid = _running_job(code, {}, profile="recon")
    rc, out = _helper(code, "forge", ["GET", "/user"], jid, capsys=capsys)
    assert rc == 1 and "нет разрешения на API хостинга" in out.err
    code.write(forge='"github"', forge_access='"no"')
    jid = _running_job(code, {"forge": True}, profile="recon")
    rc, out = _helper(code, "forge", ["GET", "/user"], jid, capsys=capsys)
    assert rc == 1 and "forge_access = no" in out.err


def test_forge_link_recorded(code, tmp_path, api, capsys):
    jid = _forge_code(code, tmp_path, api, "github")
    api.routes["/repos/o/r/pulls"] = (201, json.dumps({"html_url": "https://github.com/o/r/pull/7"}).encode())
    rc, _ = _helper(code, "forge", ["POST", "/repos/o/r/pulls"], jid, capsys=capsys)
    assert rc == 0
    view = code.jobs.external_view([code.jobs.read(jid)])
    assert view["links"] == ["https://github.com/o/r/pull/7"] and view["forge_count"] == 1


def test_presets_module_contract():
    """Новый хостинг — модуль с PRESET: реестр берёт список типов из PRESETS."""
    assert set(PRESETS) == {"gitlab", "github"}
    for p in PRESETS.values():
        assert p.floor and callable(p.headers)
    assert PRESETS["gitlab"].default_url is None and PRESETS["github"].default_url == "https://api.github.com"


# ---------- 5. settings, .git/info/exclude, gitsafe ----------

def _settings(profile, grants=None, phase=None):
    proj = ProjectCode(enabled=True, dirs=(".",), prompts="p", sandbox_exclude=("docker compose *",))
    return claude_cli.settings(profile, "T.md", CodeGlobal(), proj, [], [], ["/py", "-m", "wshub.hook"],
                               Path("/home/u"), Path("/home/u/proj"), phase=phase, grants=grants)


@pytest.mark.parametrize("profile", ["recon", "task", "review"])
def test_settings_helper_only_with_grant(profile):
    s = _settings(profile)
    assert not [a for a in s["permissions"]["allow"] if "wshub" in a]
    assert not [c for c in s["sandbox"]["excludedCommands"] if "wshub" in c]
    s = _settings(profile, {"push": True, "forge": True})
    want = (["wshub push *"] if profile == "task" else []) + ["wshub forge *"]
    assert [a for a in s["permissions"]["allow"] if "wshub" in a] == [f"Bash({c})" for c in want]
    assert [c for c in s["sandbox"]["excludedCommands"] if "wshub" in c] == want
    # фаза плана — без помощника
    p = _settings(profile, {"push": True, "forge": True}, phase="plan")
    assert not [a for a in p["permissions"]["allow"] if "wshub" in a] and p["sandbox"]["excludedCommands"] == []


@pytest.mark.parametrize("profile,phase", [("recon", None), ("task", None), ("review", None), ("task", "plan")])
def test_settings_filters_and_remote(profile, phase):
    s = _settings(profile, phase=phase)
    allow, deny = s["permissions"]["allow"], s["permissions"]["deny"]
    for c in ("head *", "tail *", "wc *", "grep *", "cut *", "git remote", "git remote -v", "git remote get-url *",
              "git ls-files *", "ls *"):
        assert f"Bash({c})" in allow
    task = profile == "task" and phase is None
    assert ("Bash(sort *)" in allow) == task and ("Bash(uniq *)" in allow) == task
    assert "Bash(git remote *)" not in deny and "Bash(git remote *)" not in allow
    for sub in ("add", "set-url", "remove", "rm", "rename", "prune", "update", "set-head", "set-branches", "show"):
        assert f"Bash(git remote {sub} *)" in deny
    hook = s["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
    assert hook.startswith(f"/py -m wshub.hook --profile {'plan' if phase else profile}")


def test_hook_profile_from_settings_runs(tmp_path):
    """Строка хука с --profile запускается: --output у task в рабочий каталог — да, в recon — отказ."""
    import shlex
    import sys
    for profile, rc in (("task", 0), ("recon", 2)):
        cmd = _settings(profile)["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
        argv = [sys.executable, *shlex.split(cmd)[1:]]
        event = json.dumps({"tool_name": "Bash", "tool_input": {"command": "git diff --output=out.diff"},
                            "cwd": str(tmp_path)})
        r = subprocess.run(argv, input=event, capture_output=True, text=True,
                           env={**os.environ, "PYTHONPATH": str(Path(__file__).parent.parent / "src")})
        assert r.returncode == rc, r.stderr


def test_exclude_block_written_once_foreign_lines_kept(code):
    ex = code.root / ".git" / "info" / "exclude"
    ex.write_text("# мои правила\n*.log\n")
    jobs_mod.write_exclude(code.root)
    jobs_mod.write_exclude(code.root)
    text = ex.read_text()
    assert text.startswith("# мои правила\n*.log\n")
    assert text.count(jobs_mod.EXCLUDE_BEGIN) == 1 and text.count(jobs_mod.EXCLUDE_END) == 1
    for m in jobs_mod.SANDBOX_MOUNTS:
        assert f"/{m}\n" in text
    ex.write_text(text + "tmp/\n")  # строка после блока — чужая, остаётся
    jobs_mod.write_exclude(code.root)
    assert "tmp/" in ex.read_text() and ex.read_text().count(jobs_mod.EXCLUDE_BEGIN) == 1


def test_exclude_hides_mount_points_from_status(code):
    """Точки монтирования claude 2.1.291 (стенд WSH-3.4), в том числе под .claude/: git status их не видит."""
    mounts = (".bashrc", ".mcp.json", ".idea", ".claude/agents", ".claude/settings.json", ".claude/skills")
    for m in mounts:
        p = code.root / m
        p.parent.mkdir(exist_ok=True)
        p.mkdir() if m in (".idea",) else p.write_text("")
    (code.root / ".claude" / "own.md").write_text("x")  # прочее под .claude/ — видно
    jid = code.jobs.start(code.jobs.plan("proj", code.taskfile()))["id"]
    wait_terminal(code.jobs, jid)
    r = subprocess.run(["git", "-C", str(code.root), "status", "--porcelain", "-uall"], capture_output=True, text=True)
    assert not [x for x in r.stdout.splitlines() if any(m in x for m in mounts)]
    assert "?? .claude/own.md" in r.stdout.splitlines()


def test_exclude_symlink_refused(code, tmp_path):
    target = tmp_path / "elsewhere"
    target.write_text("")
    ex = code.root / ".git" / "info" / "exclude"
    ex.unlink(missing_ok=True)
    ex.symlink_to(target)
    with pytest.raises(OSError):
        jobs_mod.write_exclude(code.root)
    assert target.read_text() == ""


def test_git_config_unsafe_names_key_and_value(code):
    git(code.root, "config", "core.hooksPath", "/path/hooks")
    git(code.root, "config", "url.https://user:pw@host/.insteadOf", "https://host/")
    with pytest.raises(gitsafe.GitUnsafe) as e:
        gitsafe.run("git", code.root, ["status"], dict(os.environ), 30)
    msg = str(e.value)
    assert "core.hookspath = /path/hooks — исправь: git -C" in msg and "--unset-all core.hookspath" in msg
    assert "url.https://***@host/.insteadof" in msg and "pw" not in msg
    assert e.value.keys == ["core.hookspath", "url.https://user:pw@host/.insteadof"]


# ---------- круг 1 ревью: запись по списку разрешений, секреты, PATH, --out, --data, cwd ----------

ALLOWED = [
    ("gitlab", "POST", "/projects/1/merge_requests"),
    ("gitlab", "PUT", "/projects/1/merge_requests/2"),
    ("gitlab", "POST", "/projects/1/merge_requests/2/notes"),
    ("gitlab", "POST", "/projects/1/merge_requests/2/discussions"),
    ("gitlab", "POST", "/projects/1/merge_requests/2/discussions/0a1b2c3d/notes"),
    ("gitlab", "POST", "/projects/1/issues"),
    ("gitlab", "PUT", "/projects/1/issues/3"),
    ("gitlab", "POST", "/projects/1/issues/3/notes"),
    ("gitlab", "POST", "/projects/group%2Frepo/merge_requests"),  # закодированный проект
    ("gitlab", "PUT", "/api/v4/projects/1/merge_requests/2.json"),  # префикс и суффикс формата
    ("github", "POST", "/repos/o/r/pulls"),
    ("github", "PATCH", "/repos/o/r/pulls/2"),
    ("github", "POST", "/repos/o/r/pulls/2/comments"),
    ("github", "POST", "/repos/o/r/pulls/2/comments/9/replies"),
    ("github", "POST", "/repos/o/r/issues"),
    ("github", "PATCH", "/repos/o/r/issues/3"),
    ("github", "POST", "/repos/o/r/issues/3/comments"),
    ("github", "POST", "/repos/o/r/issues/3/labels"),
]


@pytest.mark.parametrize("kind,method,path", ALLOWED)
def test_forge_write_allowlist_passes(code, tmp_path, api, capsys, kind, method, path):
    jid = _forge_code(code, tmp_path, api, kind)
    rc, out = _helper(code, "forge", [method, path], jid, capsys=capsys)
    assert rc == 0, out.err
    assert api.requests[-1]["method"] == method


@pytest.mark.parametrize("kind,method,path", ALLOWED)
def test_allowlist_never_hits_floor(kind, method, path):
    from wshub.helper import api_path
    _url, check, raw = api_path(PRESETS[kind], path)
    assert PRESETS[kind].write_allowed(method, raw) and PRESETS[kind].floor_hit(check) is None


@pytest.mark.parametrize("kind,method,path", [
    ("gitlab", "POST", "/projects/1/members"),
    ("gitlab", "POST", "/projects/1/hooks"),
    ("gitlab", "POST", "/projects/1/variables"),
    ("gitlab", "POST", "/projects/1/pipeline"),
    ("gitlab", "POST", "/projects/1/jobs/7/play"),
    ("gitlab", "PUT", "/projects/1"),
    ("gitlab", "POST", "/projects/1/merge_requests/2%2F..%2F3/notes"),  # закодированный «/» в номере
    ("gitlab", "PUT", "/projects/1/merge_requests"),  # метод не тот
    ("gitlab", "POST", "/projects/1/merge_requests/2/discussions/xyz/notes"),  # id обсуждения — hex
    ("github", "PUT", "/repos/o/r/collaborators/u"),
    ("github", "POST", "/repos/o/r/hooks"),
    ("github", "POST", "/repos/o/r/actions/workflows/x/dispatches"),
    ("github", "POST", "/repos/o/r/pulls/2%2F3/comments"),
    ("github", "PATCH", "/repos/o/r"),
    ("github", "POST", "/user/repos"),
])
def test_forge_write_outside_allowlist_refused(code, tmp_path, api, capsys, kind, method, path):
    jid = _forge_code(code, tmp_path, api, kind)
    rc, out = _helper(code, "forge", [method, path], jid, capsys=capsys)
    assert rc == 1 and api.requests == []
    assert "запись через API — только MR/PR, issues и комментарии" in out.err or "без . и .." in out.err, out.err


@pytest.mark.parametrize("kind,path", [
    ("gitlab", "/projects/1/variables"),
    ("gitlab", "/projects/1/variables/KEY"),
    ("gitlab", "/projects/1/hooks"),
    ("gitlab", "/projects/1/access_tokens"),
    ("gitlab", "/projects/1/deploy_tokens"),
    ("gitlab", "/groups/2/variables"),
    ("github", "/repos/o/r/actions/variables"),
    ("github", "/repos/o/r/environments/prod/variables"),
    ("github", "/repos/o/r/hooks"),
    ("github", "/repos/o/r/hooks/1"),
])
def test_forge_secret_reads_refused(code, tmp_path, api, capsys, kind, path):
    jid = _forge_code(code, tmp_path, api, kind, write=False)
    rc, out = _helper(code, "forge", ["GET", path], jid, capsys=capsys)
    assert rc == 1 and "чтение через API запрещено" in out.err and api.requests == []


def test_forge_out_parent_symlink_race(code, tmp_path, api, capsys, monkeypatch):
    """Каталог, подменённый на симлинк после проверки пути, — отказ записи, файл снаружи не появляется."""
    jid = _forge_code(code, tmp_path, api, "github")
    (code.root / "sub").mkdir()
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    real_check = helper._work_file

    def check_then_swap(ctx, cwd, val, what):
        p = real_check(ctx, cwd, val, what)
        if what == "--out":
            (code.root / "sub").rmdir()
            (code.root / "sub").symlink_to(outside)
        return p
    monkeypatch.setattr(helper, "_work_file", check_then_swap)
    rc, out = _helper(code, "forge", ["GET", "/x", "--out", "sub/x.json"], jid, capsys=capsys)
    assert rc == 1 and "не записан" in out.err and not (outside / "x.json").exists()



def test_forge_out_checked_before_request(code, tmp_path, api, capsys):
    """Мелочь 2 круга 3: --out открывается до HTTP-запроса — POST с --out в FIFO отказывает, а запрос не уходит."""
    jid = _forge_code(code, tmp_path, api, "github")
    (code.root / "body.json").write_text('{"title": "t"}')
    os.mkfifo(code.root / "o.fifo")
    rc, out = _helper(code, "forge", ["POST", "/repos/o/r/pulls", "--data", "body.json", "--out", "o.fifo"], jid,
                      capsys=capsys)
    assert rc == 1 and "не обычный файл" in out.err and api.requests == []
    assert _external(code, jid)[-1]["exit"] == 1

def test_open_out_chain(tmp_path):
    top = tmp_path / "w"
    (top / "a" / "b").mkdir(parents=True)
    os.close(helper.open_out(top, "a/b/x.json"))
    assert (top / "a" / "b" / "x.json").is_file()
    (top / "l").symlink_to(top / "a")
    with pytest.raises(OSError):
        helper.open_out(top, "l/y.json")
    (top / "a" / "lf").symlink_to(tmp_path / "target")
    with pytest.raises(OSError):
        helper.open_out(top, "a/lf")


def test_forge_data_deny_read(code, tmp_path, api, capsys, monkeypatch):
    jid = _forge_code(code, tmp_path, api, "github")
    tok = tmp_path / "secrets" / "forge.token"
    code.write(forge='"github"', forge_access='"task"', forge_write="true", forge_url=f'"{api.url}"',
               forge_token=f'"{tok}"', deny_read='["conf/secret*", "~/.config/team-service"]')
    (code.root / "conf").mkdir()
    (code.root / "conf" / "secret.json").write_text("{}")
    rc, out = _helper(code, "forge", ["POST", "/repos/o/r/issues", "--data", "conf/secret.json"], jid, capsys=capsys)
    assert rc == 1 and "deny_read" in out.err
    home = Path(os.environ["HOME"])
    (home / ".config" / "team-service").mkdir(parents=True)
    (code.root / "lnk.json").symlink_to(home / ".config" / "team-service" / "x.json")
    (home / ".config" / "team-service" / "x.json").write_text("{}")
    rc, out = _helper(code, "forge", ["POST", "/repos/o/r/issues", "--data", "lnk.json"], jid, capsys=capsys)
    assert rc == 1 and api.requests == []
    (code.root / "ok.json").write_text("{}")
    rc, out = _helper(code, "forge", ["POST", "/repos/o/r/issues", "--data", "ok.json"], jid, capsys=capsys)
    assert rc == 0, out.err


def test_helper_cwd_via_symlinked_path(code, tmp_path, api, capsys):
    """Каталог задачи записан через симлинк — помощник из настоящего каталога не получает ложного отказа."""
    jid = _forge_code(code, tmp_path, api, "github", write=False)
    alias = tmp_path / "alias"
    alias.symlink_to(code.root)
    d = code.jobs.read(jid)
    d["dir"] = str(alias)
    code.jobs.write(jid, d)
    rc, out = _helper(code, "forge", ["GET", "/user"], jid, cwd=code.root, capsys=capsys)
    assert rc == 0, out.err


def test_helper_env_path(code, hub_wshub, tmp_path):
    env = {"PATH": f"bin:{code.root}/node_modules/.bin:{code.root}:/usr/bin::/bin:/usr/bin"}
    out = jobs_mod.helper_env(env, code.root)
    assert out["PATH"] == f"{hub_wshub.parent}:/usr/bin:/bin"


def test_helper_env_refused_for_python_m(code, monkeypatch):
    monkeypatch.setattr(claude_cli, "wshub_exe", lambda: None)
    code.write(push='"task"')
    with pytest.raises(JobError, match="python -m wshub"):
        code.jobs.start(code.jobs.plan("proj", code.taskfile(), push=True, reason=REASON))
    # без разрешений — старт как обычно
    jid = code.jobs.start(code.jobs.plan("proj", code.taskfile()))["id"]
    wait_terminal(code.jobs, jid)


def test_no_glob_tool():
    assert "Glob" not in claude_cli.FILE_TOOLS and "Grep" not in claude_cli.FILE_TOOLS

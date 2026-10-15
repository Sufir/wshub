"""wshub-code, ТЗ WSH-3.3: снимок git на старте звена и блок «Вне кода» в итоге — коммиты, правки .git/hooks
и .git/config, вызовы docker; предупреждение первой строкой code_result и wshub code result; цепочка; отказ
gitsafe на старте. Настоящий claude не вызывается — tests/fake_claude.py."""
from __future__ import annotations

import json
import os
import subprocess

import pytest
from test_code import git, make_code, wait_terminal
from test_code_plan import _call, _wait_state

from wshub import code_cli
from wshub.jobs import GIT_START, META_WARNING, git_meta, meta_changes
from wshub.runner import DOCKER_RE, _docker_calls


@pytest.fixture
def code(tmp_path, monkeypatch):
    return make_code(tmp_path, monkeypatch)


def _run(code, monkeypatch, **env) -> str:
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    d = code.jobs.start(code.jobs.plan("proj", code.taskfile()))
    assert wait_terminal(code.jobs, d["id"])["state"] == "completed"
    return d["id"]


def _finish_rec(code, jid) -> dict:
    lines = [json.loads(x) for x in (code.state / "audit.jsonl").read_text().splitlines()]
    return next(r for r in lines if r["tool"] == "code_finish" and r["job"] == jid)["params"]


def _cli_result(code, jid, capsys) -> list[str]:
    assert code_cli.code_main("code", ["result", jid], code.cfg, code.state) == 0
    return capsys.readouterr().out.splitlines()


def test_empty_block_no_warning(code, capsys, monkeypatch):
    jid = _run(code, monkeypatch)
    snap = json.loads((code.jobs.job_dir(jid) / GIT_START).read_text())
    head = subprocess.run(["git", "-C", str(code.root), "rev-parse", "HEAD"], capture_output=True, text=True,
                          check=True).stdout.strip()
    assert snap["head"] == head and "error" not in snap
    assert snap["config"]["sha256"] and isinstance(snap["hooks"], dict)
    text = _call(code, "code_result", {"job": jid})
    assert not text.startswith("ВНИМАНИЕ")
    out = json.loads(text)
    branch = subprocess.run(["git", "-C", str(code.root), "symbolic-ref", "--short", "HEAD"], capture_output=True,
                            text=True, check=True).stdout.strip()
    assert out["outside_code"] == {"commits": [], "commits_count": 0, "docker": [], "docker_count": 0,
                                   "git_meta_changed": [], "head_start": head, "head_end": head,
                                   "branch_start": branch, "branch_end": branch}
    assert "outside_code" not in out["result"]  # блок один — на верхнем уровне
    assert _cli_result(code, jid, capsys)[0] == "{"
    p = _finish_rec(code, jid)
    assert (p["docker_count"], p["commits_count"], p["git_meta_changed"]) == (0, 0, [])


def test_docker_calls_listed_with_denial(code, monkeypatch):
    jid = _run(code, monkeypatch, FAKE_DOCKER="1")
    out = json.loads(_call(code, "code_result", {"job": jid}))["outside_code"]
    assert out["docker"] == ["docker compose up -d db",
                             "cd app && docker compose run --rm app pytest -v tests/x.py::t — отказ",
                             "docker compose run --rm app false — ошибка"]
    assert out["docker_count"] == 3 and out["git_meta_changed"] == []
    assert _finish_rec(code, jid)["docker_count"] == 3


def test_commits_listed(code, monkeypatch):
    jid = _run(code, monkeypatch, FAKE_COMMIT="1")
    out = json.loads(_call(code, "code_result", {"job": jid}))["outside_code"]
    assert out["commits_count"] == 1 and out["commits"][0].endswith(" fake commit dontAsk")
    assert _finish_rec(code, jid)["commits_count"] == 1


def test_git_meta_change_warns_first_line(code, capsys, monkeypatch):
    jid = _run(code, monkeypatch, FAKE_GIT_META="1")
    text = _call(code, "code_result", {"job": jid})
    first, body = text.split("\n", 1)
    assert first == META_WARNING
    changed = [".git/hooks/pre-commit — новый", ".git/config — добавлен ключ wshubfake.mark"]
    assert json.loads(body)["outside_code"]["git_meta_changed"] == changed
    assert _cli_result(code, jid, capsys)[0] == META_WARNING
    assert _finish_rec(code, jid)["git_meta_changed"] == changed


def test_chain_root_covers_both_links(code, monkeypatch):
    monkeypatch.setenv("FAKE_DOCKER", "1")
    monkeypatch.setenv("FAKE_COMMIT", "1")
    root = code.jobs.start(code.jobs.plan("proj", code.taskfile(size="M", risk="low")))["id"]
    snap = _wait_state(code.jobs, root, ("completed", "failed", "plan_ready"))
    assert snap["state"] == "completed" and len(snap["chain"]) == 2
    for x in snap["chain"]:  # у каждого звена — свой снимок на старте
        assert (code.jobs.job_dir(x["id"]) / GIT_START).is_file()
    out = json.loads(_call(code, "code_result", {"job": root}))["outside_code"]
    assert out["docker_count"] == 6 and len(out["docker"]) == 6
    assert out["commits_count"] == 2
    assert out["commits"][0].endswith("fake commit dontAsk") and out["commits"][1].endswith("fake commit plan")
    assert "no_data" not in out


def test_gitsafe_refused_at_start_task_runs(code, monkeypatch):
    git(code.root, "config", "core.fsmonitor", "true")
    jid = _run(code, monkeypatch)
    snap = json.loads((code.jobs.job_dir(jid) / GIT_START).read_text())
    assert snap["head"] is None and snap["error"].startswith("git_config_unsafe")
    out = json.loads(_call(code, "code_result", {"job": jid}))["outside_code"]
    assert any("снимок на старте" in e for e in out["errors"])
    assert out["git_meta_changed"] == []  # .git/hooks и .git/config хаб читает сам, без git


def test_hook_modified_deleted_symlink(tmp_path):
    repo = tmp_path / "r"
    (repo / ".git" / "hooks").mkdir(parents=True)
    hooks = repo / ".git" / "hooks"
    (hooks / "a").write_text("1")
    (hooks / "b").write_text("1")
    (hooks / "c").symlink_to("/bin/true")
    (repo / ".git" / "config").write_text("[core]\n")
    before = git_meta(repo)
    (hooks / "a").write_text("2")
    (hooks / "b").unlink()
    (hooks / "c").unlink()
    (hooks / "c").symlink_to("/bin/false")
    os.mkfifo(repo / ".git" / "config.new")
    (repo / ".git" / "config").unlink()
    os.rename(repo / ".git" / "config.new", repo / ".git" / "config")  # FIFO: чтение не должно повиснуть
    now = git_meta(repo)
    assert now["config"] == {"type": "not_regular"}
    assert meta_changes(before, now) == [".git/hooks/a — изменён", ".git/hooks/b — удалён",
                                         ".git/hooks/c — изменён", ".git/config — изменён"]


@pytest.mark.parametrize("cmd,hit", [
    ("docker compose ps", True), ("docker-compose up", True), ("cd x && docker ps", True),
    ("true || docker ps", True), ("a; docker ps", True), ("cat f | docker run -i img", True),
    ("echo 1\ndocker ps", True), ("ls docker", False), ("echo docker", False), ("dockerd", False),
    ("mydocker ps", False), ("./docker-compose.yml", False),
])
def test_docker_command_detection(cmd, hit):
    assert bool(DOCKER_RE.search(cmd)) is hit


def test_docker_limits(tmp_path):
    ev = tmp_path / "events.jsonl"
    rows = []
    for i in range(35):
        rows.append({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": f"u{i}", "name": "Bash", "input": {"command": "docker ps " + "x" * 300}}]}})
    ev.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    calls, n = _docker_calls(ev)
    assert n == 35 and len(calls) == 30 and all(len(c) == 200 for c in calls)


def _repo(tmp_path):
    repo = tmp_path / "r"
    (repo / ".git" / "hooks").mkdir(parents=True)
    (repo / ".git" / "config").write_text("[core]\n")
    return repo, repo / ".git" / "hooks"


def test_non_utf8_hook_name_serializes(tmp_path):
    """Ревью: имя с не-UTF-8 байтами не должно ронять запись git_start.json и result.json."""
    repo, hooks = _repo(tmp_path)
    before = git_meta(repo)
    os.close(os.open(os.fsencode(hooks) + b"/pre-commit\xff", os.O_CREAT | os.O_WRONLY, 0o755))
    now = git_meta(repo)
    changes = meta_changes(before, now)
    assert changes == [".git/hooks/pre-commit\\xff — новый"]
    json.dumps([now, changes], ensure_ascii=False).encode("utf-8")


@pytest.mark.nonroot
def test_unreadable_hook_and_dir_still_reported(tmp_path):
    """Ревью: хук без права чтения (исполняемый ELF запускается и так) и нечитаемый .git/hooks — не тишина."""
    repo, hooks = _repo(tmp_path)
    before = git_meta(repo)
    (hooks / "pre-push").write_text("x")
    (hooks / "pre-push").chmod(0o111)
    assert meta_changes(before, git_meta(repo)) == [".git/hooks/pre-push — новый"]
    hooks.chmod(0o311)
    try:
        assert meta_changes(before, git_meta(repo)) == [".git/hooks — не прочитан"]
    finally:
        hooks.chmod(0o755)


def test_hooks_dir_replaced_by_symlink(tmp_path):
    repo, hooks = _repo(tmp_path)
    before = git_meta(repo)
    other = tmp_path / "elsewhere"
    other.mkdir()
    hooks.rmdir()
    hooks.symlink_to(other)
    assert meta_changes(before, git_meta(repo)) == [".git/hooks — изменён"]


def test_meta_list_clipped():
    from wshub.jobs import outside_view
    oc = {"git_meta_changed": [f".git/hooks/h{i} — новый" for i in range(35)]}
    out = outside_view([{"id": "j", "result": {"outside_code": oc}}])
    assert len(out["git_meta_changed"]) == 31 and out["git_meta_changed"][-1] == "…ещё 5"

"""wshub-code, ТЗ WSH-3.4, часть Б: одни и те же «ходы» и «действия» в code_status, code_wait и code_result,
elapsed_s завершённой задачи, только существующие пути, описания инструментов, «отказ» и «ошибка», коммиты при
смене ветки и reset, ключи .git/config без значений, предел docker в ответе MCP. claude не вызывается."""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import time

import pytest
from test_code import git, make_code, wait_terminal
from test_code_plan import _call

from wshub import code_cli, code_events, code_server
from wshub.jobs import GIT_START

TS = "2026-10-15T08:00:{:02d}.000Z"


@pytest.fixture
def code(tmp_path, monkeypatch):
    return make_code(tmp_path, monkeypatch)


def _write(path, events):
    path.write_text("".join(json.dumps(e, ensure_ascii=False) + "\n" for e in events), encoding="utf-8")


def _a(mid, *blocks, parent=None, sec=0):
    return {"type": "assistant", "timestamp": TS.format(sec), "parent_tool_use_id": parent,
            "message": {"id": mid, "content": list(blocks)}}


def _use(uid, name, **inp):
    return {"type": "tool_use", "id": uid, "name": name, "input": inp}


def _res(uid, err=None, text="ok", parent=None):
    return {"type": "user", "parent_tool_use_id": parent, "timestamp": TS.format(0),
            "message": {"content": [{"type": "tool_result", "tool_use_id": uid, "is_error": err, "content": text}]}}


# Как у настоящего claude 2.1.291: блоки одного ответа API — отдельными событиями с тем же message.id,
# параллельные вызовы и результаты чередуются; субагент — с parent_tool_use_id; два прогона в одном файле
FIXTURE = [
    {"type": "system", "subtype": "init"},
    _a("m1", _use("u1", "Read", file_path="/w/src/a.py"), sec=1),
    _res("u1"),
    _a("m1", _use("u2", "Bash", command="git log --oneline | tail -50"), sec=2),
    {"type": "rate_limit_event", "rate_limit_info": {"unifiedWindows": {"five_hour": {"utilization": 0.18},
                                                                        "seven_day": {"utilization": 0.24}}}},
    _res("u2"),
    _a("m2", {"type": "thinking", "thinking": "…"}, sec=3),
    _a("m2", _use("u3", "Task", description="поиск"), sec=3),
    _a("s1", _use("s-u1", "Grep", pattern="TODO"), parent="u3", sec=4),
    _res("s-u1", parent="u3"),
    _res("u3"),
    {"type": "system", "subtype": "permission_denied", "tool_use_id": "u4"},
    _a("m3", _use("u4", "Bash", command="ls ~/.config/x"), sec=5),
    _res("u4", True, "Permission to use Bash with command ls ~/.config/x has been denied."),
    _a("m3", _use("u5", "Bash", command="git push origin main"), sec=6),
    _res("u5", True, "PreToolUse:Bash hook error: [python -m wshub.hook]: wshub: git push: …"),
    _a("m4", _use("u6", "Bash", command="pytest -q"), sec=7),
    _res("u6", True, "Exit code 1\nFAILED tests/x.py"),
    _a("m5", {"type": "text", "text": "готово"}, sec=8),
    {"type": "result", "num_turns": 7, "subtype": "success", "permission_denials": [{"tool_use_id": "u5"}]},
    {"type": "system", "subtype": "init"},
    _a("m6", _use("u7", "Edit", file_path="/w/src/b.py"), sec=9),
    _res("u7"),
    _a("m7", {"type": "text", "text": "ещё"}, sec=10),
    {"type": "result", "num_turns": 2, "subtype": "success", "permission_denials": []},
]


def test_turns_match_num_turns_not_message_ids(tmp_path):
    """Б1: ходы = результаты основного потока + прогон (= сумма num_turns), а не message.id и не события."""
    ev = tmp_path / "events.jsonl"
    _write(ev, FIXTURE)
    s = code_events.Scan("/w")
    s.feed_from(ev.read_bytes(), 0)
    assert s.turns == 7 + 2 == sum(r["num_turns"] for r in s.results)
    assert s.actions == 8  # все tool_use, включая субагента
    ids = {e["message"]["id"] for e in FIXTURE if e["type"] == "assistant"}
    assert len(ids) == 8 and sum(1 for e in FIXTURE if e["type"] == "assistant") == 11  # другие счёты
    assert s.rate == {"five_hour": 0.18, "seven_day": 0.24}


def test_marks_denial_vs_error(tmp_path):
    """Б5: «отказ» — права (permission_denied / текст) и хук; «ошибка» — прочий is_error."""
    ev = tmp_path / "events.jsonl"
    _write(ev, FIXTURE)
    s = code_events.scan(ev, "/w")
    assert s.marks == {"u4": "отказ", "u5": "отказ", "u6": "ошибка"}
    assert (s.denials, s.errors) == (2, 1)
    feed = {a["id"]: a for a in s.feed}
    assert feed["u1"]["arg"] == "src/a.py" and feed["u2"]["arg"] == "git log --oneline | tail -50"
    assert feed["s-u1"]["sub"] and feed["s-u1"]["arg"] == "TODO" and feed["u3"]["arg"] == "поиск"


def test_mark_from_late_denials(tmp_path):
    """Отказ, известный только по permission_denials итога (текст не опознан), — «отказ», а не «ошибка»."""
    ev = tmp_path / "events.jsonl"
    _write(ev, [_a("m", _use("x", "Bash", command="cmd")), _res("x", True, "blocked"),
                {"type": "result", "num_turns": 1, "permission_denials": [{"tool_use_id": "x"}]}])
    s = code_events.scan(ev)
    assert s.marks == {"x": "отказ"} and (s.denials, s.errors) == (1, 0)


def test_scan_incremental_and_partial_line(tmp_path):
    ev = tmp_path / "events.jsonl"
    lines = [json.dumps(e) + "\n" for e in FIXTURE]
    ev.write_text("".join(lines[:5]) + lines[5][:10])  # последняя строка недописана
    s = code_events.scan(ev, "/w")
    first = s.offset
    assert first == len("".join(lines[:5]).encode())
    ev.write_text("".join(lines))
    s = code_events.scan(ev, "/w")
    assert s.turns == 9 and s.offset == ev.stat().st_size and s.offset > first


def test_bash_arg_clipped():
    assert len(code_events.main_arg("Bash", {"command": "x" * 300}, None)) == 80
    assert code_events.main_arg("Bash", {"command": "a\n  b"}, None) == "a b"
    assert code_events.main_arg("Read", {"file_path": "/etc/passwd"}, "/w") == "/etc/passwd"  # вне cwd — как есть


def test_same_numbers_status_wait_result(code, capsys):
    jid = code.jobs.start(code.jobs.plan("proj", code.taskfile()))["id"]
    wait_terminal(code.jobs, jid)
    st = json.loads(_call(code, "code_status", {"job": jid}))
    wt = json.loads(_call(code, "code_wait", {"job": jid, "seconds": 1}))
    rs = json.loads(_call(code, "code_result", {"job": jid}))
    assert (st["turns"], st["actions"]) == (wt["turns"], wt["actions"]) == (rs["turns"], rs["actions"])
    assert st["turns"] == 1 and st["actions"] == 1  # фейк: init, текст, Read без результата
    assert wt["cap_s"] == code.hub.registry.get().code.glob.max_wait_s and wt["waited_s"] == 0


def test_elapsed_stops_at_finish(code):
    jid = code.jobs.start(code.jobs.plan("proj", code.taskfile()))["id"]
    wait_terminal(code.jobs, jid)
    a = code.jobs.snapshot(jid)["elapsed_s"]
    b = code.jobs.snapshot(jid, now=time.time() + 3600)["elapsed_s"]
    assert a == b and a < 60
    d = code.jobs.read(jid)  # старая задача без finished_ts — по finished
    d.pop("finished_ts")
    code.jobs.write(jid, d)
    assert code.jobs.snapshot(jid, now=time.time() + 3600)["elapsed_s"] < 60


def test_result_only_existing_paths(code):
    jid = code.jobs.start(code.jobs.plan("proj", code.taskfile()))["id"]
    wait_terminal(code.jobs, jid)
    files = json.loads(_call(code, "code_result", {"job": jid}))["files"]
    assert os.path.isfile(files["events"]) and os.path.isfile(files["stderr"])
    (code.jobs.job_dir(jid) / "events.jsonl").unlink()
    (code.jobs.job_dir(jid) / "stderr.log").unlink()
    files = json.loads(_call(code, "code_result", {"job": jid}))["files"]
    assert files["events"] == files["stderr"] == "нет (задача не дошла до запуска claude)"


def test_descriptions(code):
    tools = {t.name: " ".join(t.description.split())
             for t in asyncio.run(code_server.build_code_server(code.hub).list_tools())}
    assert "min(seconds, max_wait_s)" in tools["code_wait"] and "waited_s" in tools["code_wait"] \
        and "cap_s" in tools["code_wait"]
    for name in ("code_resume", "code_approve", "code_replan"):
        assert "одна строка ≤ 300 символов" in tools[name]
    for name in ("code_resume", "code_approve"):
        assert "длинную инструкцию — разделом в ТЗ, в note — ссылку на раздел" in tools[name]


# ---------- Б5–Б8: блок «Вне кода» ----------

def _run(code, monkeypatch, **env) -> str:
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    jid = code.jobs.start(code.jobs.plan("proj", code.taskfile()))["id"]
    assert wait_terminal(code.jobs, jid)["state"] == "completed"
    return jid


def test_docker_denial_vs_error(code, monkeypatch):
    jid = _run(code, monkeypatch, FAKE_DOCKER="1")
    out = json.loads(_call(code, "code_result", {"job": jid}))["outside_code"]
    assert out["docker"] == ["docker compose up -d db",
                             "cd app && docker compose run --rm app pytest -v tests/x.py::t — отказ",
                             "docker compose run --rm app false — ошибка"]


def _head(code) -> str:
    return subprocess.run(["git", "-C", str(code.root), "rev-parse", "HEAD"], capture_output=True, text=True,
                          check=True).stdout.strip()


def _branch(code) -> str:
    return subprocess.run(["git", "-C", str(code.root), "symbolic-ref", "--short", "HEAD"], capture_output=True,
                          text=True, check=True).stdout.strip()


def test_commits_head_and_branch(code, monkeypatch):
    start, br = _head(code), _branch(code)
    jid = _run(code, monkeypatch, FAKE_COMMIT="1")
    out = json.loads(_call(code, "code_result", {"job": jid}))["outside_code"]
    assert (out["head_start"], out["branch_start"], out["branch_end"]) == (start, br, br)
    assert out["head_end"] == _head(code) and out["commits_count"] == 1 and "head_moved" not in out


def test_commits_branch_switch_to_foreign(code, monkeypatch):
    """Смена ветки на уже существующую с чужим коммитом: «HEAD ушёл», чужого коммита в списке нет."""
    start, br = _head(code), _branch(code)
    git(code.root, "switch", "-qc", "other")
    subprocess.run(["git", "-C", str(code.root), "commit", "-q", "--allow-empty", "-m", "чужой"], check=True,
                   env={**os.environ, "GIT_AUTHOR_NAME": "o", "GIT_AUTHOR_EMAIL": "o@e", "GIT_COMMITTER_NAME": "o",
                        "GIT_COMMITTER_EMAIL": "o@e", "GIT_COMMITTER_DATE": "2020-01-01T00:00:00",
                        "GIT_AUTHOR_DATE": "2020-01-01T00:00:00"})
    git(code.root, "switch", "-q", br)
    jid = _run(code, monkeypatch, FAKE_SWITCH="other")
    out = json.loads(_call(code, "code_result", {"job": jid}))["outside_code"]
    assert out["head_moved"] == [f"HEAD ушёл: {br} {start[:9]} → other {_head(code)[:9]}"]
    assert out["commits"] == [] and out["commits_count"] == 0


def test_commits_new_branch_listed(code, monkeypatch):
    """Задача создала свою ветку и закоммитила — коммиты видны, «HEAD ушёл» нет."""
    git(code.root, "branch", "feat")
    jid = _run(code, monkeypatch, FAKE_SWITCH="feat", FAKE_COMMIT="1")
    out = json.loads(_call(code, "code_result", {"job": jid}))["outside_code"]
    assert out["branch_end"] == "feat" and out["commits_count"] == 1 and "head_moved" not in out


def test_commits_reset_back(code, monkeypatch):
    git(code.root, "commit", "-q", "--allow-empty", "-m", "второй")
    start, br = _head(code), _branch(code)
    jid = _run(code, monkeypatch, FAKE_RESET="1")
    out = json.loads(_call(code, "code_result", {"job": jid}))["outside_code"]
    assert out["head_moved"] == [f"HEAD ушёл: {br} {start[:9]} → {br} {_head(code)[:9]}"]
    assert out["commits_count"] == 0


def test_commits_empty_repo_from_root(code, monkeypatch):
    shutil.rmtree(code.root / ".git")
    git(code.root, "init", "-q")
    jid = _run(code, monkeypatch, FAKE_COMMIT="1")
    snap = json.loads((code.jobs.job_dir(jid) / GIT_START).read_text())
    assert snap["head"] is None and snap["empty"] is True and "error" not in snap
    out = json.loads(_call(code, "code_result", {"job": jid}))["outside_code"]
    assert out["commits_count"] == 1 and out["head_start"] is None and "errors" not in out


def test_config_keys_names_only(code, monkeypatch):
    """Б7: добавлен, удалён, изменён ключ — имена без значений; опасный помечен; секрет нигде не хранится."""
    git(code.root, "config", "wshub.gone", "1")
    git(code.root, "config", "wshub.changed", "a")
    git(code.root, "config", "remote.origin.url", "https://user:S3cr3tTok@host/r.git")

    monkeypatch.setenv("FAKE_SLEEP", "1")
    jid = code.jobs.start(code.jobs.plan("proj", code.taskfile()))["id"]  # снимок на старте уже записан
    git(code.root, "config", "--unset", "wshub.gone")
    git(code.root, "config", "wshub.changed", "b")
    git(code.root, "config", "wshub.added", "S3cr3tTok2")
    git(code.root, "config", "core.hooksPath", "/tmp/h")
    wait_terminal(code.jobs, jid)
    text = _call(code, "code_result", {"job": jid})
    changed = json.loads(text.split("\n", 1)[1])["outside_code"]["git_meta_changed"]
    assert changed == [".git/config — добавлен ключ core.hookspath (опасный)",
                       ".git/config — добавлен ключ wshub.added", ".git/config — изменён ключ wshub.changed",
                       ".git/config — удалён ключ wshub.gone"]
    snap = (code.jobs.job_dir(jid) / GIT_START).read_text()
    assert "remote.origin.url" in snap
    for blob in (snap, text, (code.jobs.job_dir(jid) / "result.json").read_text(), code.hub.audit_path.read_text()):
        assert "S3cr3tTok" not in blob


def test_docker_limit_mcp_10_cli_30(code, capsys):
    from wshub.jobs import outside_view
    oc = {"docker": [f"docker ps {i}" for i in range(30)], "docker_count": 35}
    assert len(outside_view([{"id": "j", "result": {"outside_code": oc}}], 10)["docker"]) == 10
    jid = code.jobs.start(code.jobs.plan("proj", code.taskfile()))["id"]
    wait_terminal(code.jobs, jid)
    d = code.jobs.read(jid)
    d["result"]["outside_code"] = {**d["result"]["outside_code"], **oc}
    code.jobs.write(jid, d)
    out = json.loads(_call(code, "code_result", {"job": jid}))["outside_code"]
    assert len(out["docker"]) == 10 and out["docker_count"] == 35
    assert code_cli.code_main("code", ["result", jid], code.cfg, code.state) == 0
    cli = json.loads(capsys.readouterr().out.split("\nоткрыть сессию:")[0])
    assert len(cli["outside_code"]["docker"]) == 30


def test_push_upstream_keys_not_a_warning(code, tmp_path, monkeypatch):
    """Ключи upstream, которые поставил сам wshub push -u (успешный пуш в external.jsonl), — не повод для
    предупреждения; тот же ключ другой ветки — повод."""
    from wshub.runner import _drop_push_upstream
    jd = tmp_path / "j"
    jd.mkdir()
    (jd / "external.jsonl").write_text(json.dumps({"cmd": "push", "args": ["origin", "feat-x"], "exit": 0}) + "\n"
                                       + json.dumps({"cmd": "push", "args": ["origin", "bad"], "exit": 1}) + "\n")
    lines = [".git/config — добавлен ключ branch.feat-x.merge", ".git/config — добавлен ключ branch.feat-x.remote",
             ".git/config — добавлен ключ branch.bad.remote", ".git/config — добавлен ключ core.hookspath (опасный)"]
    assert _drop_push_upstream(lines, jd) == lines[2:]
    assert _drop_push_upstream(lines, tmp_path) == lines  # нет учёта — всё как есть


# ---------- ревью круга 1, пп. 10–11: текст отказа хаба виден чату; удалённый cwd процесса хаба ----------

def test_hub_refusal_text_reaches_chat(code, capsys):
    """Отказ prefetch по git_config_unsafe: текст с ключом — в code_status, code_wait, code_result, CLI и карточке."""
    code.write(prefetch="true")
    git(code.root, "config", "core.fsmonitor", "true")
    jid = code.jobs.start(code.jobs.plan("proj", code.taskfile()))["id"]
    assert wait_terminal(code.jobs, jid)["state"] == "failed"
    assert code.jobs.read(jid)["reason"] == "git_config_unsafe"
    for name, args in (("code_status", {"job": jid}), ("code_wait", {"job": jid, "seconds": 1}),
                       ("code_result", {"job": jid})):
        out = json.loads(_call(code, name, args))
        assert "core.fsmonitor = true" in out["error"] and "--unset-all core.fsmonitor" in out["error"], name
    assert code_cli.code_main("code", ["result", jid], code.cfg, code.state) == 0
    assert "core.fsmonitor = true" in json.loads(capsys.readouterr().out.split("\nоткрыть сессию:")[0])["error"]
    srv = code_server.build_code_server(code.hub)
    out = asyncio.run(srv.call_tool("code_card_data", {"job": jid}))
    card = json.loads((out[0] if isinstance(out, tuple) else out)[0].text)
    assert "core.fsmonitor" in card["end"]["error"]


def test_no_error_field_when_clean(code):
    jid = code.jobs.start(code.jobs.plan("proj", code.taskfile()))["id"]
    wait_terminal(code.jobs, jid)
    assert "error" not in json.loads(_call(code, "code_result", {"job": jid}))


def test_deleted_cwd_unsafe_keys_and_prefetch(code, tmp_path, monkeypatch):
    """Процесс хаба с удалённым рабочим каталогом: unsafe_keys и prefetch задачи проходят (ревью, п. 11)."""
    from wshub import gitsafe
    gone = tmp_path / "gone"
    gone.mkdir()
    monkeypatch.chdir(gone)
    gone.rmdir()
    assert gitsafe.unsafe_keys("git", code.root / ".git", dict(os.environ)) == []
    code.write(prefetch="true")
    jid = code.jobs.start(code.jobs.plan("proj", code.taskfile()))["id"]  # надзиратель — с cwd каталога задачи
    snap = wait_terminal(code.jobs, jid)
    assert snap["state"] == "completed", code.jobs.read(jid).get("error")


def test_git_launch_failure_is_not_unsafe_config(code, tmp_path):
    """Сбой запуска git («Unable to read current working directory») — prefetch с понятной причиной, а не
    git_config_unsafe; нечитаемый конфиг — «конфиг не прочитан»; плохой ключ — «плохой ключ»."""
    from wshub import gitsafe
    fake = tmp_path / "fakegit"
    fake.write_text("#!/bin/sh\necho 'fatal: Unable to read current working directory: No such file or directory' >&2\n"
                    "exit 128\n")
    fake.chmod(0o755)
    with pytest.raises(gitsafe.GitUnsafe) as e:
        gitsafe.unsafe_keys(str(fake), code.root / ".git", dict(os.environ))
    assert e.value.reason == "prefetch" and "рабочий каталог процесса хаба удалён" in str(e.value)
    (code.root / ".git" / "config").write_text("[broken\n")
    with pytest.raises(gitsafe.GitUnsafe) as e:
        gitsafe.unsafe_keys("git", code.root / ".git", dict(os.environ))
    assert e.value.reason == "git_config_unsafe" and str(e.value).startswith("конфиг не прочитан")

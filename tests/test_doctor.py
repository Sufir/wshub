"""wshub doctor на подменённых путях: Windows-профиль, /proc, git-репозиторий — во временной папке."""
import json
import os
import sys

import pytest

from wshub import doctor
from wshub.doctor import Ctx
from wshub.runtime import proc_start
from wshub.server import main


def write_json(p, data):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data), encoding="utf-8")


@pytest.fixture
def fake(env, tmp_path):
    """Профиль Windows с MSIX-пакетом и классическим конфигом, пустой /proc, git-репозиторий с HEAD."""
    users = tmp_path / "Users"
    appdata = users / "me" / "AppData"
    (users / "Public").mkdir(parents=True)
    exe = tmp_path / "bin" / "wshub"
    exe.parent.mkdir()
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)
    entry = {"command": "wsl.exe", "args": ["-d", "Ubuntu-test", "--", str(exe)]}
    msix = appdata / "Local/Packages/Claude_abc123/LocalCache/Roaming/Claude/claude_desktop_config.json"
    write_json(msix, {"mcpServers": {"git": {}, "wshub": entry}, "preferences": {}})
    classic = appdata / "Roaming/Claude/claude_desktop_config.json"
    write_json(classic, {"mcpServers": {"git": {}, "fs-old": {}}})
    repo = tmp_path / "repo"
    (repo / ".git/refs/heads").mkdir(parents=True)
    (repo / ".git/HEAD").write_text("ref: refs/heads/main\n")
    (repo / ".git/refs/heads/main").write_text("b" * 40 + "\n")
    proc = tmp_path / "proc"
    proc.mkdir()
    ctx = Ctx(config=env.cfg, state=env.state, home=tmp_path / "home", win_users=users, proc=proc, repo=repo,
              distro="Ubuntu-test", self_pid=999999, protected_overlap=env.hub.protected_overlap)
    return ctx, msix, classic, exe


def by_id(res):
    return {c["id"]: c for c in res["checks"]}


def test_desktop_msix_entry(fake):
    ctx, msix, classic, exe = fake
    c = by_id(doctor.run(ctx))["desktop"]
    assert c["status"] == "ok", c
    text = "\n".join(c["detail"])
    assert f"[читается Desktop] {msix} — wshub → wsl.exe -d Ubuntu-test -- {exe}" in text
    assert f"[не читается] {classic} — записи wshub нет (серверы: fs-old, git)" in text
    assert "Public" not in text


def test_desktop_entry_only_in_classic(fake):
    ctx, msix, classic, exe = fake
    write_json(msix, {"mcpServers": {"git": {}}})
    write_json(classic, {"mcpServers": {"wshub": {"command": str(exe)}}})
    c = by_id(doctor.run(ctx))["desktop"]
    assert c["status"] == "fail" and "читается Desktop" in c["fix"]
    assert any("[не читается]" in line and "wshub →" in line for line in c["detail"])


def test_desktop_classic_install(fake):
    ctx, msix, classic, exe = fake
    import shutil
    shutil.rmtree(msix.parents[4])  # пакета нет
    (ctx.win_users / "me/AppData/Local/AnthropicClaude").mkdir()
    write_json(classic, {"mcpServers": {"wshub": {"command": str(exe)}}})
    c = by_id(doctor.run(ctx))["desktop"]
    assert c["status"] == "ok" and f"[читается Desktop] {classic}" in c["detail"][0]


def test_desktop_broken_entry(fake):
    ctx, msix, classic, exe = fake
    write_json(msix, {"mcpServers": {"wshub": {"command": "wsl.exe",
                                               "args": ["-d", "Other", "--", str(exe) + "-missing"]}}})
    c = by_id(doctor.run(ctx))["desktop"]
    assert c["status"] == "fail"
    assert "не существует" in c["detail"][0] and "дистрибутив Other" in c["detail"][0]
    msix.write_text("{not json")
    c = by_id(doctor.run(ctx))["desktop"]
    assert c["status"] == "fail" and "не читается как JSON" in c["detail"][0]


def test_no_desktop(fake, tmp_path):
    ctx = fake[0]
    ctx.win_users = tmp_path / "none"
    assert by_id(doctor.run(ctx))["desktop"]["status"] == "info"


def test_registry_and_paths(fake, env):
    ctx = fake[0]
    res = by_id(doctor.run(ctx))
    assert res["registry"]["status"] == "ok" and "проектов 2" in res["registry"]["detail"][0]
    assert res["paths"]["status"] == "ok"
    env.workspaces["gone"] = {"path": str(env.tmp / "gone"), "mode": "ro"}
    env.workspaces["self"] = {"path": str(env.state), "mode": "ro"}
    env.state.mkdir(exist_ok=True)
    env.write_registry()
    res = by_id(doctor.run(ctx))
    assert res["paths"]["status"] == "fail" and "gone, self" in res["paths"]["fix"]
    env.cfg.write_text("[defaults\n")
    res = by_id(doctor.run(ctx))
    assert res["registry"]["status"] == "fail" and res["paths"]["status"] == "info"


def test_processes_and_stale_head(fake, env):
    ctx = fake[0]
    run = env.state / "run"
    pid = 4242
    (ctx.proc / str(pid)).mkdir()
    (ctx.proc / str(pid) / "stat").write_text(f"{pid} (python) S " + " ".join(["0"] * 18) + " 777 0 0\n")
    (ctx.proc / str(pid) / "cmdline").write_bytes(b"python\0-m\0wshub\0")
    write_json(run / f"{pid}.json", {"pid": pid, "proc_start": "777", "started": 1.0, "head": "a" * 40,
                                     "handles": [{"hid": "1" * 16}], "last_call": None})
    write_json(run / "5555.json", {"pid": 5555, "proc_start": "1", "started": 1.0, "head": "b" * 40})  # мёртвый
    res = doctor.run(ctx)
    c = by_id(res)["procs"]
    assert [r["pid"] for r in res["processes"]["registered"]] == [pid]
    assert res["processes"]["registered"][0]["stale"] and res["processes"]["legacy"] == []
    assert c["status"] == "warn" and "перезапусти Desktop" in c["fix"] and "(устарел)" in c["detail"][0]

    write_json(run / f"{pid}.json", {"pid": pid, "proc_start": "777", "started": 1.0, "head": "b" * 40})
    assert by_id(doctor.run(ctx))["procs"]["status"] == "ok"
    write_json(run / f"{pid}.json", {"pid": pid, "proc_start": "778", "started": 1.0, "head": "b" * 40})  # pid занят другим
    res = doctor.run(ctx)
    assert res["processes"]["registered"] == []
    # процесс wshub без run-файла — старая версия
    assert [r["pid"] for r in res["processes"]["legacy"]] == [pid]
    assert by_id(res)["procs"]["status"] == "warn"
    (ctx.proc / str(pid) / "cmdline").write_bytes(b"/home/u/.local/bin/wshub\0doctor\0")
    assert by_id(doctor.run(ctx))["procs"]["status"] == "info"


def test_storage(fake, env):
    ctx = fake[0]
    res = by_id(doctor.run(ctx))
    assert res["audit"]["status"] == "info" and res["backup"]["status"] == "info"
    ws = env.open("payload", "rw")
    env.hub.write(ws, "a.txt", "new")
    res = by_id(doctor.run(ctx))
    assert res["audit"]["status"] == "ok" and "байт" in res["audit"]["detail"][0]
    assert res["backup"]["status"] == "ok" and "файлов 1" in res["backup"]["detail"][0]


def test_report_format(fake):
    out = doctor.format_report(doctor.run(fake[0]))
    assert "[OK  ] Реестр" in out and "итог:" in out and "→" in out


def test_cli(env, monkeypatch, capsys):
    monkeypatch.setenv("WSHUB_CONFIG", str(env.cfg))
    monkeypatch.setenv("WSHUB_STATE", str(env.state))
    with pytest.raises(SystemExit) as e:
        main(["doctor"])
    out = capsys.readouterr().out
    assert "Реестр" in out and "Запись wshub в Claude Desktop" in out and "итог:" in out
    assert e.value.code in (0, 1)
    with pytest.raises(SystemExit) as e:
        main(["bogus"])
    assert e.value.code == 2 and "wshub doctor" in capsys.readouterr().err


def test_real_proc_start():
    assert proc_start(os.getpid()) is not None or sys.platform != "linux"

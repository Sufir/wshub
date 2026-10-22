"""wshub-code, ТЗ WSH-3.8, часть Э: концы строк — до (eol_mixed и строка блока «От хаба») и после (eol_changed в
блоке «Вне кода», строка ⚠ в итоге, CLI и карточке). git — настоящий: что --numstat учитывает --ignore-cr-at-eol,
проверяет test_eol_changed_kinds."""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess

import pytest
from test_code import git, make_code, wait_terminal

from wshub import code_cli, code_server, gitsafe, runner
from wshub import jobs as jobs_mod

LINE = ("Edit приводит концы строк всего файла к одному виду — после правки такого файла сверь `git diff --stat` с "
        "`git diff --ignore-cr-at-eol --stat` и верни концы строк, которые не правил.")


@pytest.fixture
def code(tmp_path, monkeypatch):
    return make_code(tmp_path, monkeypatch)


def _commit(code, files: dict[str, bytes]):
    for rel, data in files.items():
        p = code.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
    git(code.root, "-c", "core.autocrlf=false", "add", "-f", *files)
    git(code.root, "-c", "core.autocrlf=false", "commit", "-qm", "files")


def _call(code, name, args) -> str:
    out = asyncio.run(code_server.build_code_server(code.hub).call_tool(name, args))
    content = out[0] if isinstance(out, tuple) else out
    return content[0].text


MIXED = b"a\r\nb\nc\r\n"


# ---------- Э1. до ----------

def test_eol_mixed_listed_in_plan_block_and_job(code):
    _commit(code, {"docs/mixed.txt": MIXED, "lf.txt": b"a\nb\n", "crlf.txt": b"a\r\nb\r\n"})
    p = code.jobs.plan("proj", code.taskfile())
    assert p.eol_mixed == ["docs/mixed.txt"]
    assert f"\n- Смешанные концы строк (CRLF и LF в одном файле): docs/mixed.txt. {LINE}\n" in p.prompt
    d = code.jobs.start(p)
    assert d["eol_mixed"] == ["docs/mixed.txt"]
    wait_terminal(code.jobs, d["id"])
    view = code.jobs.result_view(d["id"])
    assert (view["eol_mixed"], view["eol_mixed_count"]) == (["docs/mixed.txt"], 1)


def test_eol_mixed_none_no_line(code):
    _commit(code, {"lf.txt": b"a\nb\n", "crlf.txt": b"a\r\nb\r\n"})
    p = code.jobs.plan("proj", code.taskfile())
    assert p.eol_mixed == [] and "Смешанные концы строк" not in p.prompt


def test_eol_mixed_25_files_block_20(code):
    _commit(code, {f"m/{i:02d}.txt": MIXED for i in range(25)})
    p = code.jobs.plan("proj", code.taskfile())
    assert len(p.eol_mixed) == 25
    line = next(x for x in p.prompt.splitlines() if x.startswith("- Смешанные концы строк"))
    shown = line.split("одном файле): ", 1)[1].split(" (и ещё", 1)[0].split(", ")
    assert len(shown) == 20 and shown[-1] == "m/19.txt" and "(и ещё 5)." in line


def test_eol_mixed_job_json_capped(code):
    p = code.jobs.plan("proj", code.taskfile())
    p.eol_mixed = [f"f{i}.txt" for i in range(250)]
    d = code.jobs.start(p)
    assert len(d["eol_mixed"]) == jobs_mod.EOL_JOB_MAX == 200
    wait_terminal(code.jobs, d["id"])


@pytest.mark.parametrize("fail", ["oserror", "rc", "unsafe"])
def test_eol_mixed_git_failure_warning(code, monkeypatch, fail):
    _commit(code, {"mixed.txt": MIXED})
    real = jobs_mod.gitsafe.run

    def run(git_, cwd, args, *a, **kw):
        if args[:2] != ["ls-files", "--eol"]:
            return real(git_, cwd, args, *a, **kw)
        if fail == "oserror":
            raise OSError("git не запустился")
        if fail == "unsafe":
            raise gitsafe.GitUnsafe("git_config_unsafe", "плохой ключ", ["core.fsmonitor"])
        return subprocess.CompletedProcess(args, 128, "", "fatal: сбой")

    monkeypatch.setattr(jobs_mod.gitsafe, "run", run)
    p = code.jobs.plan("proj", code.taskfile())
    assert "Смешанные концы строк" not in p.prompt and p.eol_mixed == []
    assert [w for w in p.warnings if w.startswith("смешанные концы строк не посчитаны: ")], p.warnings


def test_block_eol_no_paths_when_one_does_not_fit(monkeypatch):
    """Последняя ступень сокращения: и один путь не влезает — «файлов: N, список — eol_mixed в job.json», хвост
    инструкции цел (предел блока уменьшен, чтобы до неё дойти)."""
    monkeypatch.setattr(jobs_mod, "HUB_BLOCK_MAX", 560)
    block = jobs_mod.hub_block(None, (), None, [], "p\n", mixed=["q" * 300 + str(i) for i in range(5)])
    assert len(block.encode("utf-8")) <= 560
    assert "одном файле): файлов: 5, список — `eol_mixed` в job.json. Edit" in block
    assert block.rstrip("\n").endswith(LINE)


def test_block_eol_shortened_with_hidden():
    hidden = [f"h-{i}/" + "z" * 120 for i in range(25)]
    mixed = [f"m-{i}/" + "y" * 120 for i in range(25)]
    block = jobs_mod.hub_block(None, (), None, hidden, "p\n", switch="task", mixed=mixed)
    assert len(block.encode("utf-8")) <= jobs_mod.HUB_BLOCK_MAX
    assert "- Смешанные концы строк (CRLF и LF в одном файле): m-0/" in block and "Песочница скрывает" in block
    assert block.rstrip("\n").endswith(LINE) and jobs_mod.HUB_SWITCH["task"] in block
    # пути короче один за другим из обоих списков: показано поровну (± 1)
    nh = block.split("файлы: ", 1)[1].split(". В `git", 1)[0].count("h-")
    ne = block.split("одном файле): ", 1)[1].split(". Edit", 1)[0].count("m-")
    assert abs(nh - ne) <= 1 and 0 < ne < 20 and nh > 0


# ---------- Э2. после ----------

def _three(code):
    """a — только концы строк, b — правка содержимого без смены концов, c — правка плюс смена концов во всём файле,
    d — без изменений."""
    body = b"".join(b"line %d\n" % i for i in range(10))
    _commit(code, {"a.txt": body, "b.txt": body, "c.txt": body, "d.txt": body})
    (code.root / "a.txt").write_bytes(body.replace(b"\n", b"\r\n"))
    (code.root / "b.txt").write_bytes(body.replace(b"line 3", b"LINE 3"))
    (code.root / "c.txt").write_bytes(body.replace(b"line 3", b"LINE 3").replace(b"\n", b"\r\n"))


def test_eol_changed_kinds(code):
    """Проверка на настоящем git (Э2.2): --numstat учитывает --ignore-cr-at-eol."""
    _three(code)
    head = subprocess.run(["git", "-C", str(code.root), "rev-parse", "HEAD"], capture_output=True, text=True,
                          check=True).stdout.strip()
    git_ = shutil.which("git")
    assert runner._eol_changed(git_, code.root, dict(os.environ), head) == (["a.txt", "c.txt"], 2)


def test_eol_changed_in_outside_code_and_errors(code, monkeypatch, tmp_path):
    _three(code)
    jdir = tmp_path / "jd"
    jdir.mkdir()
    # снимок на старте хранит HEAD (последний коммит), правки рабочего дерева в нём не участвуют
    (jdir / jobs_mod.GIT_START).write_text(json.dumps(jobs_mod.start_snapshot(code.root, dict(os.environ))))
    out = runner._outside_code(shutil.which("git"), code.root, dict(os.environ), jdir)
    assert (out["eol_changed"], out["eol_changed_count"]) == (["a.txt", "c.txt"], 2) and "errors" not in out

    def boom(*a, **kw):
        raise RuntimeError("git diff --numstat вернул 128: fatal")

    monkeypatch.setattr(runner, "_numstat", boom)
    out = runner._outside_code(shutil.which("git"), code.root, dict(os.environ), jdir)
    assert "eol_changed" not in out and "концы строк: git diff --numstat вернул 128: fatal" in out["errors"]


def test_eol_changed_limit(code):
    body = b"x\ny\n"
    _commit(code, {f"f{i:02d}.txt": body for i in range(35)})
    for i in range(35):
        (code.root / f"f{i:02d}.txt").write_bytes(b"x\r\ny\r\n")
    head = subprocess.run(["git", "-C", str(code.root), "rev-parse", "HEAD"], capture_output=True, text=True,
                          check=True).stdout.strip()
    files, n = runner._eol_changed(shutil.which("git"), code.root, dict(os.environ), head)
    assert n == 35 and len(files) == jobs_mod.META_MAX + 1 and files[-1] == "…ещё 5"


def test_eol_in_result_cli_and_card(code, capsys):
    """Задача стартует с рабочим деревом, где a.txt сменил только концы строк, а mixed.txt — со смешанными: итог
    показывает обе строки в code_result, wshub code result и данных карточки."""
    _commit(code, {"a.txt": b"one\ntwo\n", "mixed.txt": MIXED})
    (code.root / "a.txt").write_bytes(b"one\r\ntwo\r\n")
    jid = code.jobs.start(code.jobs.plan("proj", code.taskfile()))["id"]
    wait_terminal(code.jobs, jid)
    warn = "⚠ концы строк изменены без правки содержимого: a.txt — проверь `git diff --ignore-cr-at-eol`"
    view = code.jobs.result_view(jid)
    assert view["eol_warning"] == warn and view["outside_code"]["eol_changed"] == ["a.txt"]
    assert view["eol_mixed"] == ["mixed.txt"]
    out = json.loads(_call(code, "code_result", {"job": jid}))
    assert out["eol_warning"] == warn and out["eol_mixed"] == ["mixed.txt"]
    assert out["outside_code"]["eol_changed_count"] == 1
    assert code_cli.code_main("code", ["result", jid], code.cfg, code.state) == 0
    cli = capsys.readouterr().out
    assert warn in cli and '"eol_mixed": [' in cli
    end = code_server.card_data(code.jobs, jid, 0)["end"]
    assert end["eol_warning"] == warn and (end["eol_mixed"], end["eol_mixed_count"]) == (["mixed.txt"], 1)


def test_eol_warning_more():
    oc = {"eol_changed": ["a", "b", "…ещё 3"], "eol_changed_count": 5}
    assert jobs_mod.eol_warning(oc) == \
        "⚠ концы строк изменены без правки содержимого: a, b (и ещё 3) — проверь `git diff --ignore-cr-at-eol`"
    assert jobs_mod.eol_warning({}) is None


def test_card_html_renders_eol():
    from pathlib import Path
    html = (Path(code_server.__file__).parent / "code_card.html").read_text()
    assert "end.eol_warning" in html and "end.eol_mixed_count" in html

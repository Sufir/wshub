"""wshub-code, ТЗ WSH-3.8, часть К: одна живая задача на рабочий каталог (любой профиль, все проекты реестра)."""
from __future__ import annotations

import os

import pytest
from test_code import SUB_DIRS, add_sub, make_code, wait_terminal
from test_code_plan import _wait_state

from wshub import jobs as jobs_mod
from wshub.jobs import JobError
from wshub.runtime import proc_start


@pytest.fixture
def code(tmp_path, monkeypatch):
    return make_code(tmp_path, monkeypatch)


def _live(code, profile="task", dir=None, project="proj", **over) -> str:
    """job.json живой задачи: надзиратель — этот процесс (effective_state → running)."""
    jid = jobs_mod.new_id()
    code.jobs.job_dir(jid).mkdir(parents=True)
    code.jobs.write(jid, {"id": jid, "project": project, "dir": str(dir or code.root), "profile": profile,
                          "state": "running", "sup_pid": os.getpid(), "sup_start": proc_start(os.getpid()), **over})
    return jid


def _job_dirs(code) -> set[str]:
    return {p.name for p in code.jobs.root.iterdir() if p.is_dir()}


def test_second_task_in_same_dir_refused(code):
    code.write(max_parallel="3")
    jid = _live(code)
    before = _job_dirs(code)
    with pytest.raises(JobError) as e:
        code.jobs.start(code.jobs.plan("proj", code.taskfile()))
    assert str(e.value) == (f"в каталоге {code.root.resolve()} уже идёт задача {jid} (task, running) — одна живая задача на "
                            "каталог; дождись её (code_wait) или останови (code_cancel)")
    assert _job_dirs(code) == before  # отказ не оставляет каталога задачи


@pytest.mark.parametrize("live,new", [("recon", "task"), ("review", "task"), ("task", "recon"), ("task", "review"),
                                      ("recon", "review")])
def test_any_profile_blocks_and_is_blocked(code, live, new):
    code.write(max_parallel="3")
    jid = _live(code, profile=live)
    with pytest.raises(JobError, match=f"уже идёт задача {jid} \\({live}, running\\)"):
        code.jobs.start(code.jobs.plan("proj", code.taskfile(profile=new)))


def test_other_dir_of_same_project_starts(code):
    add_sub(code)
    code.write(max_parallel="2", dirs=SUB_DIRS)
    _live(code)
    d = code.jobs.start(code.jobs.plan("proj", code.taskfile(dir="sub")))
    assert d["dir"] == str((code.root / "sub").resolve())
    assert wait_terminal(code.jobs, d["id"])["state"] == "completed"


def test_same_dir_in_two_projects_refused(code):
    """Корни проектов реестра пересекаются: каталог тот же — отказ, хотя max_parallel у проектов свой."""
    code.cfg.write_text(code.cfg.read_text() + f'\n[workspace.other]\npath = "{code.root}"\nmode = "rw"\n\n'
                        '[workspace.other.code]\nenabled = true\ndirs = ["."]\nprompts = "prompts"\n',
                        encoding="utf-8")
    jid = _live(code, project="other")
    with pytest.raises(JobError, match=f"уже идёт задача {jid}"):
        code.jobs.start(code.jobs.plan("proj", code.taskfile()))


def test_dir_compared_by_realpath(code, tmp_path):
    """Каталог живой задачи записан через симлинк — тот же каталог."""
    code.write(max_parallel="3")
    link = tmp_path / "link"
    link.symlink_to(code.root)
    jid = _live(code, dir=link)
    with pytest.raises(JobError, match=f"уже идёт задача {jid}"):
        code.jobs.start(code.jobs.plan("proj", code.taskfile()))


def test_orphaned_blocks_terminal_does_not(code):
    code.write(max_parallel="3")
    # надзиратель мёртв, Code жив (этот процесс) — orphaned
    jid = _live(code, sup_pid=2 ** 22 + 7, sup_start="x", pid=os.getpid(), proc_start=proc_start(os.getpid()))
    assert code.jobs.effective_state(code.jobs.read(jid)) == "orphaned"
    with pytest.raises(JobError, match=f"уже идёт задача {jid} \\(task, orphaned\\)"):
        code.jobs.start(code.jobs.plan("proj", code.taskfile()))
    d = code.jobs.read(jid)
    d.update(state="completed", sup_pid=None, pid=None)
    code.jobs.write(jid, d)
    wait_terminal(code.jobs, code.jobs.start(code.jobs.plan("proj", code.taskfile()))["id"])


def test_chain_links_and_resume_pass(code):
    """План в plan_ready не живой — исполнение в том же каталоге стартует; code_resume — тоже."""
    root = code.jobs.start(code.jobs.plan("proj", code.taskfile(size="L", risk="low")))["id"]
    _wait_state(code.jobs, root, ("plan_ready",))
    child = code.jobs.approve(root)
    assert child["phase"] == "execute" and child["dir"] == str(code.root.resolve())
    wait_terminal(code.jobs, child["id"])
    r = code.jobs.resume(child["id"], "cont", note="дальше")
    assert wait_terminal(code.jobs, r["id"])["state"] == "completed"

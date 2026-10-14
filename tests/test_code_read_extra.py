"""wshub-code: read_extra — Read вне dir по путям и glob от корня проекта (ТЗ WSH-3.1, п. 2.2 и 4.2).
Настоящий claude не вызывается — вместо него tests/fake_claude.py."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from test_code import GOLDEN, _golden_settings, make_code, wait_terminal
from test_code_plan import _call

from wshub import claude_cli
from wshub.registry import CodeGlobal, ProjectCode, RegistryError, parse


@pytest.fixture
def code(tmp_path, monkeypatch):
    c = make_code(tmp_path, monkeypatch)
    (c.root / "svc").mkdir()
    (c.root / "CLAUDE.md").write_text("# правила\n")
    (c.root / ".agents").mkdir()
    (c.root / ".agents" / "testing.md").write_text("# тесты\n")
    return c


def _settings_file(code, jid) -> dict:
    return json.loads((code.jobs.job_dir(jid) / "settings.json").read_text())


def _extra_rules(s: dict) -> list[str]:
    return [a for a in s["permissions"]["allow"] if a.startswith("Read(//")]


def test_read_extra_rules_in_settings(code):
    """Каждый элемент — allow Read(//<абсолютный путь>); deny, allowRead песочницы и denyWrite не меняются."""
    code.write(read_extra='["CLAUDE.md", ".agents/*.md"]')
    d = code.jobs.start(code.jobs.plan("proj", code.taskfile(profile="recon")))
    wait_terminal(code.jobs, d["id"])
    s = _settings_file(code, d["id"])
    assert _extra_rules(s) == [f"Read(/{code.root.resolve()}/CLAUDE.md)", f"Read(/{code.root.resolve()}/.agents/*.md)"]
    code.write()
    d0 = code.jobs.start(code.jobs.plan("proj", code.taskfile(profile="recon")))
    wait_terminal(code.jobs, d0["id"])
    s0 = _settings_file(code, d0["id"])
    assert s["permissions"]["deny"] == s0["permissions"]["deny"]  # deny-правила остаются и побеждают
    assert s["sandbox"] == s0["sandbox"]  # в allowRead песочницы read_extra не попадает
    assert [a for a in s["permissions"]["allow"] if a not in _extra_rules(s)] == s0["permissions"]["allow"]


@pytest.mark.parametrize("profile", ["recon", "task", "review"])
def test_read_extra_all_profiles_and_plan(profile):
    """Правила есть в каждом профиле и в фазе плана; пустой список — ни одного лишнего правила (эталон)."""
    proj = ProjectCode(enabled=True, dirs=(".",), prompts="p", deny_edit=("docker-compose*.yml",))
    protected = [Path("/home/u/.config/wshub"), Path("/home/u/.local/state/wshub")]
    extra = ["/home/u/CLAUDE.md", "/home/u/.agents/*.md"]
    rules = ["Read(//home/u/CLAUDE.md)", "Read(//home/u/.agents/*.md)"]
    for phase in (None, "plan"):
        s = claude_cli.settings(profile, "org/TASK.md", CodeGlobal(), proj, protected, protected,
                                ["/py", "-m", "wshub.hook"], Path("/home/u"), Path("/home/u/proj"), phase=phase,
                                read_extra=extra)
        assert _extra_rules(s) == rules, phase
        assert not set(extra) & set(s["sandbox"]["filesystem"]["allowRead"])
        empty = claude_cli.settings(profile, "org/TASK.md", CodeGlobal(), proj, protected, protected,
                                    ["/py", "-m", "wshub.hook"], Path("/home/u"), Path("/home/u/proj"),
                                    phase=phase)
        assert empty == (_golden_settings(profile) if phase is None else GOLDEN["plan"][profile])
        assert [a for a in s["permissions"]["allow"] if a not in rules] == empty["permissions"]["allow"]


def test_read_extra_in_plan_phase_job(code):
    """Задача с планом: settings фазы плана из задачи тоже несут правила read_extra."""
    code.write(read_extra='["CLAUDE.md"]')
    root = code.jobs.start(code.jobs.plan("proj", code.taskfile(size="L", risk="low")))["id"]
    snap = wait_terminal(code.jobs, root)
    assert snap["state"] == "plan_ready"
    sp = _settings_file(code, root)
    assert sp["useAutoModeDuringPlan"] is False and _extra_rules(sp) == [f"Read(/{code.root.resolve()}/CLAUDE.md)"]
    child = code.jobs.approve(root)  # звено исполнения (start с resume) — тоже с read_extra
    wait_terminal(code.jobs, child["id"])
    se = _settings_file(code, child["id"])
    assert "useAutoModeDuringPlan" not in se and _extra_rules(se) == [f"Read(/{code.root.resolve()}/CLAUDE.md)"]


def test_read_extra_empty_no_rules(code):
    d = code.jobs.start(code.jobs.plan("proj", code.taskfile()))
    wait_terminal(code.jobs, d["id"])
    assert _extra_rules(_settings_file(code, d["id"])) == []
    assert parse(code.cfg.read_text()).code.projects["proj"].read_extra == ()


@pytest.mark.parametrize("item", ["/etc/passwd", "../CLAUDE.md", "svc/../../x", "a/../b", "a?.md", "a[", "a]",
                                  "a{", "a}", "a(", "a)", "a\\b", ""])
def test_read_extra_registry_errors(code, item):
    """Абсолютный путь, «..», символы шаблона кроме * — ошибка [workspace.<имя>.code], ломает только
    wshub-code."""
    code.write(read_extra=json.dumps([item]))
    reg = parse(code.cfg.read_text())
    assert "read_extra" in reg.code.project_errors["proj"] and reg.workspaces["proj"]


def test_read_extra_symlink_outside_refused(code, tmp_path):
    """Симлинк внутри корня, ведущий наружу, — отказ до создания файлов задачи (мутация «read_extra без
    проверки выхода за корень»)."""
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.md").write_text("x")
    (code.root / "lnk").symlink_to(outside)
    t = code.taskfile()
    for item in ("lnk/secret.md", "lnk/*.md", "lnk"):
        code.write(read_extra=json.dumps([item]))
        before = sorted(code.jobs.root.iterdir()) if code.jobs.root.exists() else []
        with pytest.raises(RegistryError, match="вне корня проекта"):
            code.jobs.start(code.jobs.plan("proj", t))
        assert (sorted(code.jobs.root.iterdir()) if code.jobs.root.exists() else []) == before


def test_read_extra_glob_resolved_base(code):
    """glob внутри корня — правило с разрешённой базой: симлинк на каталог внутри корня раскрывается."""
    (code.root / "rules").symlink_to(code.root / ".agents")
    code.write(read_extra='["rules/*.md", "*.md", "svc/**"]')
    plan = code.jobs.plan("proj", code.taskfile())
    root = code.root.resolve()
    assert code.jobs._read_extra(plan.root, plan.proj) == [f"{root}/.agents/*.md", f"{root}/*.md", f"{root}/svc/**"]


def test_read_extra_base_with_pattern_chars_refused(code, tmp_path):
    """Разрешённая база с символами шаблона (цель симлинка, корень) — отказ: иначе Read(//…) станет шаблоном."""
    (code.root / ".agents[x]").mkdir()
    (code.root / "rules").symlink_to(code.root / ".agents[x]")
    root = code.root.resolve()
    with pytest.raises(RegistryError, match="символами шаблона"):
        code.jobs._read_extra(root, ProjectCode(read_extra=("rules/*.md",)))


def test_read_extra_protected_overlap_refused(code):
    """Пересечение со служебными каталогами wshub — отказ: база внутри служебного и служебный внутри базы.
    (Через plan не проверить: dirs = ["."] с таким служебным каталогом сам отказывает раньше.)"""
    root = code.root.resolve()
    code.hub.protected_data = [*code.hub.protected_data, root / "svc" / "state"]
    for item in ("svc/state/x.md", "svc", "svc/*.md"):
        with pytest.raises(RegistryError, match="служебным каталогом"):
            code.jobs._read_extra(root, ProjectCode(read_extra=(item,)))
    assert code.jobs._read_extra(root, ProjectCode(read_extra=("CLAUDE.md",))) == [f"{root}/CLAUDE.md"]


def test_code_projects_shows_read_extra(code):
    code.write(read_extra='["CLAUDE.md", ".agents/*.md"]')
    out = json.loads(_call(code, "code_projects", {}))[0]
    assert out["read_extra"] == ["CLAUDE.md", ".agents/*.md"]  # как в реестре, от корня проекта

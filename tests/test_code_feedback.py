"""wshub-code, ТЗ WSH-3.16 (отзыв rt о режиме auto): ключ проекта attribution (1.2), строки блока «От хаба» (1.3),
`--out` помощника создаёт недостающие каталоги (1.4). Пуш и API хостинга в auto (1.1) — test_code_auto.py.
Настоящий claude не вызывается."""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import pytest
from test_code import GOLDEN, make_code, wait_terminal
from test_code_gates import _gate_job, _gates
from test_code_plan import _wait_state
from test_code_push import _Api, _external, _forge_code, _helper, _running_job, _token_file

from wshub import claude_cli, code_server, helper
from wshub import jobs as jobs_mod
from wshub.registry import CodeGlobal, Gate, ProjectCode, parse

HEAD = "\n---\nОт хаба wshub (добавлено к шаблону):\n"
OFF = {"commit": "", "pr": ""}
SPLIT = jobs_mod.HUB_SANDBOX_SPLIT
PLAN = jobs_mod.HUB_AUTO_PLAN
PUSH = jobs_mod.HUB_AUTO_PUSH
FORGE = jobs_mod.HUB_AUTO_FORGE


@pytest.fixture
def code(tmp_path, monkeypatch):
    return make_code(tmp_path, monkeypatch)


@pytest.fixture(autouse=True)
def hub_wshub(tmp_path, monkeypatch) -> Path:
    exe = tmp_path / "hubbin" / "wshub"
    exe.parent.mkdir()
    exe.write_text("#!/bin/sh\nexit 0\n")
    exe.chmod(0o755)
    monkeypatch.setattr(claude_cli, "wshub_exe", lambda: str(exe))
    return exe


@pytest.fixture
def api():
    a = _Api()
    yield a
    a.close()


def _call(code, name, args) -> str:
    out = asyncio.run(code_server.build_code_server(code.hub).call_tool(name, args))
    content = out[0] if isinstance(out, tuple) else out
    return content[0].text


def _block(prompt: str) -> str:
    return prompt.split(HEAD, 1)[1] if HEAD in prompt else ""


def _settings_json(code, jid) -> dict:
    return json.loads((code.jobs.job_dir(jid) / "settings.json").read_text())


# ---------- 1.2 attribution ----------

def test_registry_attribution(code):
    assert parse(code.cfg.read_text()).code.projects["proj"].attribution is True  # ключа нет — true
    code.write(attribution="false")
    assert parse(code.cfg.read_text()).code.projects["proj"].attribution is False
    code.write(attribution="true")
    assert parse(code.cfg.read_text()).code.projects["proj"].attribution is True
    for bad in ('"false"', "0", '"no"', "[]"):
        code.write(attribution=bad)
        reg = parse(code.cfg.read_text())
        assert reg.code.projects.get("proj") is None
        assert reg.code.project_errors["proj"] == "[workspace.proj.code] attribution: true или false"


def test_code_projects_shows_attribution(code):
    assert json.loads(_call(code, "code_projects", {}))[0]["attribution"] is True
    code.write(attribution="false")
    assert json.loads(_call(code, "code_projects", {}))[0]["attribution"] is False


def _settings(profile, phase=None, mode="sandbox", **kw):
    proj = ProjectCode(enabled=True, dirs=(".",), prompts="p", deny_edit=("docker-compose*.yml",), **kw)
    protected = [Path("/home/u/.config/wshub"), Path("/home/u/.local/state/wshub")]
    return claude_cli.settings(profile, "org/TASK.md", CodeGlobal(), proj, protected, protected,
                               ["/py", "-m", "wshub.hook"], Path("/home/u"), Path("/home/u/proj"), phase=phase,
                               mode=mode)


LINKS = [("task", None, "sandbox"), ("task", None, "auto"), ("task", "plan", "sandbox"), ("task", "plan", "auto"),
         ("recon", None, "sandbox"), ("review", None, "sandbox"), ("recon", "plan", "sandbox"),
         ("review", "plan", "auto")]


@pytest.mark.parametrize("profile,phase,mode", LINKS)
def test_attribution_off_in_every_settings(profile, phase, mode):
    """1.2.2: attribution = false — ключ во всех settings задачи (sandbox, auto, фаза плана, все профили); остальное —
    как при true."""
    off = _settings(profile, phase, mode, attribution=False)
    assert off.pop("attribution") == OFF
    assert off == _settings(profile, phase, mode)


@pytest.mark.parametrize("profile,phase,mode", LINKS)
def test_attribution_default_unchanged(profile, phase, mode):
    """1.2.3: true или ключа нет — settings без ключа; в sandbox — эталон golden_settings.json."""
    s = _settings(profile, phase, mode)
    assert "attribution" not in s and s == _settings(profile, phase, mode, attribution=True)
    if mode == "sandbox":
        assert s == (GOLDEN["plan"][profile] if phase == "plan" else GOLDEN[profile])


def test_attribution_end_to_end(code):
    """Сквозной старт: фаза плана auto-проекта, исполнение после утверждения, recon — ключ в settings.json звена."""
    code.write(mode='"auto"', attribution="false")
    root = code.jobs.start(code.jobs.plan("proj", code.taskfile(size="L")))["id"]
    assert _settings_json(code, root)["attribution"] == OFF and code.jobs.read(root)["phase"] == "plan"
    _wait_state(code.jobs, root, ("plan_ready",))
    child = code.jobs.approve(root)
    assert child["mode"] == "auto" and _settings_json(code, child["id"])["attribution"] == OFF
    wait_terminal(code.jobs, child["id"])
    rec = code.jobs.start(code.jobs.plan("proj", code.taskfile(profile="recon")))["id"]
    assert _settings_json(code, rec)["attribution"] == OFF
    wait_terminal(code.jobs, rec)
    code.write(mode='"auto"')
    jid = code.jobs.start(code.jobs.plan("proj", code.taskfile()))["id"]
    assert "attribution" not in _settings_json(code, jid)  # по умолчанию — ключа нет
    wait_terminal(code.jobs, jid)


# ---------- 1.3 строки блока «От хаба» ----------

def test_block_lines_texts():
    """Тексты ТЗ дословно; потолок Bash в строке forge — потолок режима auto."""
    assert PLAN == ("- Фаза плана идёт в песочнице: docker, сборки и сеть недоступны — они будут в фазе исполнения. "
                    "Не ставь BLOCKED из-за них: впиши в план, что проверишь при исполнении.")
    # текст строки 1.3.2 — решение архитектора по ревью (находка 1), дословно
    assert SPLIT == "- Отказ составной команды (`;`, `&&`, `|`) — не закрытый Bash: разбей на отдельные вызовы."
    assert PUSH == "- Пуш — только `wshub push <remote> <ветка>` (голый `git push` запрещён)."
    assert f"с таймаутом Bash до {claude_cli.AUTO_BASH_MAX_TIMEOUT_MS} мс." in FORGE
    assert "Ждать пайплайн — в одном вызове Bash: цикл `sleep` + `wshub forge GET …`" in FORGE


@pytest.mark.parametrize("mode", ["sandbox", "auto"])
@pytest.mark.parametrize("profile,size,want_split,want_plan", [
    ("task", "S", "sandbox", False),  # исполнение без плана: строка песочницы — только в sandbox
    ("task", "L", "both", True),  # фаза плана: песочница в обоих режимах, строка плана — только auto-проекту
    ("recon", "S", "both", False),
    ("review", "S", "both", False),
])
def test_block_lines_where_applicable(code, mode, profile, size, want_split, want_plan):
    code.write(mode=f'"{mode}"')
    p = code.jobs.plan("proj", code.taskfile(profile=profile, size=size))
    block = _block(p.prompt)
    assert (SPLIT in block) is (want_split == "both" or mode == want_split)
    assert (PLAN in block) is (want_plan and mode == "auto")
    assert PUSH not in block and FORGE not in block  # ничего не выдано


def test_block_lines_after_plan(code):
    """Исполнение после плана: в sandbox — строка песочницы, в auto — нет; строка плана — нигде."""
    for mode, split in (("sandbox", True), ("auto", False)):
        code.write(mode=f'"{mode}"')
        root = code.jobs.start(code.jobs.plan("proj", code.taskfile(size="L")))["id"]
        _wait_state(code.jobs, root, ("plan_ready",))
        child = code.jobs.approve(root)
        block = _block((code.jobs.job_dir(child["id"]) / "prompt.txt").read_text())
        assert (SPLIT in block) is split and PLAN not in block
        wait_terminal(code.jobs, child["id"])


def test_helper_lines_only_in_auto(code, tmp_path):
    """Строки wshub push / wshub forge — только исполнению в auto с выданным разрешением; sandbox с теми же
    разрешениями — без них (там свои правила помощника)."""
    tok = _token_file(tmp_path)
    keys = dict(push='"always"', forge='"github"', forge_access='"always"', forge_token=f'"{tok}"')
    code.write(**keys)
    p = code.jobs.plan("proj", code.taskfile())
    assert p.grants["push"] and p.grants["forge"] and PUSH not in p.prompt and FORGE not in p.prompt
    code.write(mode='"auto"', **keys)
    block = _block(code.jobs.plan("proj", code.taskfile()).prompt)
    assert block == PUSH + "\n" + FORGE + "\n"
    assert PUSH not in code.jobs.plan("proj", code.taskfile(size="L")).prompt  # фаза плана — не исполнение
    for profile in ("recon", "review"):  # API выдан и читающим профилям, но они в песочнице (ревью субагента, п. 2)
        p = code.jobs.plan("proj", code.taskfile(profile=profile))
        assert p.grants["forge"] and PUSH not in p.prompt and FORGE not in p.prompt


def test_block_limit_with_new_lines():
    """Лимит 2 КБ прежний; строки помощника и фазы плана не сокращаются — сокращаются списки."""
    gates = tuple(Gate(f"g{i}-" + "x" * 35, ("cmd-" + "y" * 200,), 3600) for i in range(10))
    hidden = [f"dir-{i}/" + "z" * 120 for i in range(25)]
    block = jobs_mod.hub_block("н" * 300, gates, "plan", hidden, "p\n", grants={"push": True, "forge": True},
                               auto_plan=True)
    assert len(block.encode("utf-8")) <= jobs_mod.HUB_BLOCK_MAX
    for line in (PUSH, FORGE, PLAN):
        assert line + "\n" in block
    assert jobs_mod.hub_block(None, (), None, [], "p\n") == ""  # без строк — блока нет


EOL_TAIL = "`git diff --ignore-cr-at-eol --stat -- <файл>` и верни концы строк, которые не правил.\n"
REAL_GATES = (Gate("test-docker", (".venv/bin/python", "-m", "pytest", "-m", "docker"), 600),
              Gate("check", ("scripts/check.sh",), 1500))


def test_block_overflow_drops_split_first():
    """Находка 1 ревью, решение архитектора: sandbox-исполнение task с гейтами, строкой wshub switch, скрытыми и
    смешанными файлами; замечание такой длины, что без строки о составных командах блок влезает в 2 КБ, а с ней — нет.
    Строка уходит первой: блок ровно как без неё — замечание целиком, списки не сокращены, хвост инструкции о концах
    строк на месте."""
    def block(note, sandbox):
        return jobs_mod.hub_block(note, REAL_GATES, None, [".env.example"], "p\n", switch="task",
                                  mixed=["docs/mixed.txt"], sandbox=sandbox)

    base = len(block(None, False).encode("utf-8"))
    note_line = len("- Замечание оператора: \n".encode())
    note = "н" * ((jobs_mod.HUB_BLOCK_MAX - base - note_line - 20) // 2)  # 20 байт запаса до лимита
    assert len(note) > 20  # сценарий осмысленный: замечание есть
    without = block(note, False)
    assert len(without.encode("utf-8")) <= jobs_mod.HUB_BLOCK_MAX < len(without.encode("utf-8")) + len(
        (SPLIT + "\n").encode("utf-8"))  # со строкой 1.3.2 блок переполнился бы
    got = block(note, True)
    assert got == without and SPLIT not in got
    assert f"- Замечание оператора: {note}\n" in got and got.endswith(EOL_TAIL) and "и ещё" not in got


def test_block_split_kept_when_fits():
    """Влезает — строка о составных командах на месте, ничего не сокращено."""
    block = jobs_mod.hub_block("учти ревью", REAL_GATES[:1], None, [".env.example"], "p\n", switch="task",
                               sandbox=True)
    assert SPLIT + "\n" in block and "- Замечание оператора: учти ревью\n" in block
    assert ".env.example" in block and "и ещё" not in block


# ---------- 1.4 --out создаёт каталоги ----------

def test_forge_out_creates_dirs(code, tmp_path, api, capsys):
    jid = _forge_code(code, tmp_path, api, "github")
    d = code.jobs.read(jid)
    code.jobs.write(jid, {**d, "mode": "auto"})  # помощник не смотрит режим: в auto — те же проверки (1.1.6)
    api.routes["/user"] = (200, b'{"n": 1}')
    rc, out = _helper(code, "forge", ["GET", "/user", "--out", "a/b/c.json"], jid, capsys=capsys)
    assert rc == 0, out.err
    assert (code.root / "a" / "b").is_dir() and json.loads((code.root / "a" / "b" / "c.json").read_text()) == {"n": 1}
    rc, out = _helper(code, "forge", ["GET", "/user", "--out", "a/b/d.json"], jid, capsys=capsys)  # каталоги есть
    assert rc == 0 and (code.root / "a" / "b" / "d.json").exists()


@pytest.mark.parametrize("out_arg,msg,gone", [
    (".git/x/y.json", "в .git", ".git/x"),
    (".claude/x/y.json", "защищённый путь песочницы", ".claude/x"),
    ("lnk/x/y.json", "только внутри рабочего каталога", None),  # симлинк наружу
    ("gen/x/y.json", "каталог gen закрыт deny_edit", "gen"),  # создаваемый каталог по маске deny_edit
    ("ok/gen/y.json", "каталог ok/gen закрыт deny_edit", "ok"),
])
def test_forge_out_dirs_refused(code, tmp_path, api, capsys, out_arg, msg, gone):
    outside = tmp_path / "outside"
    outside.mkdir()
    (code.root / "lnk").symlink_to(outside)
    tok = _token_file(tmp_path)
    code.write(forge='"github"', forge_access='"task"', forge_url=f'"{api.url}"', forge_token=f'"{tok}"',
               deny_edit='["gen"]')
    jid = _running_job(code, {"forge": True}, profile="recon")
    rc, out = _helper(code, "forge", ["GET", "/x", "--out", out_arg], jid, capsys=capsys)
    assert rc == 1 and msg in out.err, out.err
    assert api.requests == []  # отказ — до запроса в API
    assert list(outside.iterdir()) == []
    if gone:
        assert not (code.root / gone).exists()


def test_forge_out_symlinked_parent_not_created(code, tmp_path, api, capsys):
    """Симлинк внутри рабочего каталога в родителе: _work_file пропускает, open_out (O_NOFOLLOW) — нет, и
    недостающий каталог за симлинком не создаётся."""
    (code.root / "real").mkdir()
    (code.root / "lnk").symlink_to(code.root / "real")
    jid = _forge_code(code, tmp_path, api, "github")
    rc, out = _helper(code, "forge", ["GET", "/x", "--out", "lnk/new/y.json"], jid, capsys=capsys)
    assert rc == 1 and "не записан" in out.err and api.requests == []
    assert not (code.root / "real" / "new").exists()


def test_open_out_creates_only_listed(code):
    with pytest.raises(FileNotFoundError):
        helper.open_out(code.root, "n/m/x.log")
    with pytest.raises(FileNotFoundError):
        helper.open_out(code.root, "n/m/x.log", ("n",))  # m не в списке — не создаётся
    assert (code.root / "n").is_dir() and not (code.root / "n" / "m").exists()
    os.close(helper.open_out(code.root, "n/m/x.log", ("n", "n/m")))
    assert (code.root / "n" / "m" / "x.log").is_file()


def test_open_out_created_dir_swapped_for_symlink(code, tmp_path, monkeypatch):
    """Гонка: на месте создаваемого каталога появился симлинк наружу (подменяем mkdir) — открытие с O_NOFOLLOW
    отказывает, снаружи ничего не пишется (ревью субагента, п. 3)."""
    outside = tmp_path / "outside"
    outside.mkdir()
    real_symlink = os.symlink
    monkeypatch.setattr(helper.os, "mkdir", lambda part, mode=0o777, *, dir_fd=None: real_symlink(
        str(outside), part, dir_fd=dir_fd))
    with pytest.raises(OSError):
        helper.open_out(code.root, "n/x.log", ("n",))
    assert list(outside.iterdir()) == []


def test_gate_out_creates_dirs_and_refuses(code, capsys):
    _gates(code, ok=json.dumps(["sh", "-c", "echo full"]))
    jid = _gate_job(code)
    rc, out = _helper(code, "gate", ["ok", "--out", "logs/a/b.log"], jid, capsys=capsys)
    assert rc == 0, out.err
    assert (code.root / "logs" / "a" / "b.log").read_text() == "full\n"
    assert _external(code, jid)[-1]["args"] == ["ok", "--out", "logs/a/b.log"]
    rc, out = _helper(code, "gate", ["ok", "--out", ".git/x/y.log"], jid, capsys=capsys)
    assert rc == 1 and "в .git" in out.err and not (code.root / ".git" / "x").exists()
    rc, out = _helper(code, "gate", ["ok", "--out", ".claude/x/y.log"], jid, capsys=capsys)
    assert rc == 1 and "защищённый путь песочницы" in out.err and not (code.root / ".claude" / "x").exists()


def test_gate_out_dirs_deny_edit_and_symlink(code, tmp_path, capsys):
    """У гейта — те же проверки создаваемых каталогов, что у forge: deny_edit каталога, симлинк наружу (ревью
    субагента, п. 4)."""
    outside = tmp_path / "outside"
    outside.mkdir()
    (code.root / "lnk").symlink_to(outside)
    code.write(gates="{ ok = " + json.dumps(["sh", "-c", "echo full"]) + " }", deny_edit='["gen"]')
    jid = _gate_job(code)
    rc, out = _helper(code, "gate", ["ok", "--out", "gen/x/y.log"], jid, capsys=capsys)
    assert rc == 1 and "каталог gen закрыт deny_edit" in out.err and not (code.root / "gen").exists()
    rc, out = _helper(code, "gate", ["ok", "--out", "lnk/x/y.log"], jid, capsys=capsys)
    assert rc == 1 and "только внутри рабочего каталога" in out.err and list(outside.iterdir()) == []

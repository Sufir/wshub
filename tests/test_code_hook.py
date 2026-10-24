"""Хук wshub code-hook (ТЗ WSH-2, п. 2.6, кейс 12): таблица «команда → решение» и контракт main()."""
from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from wshub import hook

HOME = "/home/u"


@pytest.fixture(autouse=True)
def _home(monkeypatch):
    monkeypatch.setenv("HOME", HOME)


DATA = [Path(f"{HOME}/.config/wshub"), Path(f"{HOME}/.local/state/wshub")]
CWD = f"{HOME}/proj/org"

DENY = [
    # git: пуш в любой форме
    "git push",
    "git push origin HEAD",
    "git -C . push",
    "git -C /x/y push --force",
    "/usr/bin/git push origin x",
    "cd x && git push",
    "git status; git push",
    "git status\ngit push",
    "git status || git push",
    "git status | git push",
    "git status & git push",
    "git --git-dir=.git --work-tree=. push",
    "git --no-pager push",
    "echo $(git push)",
    'echo "$(git push)"',
    "echo `git push`",
    'echo "`git push`"',
    "env GIT_TRACE=1 git push",
    "FOO=1 git push",
    "nohup git push &",
    "timeout 30 git push",
    "flock /tmp/l git push",
    "xargs git push < refs.txt",
    "find . -name x -exec git push \\;",
    "{ git push; }",
    "if true; then git push; fi",
    "for r in a b; do git push origin $r; done",
    "/usr/lib/git-core/git-push origin x",
    # git: подмена конфигурации, сеть, remote, clean, config на запись
    "git -c a=b push",
    "git -c core.sshCommand=x status",
    "git -ccore.hooksPath=/x status",
    "git --config-env=core.pager=X log",
    "git --exec-path=/tmp/x status",
    "git remote set-url origin x",
    "git remote show origin",
    "git remote add x https://x",
    "git remote -v add x y",
    "git remote prune origin",
    "git remote update",
    "git remote set-head origin -a",
    "git remote get-url",
    "git remote get-url a b",
    "git remote rename origin x",
    "git fetch --all",
    "git pull --ff-only",
    "git ls-remote origin",
    "git clean -fd",
    "git config url.x.insteadOf y",
    "git config core.hooksPath /tmp/h",
    "git config --add remote.origin.url x",
    "git config --unset user.name",
    "git submodule update --init",
    "git-remote-https origin https://x",
    # git: запись файла и запуск программы из «читающих» подкоманд (allow recon/review и фазы плана)
    "git log --output=x -1",
    "git show --output x HEAD",
    "git diff --output=/tmp/x",
    "git -C . log -p --output=x",
    'git grep -O"touch x" hello',
    "git grep -Ovim hello",
    "git grep -nO'sh -c id' x",
    "git grep -iO x",
    'git grep --open-files-in-pager="sh -c id" x',
    "git grep --open=vim x",
    "git grep --op=vim x",
    "git -C . grep -n -O x",
    "/usr/lib/git-core/git-grep -Ovim x",
    "/usr/lib/git-core/git-log --output=x",
    "GIT_EXTERNAL_DIFF=./x git diff --ext-diff",
    "env GIT_EXTERNAL_DIFF=./x git diff",
    "PAGER='sh -c id' git log",
    "GIT_PAGER=x git log",
    "GIT_DIR=fixtures/evil.git git status",
    "timeout 5 env GIT_CONFIG_GLOBAL=x git log",
    # docker: выход из изоляции контейнера на хост, файлы хоста, выгрузка, удаление томов (пункт 5)
    "docker run -v /:/host alpine",
    "docker run --rm -it -v /etc:/x alpine sh",
    "docker run -itv/home/u:/h alpine",
    "docker run -v ~/.ssh:/k alpine",
    "docker run -v $(pwd):/w alpine",
    "docker run -v .:/w alpine",
    "docker run --volume=/home/u/proj:/w alpine",
    "docker run --mount type=bind,source=/,target=/h alpine",
    "docker run --mount type=npipe,source=x,target=/y alpine",
    "docker run --privileged alpine",
    "docker run --cap-add SYS_ADMIN alpine",
    "docker run --pid=host alpine",
    "docker run --network host alpine",
    "docker run --net=host alpine",
    "docker run --volumes-from other alpine",
    "docker run --env-file /home/u/.config/payload/x alpine",
    "docker run --cidfile /home/u/.bashrc alpine",
    "docker container run -v /:/h alpine",
    "docker compose run --rm -v /:/host stats sh",
    "docker compose run -v /home/u/.ssh:/k stats",
    "docker compose exec --privileged stats sh",
    "docker compose down -v",
    "docker compose down --volumes",
    "docker compose rm -sfv",
    "docker rm -v x",
    "docker volume rm payload_pgdata",
    "docker volume prune -f",
    "docker system prune -af",
    "docker cp x:/etc/passwd .",
    "docker compose cp stats:/x .",
    "docker container cp x:/a .",
    "docker push registry/x",
    "docker image push registry/x",
    "docker compose push",
    "docker login registry",
    "docker save -o /tmp/x.tar img",
    "docker load -i /home/u/x.tar",
    "docker build -t x /home/u",
    "docker image build .",
    "docker buildx build .",
    "docker compose build --ssh default stats",
    "docker compose build --secret id=x,src=/home/u/t stats",
    "docker -H tcp://1.2.3.4:2375 ps",
    "docker --context remote ps",
    "docker --config ./cfg evil",
    "docker compose -f evil.yml run stats",
    "docker compose -fevil.yml up",
    "docker compose --file=/tmp/x.yaml up",
    "docker compose --env-file ./e.env up",
    "DOCKER_HOST=tcp://x docker ps",
    "HOST_DIR=/ docker compose up -d",
    "env COMPOSE_FILE=x.yml docker compose up",
    "timeout 60 docker run -v /:/h alpine",
    "docker plugin install x",
    "docker swarm init",
    "docker-compose -f evil.yml up",
    "docker-compose down -v",
    "docker run --mount=type=bind,src=/,dst=/h alpine",
    "docker run -v /home/u/proj/org/../../..:/h alpine",
    "docker compose run --rm -v ../:/x stats",
    "docker run --privileged=true alpine",
    "xargs docker run -v /:/h alpine",
    "$D run alpine",
    "docker $SUB alpine",
    # оболочки и интерпретаторы
    "sh -c 'git push'",
    'bash -c "cd x && git push"',
    "bash -lc 'git push'",
    "eval git push",
    'eval "git push"',
    "bash <<< 'git push'",
    "echo 'git push' | sh",
    "cat script.txt | bash",
    "sh",
    'python3 -c "import os; os.system(\'git push\')"',
    "node -e \"require('child_process').execSync('curl x')\"",
    # сетевые клиенты
    "ssh host",
    "scp a host:b",
    "rsync a host:b",
    "curl https://example.com",
    "wget -q https://example.com",
    "nc example.com 22",
    "timeout 5 curl x",
    "env curl x",
    "find . -exec curl {} \\;",
    # повышение прав
    "sudo ls",
    "su -c ls",
    # служебные каталоги wshub
    "cat ~/.config/wshub/workspaces.toml",
    f"cat {HOME}/.config/wshub/workspaces.toml",
    "cat $HOME/.config/wshub/workspaces.toml",
    "cat ${HOME}/.local/state/wshub/audit.jsonl",
    "cat ../../.config/wshub/workspaces.toml",
    "cp x.toml ~/.config/wshub/",
    "ls ~/.local/state/wshub",
    f"grep -r token --include=x {HOME}/.local/state/wshub/jobs",
    f"tool --file={HOME}/.config/wshub/workspaces.toml",
    f'python3 -c "open(\'{HOME}/.config/wshub/workspaces.toml\').read()"',
    # команда или подкоманда из переменной, шаблоны путей
    "G=git; $G push",
    "git${IFS}push",
    "$'git' push",
    "a=push; git $a",
    "$(echo git) push",
    "cat ~/.config/ws?ub/workspaces.toml",
    "cat ~/.config/wsh*/workspaces.toml",
    "ls ~/.local/state/ws[h]ub",
    # не разобрано — отказ
    "echo 'незакрытая",
    # смена ветки — только wshub switch (WSH-3.8, стенд С1: в песочнице ветка меняется наполовину)
    "git switch b",
    "git switch -c PAY-1 --no-track origin/master",
    "git switch --detach origin/x",
    "git -C . switch b",
    "git-switch b",
    "git status && git switch b",
    "git checkout b",
    "git checkout -b x origin/master",
    "git checkout --detach origin/x",
    "git checkout .",
    "git checkout src/app.py",
    "git-checkout b",
]

ALLOW = [
    "git status",
    "git status --short",
    "git log --oneline -5",
    "git log --grep push",
    "git diff HEAD~1",
    "git show HEAD:README.md",
    "git add -A",
    'git commit -m "push later"',
    "git commit -m push",
    "git checkout -- src/app.py",
    "git checkout HEAD -- src/app.py",
    "git checkout HEAD~1 -- a.txt b.txt",
    "git rev-parse --abbrev-ref --symbolic-full-name @{u}",
    "git remote",
    "git remote -v",
    "git remote --verbose",
    "git remote get-url origin",
    "git remote get-url --push master",
    "echo wshub push origin x",
    "git -C . status",
    "git config --get user.name",
    "git config --list",
    "git config get user.email",
    "grep push file",
    "grep -rn 'ssh' src/",
    'echo "git push"',
    "ls -la",
    "cat README.md",
    "docker compose run --rm migrations",
    "bash scripts/check.sh",
    'python3 -c "print(1)"',
    "echo ~/.config/other",
    f"cat {HOME}/.config/wshubx/file",
    "ls src/*.py",
    "rm -rf build/*",
    "echo $HOME",
    "git log --format=%H -1",
    "git grep -n hello",
    "git grep -e Oops",
    "git grep -ieOops",
    "git grep -A3 -B2 hello",
    "git grep --no-open-files-in-pager x",
    "git grep --or -e a -e b",
    "git diff --output-indicator-new=+ HEAD",
    "git log --oneline -- --output",
    "git log -SOops",
    "FOO=1 git status",
    "cat /etc/*.conf",
    # docker: обычная работа со стеком проекта
    "docker compose run --rm migrations",
    "docker compose -f /home/sufir/Payload/docker-compose.yml run --rm migrations",
    "docker compose exec rest composer test",
    "docker compose exec -T -e SUITE=Functional -e TEST=Api/X stats composer test:one",
    "docker compose exec -T stats sh -lc 'APP_MODE=testing composer test'",
    "docker compose exec db psql -U app -c 'select 1'",
    "docker compose exec redis redis-cli KEYS 'org:*'",
    "docker compose exec stats vendor/bin/phpunit -v tests/Unit",
    "docker compose run --rm stats vendor/bin/phpunit -v tests/Unit",
    "docker compose run --rm -v /home/u/proj/org:/app stats composer test",
    "docker compose run --rm -v cache:/root/.cache stats go test ./...",
    "docker compose run --rm --mount type=volume,source=gomod,target=/go/pkg/mod stats go build",
    "docker compose run --rm --mount type=tmpfs,target=/tmp stats ls",
    "docker compose up -d stats",
    "docker compose up -d --build stats",
    "docker compose build stats",
    "docker compose down",
    "docker compose logs --tail 100 stats",
    "docker compose ps",
    "docker compose config --images",
    "docker compose --profile dev up -d",
    "docker compose -p payload ps",
    "docker ps -a",
    "docker logs payload-rest",
    "docker exec -it payload-rest sh",
    "docker run --rm -v /home/u/proj/org:/w -w /w golang:1.25 go vet ./...",
    "docker run --rm alpine echo ok",
    "docker images",
    "docker volume ls",
    "docker system df",
    "docker image ls",
    "docker container ls",
    "docker rm -f x",
    "docker-compose ps",
    "docker compose run --rm app pytest -v tests/test_x.py::test_y",
    "docker compose exec -T app pytest -v tests/test_x.py::TestA::test_b",
    "docker run --rm img pytest -v a.py::t",
]


@pytest.mark.parametrize("cmd", DENY)
def test_deny(cmd):
    assert hook.check(cmd, CWD, DATA) is not None, cmd


@pytest.mark.parametrize("cmd", ALLOW)
def test_allow(cmd):
    assert hook.check(cmd, CWD, DATA) is None, (cmd, hook.check(cmd, CWD, DATA))


def test_symlink_to_data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    data = tmp_path / ".config/wshub"
    data.mkdir(parents=True)
    work = tmp_path / "proj"
    work.mkdir()
    (work / "link").symlink_to(data)
    assert hook.check("cat link/workspaces.toml", str(work), [data]) is not None


def test_reasons_are_specific():
    assert "пуш" in hook.check("git push", CWD, DATA)
    assert "сет" in hook.check("curl x", CWD, DATA)
    assert "служебного каталога" in hook.check("cat ~/.config/wshub/x", CWD, DATA)


def _run(event, argv=None) -> tuple[int, str]:
    err = io.StringIO()
    stdin = io.StringIO(event if isinstance(event, str) else json.dumps(event))
    return hook.main(argv if argv is not None else [str(p) for p in DATA], stdin=stdin, stderr=err), err.getvalue()


def test_main_contract():
    assert _run({"tool_name": "Bash", "tool_input": {"command": "git status"}, "cwd": CWD}) == (0, "")
    code, err = _run({"tool_name": "Bash", "tool_input": {"command": "git push"}, "cwd": CWD})
    assert code == 2 and err.startswith("wshub: ")
    assert _run({"tool_name": "Read", "tool_input": {"file_path": "x"}})[0] == 0  # не Bash — не наше дело


@pytest.mark.parametrize("event", [
    "не json",
    {"tool_name": "Bash"},
    {"tool_name": "Bash", "tool_input": {}},
    {"tool_name": "Bash", "tool_input": {"command": 42}},
])
def test_main_fail_closed(event):
    assert _run(event)[0] == 2


def test_main_data_dirs_from_argv():
    event = {"tool_name": "Bash", "tool_input": {"command": "cat /srv/state/x"}, "cwd": CWD}
    assert _run(event, argv=["/srv/state"])[0] == 2
    assert _run(event, argv=[])[0] == 0


# ---------- ТЗ WSH-3.4: помощник хаба, --output по профилю, --profile ----------

HELPER_ALLOW = [
    "wshub push origin feat-x",
    "wshub push origin PAY-1 -o merge_request.create -o merge_request.target=master",
    'wshub push master PAY-1 -o merge_request.title="PAY-1: суть; правка | ещё"',
    'wshub push origin x -o merge_request.description="Сделано\\n\\nПроверено"',
    "wshub forge GET /api/v4/projects/1/merge_requests/2/changes",
    "wshub forge GET projects/1/pipelines --out .agents/p.json",
    "wshub forge GET projects/1/pipelines --out=out/p.json",
    "wshub forge POST repos/o/r/pulls --data .agents/pr.json",
    f"wshub forge GET x --out {CWD}/p.json",
    # гейты (ТЗ WSH-3.5): имя и, может быть, --out
    "wshub gate test-docker",
    "wshub gate check_py310",
    "wshub gate 0",
    "wshub gate check --out .agents/gate.log",
    "wshub gate check --out=out/gate.log",
    f"wshub gate check --out {CWD}/gate.log",
    "/home/u/.local/bin/wshub gate check",
    # смена ветки (ТЗ WSH-3.8): ветка, -c ветка база, --detach ссылка
    "wshub switch WSH-3.8",
    "wshub switch feature/x_1.2",
    "wshub switch -c WSH-3.8 origin/main",
    "wshub switch -c pay-1125 HEAD",
    "wshub switch --detach origin/pay-1125",
    "wshub switch --detach HEAD~1",
    "wshub switch --detach HEAD^",
    "wshub switch --detach 7e3a1a9",
    "/home/u/.local/bin/wshub switch main",
]

HELPER_DENY = [
    # не целиком: звенья, фон, перенаправления, подстановки, обёртки, присваивания
    "wshub push origin x && git status",
    "wshub push origin x; ls",
    "git status && wshub push origin x",
    "wshub push origin x | tee log",
    "wshub push origin x > out.txt",
    "wshub push origin x 2>/dev/null",
    "wshub push origin x &",
    "wshub push origin x\nls",
    "wshub push origin $BR",
    "wshub push origin `git branch --show-current`",
    'wshub push origin "$(git branch --show-current)"',
    "FOO=1 wshub push origin x",
    "WSHUB_JOB=other wshub push origin x",
    "env wshub push origin x",
    "timeout 60 wshub push origin x",
    "nohup wshub forge GET x",
    "xargs wshub push origin < b",
    "sh -c 'wshub push origin x'",
    "{ wshub push origin x; }",
    "! wshub push origin x",
    # другие подкоманды wshub
    "wshub",
    "wshub code jobs",
    "wshub code start p t.md",
    "/home/u/.local/bin/wshub code jobs",
    # --out помощника — по правилам записи
    "wshub forge GET x --out /tmp/p.json",
    "wshub forge GET x --out ../p.json",
    "wshub forge GET x --out .git/hooks/pre-commit",
    "wshub forge GET x --out ~/p.json",
    "wshub forge GET x --out",
    # wshub gate: имя гейта, ничего лишнего, форма команды — как у push
    "wshub gate",
    "wshub gate Test",
    "wshub gate -x",
    "wshub gate --out g.log",
    "wshub gate ../x",
    "wshub gate x/y",
    "wshub gate 'x y'",
    "wshub gate " + "a" * 41,
    "wshub gate x y",
    "wshub gate x -- y",
    "wshub gate x --data d.json",
    "wshub gate x --out",
    "wshub gate x --out=",
    "wshub gate x --out g.log extra",
    "wshub gate x --out a.log --out b.log",
    "wshub gate x --out /tmp/g.log",
    "wshub gate x --out ../g.log",
    "wshub gate x --out .git/g.log",
    "wshub gate x --out ~/g.log",
    "wshub gate x --out 'g*.log'",
    "wshub gate x && git status",
    "git status; wshub gate x",
    "wshub gate x | tail -50",
    "wshub gate x 2>&1",
    "wshub gate x > g.log",
    "wshub gate x &",
    "wshub gate $G",
    "timeout 600 wshub gate x",
    "env FOO=1 wshub gate x",
    "FOO=1 wshub gate x",
    "sh -c 'wshub gate x'",
    "bash -lc 'wshub gate x'",
    # wshub switch: три формы и ничего больше, форма команды — как у push
    "wshub switch",
    "wshub switch -",
    "wshub switch -x",
    "wshub switch -f x",
    "wshub switch --force x",
    "wshub switch --discard-changes x",
    "wshub switch --merge x",
    "wshub switch --orphan x",
    "wshub switch x y",
    "wshub switch x --track",
    "wshub switch x -- y",
    "wshub switch --detach",
    "wshub switch --detach -x",
    "wshub switch --detach x y",
    "wshub switch --detach=origin/x",
    "wshub switch --detach a..b",
    "wshub switch -c x",
    "wshub switch -c x y z",
    "wshub switch -c -x y",
    "wshub switch -c x -y",
    "wshub switch -c x origin/main --track",
    "wshub switch -C x origin/main",
    "wshub switch --create x origin/main",
    "wshub switch -c x~1 origin/main",
    "wshub switch a..b",
    "wshub switch 'a b'",
    "wshub switch x.lock",
    "wshub switch x/",
    "wshub switch x.",
    "wshub switch /x",
    "wshub switch .x",
    "wshub switch a//b",
    "wshub switch x@{1}",
    "wshub switch x:y",
    "wshub switch 'x*'",
    "wshub switch x && git status",
    "git status; wshub switch x",
    "wshub switch x | tail -5",
    "wshub switch x 2>&1",
    "wshub switch x &",
    "wshub switch $B",
    "timeout 60 wshub switch x",
    "FOO=1 wshub switch x",
    "sh -c 'wshub switch x'",
]


@pytest.mark.parametrize("cmd", HELPER_ALLOW)
def test_helper_allow_task(cmd):
    assert hook.check(cmd, CWD, DATA, "task") is None, (cmd, hook.check(cmd, CWD, DATA, "task"))


@pytest.mark.parametrize("cmd", HELPER_DENY)
def test_helper_deny(cmd):
    for profile in ("task", "review", None):
        assert hook.check(cmd, CWD, DATA, profile) is not None, (cmd, profile)


@pytest.mark.parametrize("profile", ["review", "recon", "plan", None])
def test_helper_push_only_task(profile):
    assert hook.check("wshub push origin feat-x", CWD, DATA, profile) is not None
    assert hook.check("wshub forge GET projects/1", CWD, DATA, profile) is None


@pytest.mark.parametrize("profile", ["review", "recon", "plan", None])
def test_helper_gate_only_task(profile):
    for cmd in ("wshub gate check", "wshub gate check --out .agents/gate.log"):
        reason = hook.check(cmd, CWD, DATA, profile)
        assert reason is not None and "только в профиле task" in reason, (cmd, profile, reason)


def test_helper_gate_reasons():
    assert "имя гейта" in hook.check("wshub gate Test", CWD, DATA, "task")
    assert "только --out" in hook.check("wshub gate x y", CWD, DATA, "task")
    assert "внутри рабочего каталога" in hook.check("wshub gate x --out /tmp/g.log", CWD, DATA, "task")
    assert "wshub gate" in hook.check("wshub code jobs", CWD, DATA, "task")


def test_helper_gate_out_symlink_outside(tmp_path):
    work, out = tmp_path / "work", tmp_path / "out"
    work.mkdir()
    out.mkdir()
    (work / "link").symlink_to(out)
    assert hook.check("wshub gate x --out link/g.log", str(work), [], "task") is not None
    assert hook.check("wshub gate x --out g.log", str(work), [], "task") is None


def test_main_gate_by_profile():
    event = {"tool_name": "Bash", "tool_input": {"command": "wshub gate test-docker"}, "cwd": CWD}
    assert _run(event, argv=["--profile", "task", *map(str, DATA)])[0] == 0
    assert _run(event, argv=["--profile", "plan", *map(str, DATA)])[0] == 2
    assert _run(event, argv=["--profile", "review", *map(str, DATA)])[0] == 2
    assert _run(event)[0] == 2


def test_git_switch_and_checkout_reasons():
    """git switch и git checkout без «--» — отказ с подсказкой wshub switch; git checkout -- <путь> — можно
    (WSH-3.8)."""
    for profile in ("task", "review", "recon", "plan", None):
        for cmd in ("git switch b", "git checkout b"):
            reason = hook.check(cmd, CWD, DATA, profile)
            assert reason is not None and "wshub switch" in reason, (cmd, profile, reason)
    assert "только восстановление файлов" in hook.check("git checkout b", CWD, DATA, "task")
    assert hook.check("git checkout -- a.txt", CWD, DATA, "task") is None


def test_helper_switch_review():
    """review меняет ветку для чтения: <ветка> и --detach; новую ветку не создаёт (ТЗ WSH-3.8)."""
    for cmd in ("wshub switch WSH-3.8", "wshub switch --detach origin/pay-1125", "wshub switch --detach HEAD~1"):
        assert hook.check(cmd, CWD, DATA, "review") is None, (cmd, hook.check(cmd, CWD, DATA, "review"))
    reason = hook.check("wshub switch -c WSH-3.8 origin/main", CWD, DATA, "review")
    assert reason is not None and "только в профиле task" in reason


@pytest.mark.parametrize("profile", ["recon", "plan", None, "unknown"])
def test_helper_switch_readonly_profiles(profile):
    for cmd in ("wshub switch WSH-3.8", "wshub switch --detach origin/x", "wshub switch -c x origin/main"):
        reason = hook.check(cmd, CWD, DATA, profile)
        assert reason is not None and "только в профилях task и review" in reason, (cmd, profile, reason)


def test_helper_switch_reasons():
    assert "имя ветки" in hook.check("wshub switch a..b", CWD, DATA, "task")
    assert "ссылка" in hook.check("wshub switch --detach -x", CWD, DATA, "task")
    assert "база" in hook.check("wshub switch -c x a..b", CWD, DATA, "task")
    assert "других опций нет" in hook.check("wshub switch --force x", CWD, DATA, "task")
    assert "-c <ветка> <база>" in hook.check("wshub switch x y", CWD, DATA, "task")
    assert "-c <ветка> <база>" not in hook.check("wshub switch x y", CWD, DATA, "review")
    assert "wshub switch" in hook.check("wshub code jobs", CWD, DATA, "task")


def test_main_switch_by_profile():
    event = {"tool_name": "Bash", "tool_input": {"command": "wshub switch -c WSH-1 origin/main"}, "cwd": CWD}
    assert _run(event, argv=["--profile", "task", *map(str, DATA)])[0] == 0
    assert _run(event, argv=["--profile", "review", *map(str, DATA)])[0] == 2
    assert _run(event, argv=["--profile", "plan", *map(str, DATA)])[0] == 2
    assert _run(event)[0] == 2
    event = {"tool_name": "Bash", "tool_input": {"command": "wshub switch --detach origin/x"}, "cwd": CWD}
    assert _run(event, argv=["--profile", "review", *map(str, DATA)])[0] == 0


def test_helper_out_by_profile():
    assert hook.check("wshub forge GET x --out .agents/p.json", CWD, DATA, "review") is None
    assert hook.check("wshub forge GET x --out p.json", CWD, DATA, "review") is not None
    assert hook.check("wshub forge GET x --out .agents/p.json", CWD, DATA, "recon") is not None


OUTPUT_TASK_ALLOW = [
    "git diff --output=out.diff",
    "git diff --output out.diff origin/main..HEAD",
    "git diff --output=.agents/WSH-1.diff origin/main..HEAD",
    "git log -p --output=sub/dir/x.patch -3",
    "git show --output=x.patch HEAD",
    f"git diff --output={CWD}/x.diff",
    "git -C . diff --output=x.diff",
]

OUTPUT_TASK_DENY = [
    "git diff --output=/tmp/x",
    "git diff --output=../x",
    "git diff --output=.git/hooks/pre-commit",
    "git diff --output=sub/../.git/config",
    "git diff --output=.GIT/config",
    "git diff --output=~/x",
    "git diff --output=$OUT",
    "git diff --output",
    "git diff --output=*.diff",
    "/usr/lib/git-core/git-diff --output=/tmp/x",
]


@pytest.mark.parametrize("cmd", OUTPUT_TASK_ALLOW)
def test_output_task_allow(cmd):
    assert hook.check(cmd, CWD, DATA, "task") is None, (cmd, hook.check(cmd, CWD, DATA, "task"))


@pytest.mark.parametrize("cmd", OUTPUT_TASK_DENY)
def test_output_task_deny(cmd):
    assert hook.check(cmd, CWD, DATA, "task") is not None, cmd


def test_output_review_only_agents():
    assert hook.check("git diff --output=.agents/r.diff", CWD, DATA, "review") is None
    assert hook.check("git diff --output=r.diff", CWD, DATA, "review") is not None
    assert hook.check("git diff --output=.agents/../r.diff", CWD, DATA, "review") is not None


@pytest.mark.parametrize("profile", ["recon", "plan", None, "unknown"])
def test_output_readonly_profiles(profile):
    assert hook.check("git diff --output=.agents/x.diff", CWD, DATA, profile) is not None


def test_output_symlink_outside(tmp_path):
    work, outside = tmp_path / "proj", tmp_path / "outside"
    work.mkdir()
    outside.mkdir()
    (work / "out").symlink_to(outside)
    assert hook.check("git diff --output=out/x.diff", str(work), [], "task") is not None
    assert hook.check("git diff --output=x.diff", str(work), [], "task") is None


def test_helper_and_remote_reasons():
    assert "помощник" in hook.check("wshub code jobs", CWD, DATA, "task")
    assert "wshub push" in hook.check("git push origin x", CWD, DATA, "task")
    assert "только чтение" in hook.check("git remote add x y", CWD, DATA, "task")


def test_main_profile_from_argv():
    event = {"tool_name": "Bash", "tool_input": {"command": "git diff --output=x.diff"}, "cwd": CWD}
    assert _run(event, argv=["--profile", "task", *map(str, DATA)])[0] == 0
    assert _run(event, argv=["--profile", "recon", *map(str, DATA)])[0] == 2
    assert _run(event)[0] == 2  # без --profile — как у читающего профиля
    ok = {"tool_name": "Bash", "tool_input": {"command": "git status"}, "cwd": CWD}
    assert _run(ok, argv=["--profile"])[0] == 2  # --profile без значения — fail closed
    assert _run(ok, argv=["--profile", "task"])[0] == 0

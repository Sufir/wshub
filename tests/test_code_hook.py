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
    "git remote -v",
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
    "git switch -c PAY-1 --no-track origin/master",
    "git rev-parse --abbrev-ref --symbolic-full-name @{u}",
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

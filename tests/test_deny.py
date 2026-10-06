"""Маски deny: регистр, симлинк с безобидным именем, grep в обеих ветках, перечитывание реестра."""
import pytest

from wshub.core import WsError
from wshub.policy import Policy


def test_policy_semantics():
    p = Policy((".env", ".env.*", "*.pem", "id_rsa*", "**/secrets/**", "**/node_modules/**",
                "**/.git/objects/**", "docs/private/*.md"))
    for rel in (".env", "sub/.ENV", ".Env.Local", "a/b/KEY.PEM", "Id_Rsa.pub", "secrets/x", "a/SECRETS/b/c",
                "node_modules/x.js", "web/node_modules/a/b.js", ".git/objects/ab/cd", "docs/private/x.md",
                ".env/inner.txt"):
        assert p.denied(rel), rel
    for rel in (".", "a.txt", "env.txt", "my.env.txt", "secretsx/a", "docs/x.md", ".git/config",
                "docs/private/x.txt", "pem.txt"):
        assert not p.denied(rel), rel


def test_case_insensitive(env):
    (env.proj / "Server.PEM").write_text("k")
    (env.proj / "SECRETS").mkdir()
    (env.proj / "SECRETS" / "db.txt").write_text("pw")
    (env.proj / "sub").mkdir()
    (env.proj / "sub" / ".Env.Production").write_text("X=1")
    ws = env.open()
    for p in ("Server.PEM", "SECRETS/db.txt", "sub/.Env.Production", ".env", "./sub/../.env"):
        with pytest.raises(WsError, match="deny"):
            env.hub.read(ws, p)


def test_innocent_symlink_to_env(env, engine):
    (env.proj / "notes.txt").symlink_to(env.proj / ".env")
    (env.proj / "docs").mkdir()
    (env.proj / "docs" / "cfg").symlink_to(env.proj / ".env")
    ws = env.open(mode="rw")
    for p in ("notes.txt", "docs/cfg"):
        with pytest.raises(WsError, match="deny"):
            env.hub.read(ws, p)
        with pytest.raises(WsError, match="deny"):
            env.hub.extract(ws, p)
        with pytest.raises(WsError):
            env.hub.write(ws, p, "x")
    out = env.hub.grep(ws, "TOKEN")
    assert "supersecret" not in out
    assert "config.txt:1: TOKEN_NAME=api" in out
    # даже если явно указать путь к симлинку
    with pytest.raises(WsError, match="deny"):
        env.hub.grep(ws, "TOKEN", "notes.txt")
    assert (env.proj / ".env").read_text() == "TOKEN=supersecret\n"


def test_symlinked_dir_into_secrets(env, engine):
    (env.proj / "secrets").mkdir()
    (env.proj / "secrets" / "pw.txt").write_text("TOKEN=dbpass\n")
    (env.proj / "public").symlink_to(env.proj / "secrets")
    ws = env.open()
    with pytest.raises(WsError, match="deny"):
        env.hub.read(ws, "public/pw.txt")
    assert "dbpass" not in env.hub.grep(ws, "TOKEN")


def test_grep_skips_denied_both_engines(env, engine):
    (env.proj / "sub").mkdir()
    (env.proj / "sub" / ".ENV.local").write_text("TOKEN=hidden1\n")
    (env.proj / "node_modules" / "pkg").mkdir(parents=True)
    (env.proj / "node_modules" / "pkg" / "i.js").write_text("TOKEN=hidden2\n")
    (env.proj / "k.KEY").write_text("TOKEN=hidden3\n")
    ws = env.open()
    out = env.hub.grep(ws, "TOKEN")
    assert "hidden" not in out and "supersecret" not in out
    assert "config.txt" in out
    assert env.hub.grep(ws, "TOKEN", ".", "*.txt").strip() == "config.txt:1: TOKEN_NAME=api"


def test_ls_and_find_show_names(env):
    ws = env.open()
    ls = env.hub.ls(ws, ".")
    assert ".env  [deny]" in ls
    assert ".env  [deny]" in env.hub.find(ws, ".env*")


def test_workspace_extra_deny_and_reload(env):
    (env.proj / "Users_2024.CSV").write_text("login\n")
    ws = env.open()
    assert "login" in env.hub.read(ws, "Users_2024.CSV")
    env.workspaces["payload"]["deny"] = ["users*.csv"]
    env.write_registry()  # без перезапуска и без нового хэндла
    with pytest.raises(WsError, match="deny"):
        env.hub.read(ws, "Users_2024.CSV")
    assert "users*.csv" in env.hub.workspace_open("payload")


def test_broken_registry_fails_closed(env):
    ws = env.open()
    env.cfg.write_text("[defaults\n")
    with pytest.raises(Exception, match="TOML"):
        env.hub.read(ws, "a.txt")
    env.write_registry()
    assert "hello" in env.hub.read(ws, "a.txt")


def test_new_project_picked_up(env, tmp_path):
    other = tmp_path / "other"
    other.mkdir()
    (other / "o.txt").write_text("other\n")
    assert "neo" not in env.hub.workspaces_list()
    env.workspaces["neo"] = {"path": str(other), "mode": "ro", "description": "новый"}
    env.write_registry()
    assert "neo [ro] — новый" in env.hub.workspaces_list()
    assert "other" in env.hub.read(env.open("neo"), "o.txt")

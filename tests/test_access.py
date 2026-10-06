"""Недоступные пути: обходы их пропускают и считают, read/extract дают понятную ошибку."""
import os
import re

import pytest

from wshub.core import WsError

pytestmark = pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0,
    reason="тесты запущены от root: chmod 000 его не ограничивает, недоступные пути не воспроизвести")

SKIP_RE = re.compile(r"\(пропущен[о]? (\d+) недоступн\w+ пут\w+, например: (.+)\)$")


@pytest.fixture
def locked(env):
    """locked_dir/ — 000; locked.txt и locked.pdf — 000;
    noexec/ — r-- без x (как root-каталог valinor): имена видны, но ни stat, ни вход внутрь."""
    p = env.proj
    (p / "ok.txt").write_text("needle ok\n")
    (p / "ok.pdf").write_bytes(b"%PDF-1.4\n")
    (p / "locked_dir").mkdir()
    (p / "locked_dir" / "inside.txt").write_text("needle hidden1\n")
    (p / "locked_dir" / "inside.pdf").write_bytes(b"%PDF")
    (p / "locked.txt").write_text("needle hidden2\n")
    (p / "locked.pdf").write_bytes(b"%PDF-1.4\n")
    (p / "noexec" / ".tmpdir").mkdir(parents=True)
    (p / "noexec" / "inner.txt").write_text("needle hidden3\n")
    (p / "noexec" / "inner.pdf").write_bytes(b"%PDF-1.4\n")
    (p / ".env.locked").write_text("needle secret\n")  # под deny: пропускается молча, в счётчик не идёт
    modes = [(p / "locked_dir", 0o000), (p / "locked.txt", 0o000), (p / "locked.pdf", 0o000),
             (p / "noexec", 0o444), (p / ".env.locked", 0o000)]
    for path, mode in modes:
        os.chmod(path, mode)
    yield env
    for path, _ in reversed(modes):  # вернуть права, чтобы pytest смог удалить временную папку
        os.chmod(path, 0o755)


def skipped(out: str) -> tuple[int, str]:
    m = SKIP_RE.search(out)
    assert m, f"нет строки о пропущенных путях:\n{out}"
    return int(m.group(1)), m.group(2)


def test_find(locked):
    out = locked.hub.find(locked.open(), "*.pdf")
    assert "ok.pdf" in out and "locked.pdf" in out  # имя файла 000 видно — права на каталог есть
    assert "locked_dir/inside.pdf" not in out
    n, ex = skipped(out)
    assert n >= 2 and "locked_dir — нет прав" in ex and "noexec" in ex
    assert ".env" not in out.split("(пропущен")[1]


def test_find_unreadable_start(locked):
    out = locked.hub.find(locked.open(), "*", "locked_dir")
    assert out.startswith("(ничего не найдено)")
    assert skipped(out) == (1, "locked_dir — нет прав")


def test_tree(locked):
    out = locked.hub.tree(locked.open(), ".", 4)
    assert "ok.txt" in out and "locked_dir/" in out and "inside.txt" not in out
    n, ex = skipped(out)
    assert n >= 2 and "locked_dir — нет прав" in ex


def test_ls(locked):
    ws = locked.open()
    out = locked.hub.ls(ws, ".")
    assert "ok.txt" in out and "locked_dir/" in out and "noexec/" in out
    assert "(пропущен" not in out  # в корне всё доступно для stat
    out = locked.hub.ls(ws, "noexec")
    assert ".tmpdir/" in out  # тип известен из d_type
    n, ex = skipped(out)
    assert n == 2 and "noexec/inner.txt — нет прав" in ex
    with pytest.raises(WsError, match="нет прав на чтение: locked_dir"):
        locked.hub.ls(ws, "locked_dir")


def test_grep(locked, engine):
    out = locked.hub.grep(locked.open(), "needle")
    assert "ok.txt:1: needle ok" in out
    assert "hidden" not in out and "secret" not in out
    n, ex = skipped(out)
    # порядок у rg не детерминирован, поэтому проверяем число и то, что примеры — из ожидаемого набора
    expected = {"locked_dir", "locked.txt", "locked.pdf", "noexec/inner.txt", "noexec/inner.pdf", "noexec/.tmpdir"}
    assert n == len(expected), out
    examples = [e.split(" — ")[0] for e in ex.split("; ")]
    assert len(examples) == 5 and set(examples) <= expected


def test_grep_unreadable_file_path(locked, engine):
    with pytest.raises(WsError, match="нет прав"):
        locked.hub.grep(locked.open(), "needle", "noexec/inner.txt")


@pytest.mark.parametrize("path", ["locked.txt", "noexec/inner.txt", "locked_dir/inside.txt"])
def test_read(locked, path):
    with pytest.raises(WsError, match=f"^нет прав на чтение: {re.escape(path)}$"):
        locked.hub.read(locked.open(), path)


@pytest.mark.parametrize("path", ["locked.pdf", "noexec/inner.pdf", "locked_dir/inside.pdf"])
def test_extract(locked, path):
    with pytest.raises(WsError, match=f"^нет прав на чтение: {re.escape(path)}$"):
        locked.hub.extract(locked.open(), path)


def test_write_gives_clear_error(locked):
    with pytest.raises(WsError, match="нет прав"):
        locked.hub.write(locked.open(mode="rw"), "locked_dir/new.txt", "x")


def test_plural():
    from wshub.core import _plural
    forms = ("путь", "пути", "путей")
    assert [_plural(n, *forms) for n in (1, 2, 4, 5, 11, 12, 21, 22, 25, 111)] == \
        ["путь", "пути", "пути", "путей", "путей", "путей", "путь", "пути", "путей", "путей"]

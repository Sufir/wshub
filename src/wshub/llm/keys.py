"""Ключи API (ТЗ WSH-4.2, п. 2.2): ~/.config/wshub/keys/<провайдер>, каталог 0700, файл 0600, владелец — текущий
пользователь. Значение ключа не выводится и не попадает ни в журнал, ни в тексты исключений этого модуля."""
from __future__ import annotations

import os
import secrets
import stat
from pathlib import Path

DIR_MODE = 0o700
FILE_MODE = 0o600


class KeyProblem(Exception):
    pass


def key_path(config_dir: Path, provider: str) -> Path:
    """Каталог ключей — рядом с реестром: ~/.config/wshub/keys, он уже закрыт для задач Code и файлового сервера."""
    return Path(config_dir) / "keys" / provider


def _problem(p: Path, st: os.stat_result, want: int, is_dir: bool) -> str | None:
    if stat.S_ISLNK(st.st_mode):
        return f"права ключа {p} — это симлинк; ключ хранится только обычным файлом в каталоге keys"
    if is_dir != stat.S_ISDIR(st.st_mode) or not is_dir and not stat.S_ISREG(st.st_mode):
        return f"права ключа {p} — не {'каталог' if is_dir else 'обычный файл'}"
    uid = os.geteuid()
    if st.st_uid != uid:
        return f"права ключа {p} — владелец uid {st.st_uid}, а не текущий пользователь; исправь: chown {uid} {p}"
    mode = stat.S_IMODE(st.st_mode)
    if mode != want:
        return f"права ключа {p} — {mode:04o}; исправь: chmod {want:o} {p}"
    return None


def problems(path: Path) -> list[str]:
    """Что не так с ключом: нет файла, симлинк, чужой владелец, права каталога не 0700 или файла не 0600."""
    path = Path(path)
    for p, want, is_dir in ((path.parent, DIR_MODE, True), (path, FILE_MODE, False)):
        try:
            st = os.lstat(p)
        except FileNotFoundError:
            return [f"ключа нет: {path}; задай: wshub api key set {path.name}"]
        bad = _problem(p, st, want, is_dir)
        if bad:
            return [bad]
    return []


def read(path: Path) -> str:
    """Значение ключа — только после проверки прав; симлинк не открывается (O_NOFOLLOW)."""
    path = Path(path)
    bad = problems(path)
    if bad:
        raise KeyProblem("; ".join(bad))
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError as e:
        raise KeyProblem(f"ключ {path} не открывается: {e.strerror}") from None
    with os.fdopen(fd, "rb") as f:
        bad = _problem(path, os.fstat(f.fileno()), FILE_MODE, False)  # файл могли подменить после проверки
        if bad:
            raise KeyProblem(bad)
        raw = f.read()
    try:
        value = raw.decode("utf-8").strip()
    except UnicodeDecodeError:
        value = ""
    if not value:
        raise KeyProblem(f"ключ {path} пуст или не текст; задай заново: wshub api key set {path.name}")
    return value


def write(path: Path, value: str) -> None:
    """Атомарная запись: временный файл создаётся сразу с правами 0600 (O_EXCL, без окна, когда он доступен
    другим), затем os.replace. Каталог keys создаётся с 0700; уже существующий с другими правами — отказ."""
    path = Path(path)
    if not value.strip():
        raise KeyProblem("пустой ключ — не записан")
    d = path.parent
    d.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.mkdir(d, DIR_MODE)
        os.chmod(d, DIR_MODE)  # umask мог только сузить; ставим ровно 0700
    except FileExistsError:
        pass
    bad = _problem(d, os.lstat(d), DIR_MODE, True)
    if bad:
        raise KeyProblem(bad)
    tmp = d / f".{path.name}.{secrets.token_hex(4)}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, FILE_MODE)
    try:
        os.fchmod(fd, FILE_MODE)
        data = value.strip().encode("utf-8") + b"\n"
        while data:
            data = data[os.write(fd, data):]
        os.fsync(fd)
        os.close(fd)
        fd = -1
        os.replace(tmp, path)
    except BaseException:
        if fd >= 0:
            os.close(fd)
        tmp.unlink(missing_ok=True)
        raise

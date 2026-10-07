"""Состояние процессов сервера, общее для всех копий wshub, которые запустил Desktop.

Каждый процесс пишет run/<pid>.json: время старта, git HEAD кода при старте, открытые хэндлы
(без токенов: первые 4 символа и hid — хэш токена) и последний вызов. Мёртвые pid читатели пропускают.
Отзыв хэндла — строка с hid в файле revoked; его проверяет каждый процесс на каждом вызове.
"""
from __future__ import annotations

import atexit
import hashlib
import json
import os
import tempfile
import time
from pathlib import Path

REVOKED_KEEP = 7 * 24 * 3600  # строки revoked старше — удаляются при следующем отзыве: хэндлы живут меньше


def handle_id(token: str) -> str:
    """Идентификатор хэндла для панели и отзыва: по нему токен не восстановить."""
    return hashlib.sha256(token.encode()).hexdigest()[:16]


def repo_dir() -> Path | None:
    """Корень git-репозитория с кодом wshub (editable-установка) или None."""
    root = Path(__file__).resolve().parents[2]
    return root if (root / ".git").exists() else None


def git_head(repo: Path | None) -> str | None:
    """Коммит HEAD без запуска git: .git/HEAD → ref → refs/… или packed-refs."""
    if repo is None:
        return None
    try:
        git = repo / ".git"
        if git.is_file():  # worktree: «gitdir: <путь>»
            git = (repo / git.read_text().strip().removeprefix("gitdir:").strip()).resolve()
        common = git
        if (git / "commondir").is_file():
            common = (git / (git / "commondir").read_text().strip()).resolve()
        head = (git / "HEAD").read_text().strip()
        if not head.startswith("ref:"):
            return head
        ref = head.removeprefix("ref:").strip()
        for base in (git, common):
            if (base / ref).is_file():
                return (base / ref).read_text().strip()
        packed = common / "packed-refs"
        if packed.is_file():
            for line in packed.read_text().splitlines():
                sha, _, name = line.partition(" ")
                if name == ref:
                    return sha
    except OSError:
        pass
    return None


def proc_start(pid: int, proc: Path = Path("/proc")) -> str | None:
    """Время старта процесса из /proc/<pid>/stat (поле 22): отличает живой pid от переиспользованного."""
    try:
        stat = (proc / str(pid) / "stat").read_text()
    except OSError:
        return None
    return stat.rsplit(")", 1)[-1].split()[19]


def pid_alive(pid: int, started: str | None, proc: Path = Path("/proc")) -> bool:
    if proc.is_dir():
        now = proc_start(pid, proc)
        return now is not None and (started is None or now == started)
    try:  # без /proc (macOS)
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


def live_processes(state_dir: Path, proc: Path = Path("/proc")) -> list[dict]:
    """Run-файлы живых процессов, по времени старта."""
    out = []
    for f in sorted((Path(state_dir) / "run").glob("*.json")):
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
            if pid_alive(int(d["pid"]), d.get("proc_start"), proc):
                out.append(d)
        except (OSError, ValueError, KeyError, TypeError):
            continue
    return sorted(out, key=lambda d: d.get("started", 0))


class Runtime:
    def __init__(self, state_dir: Path, clock=time.time, pid: int | None = None):
        self.state_dir = Path(state_dir)
        self.run_dir = self.state_dir / "run"
        self.revoked_path = self.state_dir / "revoked"
        self.clock = clock
        self.pid = pid or os.getpid()
        self.started = clock()
        self.repo = repo_dir()
        self.head = git_head(self.repo)
        self.last_call: dict | None = None
        self.enabled = False  # run-файл пишет только процесс сервера, не тесты и не doctor
        self._rev_stamp = None
        self._revoked: set[str] = set()

    @property
    def run_file(self) -> Path:
        return self.run_dir / f"{self.pid}.json"

    def start(self) -> None:
        self.enabled = True
        for f in self.run_dir.glob("*.json") if self.run_dir.is_dir() else ():
            try:
                d = json.loads(f.read_text(encoding="utf-8"))
                if not pid_alive(int(d["pid"]), d.get("proc_start")):
                    f.unlink()
            except (OSError, ValueError, KeyError, TypeError):
                continue
        atexit.register(self.stop)

    def stop(self) -> None:
        if self.enabled:
            self.enabled = False
            try:
                self.run_file.unlink()
            except OSError:
                pass

    def publish(self, handles: list[dict]) -> None:
        if not self.enabled:
            return
        data = {"pid": self.pid, "proc_start": proc_start(self.pid), "started": self.started,
                "head": self.head, "repo": str(self.repo) if self.repo else None,
                "handles": handles, "last_call": self.last_call}
        try:
            atomic_write_text(self.run_file, json.dumps(data, ensure_ascii=False))
        except OSError:
            pass  # панель покажет процесс устаревшим — работу инструментов это не ломает

    # ---------- отзыв ----------

    def revoked(self) -> set[str]:
        """hid отозванных хэндлов; файл перечитывается, только если изменился."""
        try:
            st = self.revoked_path.stat()
        except FileNotFoundError:
            self._rev_stamp, self._revoked = None, set()
            return self._revoked
        stamp = (st.st_mtime_ns, st.st_size, st.st_ino)
        if stamp != self._rev_stamp:
            self._revoked = {r["hid"] for r in self._revoked_rows()}
            self._rev_stamp = stamp
        return self._revoked

    def _revoked_rows(self) -> list[dict]:
        rows = []
        try:
            text = self.revoked_path.read_text(encoding="utf-8")
        except OSError:
            return rows
        for line in text.splitlines():
            try:
                r = json.loads(line)
                if isinstance(r, dict) and isinstance(r.get("hid"), str):
                    rows.append(r)
            except ValueError:
                continue
        return rows

    def revoke(self, hid: str) -> None:
        now = self.clock()
        rows = [r for r in self._revoked_rows() if now - r.get("ts", now) < REVOKED_KEEP and r["hid"] != hid]
        rows.append({"hid": hid, "ts": now})
        atomic_write_text(self.revoked_path, "".join(json.dumps(r) + "\n" for r in rows))

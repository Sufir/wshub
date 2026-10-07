"""Общие фикстуры: временный проект, реестр и состояние — только во временной папке pytest."""
from __future__ import annotations

import os
import platform
import shutil
import sys
from pathlib import Path

import pytest

from wshub.core import Hub

IN_WSL = bool(os.environ.get("WSL_DISTRO_NAME")) or "microsoft" in platform.release().lower()
IS_ROOT = hasattr(os, "geteuid") and os.geteuid() == 0


def pytest_collection_modifyitems(config, items):
    """Метки окружения: тест не падает, а пропускается с понятной причиной."""
    for item in items:
        if "wsl" in item.keywords and not IN_WSL:
            item.add_marker(pytest.mark.skip(reason="нужен настоящий WSL (wsl.exe, диски Windows в /mnt) — "
                                                    "вне WSL не проверить"))
        if "nonroot" in item.keywords and IS_ROOT:
            item.add_marker(pytest.mark.skip(reason="тесты запущены от root: chmod его не ограничивает, "
                                                    "недоступные пути и занятые файлы не воспроизвести"))


DENY = ['.env', '.env.*', '*.pem', '*.key', 'id_rsa*', 'id_ed25519*',
        '**/secrets/**', '**/node_modules/**', '**/.git/objects/**']


def registry_text(workspaces: dict[str, dict], max_read_kb: int = 512, ttl_hours: float = 8) -> str:
    def val(v):
        if isinstance(v, list):
            return "[" + ", ".join(val(x) for x in v) + "]"
        return '"' + str(v).replace("\\", "\\\\").replace('"', '\\"') + '"'

    lines = ["[defaults]", f"deny = {val(DENY)}", f"max_read_kb = {max_read_kb}", f"ttl_hours = {ttl_hours}", ""]
    for name, w in workspaces.items():
        lines.append(f"[workspace.{name}]")
        lines += [f"{k} = {val(v)}" for k, v in w.items()]
        lines.append("")
    return "\n".join(lines)


class Env:
    def __init__(self, tmp: Path):
        self.tmp = tmp
        self.proj = tmp / "Payload"
        self.outside = tmp / "outside"
        self.sibling = tmp / "Payload2"
        self.cfg = tmp / "cfg" / "workspaces.toml"
        self.state = tmp / "state"
        for d in (self.proj, self.outside, self.sibling, self.cfg.parent):
            d.mkdir(parents=True)
        (self.proj / "a.txt").write_text("hello\nworld\n")
        (self.proj / "config.txt").write_text("TOKEN_NAME=api\n")
        (self.proj / ".env").write_text("TOKEN=supersecret\n")
        (self.outside / "secret.txt").write_text("TOPSECRET outside\n")
        (self.sibling / "x.txt").write_text("TOPSECRET sibling\n")
        self.workspaces = {
            "payload": {"path": str(self.proj), "mode": "rw", "description": "тестовый rw",
                        "brief": ".agents/BRIEF.md"},
            "rt": {"path": str(self.proj), "mode": "ro", "description": "тестовый ro"},
        }
        self.write_registry()
        self.hub = Hub(self.cfg, self.state)

    def write_registry(self, **kw):
        self.cfg.write_text(registry_text(self.workspaces, **kw), encoding="utf-8")
        # mtime на ФС грубый (миллисекунды): две записи подряд могут получить одинаковый — двигаем вперёд сами
        self._mtime = max(self.cfg.stat().st_mtime_ns, getattr(self, "_mtime", 0) + 1_000_000_000)
        os.utime(self.cfg, ns=(self._mtime, self._mtime))

    def open(self, name: str = "payload", mode: str = "ro") -> str:
        out = self.hub.workspace_open(name, mode)
        return out.splitlines()[0].removeprefix("ws: ")


@pytest.fixture
def env(tmp_path) -> Env:
    return Env(tmp_path)


def _rg_path() -> str | None:
    return shutil.which("rg") or (str(Path(sys.executable).parent / "rg")
                                  if (Path(sys.executable).parent / "rg").exists() else None)


@pytest.fixture(params=["rg", "python"])
def engine(request, env):
    """Прогоняет grep-тест в обеих ветках: через ripgrep и питоновским обходом."""
    if request.param == "rg":
        rg = _rg_path()
        if not rg:
            pytest.skip("rg не найден")
        env.hub.rg = rg
    else:
        env.hub.rg = None
    return request.param

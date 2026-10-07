"""Правка реестра из панели: tomlkit сохраняет комментарии и порядок, проверка — до записи,
запись атомарная, прежняя версия — в registry-history/. Процессы сервера подхватывают файл по mtime."""
from __future__ import annotations

import hashlib
import os
import re
import stat
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Callable

from .policy import glob_regex
from .registry import LIMITS, MODES, RegistryError, is_limit, parse

NEW_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]*$")
DESC_MAX = 300
MASK_MAX = 200


class EditError(Exception):
    pass


def revision(data: bytes) -> str:
    """Версия реестра для панели: правка отклоняется, если файл изменился после загрузки."""
    return hashlib.sha256(data).hexdigest()[:16]


def mask_error(m) -> str | None:
    """Почему маска deny некорректна, или None."""
    if not isinstance(m, str) or not m.strip():
        return "пустая маска — удали строку или впиши маску"
    if m != m.strip():
        return f"«{m}»: пробелы по краям — убери их"
    if len(m) > MASK_MAX:
        return f"«{m[:40]}…»: длиннее {MASK_MAX} символов — сократи"
    if any(c in m for c in "\x00\n\r\t"):
        return f"«{m}»: управляющий символ — убери его"
    if "\\" in m:
        return f"«{m}»: обратная косая черта — разделитель пути здесь «/»"
    if ".." in m.split("/"):
        return f"«{m}»: «..» не нужен — маска и так считается от корня проекта"
    if m.count("[") != m.count("]"):
        return f"«{m}»: незакрытая «[» — закрой её или убери"
    try:
        glob_regex(m)
    except re.error as e:
        return f"«{m}»: {e} — исправь маску"
    return None


class RegistryEditor:
    def __init__(self, path: Path, history_dir: Path, protected_overlap: Callable[[Path], Path | None]):
        self.path = Path(path)
        self.history_dir = Path(history_dir)
        self.protected_overlap = protected_overlap

    def _load(self, rev: str):
        import tomlkit  # только здесь: без tomlkit сервер работает, не работает лишь правка из панели

        try:
            data = self.path.read_bytes()
        except FileNotFoundError:
            data = b""
        if rev != revision(data):
            raise EditError("реестр изменился после загрузки панели (вручную или из другого окна) — "
                            "нажми «Обновить» и повтори")
        text = data.decode("utf-8")
        try:
            parse(text)
        except RegistryError as e:
            raise EditError(f"в реестре ошибка: {e} — исправь файл {self.path} вручную") from None
        return tomlkit, tomlkit.parse(text), text

    # ---------- проверка ----------

    def _check(self, name, path, mode, description, brief, deny, *, create: bool, existing) -> dict:
        errs = []
        if not isinstance(name, str) or not name:
            errs.append("имя не задано — впиши имя")
        elif create and not NEW_NAME_RE.match(name):
            errs.append("имя: только латиница, цифры и дефис, первый символ — буква или цифра")
        elif create and name.lower() in {n.lower() for n in existing}:
            errs.append(f"имя «{name}» уже есть в реестре — выбери другое")
        elif not create and name not in existing:
            errs.append(f"проекта «{name}» нет в реестре — нажми «Обновить»")
        if mode not in MODES:
            errs.append("режим не выбран — выбери «только чтение» или «чтение и запись»")
        root = None
        if not isinstance(path, str) or not path.strip():
            errs.append("путь не задан — выбери папку")
        elif not Path(path).is_absolute():
            errs.append("путь должен быть абсолютным — выбери папку кнопкой «Выбрать»")
        else:
            p = Path(path)
            if not p.exists():
                errs.append(f"папки {path} нет — проверь путь")
            elif not p.is_dir():
                errs.append(f"{path} — не каталог; выбери папку")
            else:
                root = p.resolve()
                bad = self.protected_overlap(root)
                if bad is not None:
                    errs.append(f"папка пересекается со служебным каталогом wshub {bad} — выбери другую")
        if not isinstance(description, str):
            errs.append("описание должно быть строкой")
        elif len(description) > DESC_MAX or "\n" in description:
            errs.append(f"описание — одна строка до {DESC_MAX} символов; сократи")
        if not isinstance(brief, str):
            errs.append("путь BRIEF должен быть строкой")
        elif brief:
            b = Path(brief)
            if b.is_absolute() or ".." in b.parts or "\\" in brief or "\x00" in brief:
                errs.append("путь BRIEF — от корня проекта, без «..» и «\\», например .agents/BRIEF.md")
        if not isinstance(deny, list):
            errs.append("deny должен быть списком масок")
        else:
            seen = set()
            for m in deny:
                e = mask_error(m)
                if e:
                    errs.append("маска " + e)
                elif m.lower() in seen:
                    errs.append(f"маска «{m}» повторяется — удали повтор")
                else:
                    seen.add(m.lower())
        if errs:
            raise EditError("; ".join(errs))
        return {"path": str(path), "mode": mode, "description": description.strip(), "brief": brief.strip(),
                "deny": list(deny)}

    # ---------- изменения ----------

    def save(self, *, name, path, mode, description, brief, deny, rev, create: bool) -> list[str]:
        """Добавить (create) или изменить проект. Возвращает список изменений для панели."""
        tomlkit, doc, old_text = self._load(rev)
        wss = doc.get("workspace")
        existing = list(wss) if wss is not None else []
        v = self._check(name, path, mode, description, brief, deny, create=create, existing=existing)
        changes = []
        if create:
            if wss is None:
                wss = tomlkit.table(is_super_table=True)
                doc["workspace"] = wss
            t = tomlkit.table()
            t["path"] = v["path"]
            t["mode"] = v["mode"]
            if v["description"]:
                t["description"] = v["description"]
            if v["brief"]:
                t["brief"] = v["brief"]
            if v["deny"]:
                arr = tomlkit.array()
                for m in v["deny"]:
                    arr.add_line(m, indent="  ")
                arr.add_line(indent="")
                t["deny"] = arr
            wss[name] = t
            changes.append(f"добавлен проект {name} [{v['mode']}] → {v['path']}")
        else:
            t = wss[name]
            for key, label in (("path", "путь"), ("mode", "режим"), ("description", "описание"),
                               ("brief", "BRIEF")):
                old = t.get(key, "ro" if key == "mode" else "")
                old = str(old)
                if old == v[key]:
                    continue
                if v[key]:
                    t[key] = v[key]
                else:
                    del t[key]
                changes.append(f"{label}: {old or '(пусто)'} → {v[key] or '(пусто)'}")
            changes += self._update_deny(tomlkit, t, v["deny"])
            if not changes:
                return ["изменений нет"]
            changes = [f"проект {name}: {c}" for c in changes]
        self._commit(old_text, tomlkit.dumps(doc))
        return changes

    @staticmethod
    def _update_deny(tomlkit, t, new: list[str]) -> list[str]:
        """Маски правятся на месте: закомментированные строки массива остаются."""
        arr = t.get("deny")
        old = [str(x) for x in arr] if arr is not None else []
        if old == new:
            return []
        removed = [m for m in old if m not in new]
        added = [m for m in new if m not in old]
        if arr is None:
            arr = tomlkit.array()
            for m in new:
                arr.add_line(m, indent="  ")
            arr.add_line(indent="")
            t["deny"] = arr
        else:
            for i in reversed(range(len(arr))):
                if str(arr[i]) not in new:
                    del arr[i]
            for m in added:
                arr.add_line(m, indent="  ")
            if not removed and not added:  # только порядок — переписываем значения по порядку
                for i, m in enumerate(new):
                    arr[i] = m
        out = [f"deny +{m}" for m in added] + [f"deny −{m}" for m in removed]
        return out or ["deny: порядок масок"]

    def save_limits(self, *, values, rev) -> list[str]:
        """Секция [limits]: каждое значение — целое число больше 0. Ключи, равные текущим, не трогаются."""
        tomlkit, doc, old_text = self._load(rev)
        if not isinstance(values, dict):
            raise EditError("лимиты не заданы — заполни форму")
        errs, clean = [], {}
        for k in set(values) - set(LIMITS):
            errs.append(f"неизвестный лимит {k}")
        for k, (_default, label) in LIMITS.items():
            v = values.get(k)
            if isinstance(v, str) and re.fullmatch(r"\s*\d+\s*", v):
                v = int(v)
            if v is None or v == "":
                errs.append(f"{label}: не заполнено — впиши целое число больше 0")
            elif not is_limit(v):
                errs.append(f"{label}: «{v}» — нужно целое число больше 0")
            else:
                clean[k] = v
        if errs:
            raise EditError("; ".join(errs))
        t = doc.get("limits")
        changes = []
        for k, (default, label) in LIMITS.items():
            old = t.get(k, default) if t is not None else default
            if int(old) == clean[k]:
                continue
            if t is None:
                t = tomlkit.table()
                doc["limits"] = t
            t[k] = clean[k]
            changes.append(f"лимиты: {label}: {old} → {clean[k]}")
        if not changes:
            return ["изменений нет"]
        self._commit(old_text, tomlkit.dumps(doc))
        return changes

    def delete(self, *, name, rev) -> list[str]:
        tomlkit, doc, old_text = self._load(rev)
        wss = doc.get("workspace")
        if wss is None or name not in wss:
            raise EditError(f"проекта «{name}» нет в реестре — нажми «Обновить»")
        del wss[name]
        self._commit(old_text, tomlkit.dumps(doc))
        return [f"удалён проект {name} (файлы проекта не тронуты)"]

    def _commit(self, old_text: str, new_text: str) -> None:
        try:
            parse(new_text)  # последняя проверка: файл после правки читается сервером
        except RegistryError as e:
            raise EditError(f"после правки реестр не прошёл бы проверку: {e}; файл не изменён") from None
        if old_text:
            self.history_dir.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            dst, n = self.history_dir / f"{stamp}.toml", 1
            while dst.exists():
                dst, n = self.history_dir / f"{stamp}-{n}.toml", n + 1
            dst.write_text(old_text, encoding="utf-8")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            mode = stat.S_IMODE(self.path.stat().st_mode)
        except FileNotFoundError:
            mode = 0o600
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=f".{self.path.name}.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(new_text)
                fh.flush()
                os.fsync(fh.fileno())
            os.chmod(tmp, mode)
            os.replace(tmp, self.path)
        except BaseException:
            try:
                os.unlink(tmp)
            except FileNotFoundError:
                pass
            raise

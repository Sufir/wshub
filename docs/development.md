# Разработка

```bash
git clone https://github.com/Sufir/wshub.git && cd wshub
sudo apt install ripgrep
uv sync
uv run pytest -rs
uv run ruff check .
```

Работать с живым Desktop — установка из рабочей копии: `uv tool install --editable .`
Код подхватывается без переустановки, но Desktop держит старые процессы — перезапуск из трея
(`wshub doctor` покажет «устарел»). Новые зависимости — `uv tool install --editable --reinstall .`

## Тесты

1. Всё — во временных папках pytest: реестр, состояние, «диск Windows», профиль Desktop. Настоящие файлы не трогаются.
2. Сценарий «чистая машина» (`tests/test_install.py`): временный HOME, фейковые `/mnt/c` и MSIX-пакет;
   setup → doctor → повторный setup → uninstall, конфиг Desktop сравнивается побайтово.
3. Метки окружения — тест пропускается с причиной (`-rs`), а не падает:
   - `wsl` — нужен настоящий WSL (только чтение: дистрибутив, пользователь Windows, конфиги Desktop);
   - `nonroot` — нужен обычный пользователь (недоступные пути через chmod не воспроизводятся под root).
4. Переопределение путей для ручных опытов: `HOME`, `WSHUB_CONFIG`, `WSHUB_STATE`, `WSHUB_MNT`,
   `WSL_DISTRO_NAME`, `WSHUB_WIN_USER`. Пример: `wshub setup --dry-run` на временном HOME.

## CI и релиз

- `.github/workflows/ci.yml` — на каждый push и PR: ruff и shellcheck, pytest на Python 3.10–3.14
  (как в classifiers), сборка `uv build` с проверкой колеса.
- `.github/workflows/release.yml` — тег `vX.Y.Z`: версия в `pyproject.toml` должна совпасть с тегом,
  тесты, сборка, GitHub Release с колесом и sdist.

Выпустить версию:

```bash
uv version 0.3.0            # поднять версию в pyproject.toml
git commit -am "0.3.0" && git tag v0.3.0 && git push --follow-tags
```

## Устройство

| модуль | что внутри |
|---|---|
| `server.py` | инструменты MCP, ресурс панели, CLI |
| `core.py` | хэндлы, проверка путей, чтение и запись, копии, журнал, publish |
| `registry.py`, `registry_edit.py` | чтение реестра; правка с сохранением комментариев (tomlkit) |
| `policy.py` | маски deny |
| `runtime.py` | run-файлы процессов, отзыв, запрет, версия кода |
| `panel.py`, `panel.html` | панель (MCP Apps) |
| `doctor.py` | проверки для `wshub doctor` и вкладки «Обзор» |
| `outbox.py` | перевалка: имена NTFS, проверки пути, очистка |
| `install.py`, `jsonedit.py` | setup/uninstall/update; точечная правка конфига Desktop |

Зависимость `mcp` закреплена на `<2`: 1.x проверена с Claude Desktop.

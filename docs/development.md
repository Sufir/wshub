# Разработка

Проверки (ruff, shellcheck, pytest) запускаются **только в Docker** — локально и в CI. На хосте ничего не ставится
и не запускается: ни `uv sync`, ни `pytest`.

```bash
git clone https://github.com/Sufir/wshub.git && cd wshub
scripts/check.sh                  # всё, Python 3.13; первая сборка образа — несколько минут
scripts/check.sh --python 3.10    # другая версия (3.10–3.14)
scripts/check.sh -- -k setup -x   # только pytest со своими аргументами
scripts/check.sh --wsl            # WSL-тесты: /mnt/c/Users подключается только для чтения
```

Нужен Docker: Docker Desktop с интеграцией WSL или docker-ce в самом WSL.
Образ — `docker/check.Dockerfile`: python:<версия>-slim, ripgrep, shellcheck, uv; пользователь не root
(иначе тесты с меткой `nonroot` пропускаются). Контейнер запускается без сети: зависимости уже в образе.

Работать с живым Desktop — установка из рабочей копии: `uv tool install --editable .`
Код подхватывается без переустановки, но Desktop держит старые процессы — перезапуск из трея
(`wshub doctor` покажет «устарел»). Новые зависимости — `uv tool install --editable --reinstall .`

## Тесты

1. Всё — во временных папках внутри контейнера: реестр, состояние, «диск Windows», профиль Desktop.
2. Сценарий «чистая машина» (`tests/test_install.py`): временный HOME, фейковые `/mnt/c` и MSIX-пакет;
   setup → doctor → повторный setup → uninstall, конфиг Desktop сравнивается побайтово.
3. Метки окружения — тест пропускается с причиной (`-rs`), а не падает:
   - `wsl` — нужен WSL с дисками Windows; запускается `scripts/check.sh --wsl` (только чтение профилей);
   - `nonroot` — нужен обычный пользователь (в образе он такой).
4. Переопределение путей для ручных опытов с `wshub setup --dry-run`: `HOME`, `WSHUB_CONFIG`, `WSHUB_STATE`,
   `WSHUB_MNT`, `WSL_DISTRO_NAME`, `WSHUB_WIN_USER`.

## CI и релиз

Раннер только собирает образ и запускает контейнер — проверки и сборка пакета идут внутри.

- `.github/workflows/ci.yml` — на каждый push и PR: образ на Python 3.10–3.14 (кэш слоёв в GitHub Actions),
  в нём `scripts/check.sh --no-build`; отдельно — `uv build` в контейнере с проверкой колеса.
- `.github/workflows/release.yml` — тег `vX.Y.Z`: версия в `pyproject.toml` должна совпасть с тегом,
  проверки, сборка, GitHub Release с колесом и sdist.

Выпустить версию: поднять `version` в `pyproject.toml`, закоммитить, `git tag v0.3.0 && git push --follow-tags`.

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

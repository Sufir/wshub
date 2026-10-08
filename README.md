# wshub

[![CI](https://github.com/Sufir/wshub/actions/workflows/ci.yml/badge.svg)](https://github.com/Sufir/wshub/actions/workflows/ci.yml)
[![License: GPL-3.0-or-later](https://img.shields.io/badge/license-GPL--3.0--or--later-blue)](LICENSE)

Локальный MCP-сервер для Claude Desktop. Открывает Claude доступ к папкам проектов в WSL: чтение, поиск,
правка файлов и показ файлов карточкой в чате. Один сервер обслуживает все проекты из реестра.

Claude Desktop не позволяет подключать папки `\\wsl.localhost\…`, поэтому доступ к WSL идёт через wshub.

## Схема

```
Claude → Claude Desktop (Windows) → wsl.exe -d <дистрибутив> → wshub (WSL) → папки проектов
                ↑                                                  │
                └───── C:\Users\<user>\ClaudeOutbox ◄── publish ───┘
```

## Требования

| компонент | версия |
|---|---|
| Windows | 10 или 11, WSL 2 с Ubuntu |
| Claude Desktop | Microsoft Store или установщик с claude.ai; запущен хотя бы один раз |
| Python | ставится автоматически (uv) |

## Установка

```bash
curl -LsSf https://raw.githubusercontent.com/Sufir/wshub/main/install.sh | sh
~/.local/bin/wshub setup
```

После установки Claude Desktop перезапускается полностью: значок в трее → **Quit**, затем повторный запуск.

| команда | действие |
|---|---|
| `install.sh` | ставит ripgrep и git (apt), uv и wshub в `~/.local/bin` |
| `wshub setup` | добавляет запись wshub в конфиг Desktop, создаёт реестр, первый проект и папку перевалки |

`wshub setup` спрашивает только путь первого проекта и режим `ro`/`rw`; остальное определяется автоматически.
Повторный запуск ничего не меняет. Неинтерактивно: `wshub setup --yes --project <путь> --mode ro`.
Подробности и ручная установка — [docs/install.md](docs/install.md).

## Использование

Работает в чатах, начатых в Claude Desktop на компьютере.

| задача | запрос в чате | инструмент |
|---|---|---|
| открыть проект | «Открой проект myproject через wshub» | `workspace_open` |
| управлять проектами, сессиями, копиями | «Открой панель wshub» | `panel` |
| показать файл | «Покажи report.pdf» | `publish` |

При первом вызове Desktop запрашивает разрешение на инструменты wshub — **Always allow**.
Полный список инструментов — [docs/tools.md](docs/tools.md), формат реестра — [docs/registry.md](docs/registry.md).

## Безопасность

| механизм | поведение |
|---|---|
| границы проекта | пути только внутри корня; `..`, симлинки наружу и соседние папки отклоняются; реестр меняет только человек |
| маски секретов | `.env`, ключи, `secrets/`, `.git/config` и др.: имена видны, содержимое недоступно, `grep` их пропускает |
| самозащита | папка, задевающая данные wshub (реестр, состояние), не подключается; код wshub (пакет, репозиторий, venv) — только `ro` |
| хэндлы | доступ к проекту действует 8 ч; отзыв и запрет проекта — в панели, для всех процессов |
| копии перед записью | запись только в режиме `rw` и не в `.git`; прежняя версия — в `~/.local/state/wshub/backup` |
| журнал | каждый вызов — в `~/.local/state/wshub/audit.jsonl`: sha256 до и после, без содержимого |

## Диагностика

`wshub doctor` проверяет окружение и выводит способ исправления для каждой проблемы.

| симптом | причина | решение |
|---|---|---|
| Desktop из Microsoft Store не видит wshub | конфиг читается из пакета `%LOCALAPPDATA%\Packages\Claude_<id>\LocalCache\Roaming\Claude\`, а не из `%APPDATA%\Claude\` | `wshub setup` пишет в нужный файл |
| после обновления работает старый код | процессы wshub запущены до обновления | Quit из трея и запуск Desktop; doctor помечает процессы «устарел» |
| нет wshub или панели в чате | чат начат не в Desktop (например, с телефона) | новый чат в Desktop на компьютере |
| `publish` запрашивает доступ к папке | доступ к перевалке подтверждается один раз в каждом чате | разрешить; копии живут 15 мин, карточка остаётся в чате |
| `grep` медленный или прерывается по таймауту | нет ripgrep | `sudo apt install ripgrep` |

Записи других серверов и настройки в конфиге Desktop `setup` не изменяет; копия файла — рядом, `*.wshub-<время>.bak`.

## Обновление и удаление

```bash
wshub update                # обновление; затем перезапуск Desktop из трея
wshub uninstall             # удалить запись из конфига Desktop
wshub uninstall --purge     # то же + реестр, журнал и копии файлов
uv tool uninstall wshub     # удалить программу
```

## Документация

| файл | содержание |
|---|---|
| [docs/install.md](docs/install.md) | что делают `install.sh` и `setup`, расположение конфигов Desktop, ручная установка, удаление |
| [docs/tools.md](docs/tools.md) | инструменты, `publish`, панель, CLI |
| [docs/registry.md](docs/registry.md) | формат реестра, перевалка, файлы состояния |
| [docs/development.md](docs/development.md) | разработка; тесты и CI — только в Docker (`scripts/check.sh`) |

## Лицензия

[GPL-3.0-or-later](LICENSE)

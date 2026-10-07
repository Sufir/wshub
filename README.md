# wshub

[![CI](https://github.com/Sufir/wshub/actions/workflows/ci.yml/badge.svg)](https://github.com/Sufir/wshub/actions/workflows/ci.yml)
[![Лицензия: GPL-3.0-or-later](https://img.shields.io/badge/license-GPL--3.0--or--later-blue)](LICENSE)

Даёт Claude доступ к папкам проектов в WSL: читать, искать, править и показывать файлы карточкой в чате.
Один локальный MCP-сервер на все проекты; какие папки видны и в каком режиме — решаешь ты в реестре.
Нужен, потому что Claude Desktop не даёт выбрать `\\wsl.localhost\…` как папку.

```
Claude (облако) → Claude Desktop (Windows) → wsl.exe -d <дистрибутив> → wshub (WSL) → папки проектов
                         ↑                                                 │
                         └──── C:\Users\<user>\ClaudeOutbox ◄── publish ───┘
```

## Установка

Нужны Windows с WSL (Ubuntu) и Claude Desktop, запущенный хотя бы раз. В терминале WSL:

```bash
curl -LsSf https://raw.githubusercontent.com/Sufir/wshub/main/install.sh | sh
~/.local/bin/wshub setup
```

1. Первая команда ставит uv, ripgrep и wshub (sudo может спросить пароль).
2. `setup` сам найдёт Desktop и его конфиг, спросит путь первого проекта и режим `ro`/`rw`, создаст папку перевалки.
3. Перезапусти Desktop полностью: значок в трее → Quit, затем запусти снова.

Повторный `wshub setup` безопасен. Без вопросов: `wshub setup --yes --project ~/myproject --mode ro`.
Ручная установка и что именно меняется — [docs/install.md](docs/install.md).

## Первые шаги

Новый чат, начатый в Claude Desktop на компьютере:

1. «Открой проект myproject через wshub» — Claude получит хэндл и BRIEF проекта. На запрос инструментов — «Always allow».
2. «Открой панель wshub» — проекты, сессии, журнал, копии файлов, перевалка. Проекты добавляются здесь.
3. «Покажи мне report.pdf» — `publish` положит файл карточкой в чат, минуя модель.

Инструменты и панель — [docs/tools.md](docs/tools.md), реестр — [docs/registry.md](docs/registry.md).

## Безопасность

1. **Только свои пути.** Всё — от корня проекта; `..`, симлинки наружу и соседние папки отклоняются. Реестр меняет только человек.
2. **Маски секретов.** `.env`, ключи, `secrets/` и т. п.: имена видны, содержимое — нет; `grep` их пропускает.
3. **Хэндлы с TTL и отзыв.** Доступ к проекту живёт 8 часов; в панели — отзыв и запрет проекта для всех сессий.
4. **Копия перед записью.** Запись только в `rw` и не в `.git`; прежняя версия — в `~/.local/state/wshub/backup`.
5. **Журнал.** Каждый вызов — в `~/.local/state/wshub/audit.jsonl` (sha256 до и после, без содержимого).

## Частые проблемы

`wshub doctor` проверяет всё ниже и пишет, что сделать.

1. **Desktop из Microsoft Store не видит wshub.** Он читает конфиг внутри пакета:
   `%LOCALAPPDATA%\Packages\Claude_<id>\LocalCache\Roaming\Claude\claude_desktop_config.json`, а не `%APPDATA%\Claude\…`.
   `setup` пишет в нужный файл; doctor помечает, какой читается.
2. **После обновления работает старый код.** Перезапусти Desktop из трея (Quit), не закрытием окна. doctor покажет «устарел».
3. **Нет wshub в чате с телефона или нет панели.** Локальные серверы видны только в чатах, начатых в Desktop на компьютере.
4. **`publish` спрашивает доступ к папке.** Так и задумано: один раз в каждом новом чате. Копии в перевалке живут 15 минут, карточка остаётся в чате.
5. **`grep` медленный или падает по таймауту.** Нет ripgrep: `sudo apt install ripgrep`.

Другие серверы и настройки в конфиге Desktop `setup` не трогает; копия — рядом, `*.wshub-<время>.bak`.

## Обновление и удаление

```bash
wshub update                # затем перезапуск Desktop из трея
wshub uninstall [--purge]   # убрать из Desktop; --purge — ещё реестр, журнал и копии
uv tool uninstall wshub     # удалить программу
```

Разработка, тесты и CI — [docs/development.md](docs/development.md). Лицензия — [GPL-3.0-or-later](LICENSE).

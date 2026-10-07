# Установка: что происходит и как сделать руками

Обычный путь — две команды из [README](../README.md#установка). Здесь — что они меняют, ручная установка и удаление.

## Что делает `install.sh`

1. Ставит через `apt` то, чего нет: `git` (uv берёт код из GitHub), `ripgrep` (быстрый `grep`), `curl`.
2. Ставит [uv](https://docs.astral.sh/uv/) в `~/.local/bin`, если его нет.
3. `uv tool install --force git+https://github.com/Sufir/wshub` → `~/.local/bin/wshub`. Python нужной версии uv скачает сам.

Не трогает установку из рабочей копии (`uv tool install --editable`) — для неё `WSHUB_FORCE=1`.
Конкретная версия: `WSHUB_REF=v0.2.0`. Запускать от своего пользователя, не от root.

## Что делает `wshub setup`

| шаг | что меняется | повторный запуск |
|---|---|---|
| зависимости | ничего: проверка wshub, ripgrep, дистрибутива WSL, пользователя Windows | — |
| Claude Desktop | запись `wshub` в конфиге, который Desktop читает; копия `*.wshub-<время>.bak` рядом | запись та же — файл не трогается |
| реестр | `~/.config/wshub/workspaces.toml` с масками секретов и первый проект | проекты есть — не спрашивает |
| перевалка | папка `C:\Users\<user>\ClaudeOutbox` и секция `[outbox]` | секция есть — не трогает |
| doctor | ничего: сводка проверок | — |

Определяется само: дистрибутив (`WSL_DISTRO_NAME`), пользователь Windows (`cmd.exe` → единственная папка
с Desktop → выбор из списка), где Desktop берёт конфиг, путь `~/.local/bin/wshub`.
Спрашивается только путь первого проекта и режим `ro`/`rw` — без подставленных значений.

Если Desktop запущен, `setup` попросит закрыть его из трея: при выходе Desktop может записать конфиг своей копией.

Флаги: `--yes` — без вопросов (проект только из `--project <путь> --mode ro|rw`), `--dry-run` — показать план.

### Где Desktop берёт конфиг

| установка | файл |
|---|---|
| Microsoft Store (MSIX) | `%LOCALAPPDATA%\Packages\Claude_<id>\LocalCache\Roaming\Claude\claude_desktop_config.json` |
| установщик с claude.ai | `%APPDATA%\Claude\claude_desktop_config.json` |
| Desktop под Linux / macOS | `~/.config/Claude/…` / `~/Library/Application Support/Claude/…` |

MSIX-Desktop читает только копию в пакете: перенаправление AppData действует лишь для процессов внутри пакета,
поэтому из WSL и Проводника виден классический файл, который такой Desktop игнорирует.

## Ручная установка

```bash
sudo apt install ripgrep git
curl -LsSf https://astral.sh/uv/install.sh | sh
uv tool install git+https://github.com/Sufir/wshub
```

Закрой Desktop из трея и добавь в `mcpServers` нужного файла (остальное не трогай):

```json
"wshub": { "command": "wsl.exe", "args": ["-d", "Ubuntu-22.04", "--", "/home/<user>/.local/bin/wshub"] }
```

`Ubuntu-22.04` — имя твоего дистрибутива (`wsl -l` в PowerShell). Desktop под Linux/macOS:
`"wshub": { "command": "/home/<user>/.local/bin/wshub" }`.

Реестр — по образцу из [registry.md](registry.md), перевалка — `wshub outbox set C:\Users\<user>\ClaudeOutbox`.
Проверка — `wshub doctor`, затем запусти Desktop.

## Обновление

`wshub update` — `uv tool upgrade wshub` и напоминание: перезапусти Desktop из трея, иначе работает старый код
(`wshub doctor` → «Процессы сервера», «устарел»). Установка из рабочей копии обновляется через `git pull`.

## Удаление

1. `wshub uninstall` — убирает запись из конфига Desktop (копия рядом). Если раздел `mcpServers` или сам файл
   создал `setup`, они тоже убираются: конфиг становится побайтово таким, как до установки.
2. `wshub uninstall --purge` — ещё реестр, состояние (журнал, копии файлов) и каталоги wshub в перевалке.
   Сама папка перевалки и папки проектов остаются.
3. `uv tool uninstall wshub` — удалить программу.

## Переменные окружения

Для тестов и нестандартных систем; обычно не нужны.

| переменная | что задаёт |
|---|---|
| `WSHUB_CONFIG`, `WSHUB_STATE` | реестр и каталог состояния |
| `WSHUB_MNT` | где смонтированы диски Windows (по умолчанию `/mnt`) |
| `WSHUB_WIN_USER` | пользователь Windows, если автоопределение не подходит |

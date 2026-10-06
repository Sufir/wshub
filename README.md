# wshub

Локальный MCP-сервер (stdio), который даёт Claude Desktop доступ к папкам проектов по реестру
`~/.config/wshub/workspaces.toml`. Один сервер вместо отдельного filesystem-сервера на каждый проект.

Клиент не сообщает серверу, из какого проекта пришёл вызов, поэтому проект открывается явно:
`workspace_open(name)` возвращает хэндл `ws`, который передаётся первым аргументом во все остальные инструменты.

## Инструменты

| инструмент | что делает |
|---|---|
| `workspaces_list()` | проекты из реестра: имя, режим, описание |
| `workspace_open(name, mode="ro")` | хэндл, срок его жизни, политика и BRIEF проекта |
| `ls`, `tree`, `find`, `grep`, `read` | чтение; `grep` через ripgrep, если он есть в `PATH` |
| `extract` | текст из PDF, DOCX, XLSX (листы — в виде CSV) |
| `write`, `edit` | запись, только в режиме `rw`; `edit` заменяет ровно одно вхождение |

## Установка

Нужны Python ≥ 3.10 и [uv](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/Sufir/wshub.git
uv tool install --editable ./wshub    # → ~/.local/bin/wshub
```

Claude Desktop (`claude_desktop_config.json`), Linux/macOS:

```json
"wshub": { "command": "/home/<user>/.local/bin/wshub" }
```

Windows + WSL:

```json
"wshub": { "command": "wsl.exe", "args": ["-d", "Ubuntu-22.04", "--", "/home/<user>/.local/bin/wshub"] }
```

Desktop держит конфиг в памяти и может перезаписать файл, пока запущен: правьте конфиг при закрытом Desktop.

## Реестр

```toml
[defaults]
deny = [".env", ".env.*", "*.pem", "*.key", "id_rsa*", "id_ed25519*",
        "**/secrets/**", "**/node_modules/**", "**/.git/objects/**"]
max_read_kb = 512
ttl_hours = 8

[workspace.myproject]
path = "/home/<user>/myproject"
mode = "rw"                 # "ro" | "rw"
description = "Мой проект"
brief = ".agents/BRIEF.md"  # от корня проекта; отдаётся в workspace_open
deny = ["users*.csv"]       # добавляется к defaults.deny
```

Сервер только читает реестр и перечитывает его при изменении — перезапуск не нужен.
Ошибка в реестре блокирует все вызовы, пока её не исправят.

## Безопасность

- Пути проверяются через `resolve()` + `is_relative_to(корень)`; симлинки наружу, `../` и соседние папки с тем же префиксом отклоняются.
- Маски `deny` — без учёта регистра, по пути от корня и по имени, проверяются и запрошенный путь, и цель симлинка.
  `ls`/`find` имена показывают, `read`/`extract`/`write`/`edit` отказывают, `grep` такие файлы пропускает.
- Запись: не в `.git`, не через симлинк, атомарно (временный файл + `os.replace`) с сохранением прав.
  Перед изменением — копия в `~/.local/state/wshub/backup/<проект>/<время>/`.
- Журнал вызовов `~/.local/state/wshub/audit.jsonl` (sha256 до и после записи, без содержимого файлов).
- Сервер отказывается открыть проект, который содержит собственные каталоги wshub (код, реестр, состояние, установку).
- Недоступные по правам пути обходы пропускают и перечисляют в конце ответа.

## Разработка

```bash
uv sync
uv run pytest
```

## Лицензия

[MIT](LICENSE)

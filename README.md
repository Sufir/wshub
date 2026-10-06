# wshub

Локальный MCP-сервер (stdio) для Claude Desktop: доступ к папкам проектов по реестру
`~/.config/wshub/workspaces.toml`. Клиент не сообщает, из какого проекта пришёл вызов,
поэтому проект открывается явно (`workspace_open`) и дальше передаётся хэндлом `ws`.

## Установка

```bash
uv tool install --editable /home/sufir/wshub
```

Подключение в Claude Desktop: `"command": "wsl.exe"`, `"args": ["-d", "Ubuntu-22.04", "--", "/home/sufir/.local/bin/wshub"]`.

## Инструменты

| инструмент | что делает |
|---|---|
| `workspaces_list()` | проекты из реестра |
| `workspace_open(name, mode="ro")` | хэндл, срок жизни, политика, BRIEF |
| `ls`, `tree`, `find`, `grep`, `read` | чтение |
| `extract` | текст из PDF / DOCX / XLSX |
| `write`, `edit` | запись (только `rw`) |

## Реестр

```toml
[defaults]
deny = [".env", "*.pem"]   # без учёта регистра; по имени и по пути от корня
max_read_kb = 512
ttl_hours = 8

[workspace.payload]
path = "/home/sufir/Payload"
mode = "rw"                 # "ro" | "rw"
description = "..."
brief = ".agents/BRIEF.md"  # от корня проекта
deny = []                   # добавляется к defaults.deny
```

Сервер только читает реестр и перечитывает его при изменении. Ошибка в реестре — отказ во всех вызовах,
пока её не исправят.

## Состояние

- `~/.local/state/wshub/audit.jsonl` — журнал вызовов (без содержимого файлов);
- `~/.local/state/wshub/backup/<проект>/<YYYYMMDD-HHMMSS>/<путь>` — копии перед изменением.

## Тесты

```bash
uv run pytest
```

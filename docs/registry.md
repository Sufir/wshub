# Реестр `~/.config/wshub/workspaces.toml`

Какие папки видит Claude и как. Правит только человек: `wshub setup`, панель wshub, `wshub outbox set` или руками.
Сервер перечитывает файл при изменении — перезапуск не нужен.

```toml
[defaults]
deny = [".env", ".env.*", "*.pem", "*.key", "id_rsa*", "id_ed25519*",
        "**/secrets/**", "**/node_modules/**", "**/.git/objects/**"]
max_read_kb = 512   # больше за один read не отдаётся
ttl_hours = 8       # срок жизни хэндла workspace_open

[workspace.myproject]
path = "/home/<user>/myproject"
mode = "rw"                 # "ro" | "rw"
description = "Мой проект"  # видит модель в workspaces_list
brief = ".agents/BRIEF.md"  # от корня проекта; отдаётся в workspace_open
deny = ["users*.csv"]       # добавляется к defaults.deny

[limits]
journal_max_mb = 5          # больше — журнал уходит в архив
journal_keep_files = 5      # архивов журнала хранить
backup_keep_days = 30       # копия удаляется, если старше и у файла есть
backup_keep_per_file = 10   # столько более новых копий; последняя не удаляется никогда

[outbox]
path = "/mnt/c/Users/<user>/ClaudeOutbox"
ttl_minutes = 15
max_file_mb = 50
max_total_mb = 500
max_files_per_call = 10
```

## Правила

1. Имя проекта — латиница, цифры, `-` и `_`. Путь — абсолютный, не пересекается со служебными каталогами wshub.
2. `mode` в реестре — потолок: `workspace_open(name, "rw")` не даст записи в проект `ro`.
3. Маски `deny` — без учёта регистра, по пути от корня и по имени; проверяются и путь, и цель симлинка.
   `ls`/`find` имена показывают, `read`/`extract`/`write`/`edit` отказывают, `grep` пропускает.
4. Ошибка в реестре (битый TOML, неверное значение) блокирует все вызовы, пока её не исправят.
   Неизвестные ключи пропускаются, doctor пишет «неизвестный ключ <имя> — пропущен».
5. Панель сохраняет через tomlkit (комментарии и порядок остаются), атомарно; прежняя версия —
   `~/.local/state/wshub/registry-history/`.

## Перевалка `[outbox]`

Папка на диске Windows, куда `publish` копирует файлы, чтобы Desktop показал их карточкой.

1. Путь — на диске Windows (`/mnt/<буква>/…`), вне проектов, не корень диска и не сама домашняя папка.
   Можно писать `C:\Users\<user>\ClaudeOutbox` — в `wshub outbox set` и панели путь переводится сам.
2. Один вызов — каталог `<YYYYMMDD-HHMMSS>-<6 символов>`; wshub трогает только такие, чужие файлы остаются.
3. Каталоги старше `ttl_minutes` удаляются при `publish` и старте сервера; сверх `max_total_mb` — самые старые.
4. Ошибка в `[outbox]` отключает только `publish`. Облачная папка (OneDrive и т. п.) — предупреждение в doctor.

```bash
wshub outbox set C:\\Users\\<user>\\ClaudeOutbox   # создать или изменить секцию
wshub outbox clean                               # удалить все каталоги перевалки wshub
```

## Состояние `~/.local/state/wshub`

| что | зачем |
|---|---|
| `backup/<проект>/<время>/` | копии файлов до `write`/`edit` и восстановления |
| `audit.jsonl` (+ архивы) | журнал вызовов: время, инструмент, путь, sha256, без содержимого |
| `run/<pid>.json`, `revoked` | живые процессы сервера, их хэндлы и отозванные хэндлы |
| `blocked.json` | проекты, запрещённые в панели |
| `registry-history/` | прежние версии реестра после правок |
| `outbox-cleanup.json`, `desktop-setup.json` | последняя очистка перевалки; что `setup` создал в конфиге Desktop |

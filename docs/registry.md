# Реестр `~/.config/wshub/workspaces.toml`

Какие папки видит Claude и как. Правит только человек: `wshub setup`, панель wshub, `wshub outbox set` или руками.
Сервер перечитывает файл при изменении — перезапуск не нужен.

```toml
[defaults]
deny = [".env", ".env.*", "*.pem", "*.key", "id_rsa*", "id_ed25519*",
        "**/secrets/**", "**/node_modules/**", "**/.git/objects/**", "**/.git/config"]
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

1. Имя проекта — латиница, цифры, `-` и `_`. Путь — абсолютный. Пересечение (внутри или снаружи) с данными wshub
   (`~/.config/wshub`, `~/.local/state/wshub`, каталоги из `WSHUB_CONFIG` и `WSHUB_STATE`) — отказ всегда;
   с кодом wshub (пакет, репозиторий при editable-установке, venv, `uv tools/wshub`) — только `ro`.
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

## Запуск Claude Code `[code]`

Глобальная секция `[code]` и подтаблица `[workspace.<имя>.code]` настраивают сервер `wshub-code`
(запуск Claude Code из чата). Ошибка значений в них — ошибка только для `wshub-code`: файловый сервер
`wshub` продолжает работать. Полностью — в [docs/code.md](code.md).

```toml
[code]
claude = "claude"                 # имя в PATH или абсолютный путь
deny_read = ["~/.ssh", "~/.config/wshub", "~/.claude/.credentials.json", "~/.claude.json"]

[workspace.team-service.code]
enabled = true
dirs = ["org"]                    # где можно запускать; строго вне служебных каталогов wshub
prompts = ".agents/templates/code-prompts"
profiles = ["recon", "task", "review"]
model_max = "opus"                # потолок модели
# mode = "auto"                   # sandbox (по умолчанию) | auto — task без песочницы, как в терминале оператора
# на подписке по умолчанию лимитов расхода нет; задаются оператором в реестре — раскомментируй нужные
# max_turns = 150                 # ходы задачи
# max_budget_usd = 20             # бюджет задачи
# daily_budget_usd = 60           # дневной бюджет проекта
# max_hours = 4                   # время звена

[workspace.team-service.code.gates]  # гейты: проверки вне песочницы командой wshub gate <имя> (только task)
test-docker = ".venv/bin/python -m pytest -m docker"       # строка — одна команда без оболочки
check = { cmd = ["scripts/check.sh"], timeout_s = 1500 }   # cmd строкой или списком; timeout_s 1…3600, по умолчанию 600
```

`mode` — `"sandbox"` (по умолчанию: песочница, хук, закрытый allow) или `"auto"`: задача профиля `task` в фазе
исполнения идёт в `--permission-mode auto` без песочницы и хука. Другое значение — ошибка секции проекта. Подробно
и о риске — раздел «Режим auto» в [docs/code.md](code.md).

`gates` — имя (`^[a-z0-9][a-z0-9_-]{0,39}$`) → команда. В строковой форме операторы оболочки, `$` и обратная
кавычка — ошибка секции проекта; нужна цепочка — скрипт в репозитории или `cmd` списком. Подробно — раздел
«Гейты» в [docs/code.md](code.md).

Задача на ключе `~/.config/wshub/keys/anthropic` (`code_start(billing="key", reason=…)` или `wshub code start
--billing key`) отдельного ключа в реестре не требует. Деньги — только заданные оператором: бюджет `max_budget_usd`,
дневной потолок `[api] daily_usd`, зоны — при `included_usd` в `[api.providers.anthropic]` или после команды
`wshub api credits anthropic`. Подробно — раздел «Задачи на ключе» в [docs/code.md](code.md).

## API хаба `[api]`

Секции `[api]`, `[api.providers.<имя>]`, `[api.warn]` и `[workspace.<имя>.api]` настраивают доступ хаба к моделям по
ключу: `wshub api run`, пакеты, сервер `wshub-api`, деньги задач wshub-code на ключе. **Все необязательны**: без них
API включён, провайдер работает по ключу `~/.config/wshub/keys/<имя>` с умолчаниями модуля, денежных ограничений и
предупреждений нет. Полностью — в [docs/api.md](api.md), денежные настройки одной таблицей — раздел «Деньги».

```toml
[api]
# enabled = true             # общий выключатель; false — API хаба выключен целиком
# max_usd_per_call = 1.0     # потолок оценки одного запроса; не задан — нет
# daily_usd = 10.0           # потолок трат за сутки (все провайдеры и задачи Code на ключе); не задан — нет

[api.warn]                   # пороги «⚠» — только показ, ничего не останавливают; не задан — порога нет
# day_usd = 5                # траты за сутки выше
# left_usd = 10              # остаток включённой зоны ниже (только при включённой зоне)
# call_usd = 0.5             # оценка одного запроса или пакета выше

[api.providers.anthropic]    # только для переопределений; имя — из словаря провайдеров (anthropic, fake)
# included_usd = 100         # включает зоны; при included_usd > 0 нужен credits_until
# credits_until = "2026-10-23"
# models = { cheap = "…", standard = "…", strong = "…" }
# strong_requires_choice = true
# timeout_s = 300
# base_url = "https://api.anthropic.com"

[workspace.payload.api]
enabled = false              # закрыть проект от отправки файлов в API; по умолчанию — открыт
```

1. Ошибка в `[api]` или `[api.providers.*]` ломает только API хаба и задачи wshub-code на ключе (файловый сервер и
   задачи на подписке работают); ошибка в `[workspace.<имя>.api]` — только этот проект. doctor (строка «wshub-api»)
   показывает её проблемой.
2. Суммы — конечные числа больше 0 (`included_usd` — ≥ 0); `nan` и `inf` — ошибка. Неизвестный провайдер и
   неизвестные ключи — предупреждение, пропускаются. Неверный порог `[api.warn]` — тоже только предупреждение
   реестра («… — порог пропущен»): пороги ничего не останавливают, и их ошибка тоже.
3. doctor пишет включённые денежные настройки одной строкой `деньги: …` или `деньги: ограничений нет`.

## Состояние `~/.local/state/wshub`

| что | зачем |
|---|---|
| `backup/<проект>/<время>/` | копии файлов до `write`/`edit` и восстановления |
| `audit.jsonl` (+ архивы) | журнал вызовов: время, инструмент, путь, sha256, без содержимого |
| `run/<pid>.json`, `revoked` | живые процессы сервера, их хэндлы и отозванные хэндлы |
| `blocked.json` | проекты, запрещённые в панели |
| `registry-history/` | прежние версии реестра после правок |
| `outbox-cleanup.json`, `desktop-setup.json` | последняя очистка перевалки; что `setup` создал в конфиге Desktop |
| `jobs/<id>/`, `claude-flags.json` | задачи Claude Code (метаданные, события, итог); флаги установленного `claude` по версии |
| `code/gates/<проект>/<ключ>.lock`, `.holder` | один гейт на каталог задачи за раз (ключ — sha256 реального пути каталога, 16 знаков); кто держит |

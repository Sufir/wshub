# wshub-code: запуск Claude Code из чата

Второй MCP-сервер `wshub-code` даёт облачному чату Claude ставить задачи Claude Code на этом ПК: чат
вызывает инструмент, получает хэндл задачи за секунды и следит за ней короткими вызовами. Code работает в
папке проекта с ограничениями, которые задаёт хаб, а не текст промпта. Сервер отдельный: его выключают в
Desktop, не трогая доступ к файлам; у его инструментов свои разрешения.

Подключение: `wshub setup --code` добавляет запись `wshub-code` в конфиг Desktop (`uninstall` её убирает).
Нужны `claude` (Claude Code) в PATH или по пути из реестра и пакеты песочницы `bubblewrap` и `socat`
(`sudo apt-get install bubblewrap socat`) — без них песочница не стартует и задача отклоняется.

## Реестр

Глобальная секция `[code]` и подтаблица `[workspace.<имя>.code]` у проекта. Ошибки значений в них —
ошибка только для `wshub-code`: файловый сервер `wshub` с битой секцией `[code]` продолжает работать.
Неизвестные ключи — предупреждение, как в остальном реестре.

```toml
[code]
claude = "claude"        # имя в PATH или абсолютный путь
max_wait_s = 40          # потолок code_wait, секунд
network = []             # sandbox.network.allowedDomains для всех проектов
deny_read = ["~/.ssh", "~/.aws", "~/.gnupg", "~/.config/wshub", "~/.local/state/wshub",
             "~/.claude/.credentials.json", "~/.claude.json"]
pass_env = []            # имена переменных окружения сверх белого списка (секреты запрещены)

[workspace.payload.code]
enabled = true
dirs = ["org", "journal"]         # где можно запускать; "." — корень проекта
prompts = ".agents/templates/code-prompts"  # каталог шаблонов промптов, от корня проекта
profiles = ["recon", "task", "review"]
model_min = "sonnet"              # haiku < sonnet < opus < fable
model_max = "opus"
effort_max = "high"               # low < medium < high < xhigh < max
max_turns = 150
max_budget_usd = 20
daily_budget_usd = 60
max_parallel = 1
max_hours = 4
prefetch = true                   # git fetch --all --prune силами хаба перед стартом
status_regex = '\*\*Статус:\*\*\s*(\S+)'   # как найти статус в файле ТЗ
bash_allow = ["docker compose run --rm migrations"]   # добавка к allow профиля task
sandbox_exclude = ["docker compose run --rm migrations"]   # команды вне песочницы
network = []                      # добавка к [code].network
deny_read = ["~/.config/payload"] # добавка к [code].deny_read
allow_read = []                   # sandbox.filesystem.allowRead сверх рабочего каталога и настроек git
deny_edit = ["docker-compose*.yml", "Dockerfile*", ".gitlab-ci.yml"]
claude_md_excludes = []           # claudeMdExcludes
```

Умолчания: `enabled = false`; при `enabled = true` обязательны `dirs` и `prompts`; `profiles` — все три;
`model_min = "haiku"`, `model_max = "opus"`, `effort_max = "high"`, `max_turns = 100`, `max_budget_usd = 10`,
`daily_budget_usd = 30`, `max_parallel = 1`, `max_hours = 4`, `prefetch = false`; списки пустые. Каждый
элемент `dirs` после `resolve()` лежит внутри корня проекта, существует и строго не пересекается со
служебными каталогами wshub (код, реестр, состояние) — иначе ошибка реестра для `wshub-code`.

## Шапка запуска

Файл ТЗ начинается с шапки — плоское подмножество YAML между строками `---`: только `ключ: значение`,
комментарии `#`, пустые строки. Вложенность, списки, многострочные значения — ошибка.

```yaml
---
ticket: PAY-1101
dir: org
profile: task        # recon | task | review
prompt: task         # имя шаблона в каталоге prompts; по умолчанию = profile
model: sonnet
effort: high
max_turns: 80
budget_usd: 5
---
```

Обязательны `ticket`, `dir`, `profile`. Файл ТЗ лежит внутри `dir`, `dir` есть в `code.dirs`, профиль — в
`code.profiles`, файл обычный и ≤ 1 МБ. Аргументы `code_start`/`wshub code start` переопределяют шапку;
итоговые значения проверяются по потолкам и полу реестра — нарушение отклоняется, а не урезается молча.

## Шаблоны промптов

Файл `<prompts>/<prompt>.md` (каталог от корня проекта, может лежать вне `dir`: его читает хаб, не Code).
Подстановки: `{ticket}`, `{task}` (путь к ТЗ относительно `dir`), `{note}` (строка оператора для
продолжения после BLOCKED, ≤ 300 символов, без перевода строки). Другие `{…}` — ошибка.

## Профили и модель безопасности

| профиль | что разрешено дополнительно к чтению и git-чтению |
|---|---|
| recon | правит только файл ТЗ (`Edit(./<ТЗ>)`) |
| task | `Edit(./**)`, субагенты, git add/commit/switch/branch/checkout/merge --ff-only, `bash_allow` проекта |
| review | `Edit(./.agents/**)`, субагенты |

Ограничения держат четыре слоя, ни один не зависит от текста промпта:

1. **Правила permissions.** Файловые инструменты — только с путём от рабочего каталога (`Read(./**)` и
   нужный `Edit(...)`); голых правил без пути нет. Для каждого пути из `deny_read` и каждого служебного
   каталога wshub — deny `Read` и `Edit`. deny всех профилей: `git push/remote/clean`, `ssh`, `scp`, `sudo`,
   `curl`, `wget`, `Read(**/.env*)`, `WebFetch`, `WebSearch`, маски `deny_edit`.
2. **Песочница** (`sandbox`, нужны bwrap и socat). Включена, `failIfUnavailable: true`,
   `allowUnsandboxedCommands: false`, `autoAllowBashIfSandboxed: false` — Bash идёт только по allow-правилам.
   Сеть — только `network`. Чтение сужено: `denyRead` включает `~/` и `/mnt/`, `allowRead` открывает рабочий
   каталог, `~/.gitconfig`, `~/.config/git/` и `allow_read` проекта. Запись в `./.git/config`, `./.git/hooks`,
   `./.git/info`, `./.git/modules` закрыта (`denyWrite` и deny `Edit`).
3. **Хук `wshub code-hook`** на Bash закрывает формы, которые правила по тексту не ловят (git push в
   подстановке, сетевые утилиты, пути к служебным каталогам). Ошибка разбора — отказ.
4. **Окружение** — белый список: `HOME`, `USER`, `LOGNAME`, `PATH`, `LANG`, `LC_*`, `TZ`, `DOCKER_HOST`,
   `DOCKER_CONFIG`, плюс `pass_env`; `TERM=dumb`. Секретов (`SSH_AUTH_SOCK`, имена с `TOKEN`/`SECRET`/
   `PASSWORD`/`KEY`) нет даже из `pass_env`. SSH-агента у Code нет — запушить нечем.

Пуш невозможен: запрет держат правила, хук, песочница без сети к git-хосту и окружение без ключей.

### git силами хаба

`git fetch` (prefetch) и снимок (`git status`, `git log`) делает хаб вне песочницы с правами пользователя, а
не Code (у Code нет ни сети к хосту, ни ключей). Чтобы Code не подменил через репозиторий программу, которую
запустит git хаба:

- запись в исполняемую часть `.git` Code закрыта (слой 2);
- каждый вызов git хаба идёт с фиксированными переопределениями `-c`: `core.hooksPath=/dev/null`,
  `core.fsmonitor=false`, `core.sshCommand=ssh`, `credential.helper=`, `protocol.ext.allow=never`,
  `core.pager=cat`, `core.alternateRefsCommand=`, `gc.auto=0`, `maintenance.auto=false`,
  `log.showSignature=false`, `submodule.recurse=false`, `fetch.recurseSubmodules=false`,
  `status.submoduleSummary=false`, `diff.ignoreSubmodules=all`; плюс `--no-optional-locks`,
  `fetch --no-recurse-submodules`; окружение по белому списку и `GIT_TERMINAL_PROMPT=0`,
  `GIT_ASKPASS=/bin/false`, `SSH_ASKPASS=/bin/false`; stdin `/dev/null`, таймаут;
- перед запуском хаб читает `.git/config` (`git config --file … --list --no-includes`, чтение ничего не
  запускает) и, если там есть ключ из запретного списка (`include.*`, `includeIf.*`, `core.fsmonitor`,
  `core.hooksPath`, `core.sshCommand`, `core.gitProxy`, `core.askPass`, `credential.*`, `filter.*`,
  `remote.*.uploadpack`, `remote.*.receivepack`, `protocol.*.allow`, `protocol.allow`, `url.*.insteadOf`,
  `url.*.pushInsteadOf`, `hook.*`, `gc.recentObjectsHook`, `core.alternateRefsCommand`, `remote.*.vcs`,
  `gpg.program`, `gpg.*.program`, `gpg.ssh.defaultKeyCommand`), git не запускается: задача `failed`, причина
  `git_config_unsafe`. Если `.git` — файл (worktree) или рядом есть `.git/commondir`, prefetch и снимок в v0
  отклоняются.

Команды docker выполняются вне песочницы только через `sandbox_exclude` проекта, который правит человек;
правка compose-файлов и Dockerfile закрыта `deny_edit`. Это принятый остаточный риск v0.

## Состояния задачи

`starting`, `prefetch`, `running`, `cancelling` → `completed` (успех), `failed`, `max_turns`, `max_budget`,
`timeout`, `cancelled`, `orphaned` (надзиратель мёртв, Code жив), `lost` (оба мертвы, итога нет). `orphaned`
и `lost` вычисляются при любом чтении снимка. Отмена: SIGINT процессу Code, через 20 с SIGTERM его группе,
ещё через 10 с SIGKILL группе. Продолжение (`code_resume`) — новая задача с `--resume <session_id>`
родителя; `session_id` известен с первой секунды, поэтому продолжить можно даже задачу в `lost`.

## Инструменты MCP и CLI

Инструменты сервера `wshub-code`: `code_projects`, `code_start`, `code_status`, `code_wait`, `code_result`,
`code_cancel`, `code_resume`, `code_jobs`. Все вызовы — в журнал wshub; текст промпта — только sha256.

То же из терминала:

```sh
wshub code start <project> <task> [--model M] [--effort E] [--max-turns N] [--budget-usd B] [--follow]
wshub code status [job]      # без job — список последних задач
wshub code result <job>
wshub code stop <job> | wshub code stop --all
```

`wshub code` без аргументов — сам MCP-сервer (его запускает Desktop). `wshub code-run` и `wshub code-hook` —
служебные (надзиратель задачи и хук Bash), вручную не вызываются.

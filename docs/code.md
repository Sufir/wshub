# wshub-code: запуск Claude Code из чата

Второй MCP-сервер `wshub-code` даёт облачному чату Claude ставить задачи Claude Code на этом ПК: чат
вызывает инструмент, получает хэндл задачи за секунды и следит за ней короткими вызовами. Code работает в
папке проекта с ограничениями, которые задаёт хаб, а не текст промпта. Сервер отдельный: его выключают в
Desktop, не трогая доступ к файлам; у его инструментов свои разрешения.

Подключение: `wshub setup --code` добавляет запись `wshub-code` в конфиг Desktop (`uninstall` её убирает).
Нужны `claude` (Claude Code) в PATH или по пути из реестра и пакеты песочницы `bubblewrap` и `socat`
(`sudo apt-get install bubblewrap socat`) — без них песочница не стартует (`failIfUnavailable: true`), и claude завершается с ошибкой, а задача — в состоянии `failed`.
Временный каталог claude (`/tmp/claude-<uid>`, или `<CLAUDE_CODE_TMPDIR>/claude-<uid>`, если переменная
проходит в окружение задачи) должен отсутствовать или принадлежать пользователю и быть доступен на запись —
иначе claude отказывается стартовать. Старт задачи и `wshub doctor` это проверяют и называют команду
исправления (`sudo rm -rf /tmp/claude-<uid>`). Проверка не учитывает `TMPDIR`; добавил его в `pass_env` —
проверь каталог сам.

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

[workspace.team-service.code]
enabled = true
dirs = ["org", "journal"]         # где можно запускать; каждый dir — КОРЕНЬ git-репозитория (есть .git)
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
deny_read = ["~/.config/team-service"] # добавка к [code].deny_read
allow_read = []                   # sandbox.filesystem.allowRead сверх рабочего каталога и настроек git
deny_edit = ["docker-compose*.yml", "Dockerfile*", ".gitlab-ci.yml"]
claude_md_excludes = []           # claudeMdExcludes
read_extra = ["CLAUDE.md", ".agents/*.md"]   # Read вне dir: пути и glob от корня проекта, только чтение
plan_budget_share = 0.3           # доля бюджета задачи на фазу плана
plan_max_turns = 40               # ходы фазы плана
execute_prompt = "execute"        # шаблон фазы исполнения в каталоге prompts
push = "no"                       # пуш: no | task | always (раздел «Пуш и API хостинга»)
push_protect = ["main", "master"] # куда пуш запрещён всегда
forge_access = "no"               # API хостинга: no | task | always; при доступе нужны forge и forge_token

[[code.policy]]                   # строка политики для всех проектов (сильнее встроенной)
profile = "task"
size = "M"
risk = "low"
model = "sonnet"
effort = "high"
plan = "auto"
plan_model = "opus"
plan_effort = "high"

[[workspace.team-service.code.policy]] # строка политики проекта (сильнее глобальной)
profile = "task"
size = "M"
risk = "high"
model = "opus"
effort = "high"
plan = "review"
plan_model = "opus"
plan_effort = "high"
```

Умолчания: `enabled = false`; при `enabled = true` обязательны `dirs` и `prompts`; `profiles` — все три;
`model_min = "haiku"`, `model_max = "opus"`, `effort_max = "high"`, `max_turns = 100`, `max_budget_usd = 10`,
`daily_budget_usd = 30`, `max_parallel = 1`, `max_hours = 4`, `prefetch = false`, `plan_budget_share = 0.3`,
`plan_max_turns = 40`, `execute_prompt = "execute"`, `push = "no"`, `push_protect = ["main", "master"]`,
`forge_access = "no"`, `forge_write = false`; списки пустые. Каждый
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
size: M              # S | M | L — для task и recon обязателен
risk: low            # low | high — для task обязателен
prompt: task         # имя шаблона в каталоге prompts; по умолчанию = profile
max_turns: 80
budget_usd: 5
# model: opus        # model, effort, plan — только чтобы отступить от политики:
# choice: operator   #   с choice: operator — выбор оператора, иначе нужен choice_reason
# choice_reason: …   #   (причина отступления чата, ≥ 15 символов)
---
```

Обязательны `ticket`, `dir`, `profile`; для `task` — `size` и `risk`, для `recon` — `size` (без них задача не
стартует, отказ объясняет, по чему выбирается модель, и приводит строки политики профиля). Файл ТЗ лежит
внутри `dir`, `dir` есть в `code.dirs`, профиль — в `code.profiles`, файл обычный и ≤ 1 МБ. Бюджет и ходы
проверяются по потолкам реестра всегда; модель, effort и план выбирает политика (ниже).

## Политика выбора: модель, effort, план

Политика — таблица строк. Встроенная таблица — стартовые значения, их калибруют по `code_stats` строками
реестра: `[[code.policy]]` глобально и `[[workspace.<имя>.code.policy]]` для проекта.

| profile | size | risk | model | effort | plan | plan_model | plan_effort |
|---|---|---|---|---|---|---|---|
| recon | S | * | sonnet | medium | no | — | — |
| recon | M | * | sonnet | high | no | — | — |
| recon | L | * | opus | medium | no | — | — |
| review | * | * | opus | medium | no | — | — |
| task | S | low | sonnet | medium | no | — | — |
| task | S | high | sonnet | high | auto | opus | high |
| task | M | low | sonnet | high | auto | opus | high |
| task | M | high | opus | high | review | opus | high |
| task | L | low | opus | high | review | opus | high |
| task | L | high | opus | high | review | opus | high |

- `size`: S — правка в одном слое, ≤ 3 файлов, рядом эталон; M — несколько файлов или слоёв, без смены
  контрактов; L — контракт API, схема БД, перенос ответственности или больше одного сервиса. `risk: high` —
  безопасность, деньги, данные пользователей, необратимые миграции.
- `plan`: `no` — без плана; `auto` — план, затем исполнение без остановки; `review` — план, остановка,
  решение архитектора.
- Совпадение: сначала строки проекта, затем глобальные, затем встроенные; в слое — самая точная строка
  (`*` слабее значения, при равной точности `size` весомее `risk`). Нет строки — отказ с указанием строки.
- Строки реестра проверяются строго: неизвестный ключ, значение вне списка, `plan` без `plan_model` и
  `plan_effort` или повтор строки — ошибка `[code]` или проекта (ломает только `wshub-code`).
- Встроенные строки укладываются в пол и потолок по умолчанию. Нужен `xhigh` — строка политики проекта
  плюс `effort_max = "xhigh"`.

**Источники выбора**, по приоритету:

1. **Оператор — безусловно.** Флаги `wshub code start --model --effort --plan`; поля `model`, `effort`, `plan`
   в шапке вместе с `choice: operator`; аргументы `code_start` вместе с `chosen_by: "operator"`. Пол и потолок
   (`model_min`, `model_max`, `effort_max`) на выбор оператора не действуют — и для фазы плана той же задачи;
   бюджеты и ходы — действуют.
2. **Политика** — если оператор не выбирал. Её выбор подчиняется полу и потолку: строка за потолком —
   отказ с подсказкой, а не тихая подрезка.
3. **Чат.** Поля `model`, `effort`, `plan` в шапке без `choice: operator` или в аргументах без `chosen_by` —
   отступление от политики: только в пределах пола и потолка и только с причиной (`choice_reason` в шапке
   или `reason` в `code_start`, ≥ 15 символов без непечатаемых и краевых пробелов). Без причины — отказ
   «укажи причину отступления от политики
   или оставь выбор политике». Старые шапки с `model`/`effort` без `choice` попадают сюда.
   План чат может только поднять (`no < auto < review`); понизить план — только оператор, любым из трёх
   способов выше. Строки политики нет (чат задал все три поля) — понижать не от чего.

**Канал** (`channel`) — откуда пришли все поля выбора не из политики: `cli` — флаги `wshub code start`,
`mcp` — аргументы `code_start`/`code_resume`, `header` — шапка; несколько — через `+` в порядке cli, mcp,
header. У источника `policy` канала нет. **`choice_fields`** — поле → «источник:канал», например
`{"model": "chat:mcp", "plan": "operator:header"}`: оператор понизил план шапкой, чат задал модель — итоговый
источник `chat`, канал `mcp+header`.

Итог (модель, effort, план, источник `operator` | `policy` | `chat`, канал, строка политики, причина)
пишется в `job.json`, в журнал wshub (`model_source`, `channel`, `choice_fields`) и отдаётся в ответах `code_start`
(«источник: operator (mcp)»), `code_status`, `code_result`.
`code_resume` наследует выбор родителя; сменить модель или effort — по тем же правилам (`reason` или
`chosen_by`).

## Шаблоны промптов

Файл `<prompts>/<prompt>.md` (каталог от корня проекта, может лежать вне `dir`: его читает хаб, не Code).
Подстановки: `{ticket}`, `{task}` (путь к ТЗ относительно `dir`), `{note}` (строка оператора для
продолжения после BLOCKED, ≤ 300 символов, без перевода строки). Другие `{…}` — ошибка.

Задача с планом использует ещё два шаблона, их кладёт проект (хаб их не сочиняет): `execute.md` (имя меняет
`execute_prompt`) — промпт фазы исполнения; `replan.md` — промпт переделки плана, `{note}` в нём обязателен.
Нет `execute` (а при `plan = review` — `replan` с `{note}`) — отказ на старте задачи с планом, а не после фазы
плана; при `plan = auto` шаблону `execute` нельзя ждать `{note}` (его некому передать).

## Режим планирования

Задача с `plan = auto` или `review` идёт двумя фазами одной сессии Claude Code:

1. **Фаза плана** — `--permission-mode plan`, `--session-id`, модель и effort — `plan_model`/`plan_effort`,
   бюджет — `plan_budget_share` от бюджета задачи, ходы — `plan_max_turns`, промпт — шаблон из шапки.
   settings.json фазы отличается от эталона профиля ровно этим: `useAutoModeDuringPlan: false` и
   `permissions.disableAutoMode: "disable"` (без классификатора auto mode Bash вне allow получает отказ);
   allow — чтение и git только на чтение (как `recon`, без Edit и без `bash_allow`);
   `sandbox.excludedCommands` — пусто. Хук, deny и остальная песочница (в том числе `denyWrite`) — как у
   профиля.
2. **План.** Claude Code в `-p` пишет план инструментом `Write` в `~/.claude/plans/<slug>.md`. Хаб берёт путь
   из потока событий — последний успешный (с парным `tool_result` без ошибки) `Write` или `Edit` файла `.md`
   прямо в каталоге планов, изменённого не раньше старта звена, — читает файл и кладёт копию в `plan.md`
   каталога задачи (≤ 200 КБ). Такого вызова нет — задача `failed`, причина `no_plan`, исполнения нет.
   План ищется только в `~/.claude/plans/`: настройка `plansDirectory` в `.claude/settings*.json` проекта не
   поддерживается (задача с ней честно упадёт в `no_plan`, подсказка — в `reason_hint`).
3. **Фаза исполнения** — дочерняя задача: `--resume` той же сессии, `--permission-mode dontAsk`, settings —
   эталон профиля, модель и effort исполнения, шаблон `execute`, бюджет и ходы — остаток задачи.

`plan = auto`: надзиратель фазы плана сам запускает исполнение тем же путём, что `code_approve` (тот же
лок, проверки `max_parallel` и дневного бюджета). Не прошли проверки — план остаётся в `plan_ready`
с причиной (`chain_error`), дальше решает человек. `plan = review`: фаза плана останавливается в `plan_ready`
и ждёт: `code_plan` — текст плана, `code_approve` — исполнение, `code_replan(note)` — новая фаза плана в той
же сессии с шаблоном `replan`.

**Стоимость.** После `--resume` claude отдаёт `total_cost_usd` накопительно за сессию, а `--max-budget-usd`
считает только текущий запуск. Поэтому стоимость звена — разность `total_cost_usd` с предыдущим звеном
(`phase_cost_usd` в итоге), бюджет исполнения — остаток бюджета задачи; дневной бюджет и `code_stats`
считают каждую фазу один раз (это касается и обычного `code_resume`).

**Цепочка.** Снимок корневой задачи (`code_status`, `code_wait`, `code_result`) показывает фазу, id активного
звена, все звенья со стоимостью и общую стоимость. `code_cancel`, `code_plan`, `code_approve`, `code_replan`
по корню работают с активным звеном. `code_cancel` останавливает активное звено, в `plan_ready` — отменяет
цепочку; отмена, пришедшая к концу фазы плана, тоже до исполнения не доходит. `code_wait` возвращается и на
`plan_ready`.

**Имя сессии и вход руками.** Каждый запуск получает `--name "<проект>/<тикет>"` (фаза плана —
`…/plan`). `code_result` и `wshub code result` дают строку `cd <dir> && claude --resume <session_id>` —
открыть сессию в терминале и продолжить руками.

## Профили и модель безопасности

| профиль | что разрешено дополнительно к чтению и git-чтению |
|---|---|
| recon | правит только файл ТЗ (`Edit(./<ТЗ>)`) |
| task | `Edit(./**)`, субагенты, git add/commit/switch/branch/checkout/merge --ff-only, `sort`, `uniq`, `bash_allow` проекта |
| review | `Edit(./.agents/**)`, субагенты |

Всем профилям и фазе плана: git на чтение (`status`, `log`, `show`, `diff`, `grep`; `git remote`, `git remote -v`,
`git remote get-url …`) и фильтры конвейера `head`, `tail`, `wc`, `grep`, `cut` — `git log --oneline | tail -50`
проходит. Встроенных Grep и Glob у claude 2.1.291 нет (в `init.tools` их нет): поиск — `git grep` или `grep`.

Ограничения держат четыре слоя, ни один не зависит от текста промпта:

1. **Правила permissions.** Файловые инструменты — только с путём от рабочего каталога (`Read(./**)` и
   нужный `Edit(...)`); голых правил без пути нет. Для каждого пути из `deny_read` и каждого служебного
   каталога wshub — deny `Read` и `Edit`. deny всех профилей: `git push`, `git clean`, `git remote` с
   `add`, `set-url`, `remove`, `rm`, `rename`, `prune`, `update`, `set-head`, `set-branches`, `show`, `ssh`, `scp`,
   `sudo`, `curl`, `wget`, `Read(**/.env*)`, `WebFetch`, `WebSearch`, маски `deny_edit`.
2. **Песочница** (`sandbox`, нужны bwrap и socat). Включена, `failIfUnavailable: true`,
   `allowUnsandboxedCommands: false`, `autoAllowBashIfSandboxed: false` — Bash идёт только по allow-правилам.
   Сеть — только `network`. Чтение сужено: `denyRead` включает `~/` и `/mnt/`, `allowRead` открывает рабочий
   каталог, `~/.gitconfig`, `~/.config/git/` и `allow_read` проекта. Запись: у всех профилей и в фазе плана
   закрыты `./.git/config`, `./.git/hooks`, `./.git/info`, `./.git/modules` (`denyWrite` и deny `Edit`), запись
   вне рабочего каталога закрыта песочницей. `sandbox_exclude` проекта действует только в `task`; у `recon` и
   `review` в `excludedCommands` — только помощник хаба при разрешении задачи (раздел «Пуш и API хостинга»).

   **Точки монтирования песочницы** (`.bash_profile`, `.bashrc`, `.gitconfig`, `.gitmodules`, `.idea`,
   `.mcp.json`, `.profile`, `.ripgreprc`, `.vscode`, `.zprofile`, `.zshrc` и под `.claude/`: `agents`,
   `commands`, `hooks`, `launch.json`, `loop.md`, `output-styles`, `routines`, `scheduled_tasks.json`,
   `settings.json`, `settings.local.json`, `skills`, `workflows` — список claude 2.1.291; bwrap монтирует
   `/dev/null` на месте несуществующих) хаб при старте задачи прячет помеченным блоком в `.git/info/exclude`
   (`/путь` — от корня репозитория): `git status` во время прогона чистый, `git add -A` проходит. Отслеживаемых
   файлов блок не касается. Блок заменяется при каждом старте, чужие строки файла не
   трогаются. Побочный эффект: неотслеживаемый файл с таким именем в корне репозитория git тоже не видит —
   добавить его в репозиторий можно `git add -f`.

   **Запись у читающих профилей** (`recon`, `review`, фаза плана) закрывают права и хук, а не песочница:
   Bash — только git на чтение из allow (остальное — отказ в `dontAsk` и в фазе плана без auto mode);
   перенаправление `> файл` сверяется с правилами `Edit` и получает отказ; `--output` и `-O` у git ловит хук.
   Весь рабочий каталог в `denyWrite` не ставится: `<dir>/.gitconfig` — защищённый путь песочницы Claude Code,
   bwrap монтирует его в рабочий каталог и при `denyWrite: [<dir>]` не может создать точку монтирования
   (`bwrap: Can't create file at <dir>/.gitconfig: Read-only file system`) — так падает любой Bash, git тоже
   (проверено на 2.1.291, рабочий каталог под `~`). Их `Edit` ТЗ и `.agents/` — файловый инструмент вне
   песочницы.

   **`read_extra`** — что ещё, кроме рабочего каталога, можно читать встроенными файловыми инструментами
   (Read): пути и glob от корня проекта, например правила проекта —
   `read_extra = ["CLAUDE.md", ".agents/*.md"]`. Каждый элемент даёт allow `Read(//<абсолютный путь>)` во всех
   профилях и в фазе плана; база (часть пути до первого компонента с `*`) берётся после `resolve()`.
   Содержимое каталога — `dir/*`, глубже — `dir/*/*`; элемент `dir` даёт правило только на сам путь. В `allowRead` песочницы элементы не попадают: Bash
   у читающих профилей — только git. Deny-правила (`Read(**/.env*)`, `deny_read`, служебные каталоги) сильнее.
   Ошибка реестра для `wshub-code`: при разборе — абсолютный путь, `..`, символы шаблона кроме `*`
   (`? [ ] { } ( ) \`), рекурсивный шаблон `**`; при запуске задачи, как у `dirs`, — база вне корня проекта
   (в том числе через симлинк), пересекается со служебными каталогами wshub (база — корень, например у `*.md`,
   — значит, ни одного служебного каталога внутри корня) или после `resolve()` содержит символы шаблона.
   Хвост шаблона при запуске раскрывается по ФС, один уровень на компонент, и каждое совпадение после
   `resolve()` проверяется так же, как база: симлинк наружу или в служебный каталог — отказ. Файлы и
   симлинки, появившиеся после старта задачи, эта проверка не видит — остаточный риск. По умолчанию список
   пуст — лишних правил нет.
3. **Хук** на Bash (`python -m wshub.hook --profile <профиль> <служебные каталоги…>`; фаза плана —
   `--profile plan`) закрывает формы, которые правила по тексту не ловят (git push в подстановке, сетевые
   утилиты, пути к служебным каталогам; `git remote` кроме чтения; `-O` / `--open-files-in-pager` у `git grep`,
   включая сокращения и склейки; `GIT_*`, `PAGER`, `LESSOPEN`, `LESSCLOSE` перед git). `--output` у
   `log`/`show`/`diff` — в `task` только в файл внутри рабочего каталога и не в `.git`, в `review` — только
   внутри `.agents/`, у остальных — отказ; путь явный, проверка по `normpath` и `realpath`. Помощник хаба
   `wshub push` / `wshub forge` — только командой целиком (одно звено, без обёрток, присваиваний, `$`,
   обратных кавычек и перенаправлений), `wshub push` — только в `task`, другие подкоманды `wshub` — отказ.
   Без `--profile` хук ведёт себя как у читающего профиля. Ошибка разбора — отказ.

   **Пол docker** — пятый пункт хука. Для `docker` и `docker compose` отказ, если команда монтирует не
   именованный том и не абсолютный путь внутри рабочего каталога задачи (`-v`, `--volume`, `--mount`, склейки
   вида `-itv/путь:…`, подстановки `$(pwd)`); ослабляет изоляцию контейнера (`--privileged`, `--cap-add`,
   `--device`, `--pid`, `--ipc`, `--uts`, `--userns`, `--security-opt`, `--volumes-from`, `--runtime`,
   `--network host`); указывает другой демон или чужую конфигурацию docker (`-H`, `--context`, `--config`,
   переменные окружения перед `docker`); работает с файлами хоста или выгружает данные наружу (`cp`,
   `--env-file`, `--cidfile`, `push`, `login`, `save` / `load` / `import` / `export`, `docker build` и
   `buildx`, `compose build --ssh` / `--secret`); удаляет тома (`down -v`, `rm -v`, `volume rm` / `prune`,
   `system prune`); задаёт `-f` с файлом, имя которого не похоже на compose-файл (`docker-compose*.yml`,
   `compose*.yaml`), или `compose --env-file`. Команды docker идут вне песочницы (`sandbox_exclude`), и
   этот пол — их единственная граница.
4. **Окружение** — белый список: `HOME`, `USER`, `LOGNAME`, `PATH`, `LANG`, `LC_*`, `TZ`, `DOCKER_HOST`,
   `DOCKER_CONFIG`, плюс `pass_env`; `TERM=dumb`. Секретов (`SSH_AUTH_SOCK`, имена с `TOKEN`/`SECRET`/
   `PASSWORD`/`KEY`) нет даже из `pass_env`. SSH-агента и токенов у Code нет. Хаб добавляет `WSHUB_JOB`,
   `WSHUB_CONFIG` и `WSHUB_STATE` — по ним помощник находит задачу; это не секреты.

Голый `git push` и `curl` невозможны: запрет держат правила, хук, песочница без сети к git-хосту и окружение
без ключей. Пуш и API хостинга — только помощником хаба по регламенту проекта (раздел ниже).

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
  `git_config_unsafe`. Текст отказа называет ключ, значение (учётные данные в адресе — `***`, не длиннее 200
  символов) и способ починки: `core.hookspath = /path/… — исправь: git -C <репозиторий> config --unset-all
  core.hookspath или укажи верное значение`. Если `.git` — файл (worktree) или рядом есть `.git/commondir`, prefetch и снимок в v0
  отклоняются.

Команды docker выполняются вне песочницы только через `sandbox_exclude` проекта (только профиль `task`),
который правит человек; правка compose-файлов и Dockerfile закрыта `deny_edit`. Рекомендация для реестра:
`bash_allow` и `sandbox_exclude` = `["docker compose *"]`; `docker *` — по потребности. Пол закрывает
случайные необратимые действия; CLI-плагины docker — программы на хосте, это принятый риск. Сейчас — только
`docker compose *`. Принятые остаточные риски:

- у контейнеров есть сеть, песочница их не касается: смонтированное (рабочий каталог) контейнер может отправить наружу;
- `exec` в контейнер стека видит его окружение, `docker compose config` печатает подставленные значения — локальные пароли dev-стека;
- флаги проверяются по всем аргументам, включая команду внутри контейнера: на редком аргументе (`--privileged`, `--env-file`, `-v a:/b`) возможен ложный, но закрытый отказ. `-v ИСТОЧНИК:ЦЕЛЬ` — монтирование, только если ЦЕЛЬ — абсолютный путь: `pytest -v tests/x.py::test_y` внутри контейнера отказа не получает.

**Пол docker защищает от случайного, а не от намеренного обхода.** Задача, которая хочет выйти из изоляции,
найдёт путь, которого пол не видит: свой compose-файл с нужными томами и флагами (имя `docker-compose*.yml`
проходит), CLI-плагины docker (программы на хосте), склейки глобальных опций, `docker volume create --opt`
(том с `type=none,o=bind` на любой каталог хоста), запись в репозиторий и в `.git` через смонтированный рабочий
каталог — контейнер пишет туда мимо песочницы и `denyWrite`. Последнее делает видимым блок «Вне кода» (ниже).
Вернуться к этим путям, если Code начнёт работать с чужим кодом с правом на запись.

## Пуш и API хостинга

Голый `git push` и `curl` у задачи закрыты всегда. Пушит и ходит в API хостинга репозитория (GitLab, GitHub)
помощник хаба — `wshub push` и `wshub forge`: Code вызывает его из Bash, помощник идёт вне песочницы и для git и
API — единственный путь в сеть. Что можно, задаёт регламент проекта в реестре; разрешение даётся задаче.

**Регламент** — ключи `[workspace.<имя>.code]`:

| ключ | значения | по умолчанию | смысл |
|---|---|---|---|
| `push` | `no` / `task` / `always` | `no` | `no` — пуш закрыт; `task` — только задаче с разрешением; `always` — любой задаче профиля `task` |
| `push_protect` | список шаблонов веток | `["main", "master"]` | куда пуш запрещён всегда (`fnmatch`: `release/*`) |
| `forge` | `gitlab` / `github` | — | тип хостинга: заголовок токена, адрес API по умолчанию, встроенные запреты |
| `forge_access` | `no` / `task` / `always` | `no` | доступ к API хостинга через хаб, как у `push` |
| `forge_write` | `true` / `false` | `false` | POST, PUT, PATCH: заметки, создание и правка MR/PR. DELETE — никогда |
| `forge_url` | `https://…` | `github` — `https://api.github.com`; `gitlab` — нет | адрес API; для `gitlab` обязателен (`http://` — только `localhost`) |
| `forge_token` | путь к файлу, `~` раскрывается | — | обязателен, если `forge_access` не `no` |

Неверное значение, `forge_access` не `no` без `forge` или без `forge_token`, `gitlab` без `forge_url` —
ошибка секции проекта. Файл токена проверяется при старте задачи с доступом к API: обычный файл, владелец —
пользователь, права не шире `0600`; иначе отказ с текстом, как исправить. `code_projects` показывает
регламент и путь к токену; значение токена — никогда.

```toml
# проект на GitLab: пуш по разрешению на задачу, токен только на чтение
[workspace.team-service.code]
push = "task"
push_protect = ["master", "dev"]
forge = "gitlab"
forge_access = "task"
forge_url = "https://gitlab.example.com"
forge_token = "~/.config/team-service/gitlab.token"

# личный проект на GitHub: пуш в любые ветки, кроме main; PR через API
[workspace.pet-project.code]
push = "always"
forge = "github"
forge_access = "always"
forge_write = true
forge_token = "~/.config/pet-project/github.token"
```

**Разрешение задачи** — параметры `push` и `forge` у `code_start` и `code_resume`:

- регламент `task` — разрешение только с `reason` ≥ 15 символов: кто разрешил и когда («оператор в чате 15.10:
  запушить и открыть MR»); `always` — разрешение действует само (в `grants` помечено `auto` и после смены
  регламента на `task` не наследуется); `no` — отказ «в проекте <имя> пуш закрыт
  реестром (push = no)»;
- пуш — только задаче профиля `task` и только в фазе исполнения; API хостинга — любому профилю (разведке
  бывает нужно прочитать MR); в фазе плана помощника нет;
- разрешения пишутся в `job.json` (`grants: {push, forge, reason}`), их наследуют звенья цепочки (план →
  исполнение, переделка плана) и `code_resume`; `code_resume` может добавить новое — так работает «задача
  принята, оператор говорит „запушь“» → `code_resume(push=true, reason=…)`. Если регламент с тех пор стал
  `no`, унаследованное разрешение снимается с предупреждением;
- строки `code_start` в журнале — с разрешениями и `reason`.

Слова в тексте ТЗ разрешением не являются. Хаб не может проверить, что оператор действительно разрешил,
поэтому `reason` пишется в журнал, а итог и карточка задачи показывают разрешение.

**Settings задачи.** С разрешением — `Bash(wshub push *)` / `Bash(wshub forge *)` в allow и та же строка в
`sandbox.excludedCommands`; без разрешения — ни того, ни другого.

**Команды помощника.** Хаб передаёт claude `WSHUB_JOB=<id>`. Помощник читает `job.json` и проверяет: задача
идёт (`running`), его рабочий каталог — внутри рабочего каталога задачи, разрешение есть, регламент проекта в
реестре его не закрыл. Любая проверка не прошла — отказ с причиной в stderr, код выхода 1.

```sh
wshub push <remote> <ветка> [-o ключ=значение]…
wshub forge <GET|POST|PUT|PATCH> <путь> [--data <json-файл>] [--out <файл>]
```

`wshub push` от хостинга не зависит:

- `remote` — имя из `git remote` репозитория; адрес вместо имени — отказ;
- ветка — локальная, уходит на remote под тем же именем (`refs/heads/<ветка>:refs/heads/<ветка>`); форма
  `src:dst`, `+`, `refs/…` — отказ; ветка из `push_protect` — отказ;
- всегда `-u`, `--no-follow-tags`, `--recurse-submodules=no`; никогда force, delete, теги, `--all`,
  `--mirror` — других опций, кроме `-o`, нет;
- `-o` (push-опции) — как есть, например `-o merge_request.create` у GitLab; опции, в имени которых есть
  `auto_merge` или `merge_when_pipeline_succeeds`, — отказ при любом регламенте: мерж — действие оператора;
- `git push` идёт как git хаба: проверка `.git/config` по запретному списку, те же переопределения, кроме
  `core.sshCommand` и `credential.helper` (учётные данные — обычные у пользователя: ssh или credential helper
  из глобальной конфигурации git), таймаут 120 с. Вывод git — в stdout как есть: хостинг печатает в нём
  ссылку на MR/PR;
- если в окружении хаба при старте задачи есть `SSH_AUTH_SOCK`, хаб кладёт его в каталог задачи (Code туда не
  видит), и помощник отдаёт его только этому `git push`. В окружение claude `SSH_AUTH_SOCK` не попадает.

`wshub forge`:

- методы: GET — всегда; POST, PUT, PATCH — только при `forge_write = true`; DELETE — никогда;
- путь — от адреса API, начинается с `/`; без схемы, `//`, `..` (в том числе закодированных), `\`, `#` и
  пробелов;
- **пол** — при любом регламенте: API не мержит, не одобряет и не пишет в репозиторий (это делает `wshub push`
  по `push_protect`). Запись (POST, PUT, PATCH) по этим путям — отказ; путь сверяется раскодированным и без
  учёта регистра:
  - `gitlab`: `…/merge_requests/<n>/merge`, `…/approve`, `…/unapprove`, `…/rebase`; всё под `…/repository/`
    (commits, files, branches, tags, submodules и прочее); `…/protected_branches`, `…/protected_tags`;
    `…/merge_trains`, `…/approvals`, `…/approval_rules`, `…/releases` (тег); выпуск учётных данных —
    `…/access_tokens`, `…/personal_access_tokens`, `…/deploy_tokens`, `…/deploy_keys`, `/user/keys`;
  - `github`: `…/pulls/<n>/merge`, `…/merges`, `…/pulls/<n>/reviews` (одобрение), `…/pulls/<n>/update-branch`,
    `…/merge-upstream`, `…/contents/…`, `…/git/…`, `…/branches/<ветка>/…` (защита, переименование),
    `…/rulesets`, `…/releases` (тег), `…/keys`, `…/gpg_keys`, `/graphql`. Комментарии к строкам — через
    `…/pulls/<n>/comments` и `…/issues/<n>/comments`;
  - конец сегмента — `/`, конец пути или `.`: суффикс формата GitLab (`…/merge.json`) пол не обходит;
- токен читается из `forge_token` и не попадает ни в argv, ни в окружение, ни в вывод, ни в файлы: если ответ
  его содержит, он заменяется на `***`; перенаправления помощник не выполняет (иначе заголовок с токеном ушёл
  бы по `Location`);
- ответ до 20 КБ — в stdout; больше — только с `--out <файл>` внутри рабочего каталога, не в `.git/`, не в
  `.claude/`, не в точку монтирования песочницы и не по маске `deny_edit` проекта: помощник пишет вне
  песочницы и повторяет её запреты сам; `--data` — JSON-файл до 1 МБ, тоже внутри рабочего каталога, не `.env*`; ответ 4xx/5xx — код и тело в stderr, код выхода 1;
- `urllib` из стандартной библиотеки, без прокси из окружения, таймаут 30 с.

Для GitHub PR создаётся через `wshub forge POST /repos/<владелец>/<репозиторий>/pulls --data pr.json`:
push-опций для PR у GitHub нет. У GitLab MR создаёт сам пуш: `wshub push origin feat-x -o
merge_request.create`.

**Пресеты хостинга** — пакет `wshub.forges`, модуль на тип: заголовки токена (`gitlab` — `PRIVATE-TOKEN`;
`github` — `Authorization: Bearer`, `Accept: application/vnd.github+json`), префикс пути (`gitlab` —
`/api/v4`, добавляется, если его нет; `github` — от `forge_url`), адрес по умолчанию и пол. Новый хостинг —
новый модуль с `PRESET` и строка в `PRESETS`; реестр, помощник и документация берут список типов оттуда. Пол
— минимальный список, а не полное описание API: новый опасный путь, найденный на практике, добавляется в
модуль пресета строкой и тестом.

**Где видны внешние действия.** Каждый вызов помощника — строка в `<каталог задачи>/external.jsonl` и в
журнале хаба (`wshub_push`, `wshub_forge`): время, команда, аргументы без секретов (значения push-опций вида
`ci.variable=ИМЯ=значение` — `***`), код выхода, HTTP-статус, ссылка на MR/PR, если она есть в выводе.
`code_result` показывает блок «Внешние действия» (`external`) по всем звеньям: пуши, вызовы API числом и
последние 10, ссылки на MR/PR; там же — разрешения задачи (`grants`).

## Прозрачность

Что задача сделала вне диффа, хаб показывает сам, не полагаясь на её отчёт.

**Снимок на старте звена** — до запуска claude, в каталоге звена (`git_start.json`): полный `HEAD` (`git
rev-parse HEAD` через git хаба, с теми же проверками и переопределениями), `.git/hooks/` (обычный файл — имя и
sha256, симлинк — имя и цель; сам каталог — каталог он или симлинк) и sha256 `.git/config`. `.git/hooks` и
`.git/config` хаб читает сам, без git; файлы — не следуя по симлинку и не открывая спецфайлы. Файл без права на
чтение или крупнее 64 МБ сравнивается по размеру и mtime. Если git хаба отказал (небезопасный ключ, worktree),
ошибка пишется в снимок, задача идёт.

**Блок «Вне кода»** (`outside_code`) — в итоге звена, рядом со снимком git:

- `commits` — `git log --oneline <HEAD на старте>..HEAD`, не больше 20 строк, `commits_count` — всего;
- `git_meta_changed` — `.git/hooks/<имя> — новый | изменён | удалён` и `.git/config — изменён` по сравнению
  со снимком на старте; `.git/hooks — изменён` (каталог стал симлинком или исчез) и `.git/hooks — не прочитан`
  (сравнить нельзя) — тоже повод для предупреждения; не больше 30 строк;
- `docker` — Bash-вызовы, где `docker` или `docker-compose` — команда (в начале строки или после `;`, `&&`,
  `||`, `|`): не больше 30, команда — до 200 символов, с пометкой `— отказ`, если вызов вернул ошибку
  (отказ хука или ненулевой код); `docker_count` — всего. Это эвристика по тексту, а не разбор хука: формы
  `X=1 docker …`, `timeout 60 docker …`, `$(docker …)`, `bash -c 'docker …'`, `/usr/bin/docker` в блок не
  попадают;
- `errors` — что собрать не удалось (снимок на старте с ошибкой, git хаба отказал в конце).

`code_result` и `wshub code result` показывают блок на верхнем уровне ответа; у корня цепочки — по всем
звеньям (коммиты новые сверху, docker — по порядку), звенья без блока (идут, не завершились) — в `no_data`.
Если `git_meta_changed` не пуст, первая строка ответа:

> ВНИМАНИЕ: задача изменила .git/hooks или .git/config. Эти правки не видны в диффе и выполнятся твоим git —
> проверь их перед git-командами в этом репозитории

Строка журнала `code_finish` — с `docker_count`, `commits_count` и списком `git_meta_changed`. Восстанавливать
`.git` хаб не пытается — только показывает.

Окно наблюдения — от старта звена до его итога. Контейнер, запущенный задачей (`up -d`, `run -d`), живёт
дольше claude: его запись после итога звена, между `plan_ready` и `code_approve` и после последнего звена блок
не видит. Предупреждение говорит, что `.git` изменился за время звена, а не кто его изменил: правка оператора
или IDE в том же репозитории (`git switch -c` дописывает `[branch]` в `.git/config`) тоже его вызовет.

## Состояния задачи

`starting`, `prefetch`, `running`, `cancelling` → `completed` (успех), `failed`, `max_turns`, `max_budget`,
`timeout`, `cancelled`, `plan_ready` (фаза плана готова и ждёт решения; терминально для звена, не для
цепочки), `orphaned` (надзиратель мёртв, Code жив), `lost` (оба мертвы, итога нет). `orphaned`
и `lost` вычисляются при любом чтении снимка. Отмена: SIGINT процессу Code, через 20 с SIGTERM его группе,
ещё через 10 с SIGKILL группе. Продолжение (`code_resume`) — новая задача с `--resume <session_id>`
родителя; `session_id` известен с первой секунды, поэтому продолжить можно даже задачу в `lost`. Фазу плана
`code_resume` не продолжает — только `code_approve` или `code_replan`.

## Инструменты MCP и CLI

Инструменты сервера `wshub-code`: `code_projects` (с действующей политикой проекта), `code_start`,
`code_status`, `code_wait`, `code_result`, `code_cancel`, `code_resume`, `code_jobs`, `code_plan`,
`code_approve`, `code_replan`, `code_stats`. Все вызовы — в журнал wshub; текст промпта — только sha256.

`code_stats(project?, days=30)` — калибровка политики: по каждой строке политики, источнику и каналу выбора число
задач (цепочек), средние стоимость и ходы, доля `completed`, доля планов, отправленных на `replan`.

То же из терминала:

```sh
wshub code start <project> <task> [--model M] [--effort E] [--plan no|auto|review] [--max-turns N]
                  [--budget-usd B] [--follow]   # --model/--effort/--plan — выбор оператора
wshub code status [job]      # без job — список последних задач
wshub code result <job>      # плюс строка «открыть сессию: cd … && claude --resume …»
wshub code stop <job> | wshub code stop --all
wshub code stats [project] [--days N]
```

`wshub code` без аргументов — сам MCP-сервer (его запускает Desktop). `wshub code-run` и `wshub code-hook` —
служебные (надзиратель задачи и хук Bash), вручную не вызываются. `wshub push` и `wshub forge` — помощник для
задач (раздел «Пуш и API хостинга»), вне задачи отвечает отказом.

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
max_wait_s = 50          # потолок code_wait, секунд (не больше 55: хост обрывает вызов примерно на 60 с)
notify = "poll"          # poll — чат ждёт code_wait; card — карточка задачи сама пишет в чат (раздел «Карточка задачи»)
network = []             # sandbox.network.allowedDomains для всех проектов
deny_read = ["~/.ssh", "~/.aws", "~/.gnupg", "~/.config/wshub", "~/.local/state/wshub",
             "~/.claude/.credentials.json", "~/.claude.json"]
pass_env = []            # имена переменных окружения сверх белого списка (секреты запрещены)
allow_overage = false    # extra usage сверх подписки: false — задача с isUsingOverage останавливается (раздел «Оплата»)

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
проходит; список файлов — `git ls-files` и `ls`. Встроенных Grep и Glob у claude 2.1.291 нет (в `init.tools` их
нет): поиск — `git grep`/`grep`, список файлов — `git ls-files`/`ls`.

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
  `git_config_unsafe`. Текст отказа — «плохой ключ: …» или «конфиг не прочитан: …»; сбой запуска самого git
  (например, удалён рабочий каталог процесса хаба: «Unable to read current working directory») — причина
  `prefetch`, а не `git_config_unsafe`. Текст отказа хаба (поле `error`) видят `code_status`, `code_wait`,
  `code_result`, `wshub code result` и карточка задачи. Текст отказа по ключу называет ключ, значение (учётные данные в адресе — `***`, не длиннее 200
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
`sandbox.excludedCommands`; без разрешения — ни того, ни другого. Правило называет `wshub` по имени, и вне
песочницы запустится первый `wshub` из `PATH`, поэтому при разрешении `PATH` задачи начинается с каталога
настоящего `wshub` хаба, а относительные элементы и каталоги внутри рабочего каталога задачи из него убраны.
Если хаб запущен как `python -m wshub`, а не исполняемым файлом, разрешения `push` и `forge` не выдаются —
отказ старта.

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

- методы: GET — всегда, кроме путей с секретами (ниже); POST, PUT, PATCH — только при `forge_write = true` и
  только по списку пресета; DELETE — никогда;
- **запись — по списку разрешений**: только MR/PR, issues и комментарии, всё прочее — отказ «запись через API —
  только MR/PR, issues и комментарии». Путь сверяется сырым, целиком: проект/владелец/репозиторий — один
  сегмент (у GitLab — числом или закодированным путём `group%2Frepo`), номера и id заметок — цифры, id
  обсуждения — hex, суффикс `.json` допускается:
  - `gitlab`, от `/api/v4/projects/<проект>/`: POST `merge_requests`; PUT `merge_requests/<iid>`; POST
    `merge_requests/<iid>/notes`, `merge_requests/<iid>/discussions`, `merge_requests/<iid>/discussions/<id>/notes`;
    POST `issues`; PUT `issues/<iid>`; POST `issues/<iid>/notes`;
  - `github`, от `/repos/<владелец>/<репозиторий>/`: POST `pulls`; PATCH `pulls/<n>`; POST `pulls/<n>/comments`,
    `pulls/<n>/comments/<id>/replies`; POST `issues`; PATCH `issues/<n>`; POST `issues/<n>/comments`,
    `issues/<n>/labels`;
- **чтение секретов — отказ**: ответ GET уходит в транскрипт. `gitlab`: `…/variables`, `…/hooks`,
  `…/access_tokens`, `…/deploy_tokens`; `github`: `…/actions/variables`, `…/environments/<x>/variables`,
  `…/hooks`;
- путь — от адреса API, начинается с `/`; без схемы, `//`, `..` (в том числе закодированных), `\`, `#` и
  пробелов;
- **пол** — второй слой при любом регламенте: API не мержит, не одобряет и не пишет в репозиторий (это делает
  `wshub push` по `push_protect`). Запись (POST, PUT, PATCH) по этим путям — отказ, даже если путь когда-нибудь
  попадёт в список разрешений; путь сверяется раскодированным и без учёта регистра:
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
  песочницы и повторяет её запреты сам. Файл открывается по цепочке каталогов от рабочего каталога задачи
  (`O_DIRECTORY | O_NOFOLLOW` на каждом, файл — `O_NOFOLLOW`): каталог, подменённый на симлинк после
  проверки, — отказ. `--data` — JSON-файл до 1 МБ, тоже внутри рабочего каталога, по правилам чтения
  песочницы: не `.env*` и не по маскам `deny_read` проекта и `[code]` (относительные — от рабочего каталога).
  Ответ 4xx/5xx — код и тело в stderr, код выхода 1;
- `urllib` из стандартной библиотеки, без прокси из окружения, таймаут 30 с.

Граница `push_protect` — пуш помощника. CI на запушенной ветке (pipeline, Actions) работает со своими
правами и может сам писать в защищённые ветки — это настройка CI проекта, а не wshub.

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

**Снимок на старте звена** — до запуска claude, в каталоге звена (`git_start.json`): полный `HEAD` и ветка
(`git rev-parse --verify HEAD`, `git symbolic-ref --short HEAD` через git хаба, с теми же проверками и
переопределениями; ветка без коммитов — `empty`), `.git/hooks/` (обычный файл — имя и sha256, симлинк — имя и
цель; сам каталог — каталог он или симлинк), sha256 `.git/config` и список его ключей без значений: ключ → хэш
значений с солью снимка. Ключи читает `git config --file .git/config --list --no-includes` в чистом окружении
(как проверка запретного списка — чтение ничего не запускает); если `.git/config` не обычный файл или не
читается, сравнение идёт только по sha256. `.git/hooks` и
`.git/config` хаб читает сам, без git; файлы — не следуя по симлинку и не открывая спецфайлы. Файл без права на
чтение или крупнее 64 МБ сравнивается по размеру и mtime. Если git хаба отказал (небезопасный ключ, worktree),
ошибка пишется в снимок, задача идёт.

**Блок «Вне кода»** (`outside_code`) — в итоге звена, рядом со снимком git:

- `head_start`, `head_end`, `branch_start`, `branch_end` — HEAD и ветка на старте и в конце (у цепочки —
  старт первого звена и конец последнего);
- `commits` — коммиты `<HEAD на старте>..<HEAD в конце>` (`%h %s`), не больше 20 строк, `commits_count` —
  всего. Пустой репозиторий на старте — коммиты от корня. Если старт не предок конца (`reset` назад, другая
  ветка), коммитов нет, а `head_moved` — строка «HEAD ушёл: <ветка> <старт> → <ветка> <конец>». Если ветка
  сменилась, а старт — предок конца, в список идут только коммиты, сделанные после старта звена; более старые
  — чужие (задача перешла на уже существующую ветку), тогда тоже «HEAD ушёл»;
- `git_meta_changed` — `.git/hooks/<имя> — новый | изменён | удалён` и по `.git/config` — имена ключей:
  `.git/config — добавлен | удалён | изменён ключ <ключ>`, с пометкой `(опасный)` для ключей из запретного
  списка; значений нет — в конфигурации бывают учётные данные. Файл изменён без смены ключей — `.git/config —
  изменён, ключи те же`; ключи не прочитаны — `.git/config — изменён`. Ключи `branch.<ветка>.remote` и
  `.merge`, которые поставил сам `wshub push -u` этого звена (успешный пуш ветки в `external.jsonl`), в список не
  идут; `.git/hooks — изменён` (каталог стал симлинком или исчез) и `.git/hooks — не прочитан`
  (сравнить нельзя) — тоже повод для предупреждения; не больше 30 строк;
- `docker` — Bash-вызовы, где `docker` или `docker-compose` — команда (в начале строки или после `;`, `&&`,
  `||`, `|`): не больше 30, команда — до 200 символов, с пометкой `— отказ` (вызов отклонили права или хук) или
  `— ошибка` (прочий `is_error`, например ненулевой код); `docker_count` — всего. `code_result` показывает
  первые 10 команд, `wshub code result` — все 30. Это эвристика по тексту, а не разбор хука: формы
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

## Ходы, действия, время

Одни и те же цифры — в `code_status`, `code_wait`, `code_result` и в карточке задачи:

- **ходы** — как `num_turns` у claude и `--max-turns`: результаты инструментов основного потока (без
  субагентов) плюс один ход на каждый прогон (`init`). Сверено на событиях задач claude 2.1.291: сумма
  `num_turns` итоговых событий совпадает с этим счётом. Уникальные `message.id` (ответы API: параллельные
  вызовы — один ответ) и события `assistant` (по событию на блок ответа) — другие счёты, с `--max-turns` они
  расходятся. `result.num_turns` в итоге — как его вернул claude, по последнему прогону;
- **действия** — все вызовы инструментов (`tool_use`), включая субагентов;
- **«отказ» и «ошибка»** — у вызова, чей результат `is_error`: «отказ» — его отклонили права (событие
  `permission_denied`, `permission_denials` итога, текст «Permission to use …») или хук (текст
  «PreToolUse:… hook error»); «ошибка» — прочее (упавшая команда, тест). Отказ хука claude 2.1.291 тоже
  пишет в `permission_denials` итога;
- **`elapsed_s`** — у идущей задачи до текущего момента, у завершённой — до момента завершения; у корня
  цепочки — от старта первого звена до конца активного;
- `code_wait` ждёт не дольше `min(seconds, max_wait_s)` и отвечает `waited_s` и `cap_s`;
- пути в `code_result` (`files`) — только существующие; файла нет — «нет (задача не дошла до запуска claude)».

## Карточка задачи

`code_start` и `code_resume` рисуют в чате карточку задачи — ресурс `ui://wshub-code/job` (MCP Apps,
`text/html;profile=mcp-app`, один HTML-файл без внешних загрузок). Карточка опрашивает хаб скрытыми
инструментами (`visibility: ["app"]`: модель их не видит, и их вызовы не попадают в контекст чата):

- `code_card_data(job, offset)` — компактный снимок, действия из `events.jsonl` с байта `offset` (у цепочки —
  склейка файлов звеньев по порядку) и новый `offset`, на конечном состоянии — «Вне кода», внешние действия и
  первые 3 строки итога; ключ для двух следующих. Только чтение, в журнал не пишет;
- `code_card_cancel(job, key)` — то же, что `code_cancel`; без верного ключа — отказ;
- `code_card_ack(job, state, key)` — атомарно создаёт `notified-<state>-<активное звено>` в каталоге корня цепочки
  и отвечает `{first: true|false}`: переделка плана и `code_resume` дают новое звено и своё сообщение, а карточки
  корня и продолжения, видящие одно звено, — одно сообщение на двоих. При `notify` ≠ `card` и для состояния,
  которого у задачи сейчас нет, — `{first: false}`.

**Что показывает карточка:** проект · профиль · файл ТЗ, короткий id, состояние, прошедшее время (между опросами
тикает само); разрешения «пуш» и «API» с `reason` во всплывающей подсказке; фазы «план → исполнение», если у
задачи есть фаза плана; последние 6 действий — время от старта, инструмент и главный аргумент (путь от рабочего
каталога, команда Bash до 80 символов, шаблон), пометка «отказ» или «ошибка»; ходы, действия, стоимость (у
подписки — оценка, до итога — «подписка»), окно лимита (`utilization` из `rate_limit_event`, иначе «—»). В конце
— предупреждение про `.git` первой строкой, «вне кода» (коммиты, docker, «HEAD ушёл»), внешние действия (пуш,
ссылки на MR/PR, вызовы API) и первые 3 строки итога Code. Кнопки: «Отменить» (подтверждение вторым нажатием
внутри карточки) и «Журнал» (последние 50 действий). Текст из событий выводится только как текст, не как HTML.
Опрос — каждые 3 с, пока задача идёт и карточка видна; каждые 15 с, если карточка скрыта или задача в
`plan_ready`; на конечном состоянии цепочки — остановка.

**Режим `notify`** (`[code] notify`, по умолчанию `"poll"`; значение на момент старта пишется в `job.json`,
звенья цепочки и `code_resume` берут его у корня). Ответ `code_start` и `code_resume` говорит чату, что делать
дальше (`next`):

- `poll` — «Следи: code_wait(job).» Пока состояние не изменилось и не конечное, `code_wait` отвечает одной
  строкой до 200 байт: `running · 4:14 · ходов 23 · Edit src/app/report.py · ждал 50/50 с`; изменилось или
  конечное — полный снимок;
- `card` — «Заверши ход. Карточка задачи напишет в чат, когда понадобится решение по плану или будет итог.»
  Карточка один раз (`code_card_ack` с `first: true`) пишет в чат через `ui/message`: при `plan_ready` и
  `plan = review` — «wshub-code: план задачи <job> готов и ждёт решения. Прочитай: code_plan("<job>").», на
  конечном состоянии — «wshub-code: задача <job> — <state>[, <reason>]. Итог: code_result("<job>").». Хост
  вернул ошибку — карточка показывает «Не удалось написать в чат. Напиши в чат: code_result("<job>")».
  Переоткрытая карточка второго сообщения не пишет.

`card` включать, когда проверено, что хост показывает карточку и принимает `ui/message` (поддержка `ui/message` в
Claude документацией не подтверждена). Без MCP Apps карточки нет — работает только `poll`.

## Оплата

Задача Code идёт **только по подписке** claude.ai: режима оплаты по ключу API у неё нет. Подписку claude
показывает в событии `init` полем `apiKeySource: "none"`; ключ из `apiKeyHelper` или `env.ANTHROPIC_API_KEY` в
настройках проекта переключает оплату молча, и `forceLoginMethod` в `--settings` это не перебивает (PoC WSH-4.0).
Поэтому хаб держит оплату в три слоя.

**До старта** (`code_start`, `code_resume`, `code_approve`, `code_replan`, `plan = auto`) хаб читает
`.claude/settings.json` и `.claude/settings.local.json` рабочего каталога задачи: claude берёт их только оттуда.
Отказ, если файл задаёт ключ `apiKeyHelper` или в объекте `env` — имя `ANTHROPIC_*`, `CLAUDE_CODE_USE_*`,
`CLAUDE_CODE_OAUTH_*` или `AWS_BEARER_TOKEN_BEDROCK` (семейства — префиксами): «задача пошла бы не по подписке:
<файл> задаёт <ключ>». Отказ и тогда, когда хаб не может проверить, что в файле: не JSON-объект, больше 1 МБ, не
обычный файл (каталог, устройство, FIFO — в том числе через симлинк). Пустой файл (0 байт или одни пробелы) —
«настроек нет»: так остаётся точка монтирования песочницы. Каталог задачи при отказе не создаётся.

**Что запрещено задаче.** claude перечитывает файлы настроек на ходу, в том числе `apiKeyHelper` и `env`: правка
посреди сессии переключила бы оплату. Во всех профилях и в фазе плана — `deny` на `Edit(./.claude/settings.json)` и
`Edit(./.claude/settings.local.json)`. Переменные тех же семейств не доходят до окружения задачи, даже если они в
`[code] pass_env`; реестр с такими именами в `pass_env` — предупреждение, а не ошибка.

**Сверка в потоке.** Надзиратель читает `events.jsonl` по мере записи, в том же цикле, что следит за отменой:

- `init` — в `job.json` пишется `billing.source` (`apiKeySource`) и `billing.model` (фактическая модель за
  алиасом). `apiKeySource` не `"none"` или поля нет — остановка тем же путём, что `code_cancel`;
- `rate_limit_event` — последний `rate_limit_info` целиком в `billing.rate`, время прихода в `billing.rate_at`,
  счётчик событий звена в `billing.rate_events`; `isUsingOverage: true` — остановка, если не `allow_overage = true`.

Три причины завершения (состояние `failed`), каждая — в строке оплаты и в `reason_hint`:

- `billing_mismatch` — задача пошла не по подписке; фактический источник — `result.billing_source` (`null` — поля
  `apiKeySource` не было);
- `overage` — задача начала тратить extra usage сверх подписки; разрешить — `[code] allow_overage = true`
  (глобально, по умолчанию `false`);
- `subscription_limit` — итог с `is_error: true` при последнем `status: "rejected"`: лимит подписки исчерпан;
  `result.resets_at` — время сброса, подсказка — «после <время> продолжи: code_resume». На ключ задача не переходит.

Причина стража важнее отмены и таймаута: если claude вышел раньше, чем надзиратель прочитал событие, причина всё
равно ставится по хвосту потока.

**Строка оплаты** — поле `billing` в `code_status`, полном ответе `code_wait`, `code_result`, `wshub code status` и
`wshub code result`:

```
оплата: подписка · 5 ч — 11 %, сброс 15.10 10:20 · неделя — 23 %, сброс 20.10 19:00 · overage: выкл (org_level_disabled)
```

Окна — из `unifiedWindows` (`five_hour` — «5 ч», `seven_day` — «неделя», прочие — под своим именем), процент —
`utilization × 100`, время сброса — по часам хоста. `status: "allowed_warning"` — «близко к лимиту · » в начале
строки; событий ещё не было — «оплата: подписка (данных о лимитах ещё нет)». `code_result` показывает и модель:
`model_line` — «модель: sonnet → claude-sonnet-5-5». `code_projects` в поле `rate` каждой строки — последний
известный снимок лимитов (`job`, `project`, `at`, `line`) из самой свежей задачи хаба с `billing.rate`; нет такой
задачи — `null`. В журнал строкой `code_finish` идут `billing_source`, `billing_model`, `rate_events` и `rate`
(окно, его `utilization`, `status`, `isUsingOverage`). Тихая строка `code_wait` и карточка задачи строку оплаты не
показывают.

**Остаточные риски:**

- остановка по `init` не гарантирует нулевой расход: если запрос к API уходит раньше `init`, на чужой счёт попадёт не
  больше одного запроса. Главная защита — проверка до старта и запрет правки настроек;
- задача может получить `.claude/settings*.json` не правкой, а через git (`checkout`, `merge` ветки с таким файлом):
  до конца звена этого никто не заметит, поймает проверка до старта следующего звена;
- сколько `rate_limit_event` приходит в многоходовом запуске, не проверено (покажет `rate_events`): overage может
  обнаружиться поздно. Основная защита — extra usage выключен на уровне организации (`org_level_disabled`);
- значение `status: "rejected"` взято из стороннего источника, живьём исчерпание лимита не проверялось;
- шкала `utilization` (доля 0..1) — по одному наблюдению.

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
`code_approve`, `code_replan`, `code_stats`; у каждого — название по-русски («Code: запуск», «Code: ожидание»…),
если установленная `mcp` поддерживает `title`. Скрытые инструменты карточки — `code_card_data`,
`code_card_cancel`, `code_card_ack`. Все вызовы, кроме `code_card_data`, — в журнал wshub; текст промпта —
только sha256.

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

`wshub code` без аргументов — сам MCP-сервer (его запускает Desktop). Серверы и надзиратель задачи при старте
переходят в домашний каталог, надзиратель запускается с рабочим каталогом в каталоге задачи, а чтение
`.git/config` идёт с `cwd=/`: удалённый каталог, из которого запустили хаб, git не ломает. `wshub code-run` и `wshub code-hook` —
служебные (надзиратель задачи и хук Bash), вручную не вызываются. `wshub push` и `wshub forge` — помощник для
задач (раздел «Пуш и API хостинга»), вне задачи отвечает отказом.

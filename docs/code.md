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
# лимиты расхода: на подписке по умолчанию их нет, задаются оператором в реестре — раскомментируй нужные
# max_turns = 150                 # потолок ходов задачи (--max-turns)
# max_budget_usd = 20             # потолок бюджета задачи (--max-budget-usd)
# daily_budget_usd = 60           # дневной бюджет проекта
# max_hours = 4                   # потолок времени звена
max_parallel = 1
prefetch = true                   # git fetch --all --prune силами хаба перед стартом
status_regex = '\*\*Статус:\*\*\s*(\S+)'   # как найти статус в файле ТЗ
bash_allow = ["docker compose run --rm migrations"]   # добавка к allow профиля task
sandbox_exclude = ["docker compose run --rm migrations"]   # команды вне песочницы
network = []                      # добавка к [code].network
deny_read = ["~/.config/team-service"] # добавка к [code].deny_read; относительная маска — от рабочего каталога задачи
allow_read = []                   # sandbox.filesystem.allowRead сверх рабочего каталога и настроек git
deny_edit = ["docker-compose*.yml", "Dockerfile*", ".gitlab-ci.yml"]
claude_md_excludes = []           # claudeMdExcludes
read_extra = ["CLAUDE.md", ".agents/*.md"]   # Read вне dir: пути и glob от корня проекта, только чтение
plan_budget_share = 0.3           # доля бюджета задачи на фазу плана; работает, только если задан max_budget_usd
# plan_max_turns = 40             # ходы фазы плана; по умолчанию не задан
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
`model_min = "haiku"`, `model_max = "opus"`, `effort_max = "high"`, `max_parallel = 1`, `prefetch = false`,
`plan_budget_share = 0.3`, `execute_prompt = "execute"`, `push = "no"`, `push_protect = ["main", "master"]`,
`forge_access = "no"`, `forge_write = false`; списки пустые. Каждый
элемент `dirs` после `resolve()` лежит внутри корня проекта, существует и строго не пересекается со
служебными каталогами wshub (код, реестр, состояние) — иначе ошибка реестра для `wshub-code`.

**Лимиты расхода.** Ключи `max_turns`, `max_budget_usd`, `daily_budget_usd`, `max_hours`, `plan_max_turns` по
умолчанию не заданы: на подписке по умолчанию лимитов нет, их задаёт только оператор в реестре. Пока ключ не
записан, соответствующий лимит не действует:

- `max_turns`, `max_budget_usd` — флаги `--max-turns` и `--max-budget-usd` в claude не передаются (у claude
  умолчания нет: без флага он не останавливает ни по ходам, ни по расходу);
- `daily_budget_usd` — дневной бюджет проекта не считается вовсе; если он задан, а у задачи бюджета нет, идущая
  задача входит в дневную сумму как 0 (до итога);
- `max_hours` — надзиратель не останавливает задачу по времени; определение `lost` (процесс умер) не меняется;
- `plan_max_turns` и доля `plan_budget_share` — фаза плана получает потолок ходов (меньшее из `plan_max_turns`
  и `max_turns`; нет ни того, ни другого — флага нет) и долю бюджета `plan_budget_share` (только при заданном
  `max_budget_usd`); остаток ходов цепочки («ходы задачи исчерпаны») считается, только если задан `max_turns`,
  остаток бюджета — только если у задачи есть бюджет.

`max_turns` и `budget_usd` из шапки ТЗ или параметров `code_start`/`code_resume` при незаданном потолке оператора
не применяются: в предупреждениях задачи одна строка «лимит из задачи не применён: на подписке лимиты задаёт
только оператор в реестре» (лимит, унаследованный `code_resume` от родителя, молча не применяется). Потолок задан —
как раньше: выше потолка отказ, ниже применяется. `code_projects` показывает незаданный лимит словом «нет».
Остановка при платном продолжении (`allow_overage`) — не лимит подписки, а защита от трат, она не меняется.

Флаги терминала `wshub code start --max-turns/--budget-usd` — ручной выбор оператора: применяются как явный лимит
и без потолка в реестре, и выше него (ходы — в обоих режимах, бюджет — на подписке); на ключе бюджет не выше
`max_budget_usd`, если он задан (раздел «Задачи на ключе»). Лимиты на ключе — там же.

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
max_turns: 80        # необязательно; применяется, только если оператор задал потолок max_turns в реестре
budget_usd: 5        # то же для budget_usd и max_budget_usd
# switch: -c PAY-1101 origin/main   # необязательно: ветку ставит хаб до старта Code (ниже)
# model: opus        # model, effort, plan — только чтобы отступить от политики:
# choice: operator   #   с choice: operator — выбор оператора, иначе нужен choice_reason
# choice_reason: …   #   (причина отступления чата, ≥ 15 символов)
---
```

Обязательны `ticket`, `dir`, `profile`; для `task` — `size` и `risk`, для `recon` — `size` (без них задача не
стартует, отказ объясняет, по чему выбирается модель, и приводит строки политики профиля). Файл ТЗ лежит
внутри `dir`, `dir` есть в `code.dirs`, профиль — в `code.profiles`, файл обычный и ≤ 1 МБ. Бюджет и ходы
проверяются по потолкам реестра, когда потолки заданы; модель, effort и план выбирает политика (ниже).

**Ключ `switch:`** — ветка, на которой задача начинает. Три формы, те же, что у помощника `wshub switch` (раздел
«Смена ветки»):

| значение | что делает хаб | профили |
|---|---|---|
| `switch: feat-x` | `git switch --no-guess feat-x` — только существующая локальная ветка | все |
| `switch: -c PAY-1101 origin/main` | `git switch --no-track -c PAY-1101 origin/main` — новая ветка без upstream | только `task` |
| `switch: --detach origin/feat-x` | `git switch --detach origin/feat-x` | все |

Форма и профиль проверяются при `code_start`, имя новой ветки по маске `push_protect` — отказ, как у пуша. Ветку
меняет надзиратель — после `prefetch` (если он включён) и до запуска claude, вне песочницы, через тот же безопасный
вызов git, что и `prefetch`. **Уже на месте — ничего не делать**: для `<ветка>` и `-c <ветка> …` HEAD уже на этой
ветке, для `--detach <ссылка>` HEAD отсоединён и равен коммиту ссылки. Поэтому звено исполнения после плана и
`code_resume` второй раз ветку не меняют. `-c`, ветка уже есть, а HEAD не на ней — отказ «ветка <ветка> уже есть,
HEAD на <текущая> — поправь switch в шапке». Сбой — звено `failed` с `reason = switch` и текстом git в `error`,
claude не запускается. После переключения хаб заново снимает снимок git на старте: блок «Вне кода» считает коммиты
от HEAD после `switch:`. В `job.json` — поле `switch`: `{spec, from, to, done}` (`done = false` — уже было на
месте); `code_result`, `wshub code result` и карточка показывают его строкой «ветка: switch … — from → to».

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
   потолки бюджета и ходов — действуют, если оператор их задал.
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

**Блок «От хаба».** После подстановки шаблона хаб дописывает в конец промпта каждого звена (старт,
`code_resume`, `code_approve`, `code_replan`) блок — только непустые строки, весь блок не больше 2 КБ:

```

---
От хаба wshub (добавлено к шаблону):
- Замечание оператора: <note>
- Гейты — проверки вне песочницы, только командой `wshub gate <имя>` целиком, Bash с timeout не меньше указанного: test-docker (timeout 660000 мс) — .venv/bin/python -m pytest -m docker. Полный вывод — `--out <файл>`.
- Песочница скрывает отслеживаемые файлы: .env.example, cfg/.env.local (и ещё 3). В `git status` задачи они выглядят изменёнными — это не правка задачи: не восстанавливай их и не добавляй в коммит. `git add -A` и `git add .` на них падают — добавляй файлы в индекс явными путями.
```

- замечание — `note`, если в шаблоне нет `{note}` (с `{note}` оно подставляется и в блоке не повторяется);
  шаблон `replan` по-прежнему обязан содержать `{note}`;
- гейты — только профилю `task`, не больше 10 (дальше «и ещё N»), таймаут Bash — `(timeout_s + 60) × 1000` мс;
  в фазе плана — «Гейты будут в фазе исполнения: …»; команда гейта — не длиннее 100 символов, с управляющими
  символами — в кавычках с экранированием, как пути ниже;
- скрытые файлы — отслеживаемые (`git ls-files` силами хаба) по тем же маскам, что получает песочница: имя
  `.env*` и `deny_read` `[code]` и проекта (относительные — от рабочего каталога, абсолютные и `~/` — если ведут
  внутрь него). В блоке — до 20 путей, каждый не длиннее 200 символов; путь с управляющими символами — в двойных
  кавычках с экранированием (`\n`, `\t`, `\xHH`, `\uHHHH`), как у git `core.quotePath`. В `job.json` —
  `sandbox_hidden`, до 200, как есть. Сбой git — строки нет, причина — в `warnings`. Замечание из одних пробелов
  строки не даёт. Блок не помещается в 2 КБ — списки сокращаются: пути до одного, гейты до одного, затем пути до
  нуля («скрыто файлов: N, список — `sandbox_hidden` в job.json»), чтобы хвост инструкции не срезался.

## Режим планирования

Задача с `plan = auto` или `review` идёт двумя фазами одной сессии Claude Code:

1. **Фаза плана** — `--permission-mode plan`, `--session-id`, модель и effort — `plan_model`/`plan_effort`,
   бюджет — `plan_budget_share` от бюджета задачи, ходы — `plan_max_turns` (оба — только если заданы
   соответствующие лимиты реестра), промпт — шаблон из шапки.
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
   эталон профиля, модель и effort исполнения, шаблон `execute`, бюджет и ходы — остаток задачи (если у задачи
   есть бюджет и потолок ходов).

`plan = auto`: надзиратель фазы плана сам запускает исполнение тем же путём, что `code_approve` (тот же
лок, проверки `max_parallel` и дневного бюджета). Не прошли проверки — план остаётся в `plan_ready`
с причиной (`chain_error`), дальше решает человек. `code_approve` и `code_replan` из чата при `plan = auto` не
нужны: после автоперехода — отказ «план утверждён автоматически (plan = auto), исполнение — задача <id>; …»,
пока надзиратель утверждает — «план утверждается автоматически…», `code_replan` — «переделать план нельзя;
нужен разбор плана — запускай с plan = review». Автопереход не удался (`chain_error` или надзиратель мёртв) —
`code_approve` работает, как при `review`. `plan = review`: фаза плана останавливается в `plan_ready`
и ждёт: `code_plan` — текст плана, `code_approve` — исполнение, `code_replan(note)` — новая фаза плана в той
же сессии с шаблоном `replan`.

**Стоимость.** После `--resume` claude отдаёт `total_cost_usd` накопительно за сессию, а `--max-budget-usd`
считает только текущий запуск. Поэтому стоимость звена — разность `total_cost_usd` с предыдущим звеном
(`phase_cost_usd` в итоге), бюджет исполнения — остаток бюджета задачи; дневной бюджет и `code_stats`
считают каждую фазу один раз (это касается и обычного `code_resume`). Стоимость хранится в `job.json`, но на
подписке сумма-оценка в долларах не показывается (карточка, `code_status`, `code_result`, `code_jobs`,
`wshub code status/result/stats`, средняя цена в `code_stats`).

**Цепочка.** Снимок корневой задачи (`code_status`, `code_wait`, `code_result`) показывает фазу, id активного
звена и все звенья (суммы в долларах на подписке не показываются). `code_cancel`, `code_plan`, `code_approve`, `code_replan`
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
   каталога wshub — deny `Read` и `Edit`. Относительная маска `deny_read` (`[code]` и проекта) считается от
   рабочего каталога задачи и идёт абсолютным путём — и в эти правила, и в `denyRead` песочницы: сырую
   относительную строку песочница не применяет (стенд WSH-3.5). `..` в относительной маске — ошибка реестра.
   deny всех профилей: `git push`, `git clean`, `git remote` с `add`, `set-url`, `remove`, `rm`, `rename`,
   `prune`, `update`, `set-head`, `set-branches`, `show`, `ssh`, `scp`, `sudo`, `curl`, `wget`, `Read(**/.env*)`,
   `WebFetch`, `WebSearch`, маски `deny_edit`.
2. **Песочница** (`sandbox`, нужны bwrap и socat). Включена, `failIfUnavailable: true`,
   `allowUnsandboxedCommands: false`, `autoAllowBashIfSandboxed: false` — Bash идёт только по allow-правилам.
   Сеть — только `network`. Чтение сужено: `denyRead` включает `~/` и `/mnt/`, `allowRead` открывает рабочий
   каталог, `~/.gitconfig`, `~/.config/git/` и `allow_read` проекта. Запись: у всех профилей и в фазе плана
   закрыты `./.git/config`, `./.git/hooks`, `./.git/info`, `./.git/modules` (`denyWrite` и deny `Edit`), запись
   вне рабочего каталога закрыта песочницей. `sandbox_exclude` проекта действует только в `task`; у `recon` и
   `review` в `excludedCommands` — только помощник хаба при разрешении задачи (раздел «Пуш и API хостинга») и, у
   `review` в фазе исполнения, `wshub switch` (раздел «Смена ветки»).
   Вне песочницы, кроме `sandbox_exclude`, идут только `wshub push`, `wshub forge`, `wshub gate` и `wshub switch`.

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
   `wshub push` / `wshub forge` / `wshub gate` — только командой целиком (одно звено, без обёрток, присваиваний,
   `$`, обратных кавычек и перенаправлений), `wshub push` и `wshub gate` — только в `task` (у `wshub gate` — ровно
   имя гейта и, может быть, `--out`), другие подкоманды `wshub` — отказ.
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
  проверки, — отказ. Файл `--out` — только обычный: FIFO, устройство — отказ «не обычный файл» (открытие с
  `O_NONBLOCK` и проверка `fstat`, FIFO без читателя помощника не повесит). Файл открывается до HTTP-запроса:
  отказ по `--out` не приходит после выполненной записи в API. `--data` — JSON-файл до 1 МБ, тоже внутри
  рабочего каталога, по правилам чтения песочницы: не `.env*` и не по маскам `deny_read` проекта и `[code]` (относительные — от рабочего каталога).
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

## Гейты

Проверкам проекта бывает мало песочницы: тестам с docker нужен демон, сборке образа проверок — сеть. Гейт —
именованная проверка из реестра проекта: задача профиля `task` запускает её одной командой `wshub gate <имя>`,
помощник хаба исполняет её вне песочницы, с таймаутом, полным логом и строкой учёта. Проверку, которую сейчас
выпускает из песочницы `sandbox_exclude` с точной строкой команды, лучше оформить гейтом: вывод виден, конвейер не
ломает совпадение, каждый запуск учтён.

```toml
[workspace.example.code.gates]
test-docker = ".venv/bin/python -m pytest -m docker"              # строка — одна команда без оболочки
check = { cmd = ["scripts/check.sh"], timeout_s = 1500 }          # таблица: cmd строкой или списком
```

| что | правило |
|---|---|
| имя | `^[a-z0-9][a-z0-9_-]{0,39}$` — как в хуке |
| значение | строка (= `cmd`) или таблица `{ cmd, timeout_s }`; прочие ключи — предупреждение «неизвестный ключ» |
| `cmd` строкой | `shlex.split`, без оболочки; оператор оболочки (`\|`, `\|\|`, `&`, `&&`, `;`, `<`, `>`, `>>`, `2>&1` и т. п.), `$` или обратная кавычка — ошибка: нужна цепочка — скрипт в репозитории или `cmd` списком; оператор в кавычках (`awk -F '\|'`) тоже считается оператором — такую команду пиши `cmd` списком |
| `cmd` списком | непустые строки без NUL, как есть: argv задаёт оператор (например, `["sh", "-c", "…"]`) |
| `cmd[0]` | абсолютный путь; относительный с `/` — от рабочего каталога задачи, без `..`; голое имя — из `PATH` окружения гейта на момент запуска; `~` и `$` не раскрываются — ошибка в обеих формах, нужен абсолютный путь |
| `timeout_s` | целое 1…3600, по умолчанию 600 |

Нет ключа или `gates = {}` — гейтов нет. Ошибка в `gates` — ошибка секции проекта, как у прочих ключей
`[workspace.<имя>.code]`: отказывает только этот проект. `code_projects` показывает гейты списком
`{name, cmd, timeout_s}`, `cmd` — одной строкой.

**Кому.** Только профилю `task` и только в фазе исполнения: гейт исполняет код рабочего каталога. У читающих
профилей и в фазе плана правил гейта нет, хук и помощник отказывают. У задачи с гейтами в settings —
`Bash(wshub gate *)` в allow и `wshub gate *` в `sandbox.excludedCommands`, `PATH` начинается с каталога
настоящего `wshub` (как у `push`/`forge`) и `env.BASH_MAX_TIMEOUT_MS` = наибольший `timeout_s` плюс минута, не
меньше 600000: команда Bash, дошедшая до своего таймаута, уходит в фон. Хаб запущен как `python -m wshub` —
правил гейтов нет, задача стартует с предупреждением «гейты недоступны: хаб запущен как python -m wshub».
Правила settings, потолок Bash и строка гейтов в промпте пишутся на старте звена. Новый гейт или больший
`timeout_s` полностью действуют со следующего звена. Убранный из реестра гейт отказывает сразу.

**Запуск** — `wshub gate <имя> [--out <файл>]`. Помощник проверяет: задача идёт, его рабочий каталог — внутри
рабочего каталога задачи, профиль `task` и фаза не план, гейт есть в текущем реестре (реестр перечитывается:
убранный гейт — отказ «гейта <имя> нет в реестре проекта (есть: …)»), `--out` — по тем же правилам, что у
`wshub forge`. Один гейт на каталог задачи за раз: неблокирующий `flock` на
`<состояние хаба>/code/gates/<проект>/<ключ>.lock`, ключ — первые 16 знаков sha256 от реального пути рабочего
каталога задачи; занято — отказ «в каталоге <dir> уже идёт гейт <имя> задачи <id>». Рядом, в `<ключ>.holder`, —
кто держит лок (задача, гейт, каталог) и группа его гейта. Лок свободен, а группа из `.holder` ещё идёт (прежний
помощник умер, не погасив гейт) — новый помощник гасит её той же лестницей, пишет «погашен осиротевший гейт <имя>
задачи <id>» и поле `orphan_killed` в учёт, потом запускает свой гейт. Любой отказ — строка учёта и код выхода 1.

Гейты разных каталогов одного проекта (`dirs`) идут параллельно, в одном каталоге — по одному. Общие ресурсы
гейтов разных каталогов — порты, имена проектов docker compose, общие тестовые базы — разводит сам проект.

- argv — из реестра, без оболочки; рабочий каталог — рабочий каталог задачи; stdin — `/dev/null`, stdout и
  stderr — один поток; свой сеанс;
- окружение — белый список задачи (`env.json` звена) без `WSHUB_*`; окружение процесса помощника не берётся;
- пока гейт идёт, `pid`, `pgid` и `proc_start` гейта лежат в `gate.pid` каталога звена; `gate.pid` не записался —
  гейт гасится, отказ; задача уже не `running` к моменту записи (отмена пришла до запуска) — гейт гасится, код 130;
- `timeout_s` истёк — SIGTERM группе гейта, 10 с, SIGKILL группе, код 124; помощник получил SIGTERM, SIGINT или
  SIGHUP — то же, код 130; сигнал во время лестницы таймаута или после выхода гейта итог не меняет; остановка
  задачи (`code_cancel`, `max_hours`, остановка по оплате), `code-kill` и выход claude гасят группу из `gate.pid`
  той же лестницей, с защитой `proc_start`; `code_cancel` задачи в конечном состоянии (в том числе `lost`) с
  `gate.pid` гасит гейт в фоне, состояние не меняет; потомки, оставшиеся в группе после выхода гейта, тоже гасятся.

**Вывод.** Полный лог — `<каталог звена>/gates/<NN>-<имя>.log` (NN — номер вызова в звене), не больше 50 МБ:
дальше вывод не пишется, в конце — строка «… обрезано: N байт». В stdout помощника — последние 8000 символов
вывода и строка итога `wshub gate <имя>: код N · 123 с · полный вывод: --out <файл>` (с `--out` — путь файла; при
таймауте — «прерван: timeout_s = N»). Claude показывает модели около 10000 символов вывода неуспешной команды и
около 30000 — успешной: хвост с итогом виден целиком. `--out` — полный лог в файл внутри рабочего каталога, с тем
же пределом. Код выхода помощника — код гейта (таймаут — 124, сигнал помощнику — 130, гейт убит сигналом S —
128 + S).

**Учёт.** Каждый вызов — строка в `external.jsonl` звена и в журнале хаба (`wshub_gate`): имя, `--out`, код,
`duration_s`, `timeout`, путь лога. `code_result`, `wshub code result` и карточка показывают в «Внешних
действиях» последние 10 вызовов (имя, код, длительность, таймаут), число гейтов и сколько упало.

> **Гейт исполняет код рабочего каталога вне песочницы** с правами пользователя: тесты, `conftest.py`, скрипты, всё, что задача могла изменить. Такой код читает всё, что читает пользователь (в том числе токены в `~/.config`), ходит в сеть, а с docker — фактически root. Хаб держит форму команды, таймаут и учёт — это защита от случайного, а не от намеренного. Заводи гейт там, где доверяешь задачам проекта так же, как `sandbox_exclude` с `docker compose`. Контейнеры, которые запустили тесты, переживают прерванный гейт — их убирает сам тестовый код или оператор. Процессы, которые гейт увёл в свой сеанс (`setsid`, демоны, двойной fork), хаб не гасит: он гасит только группу гейта.

## Смена ветки

**Зачем.** Песочница монтирует поверх рабочего дерева файлы из `deny_edit` и скрытые (`.env*`, `deny_read`).
`git switch` и `git checkout -- <файл>` в задаче на таком файле, если он различается между ветками, падают:
`error: unable to unlink old '<файл>': Device or resource busy` (код 255). Скрытый `.env.example` в песочнице
выглядит изменённым, и `git switch` отказывает, если файл различается между ветками. Поэтому ветку меняет хаб, вне
песочницы: до старта — ключом `switch:` в шапке ТЗ (раздел «Шапка запуска»), по ходу задачи — помощником
`wshub switch`.

| форма | что делает git | профили |
|---|---|---|
| `wshub switch <ветка>` | `git switch --no-guess <ветка>` — только существующая локальная ветка | `task`, `review` |
| `wshub switch -c <ветка> <база>` | `git switch --no-track -c <ветка> <база>` | только `task` |
| `wshub switch --detach <ссылка>` | `git switch --detach <ссылка>` | `task`, `review` |

Форма одна для шапки и помощника: других опций нет — никаких `--force`, `--discard-changes`, `--merge`, `--orphan`,
`--track`. Незакоммиченные правки переносятся, как у обычного `git switch`; конфликт — отказ git как есть. Имя
ветки проверяет `git check-ref-format --branch`, ссылку и базу — `git rev-parse --verify --quiet <ссылка>^{commit}`;
`-c` на существующую ветку — отказ «ветка <ветка> уже есть — переключись на неё: wshub switch <ветка>», имя по маске
`push_protect` — отказ, как у пуша. git идёт через тот же безопасный вызов, что `prefetch`: проверка `.git/config`,
фиксированные переопределения, белый список окружения, таймаут.

**Кому.** `wshub switch` — профилям `task` и `review` в фазе исполнения, без разрешений задачи: в settings —
`Bash(wshub switch *)` в allow и `wshub switch *` в `sandbox.excludedCommands`, `PATH` начинается с каталога
настоящего `wshub`; в блоке «От хаба» — строка с формами (у `review` — без `-c`). У `recon` и в фазе плана правил
нет, хук и помощник отказывают. Хаб запущен как `python -m wshub` — правил нет, задача стартует с предупреждением
«смена ветки помощником недоступна: хаб запущен как python -m wshub»; ключ `switch:` в шапке работает и так.
`git switch *` и `git checkout *` в правилах `task` остаются: без смонтированных файлов они работают, а
`git checkout -- <файл>` нужен для отката мутаций.

**Помощник.** Проверяет: задача идёт, его рабочий каталог — внутри рабочего каталога задачи, профиль и фаза, форма.
stdout и stderr git — в stdout помощника как есть, код выхода — код git; отказ — код 1 и причина в stderr.

**Учёт.** Каждый вызов — строка в `external.jsonl` звена и в журнале хаба (`wshub_switch`): аргументы, код, `from`
и `to` (ветка или коммит). `code_result`, `wshub code result` и карточка показывают в «Внешних действиях» последние
10 смен ветки и их число (`switch`, `switch_count`).

> **`wshub switch` и `switch:` запускают git вне песочницы** в рабочем каталоге задачи. git при смене ветки применяет атрибуты из `.gitattributes`, а задача могла его изменить. Фильтры из `.git/config` хаб не допускает (`filter.*` — в запретном списке), но фильтры из глобальной конфигурации пользователя (например, git-lfs) git вызовет на файлах, которые выбрала задача. Хуки git выключены (`core.hooksPath=/dev/null`).

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
каталога, команда Bash до 80 символов, шаблон), пометка «отказ» или «ошибка»; ходы, действия, оплата
(«подписка»; суммы в долларах не показываются), окно лимита (`utilization` из `rate_limit_event`, иначе «—»). В конце
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

Режим оплаты задачи — `billing`: `subscription` (по умолчанию, подписка claude.ai) или `key` (API-ключ
`anthropic` хаба, раздел «Задачи на ключе»). Режим пишется в `job.json` полем `billing.mode` и наследуется всеми
звеньями цепочки: план → исполнение, `code_replan`, `code_resume`. Сменить его внутри цепочки нельзя: `code_resume`
с другим `billing` — отказ «сменить режим оплаты внутри цепочки нельзя». Ниже — страж подписки; на ключе он тот же,
но ожидает другой источник оплаты.

Подписку claude показывает в событии `init` полем `apiKeySource: "none"`, ключ хаба — `"apiKeyHelper"`. Ключ из
`apiKeyHelper` или `env.ANTHROPIC_API_KEY` в настройках проекта переключает оплату молча, и `forceLoginMethod` в
`--settings` это не перебивает (PoC WSH-4.0). Поэтому хаб держит оплату в три слоя — в обоих режимах: на ключе ключ
задаёт только хаб через `--settings` задачи.

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
  алиасом). `apiKeySource` не тот, что у режима (`"none"` — подписка, `"apiKeyHelper"` — ключ), или поля нет —
  остановка тем же путём, что `code_cancel`;
- `rate_limit_event` — последний `rate_limit_info` целиком в `billing.rate`, время прихода в `billing.rate_at`,
  счётчик событий звена в `billing.rate_events`; `isUsingOverage: true` — остановка, если не `allow_overage = true`;
- файлы настроек — в том же цикле надзиратель следит за `.claude/settings.json` и `.claude/settings.local.json`
  рабочего каталога: изменились `(st_mtime_ns, st_size, st_ino)`, файл появился или исчез — проверка той же функцией,
  что до старта. Нарушение или непроверяемый файл — остановка, как при `init` не по подписке. Так ловится запись мимо
  `deny` (команды `sandbox_exclude` вне песочницы, `git checkout` или `merge` ветки с таким файлом);
- `result` пришёл, а `init` не было — источник оплаты не подтверждён: `billing_mismatch` (fail closed). Нет ни `init`,
  ни `result` — прежняя причина (`no_result`, отмена и т. п.).

Три причины завершения (состояние `failed`), каждая — в строке оплаты и в `reason_hint`:

- `billing_mismatch` — задача пошла не по подписке или это не подтверждено; `result.billing_source` — фактический
  источник: `apiKeySource` из `init` (`null` — поля не было или не было самого `init`) либо «settings: <файл> задаёт
  <ключ>» (или текст «не проверить»), если поймала проверка файлов посреди звена. То, что показывает строка оплаты,
  хранится в `billing.mismatch` (`apiKeySource: …`, `settings: …` или `init не было`); в журнал `code_finish`
  `billing_source` идёт таким же, как в итоге;
- `overage` — задача начала тратить extra usage сверх подписки; разрешить — `[code] allow_overage = true`
  (глобально, по умолчанию `false`);
- `subscription_limit` — итог с `is_error: true` при последнем `status: "rejected"`: лимит подписки исчерпан;
  `result.resets_at` — время сброса, подсказка — «после <время> продолжи: code_resume». На ключ задача не переходит.
- `key_rejected` — только на ключе: API отклонил ключ (раздел «Задачи на ключе»).

Причина стража важнее отмены и таймаута: если claude вышел раньше, чем надзиратель прочитал событие, причина всё
равно ставится по хвосту потока.

**Строка оплаты** — поле `billing` в `code_status`, полном ответе `code_wait`, `code_result`, `wshub code status` и
`wshub code result`:

```
оплата: подписка · 5 ч — 11 %, сброс 15.10 10:20 · неделя — 23 %, сброс 20.10 19:00 · overage: выкл (org_level_disabled)
```

Окна — из `unifiedWindows` (`five_hour` — «5 ч», `seven_day` — «неделя», прочие — под своим именем), процент —
`utilization × 100`, время сброса — по часам хоста. `status: "allowed_warning"` — «близко к лимиту · » в начале
строки; `init` по подписке был, а событий о лимитах ещё нет — «оплата: подписка (данных о лимитах ещё нет)»; `init`
ещё не было — «оплата: источник ещё не известен»; `result` без `init` — «оплата: НЕ подтверждена (init не было)»;
несовпадение — «оплата: НЕ подписка (apiKeySource: …)» или «(settings: …)». `code_result` показывает и модель:
`model_line` — «модель: sonnet → claude-sonnet-5-5». `code_projects` в поле `rate` каждой строки — последний
известный снимок лимитов (`job`, `project`, `at`, `line`) из самой свежей задачи хаба с `billing.rate`; нет такой
задачи — `null`. В журнал строкой `code_finish` идут `billing_source`, `billing_model`, `rate_events` и `rate`
(окно, его `utilization`, `status`, `isUsingOverage`), а также `billing_mode`. Тихая строка `code_wait` и карточка
задачи строку оплаты не показывают (карточка — только «подписка» или «ключ anthropic»).

**Суммы в долларах** — по режиму: на подписке скрыты (оценка claude, не списание), на ключе показываются в
`code_status`, `code_wait`, `code_result`, `code_cancel`, `code_jobs`, `code_stats`, карточке и CLI. Задача,
остановленная с `billing_mismatch` или `key_rejected`, показывает сумму, если она есть. `code_jobs` отдаёт режим
(`billing`) и причину каждой задачи; `code_stats` группирует ещё и по режиму, средняя цена — только у групп на ключе.

**Остаточные риски:**

- остановка по `init` не гарантирует нулевой расход: если запрос к API уходит раньше `init`, на чужой счёт попадёт не
  больше одного запроса. Главная защита — проверка до старта и запрет правки настроек;
- задача может получить `.claude/settings*.json` не правкой, а через git (`checkout`, `merge` ветки с таким файлом)
  или командой вне песочницы (`sandbox_exclude`). Надзиратель опрашивает файлы раз в секунду и останавливает задачу
  путём `code_cancel` (SIGINT, через 20 с — SIGTERM группе); claude перечитывает настройки сам и сразу. Поэтому:
  между записью и остановкой на чужой счёт могут уйти запросы; файл, записанный и удалённый между двумя опросами,
  надзиратель не видит вовсе; запись, сохранившая `mtime`, размер и inode (переписать на месте той же длины и вернуть
  `mtime`), повторной проверки не вызывает;
- сколько `rate_limit_event` приходит в многоходовом запуске, не проверено (покажет `rate_events`): overage может
  обнаружиться поздно. Основная защита — extra usage выключен на уровне организации (`org_level_disabled`);
- значение `status: "rejected"` взято из стороннего источника, живьём исчерпание лимита не проверялось;
- шкала `utilization` (доля 0..1) — по одному наблюдению.

## Задачи на ключе

Задача может идти на API-ключе `anthropic` хаба вместо подписки — например, чтобы потратить промокредиты Console.

**Как запустить.** `code_start(..., billing="key", reason="кто разрешил тратить ключ и когда")` — из чата только с
`reason` (≥ 15 символов); `wshub code start <проект> <ТЗ> --billing key` — выбор оператора, без `reason`. Режим
доступен любому проекту с wshub-code; нужен только ключ `~/.config/wshub/keys/anthropic` с правами каталога 0700 и
файла 0600 (`wshub api key set anthropic`). Ключа нет или права неверны — отказ до старта текстом проверки ключа
(значение не выводится). `[api] enabled` и `[workspace.<имя>.api]` не нужны (это включение `wshub api run`),
`max_usd_per_call` к задачам Code не применяется — это потолок одного запроса API.

**Как ключ попадает в claude.** В `settings.json` задачи (его передаёт `--settings`) хаб пишет
`"apiKeyHelper": "<wshub> code key-helper"` — команду, а не ключ. `wshub code key-helper` печатает ключ, прочитанный
с проверкой прав, и больше ничего; любая проблема — код 1 и текст в stderr без ключа. Ключ не попадает в `job.json`,
`events.jsonl`, `stderr.log`, `settings.json`, окружение и argv claude, ответы MCP и карточку. Настройка из
`--settings` не «прилипает»: следующая задача без неё снова идёт на подписке. На подписке `apiKeyHelper` в
`settings.json` нет.

**Лимиты и деньги — только те, что задал оператор**, как лимиты на подписке; умолчаний нет:

- бюджет задачи — только `max_budget_usd` проекта; передаётся claude флагом `--max-budget-usd`, не задан — флага
  нет, кроме включённой зоны (ниже). Бюджет из шапки и `code_start` при незаданном потолке не применяется (как на
  подписке), флаг терминала `--budget-usd` — применяется, но не выше `max_budget_usd`, если он задан. `max_turns`,
  `max_hours`, `plan_max_turns` и `daily_budget_usd` — как на подписке;
- дневной потолок `[api] daily_usd` — только если ключ записан в реестре: общий для `wshub api` и задач Code на
  ключе, считается по журналу трат `~/.local/state/wshub/api/ledger.jsonl` (все провайдеры, местные сутки). Не
  помещается — отказ до старта;
- зоны (`docs/api.md`) — только если они включены явно: секция `[api.providers.anthropic]` с `included_usd` или
  команда `wshub api credits anthropic`. Бюджет звена должен поместиться в остаток включённой зоны или в остаток
  разрешения `wshub api allow anthropic` на купленную; иначе отказ «включённые кредиты исчерпаны… wshub api allow …».
  Купленная зона без действующего `allow` — никогда. Зоны не включены — задача стартует без их проверки;
- **зона включена, а бюджета звена нет** (`max_budget_usd` не задан) — бюджет звена = остаток зоны (или разрешения
  `allow`) вниз до цента: claude получает его флагом `--max-budget-usd`, в журнал он же идёт резервом. Поэтому вторая
  задача на той же зоне до итога первой не стартует. Без зоны флага нет;
- у звена без бюджета проверка дня и зон требует хотя бы $0.01 остатка;
- проверка и запись — под блокировкой журнала (`ledger.lock`), как у `wshub api run`. При проверке дня или зон на
  старте звена в журнал пишется **резерв** — строка с бюджетом звена (без бюджета и зоны — 0) и `job`; по итогу её
  заменяет строка с фактом (стоимость звена, `phase_cost`). Без проверок резерва нет, но **факт пишется всегда** —
  для `wshub api stats`. Из строк одного `job` действует последняя. `key_rejected` — факт $0 (при 401/403 запрос не
  тарифицируется). Отмена (`code_cancel`) или таймаут (`max_hours`) без итога — оценка по последнему
  `total_cost_usd` в потоке событий, если он был, строкой с `note = "оценка, итога нет"`; не было — резерв остаётся,
  как у звена `lost`. Строки идут с `template = "code"`; без проверки зон — во включённую зону;
- журнал трат не читается, а в реестре нет ни `daily_usd`, ни `included_usd` — задача стартует без проверок,
  в `warnings` — `⚠ журнал трат не читается: …`; задана хоть одна — отказ до старта.

**Остановка по ключу — `key_rejected`.** При 401 claude повторяет запрос около 4 минут. Хаб не ждёт: событие
`system/api_retry` с `error_status` 401 или 403 — задача сразу останавливается (как `code_cancel`), состояние
`failed`, причина `key_rejected`, в итоге — `result.error_status` и `result.error` из события. Итог с
`is_error: true` и `api_error_status` 401/403 без `api_retry` — та же причина. 429 и 5xx не трогаются: это повторы
claude.

**Строка оплаты** на ключе:

```
оплата: ключ anthropic · ≈$0.50 за задачу
оплата: ключ anthropic · ≈$0.50 за задачу · зона: включённая · цикл ≈$3.20 из $40.00, до 23.10
```

Сумма — по звеньям цепочки с итогом (пока итогов нет — «сумма за задачу — по итогу звена»). Зона и цикл (включённые
кредиты по журналу) — только если зоны проверялись (`included_usd` или команда `credits`). Пороги `[api.warn]`
(`day_usd`, `left_usd`) — хвостом строки: `… · ⚠ траты за сутки ≈$5.20 выше порога day_usd = $5.00`; только показ.
Окон подписки и overage на ключе нет: `rate_limit_event` не приходит. Несовпадение — «оплата: НЕ
ключ хаба (apiKeySource: …)»; `key_rejected` — хвост «ключ отклонён (401 authentication_failed) · задача
остановлена».

**Что видит задача и чего хаб не гарантирует.** Песочница задачи закрывает `~/.config/wshub` (`[code] deny_read` по
умолчанию: `sandbox.filesystem.denyRead` и правила `Read`/`Edit`), так что файл ключа задаче не виден; команды
`sandbox_exclude` идут вне песочницы — контейнер `scripts/check.sh` этого репозитория монтирует только образ (без
`$HOME` и `~/.config`). Ключ живёт в памяти процесса claude: его передаёт `apiKeyHelper`, и от процесса claude и
его потомков (MCP, хуки, команды вне песочницы, отладчик того же пользователя) хаб ключ не защищает. На ключе
claude пишет в stderr предупреждение «claude.ai connectors are disabled…» — это ожидаемо.

## Состояния задачи

`starting`, `prefetch`, `running`, `cancelling` → `completed` (успех), `failed`, `max_turns`, `max_budget`,
`timeout`, `cancelled`, `plan_ready` (фаза плана готова и ждёт решения; терминально для звена, не для
цепочки), `orphaned` (надзиратель мёртв, Code жив), `lost` (оба мертвы, итога нет). `orphaned`
и `lost` вычисляются при любом чтении снимка. Отмена: SIGINT процессу Code, через 20 с SIGTERM его группе,
ещё через 10 с SIGKILL группе. Продолжение (`code_resume`) — новая задача с `--resume <session_id>`
родителя; `session_id` известен с первой секунды, поэтому продолжить можно даже задачу в `lost`. Фазу плана
`code_resume` не продолжает — только `code_approve` или `code_replan`.

**Одна живая задача на каталог.** Две живые задачи в одном рабочем каталоге делили бы рабочее дерево и ветку: каждая
меняет файлы и HEAD (`review` и `recon` тоже — `git switch --detach`), а читает то, что меняет соседняя. Поэтому
старт проверяет под тем же локом, что и `max_parallel`: среди живых задач (`starting`, `prefetch`, `running`,
`cancelling`, `orphaned`) всех проектов реестра нет задачи в том же каталоге (сравнение — по реальному пути, без
симлинков). Профиль не важен. Отказ:

> в каталоге <dir> уже идёт задача <id> (<профиль>, <состояние>) — одна живая задача на каталог; дождись её
> (code_wait) или останови (code_cancel)

`max_parallel` остаётся пределом проекта: задачи в разных каталогах из `dirs` идут параллельно в его пределах.
Звенья цепочки этому не мешают: фаза плана в `plan_ready` уже не живая, исполнение после `code_approve` и
`code_resume` стартуют в том же каталоге.

## Инструменты MCP и CLI

Инструменты сервера `wshub-code`: `code_projects` (с действующей политикой проекта), `code_start`,
`code_status`, `code_wait`, `code_result`, `code_cancel`, `code_resume`, `code_jobs`, `code_plan`,
`code_approve`, `code_replan`, `code_stats`; у каждого — название по-русски («Code: запуск», «Code: ожидание»…),
если установленная `mcp` поддерживает `title`. Скрытые инструменты карточки — `code_card_data`,
`code_card_cancel`, `code_card_ack`. Все вызовы, кроме `code_card_data`, — в журнал wshub; текст промпта —
только sha256.

`code_stats(project?, days=30)` — калибровка политики: по каждой строке политики, источнику, каналу выбора и режиму
оплаты число задач (цепочек), средние ходы, доля `completed`, доля планов, отправленных на `replan` (средняя цена в
долларах — только у задач на ключе).

То же из терминала:

```sh
wshub code start <project> <task> [--model M] [--effort E] [--plan no|auto|review] [--max-turns N]
                  [--budget-usd B] [--billing subscription|key] [--follow]   # флаги — выбор оператора
wshub code status [job]      # без job — список последних задач
wshub code result <job>      # плюс строка «открыть сессию: cd … && claude --resume …»
wshub code stop <job> | wshub code stop --all
wshub code stats [project] [--days N]
```

`wshub code` без аргументов — сам MCP-сервer (его запускает Desktop). Серверы и надзиратель задачи при старте
переходят в домашний каталог, надзиратель запускается с рабочим каталогом в каталоге задачи, а чтение
`.git/config` идёт с `cwd=/`: удалённый каталог, из которого запустили хаб, git не ломает. `wshub code-run` и `wshub code-hook` —
служебные (надзиратель задачи и хук Bash), вручную не вызываются; `wshub code key-helper` — служебная команда
`apiKeyHelper` задачи на ключе. `wshub push` и `wshub forge` — помощник для
задач (раздел «Пуш и API хостинга»), вне задачи отвечает отказом.

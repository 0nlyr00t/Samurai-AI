# Samurai AI — бизнес-требования

**Статус:** BRD v0.1

**Продукт:** локальный TUI-агент для авторизованных Red Team-операций, Bug Bounty, CTF/HTB и планирования adversary emulation.

**Ключевая идея:** Samurai AI — не «чат с доступом к сканерам», а управляемый harness безопасности, который связывает методологию, LLM, инструменты, разрешения, состояние операции, доказательства и отчетность.

## 1. Принципы продукта

1. **Scope before action** — сначала границы и правила, затем выполнение.
2. **Workflow before autonomy** — автономность только внутри понятного процесса.
3. **Evidence before finding** — результат сканера еще не является уязвимостью.
4. **Operation, not chat** — основная сущность продукта — операция, а диалог является одним из представлений ее состояния.
5. **Operator stays in control** — любое действие можно остановить, проверить и продолжить после сбоя.

## 2. Анализ существующих решений

### DSH RedTeam Model

[DSH RedTeam Model](https://github.com/SeaOf0/dsh-redteam-model) и [описание плагина](https://dsh-plugin.org/plugins/seaof0/dsh-redteam-model) — ориентир по управляемости процесса: специализированные режимы, persona/playbook/skills/refs, stage gates, целевые контракты, детерминированный контроль вызовов, подтверждение опасных команд, trace vault, campaign memory, реестр результатов и независимая проверка находок.

**В Samurai AI:** runtime-политика, stage gates, операционная память, разделение «обнаружено/подтверждено», аудит. Большое количество режимов и плагинов не переносится в MVP механически.

### HexStrike AI

[HexStrike AI](https://github.com/0x4m4/hexstrike-ai) — ориентир по интеграционному слою: MCP, обширный реестр инструментов, специализированные агенты, выбор инструментов и параметров, процесс-менеджмент, кэширование, восстановление после ошибок и браузерная автоматизация.

**В Samurai AI:** единый реестр инструментов, нормализованные адаптеры, health checks, кэш, управление процессами и MCP. Количество инструментов не считается заменой качества операции; авторские показатели точности и скорости требуют независимой проверки.

### Strix

[Strix](https://github.com/usestrix/strix) — ориентир по практической проверке результатов: изолированное выполнение, динамическое тестирование, подтверждение через PoC, HTTP proxy, браузер, structured findings, CLI, headless mode, CI/CD и отчеты.

**В Samurai AI:** обязательная валидация находок, sandbox, качественный результат для разработчика и локальное хранение run artifacts.

### PentestGPT

[PentestGPT](https://github.com/GreyDGL/PentestGPT) — ориентир по декомпозиции: многоэтапные pipelines, Pentesting Task Tree, разделение reasoning/generation/parsing, поддержка разных backends и возобновление сессий.

**В Samurai AI:** явные стадии, дерево задач, передача результатов между этапами и независимость от модели.

### CAI / CSI

[CAI](https://github.com/aliasrobotics/cai) — исследовательский harness с multi-agent patterns, MCP, HITL, tracing, guardrails и систематическими benchmark-оценками. CAI переведен в архив и не должен быть зависимостью Samurai AI.

**В Samurai AI:** prompt-injection protection, benchmarking, возможность смены harness/model и сравнение нескольких оркестраторов.

### CALDERA и VECTR

[CALDERA](https://github.com/apache/caldera) и [VECTR](https://github.com/SecurityRiskAdvisors/VECTR) — ориентиры по сценариям adversary emulation, MITRE ATT&CK, кампаниям, test cases, измерению покрытия и результатов обнаружения.

**В Samurai AI:** модель кампании, сценарное планирование, ATT&CK mapping, ожидаемые результаты детекта и debrief. Собственный C2 не требуется; предусматриваются интеграции.

### Evaluation harnesses

- [Cybench](https://github.com/andyzorigin/cybench): subtask scoring, unguided/interactive режимы и полные логи.
- [NYU CTF Bench](https://github.com/NYU-LLM-CTF/NYU_CTF_Bench): задачи по шести категориям с development/test splits.
- [XBOW Validation Benchmarks](https://github.com/xbow-engineering/validation-benchmarks): историческая регрессия; maintainers указывают, что набор устарел и насыщен.

Samurai AI нужен собственный свежий holdout-набор и оценка solve rate, стоимости, времени, вмешательств оператора и неправильных действий.

## 3. Позиционирование и пользователи

Samurai AI объединяет дисциплину DSH, инструментальный слой HexStrike, проверку находок Strix, декомпозицию PentestGPT, операционное планирование CALDERA/VECTR и взаимодействие в стиле Kimi Code.

**Отличие:** управляемая автономность с доказуемым результатом, а не максимальная автономность любой ценой.

Целевые пользователи:

- Red Team operator;
- Bug Bounty researcher;
- CTF/HTB-участник;
- Red/Purple Team lead;
- Security/AppSec-специалист.

## 4. Бизнес-цели и показатели

- **BG-01. Сократить время до проверенного результата:** time-to-first-validated-finding, time-to-flag, длительность подготовки отчета.
- **BG-02. Сделать работу воспроизводимой:** доля находок с полным evidence chain, доля операций, повторяемых по сохраненному runbook.
- **BG-03. Обеспечить безопасную автономность:** 0 out-of-scope действий и 0 необратимых действий без подтверждения.
- **BG-04. Снизить стоимость:** стоимость подтвержденной находки или решенной задачи, tokens/time на этап, повторное использование артефактов.
- **BG-05. Не зависеть от одной модели:** единый evaluation suite минимум на двух провайдерах и неизменный workflow при смене модели.
- **BG-06. Сделать TUI основным рабочим инструментом:** базовый сценарий полностью выполняется с клавиатуры.

## 5. Основные режимы

### Bug Bounty

Вход: программа, scope, exclusions, правила тестирования и ограничения нагрузки.

Результат: реестр активов и изменений, подтвержденные находки, дедупликация, evidence package и черновик отчета по правилам программы.

### CTF / HTB

Вход: описание задачи, категория, файлы, lab target и win condition.

Результат: журнал гипотез и экспериментов, артефакты, flag ledger, повторяемый solution script, writeup и ретроспектива.

Подрежимы: Web, Pwn, Reverse, Crypto, Forensics, Misc, Privilege Escalation.

### Red Team

Вход: Rules of Engagement, угроза для эмуляции, цели, ограничения и критерии успеха.

Результат: одобренный план, цепочка этапов и зависимостей, доказательства, текущий статус операции, итоговый отчет и незакрытые направления.

### Operation Planning

Результат без обязательного запуска активных действий: сценарий и attack path, ATT&CK/Kill Chain mapping, test cases, ожидаемые telemetry/detection outcomes, расписание, зависимости, точки остановки и экспорт runbook в JSON/Markdown.

## 6. Функциональные требования

### Управление операцией

- **FR-01 — Operation Workspace [MVP].** Каждый запуск принадлежит операции с целью, владельцем, режимом, scope, бюджетом, статусом и версией конфигурации.
- **FR-02 — Authorization & Scope Contract [MVP].** Операция содержит разрешенные домены, URL, IP/CIDR, репозитории, учетные контексты, исключения, временные окна, rate limits и классы действий. Scope версионируется. Семантика правил: wildcard `*.example.com` покрывает и apex `example.com`; IPv4-mapped запись (`::ffff:a.b.c.d`) нормализуется к IPv4, то есть тот же target; исключения побеждают включения.
- **FR-03 — Stage Gates [MVP].** Жизненный цикл `Draft → Planning → Approval → Execution → Verification → Reporting → Complete`, с `Paused` и `Blocked`. Закрытие gate требует артефактов; модель не может сама декларативно закрыть его.
- **FR-04 — Планирование [MVP].** План включает задачи, зависимости, критерии завершения, риски, ожидаемые доказательства и бюджет. Изменения одобренного плана отображаются как diff.
- **FR-05 — ATT&CK и detection planning [MVP, базово].** Для Red Team указываются техники, ожидаемая телеметрия и критерии оценки детекта. CALDERA/VECTR export — следующий приоритет.

### Модели, агенты и инструменты

- **FR-06 — Model Router [MVP].** Несколько провайдеров и отдельные модели для planner, executor, parser, verifier. Отображаются стоимость, контекст и маршрут данных. Provider layer должен поддерживать расширяемый реестр моделей и capability negotiation: OpenAI, Anthropic, Google Gemini, Azure OpenAI, AWS Bedrock, DeepSeek, xAI, Qwen, Moonshot/Kimi, Mistral, OpenRouter и OpenAI-compatible endpoints. Локальные модели через Ollama поддерживаются как first-class provider с health check, model discovery и явным индикатором локального маршрута данных; LM Studio/vLLM подключаются через OpenAI-compatible интерфейс.
- **FR-07 — Tool Registry [MVP].** Каждый инструмент имеет capabilities, схемы ввода/вывода, версию, risk class, timeout, resource limits и требования к окружению.
- **FR-08 — Native Adapters и MCP [MVP].** Локальные адаптеры и MCP stdio/HTTP, allowlist, health check, диагностика и отложенная загрузка описаний инструментов.
- **FR-09 — Sandbox и Process Manager [MVP].** Изолированное выполнение с ограничениями CPU/RAM/диска/сети, background tasks, pause, stop, timeout и логи без блокировки TUI.
- **FR-10 — Agent Orchestration [MVP: контроллер + verifier; P1: параллелизм].** Главный агент владеет планом; подагенты изолированы и ограничены в правах. Результаты передаются через structured blackboard. Рекурсия ограничена.

### Данные и результаты

- **FR-11 — Asset Ledger [MVP, базово].** Актив содержит источник, владельца, атрибуты, время наблюдения, scope status и связи. Повторное обнаружение не создает дубликаты.
- **FR-12 — Evidence Store [MVP].** Сохраняются tool/model versions, redacted parameters, время, target, policy decision, ссылки на stdout/stderr и хеши. Большие результаты хранятся как артефакты, а не в контексте.
- **FR-13 — Findings Workflow [MVP].** `Candidate → Validating → Validated / False Positive / Duplicate → Reported`. В отчет попадают только находки с доказательствами, воспроизведением, влиянием и затронутыми активами.
- **FR-14 — Независимая проверка [MVP].** Validated требует повторного воспроизведения или проверки verifier-агентом/оператором. Совпадение сигнатуры сканера недостаточно.
- **FR-15 — Отчеты [MVP].** Bug Bounty report, CTF/HTB writeup, pentest summary и operation debrief. Markdown/JSON в MVP, HTML/PDF позднее. Перед экспортом проверяются secrets и out-of-scope данные.

### Непрерывность и расширяемость

- **FR-16 — Session Recovery [MVP].** После сбоя восстанавливаются план, задачи, артефакты, findings и процессы. Действия с неизвестным внешним результатом не повторяются автоматически.
- **FR-17 — Campaign Memory [P1].** Межсессионные методики, решения и причины неудач с изоляцией по workspace, retention и удалением.
- **FR-18 — Custom Playbooks и Skills [P1].** Эксперт меняет gates, инструкции, доказательства и инструменты без изменения ядра. Плагины требуют trust review.
- **FR-19 — Headless и интеграции [P1].** CLI без TUI, JSON events, стабильные exit codes, CI/CD и трекеры. Внешняя отправка отчетов требует отдельного подтверждения.
- **FR-20 — Evaluation Framework [MVP: внутренний; P1: пользовательский].** Фиксируются версии модели/playbook/tools, config, время, стоимость, interventions и subtask results. Используются свежие приватные задачи и контроль contamination.
- **FR-21 — Локализация RU/EN [MVP].** При первом запуске установщик/TUI предлагает выбор русского или английского языка, сохраняет его в конфигурации и позволяет переключать позже без потери операции. Все operator-facing строки CLI/TUI, ошибки, approvals, help и шаблоны отчетов должны быть внешними ресурсами; новые строки добавляются одновременно в RU и EN.

## 7. Требования к TUI

За основу принимаются паттерны [Kimi Code](https://www.kimi.com/code/docs/en/kimi-code-cli/guides/interaction.html): conversation view, input box, status bar, Plan/Goal modes, approvals, background tasks, sessions, subagents и темы.

### Компоновка

```text
SAMURAI AI  OP-024  BUG BOUNTY  SCOPE: OK       MODEL: auto
----------------------------------------------------------------
Plan 3/7 | Tasks: 2 running | Findings: 1 | Evidence: 14

[plan]      Web attack surface mapped; 3 hypotheses remain
[tool]      httpx probe                         [completed]
[approval]  Active scan requires confirmation    [waiting]
[finding]   F-003 candidate -> validating
----------------------------------------------------------------
> Describe the next objective, or use / for commands
----------------------------------------------------------------
PLAN | ASK | R1 PASSIVE | TASKS:2 | COST:0.84 | /help
```

На широком терминале допускается drawer `Plan / Agents / Assets / Findings / Evidence`; на узком он становится полноэкранной панелью.

### Поведение

- Поток событий показывает краткие карточки; длинный вывод свернут.
- `/` открывает команды, `@` — файлы и артефакты.
- `Shift-Tab` переключает Plan mode.
- `Esc`/`Ctrl-C` прерывает текущий шаг, но не удаляет операцию.
- `Ctrl+B` переводит выполнение в background.
- `Ctrl+O` раскрывает вывод; `Ctrl+T` открывает задачи.
- Поддерживается steering во время выполнения.
- `/stop` останавливает все agent-controlled задачи.
- `/lang` открывает выбор RU/EN и применяет язык без перезаписи operation state.
- Постоянно видны scope, risk, approval mode, активные задачи и бюджет.

Базовые команды: `/operation`, `/scope`, `/mode`, `/plan`, `/tasks`, `/agents`, `/assets`, `/findings`, `/evidence`, `/report`, `/sessions`, `/model`, `/permission`, `/theme`, `/lang`, `/help`.

### Approval Panel

До выполнения отображаются команда/действие, нормализованный target и scope decision, причина, risk class, side effects, redacted credentials и варианты `Allow once`, `Allow within this approved plan`, `Deny`, `Edit`.

### Визуальный стиль

- Темная тема по умолчанию, нейтральный фон, синий primary, бирюзовый accent.
- Amber — warning/approval, red — error/out-of-scope, green — validated/complete.
- Минимум декоративной ASCII-графики.
- Light theme, 256-color fallback, `NO_COLOR`, ASCII fallback и JSON themes.
- Полная работа с клавиатуры, SSH/tmux, ширина от 80 колонок.

## 8. Безопасность и доверие

### Классы риска

- **R0:** локальный read-only анализ — автоматически.
- **R1:** пассивные внешние запросы — в рамках утвержденной операции.
- **R2:** активное неразрушающее тестирование — одобрение плана и ограничений.
- **R3:** изменение состояния, эксплуатация, использование credentials, post-exploitation — отдельное подтверждение.
- **R4:** деструктивные действия, нарушение доступности и out-of-scope — запрещены политикой; исключения только для специально обозначенной lab-среды.

Режим автономности меняет количество диалогов, но не отменяет policy.

### Обязательные механизмы

- Scope проверяется tool broker/policy engine, а не только системным prompt.
- Повторная проверка после DNS resolution, redirect и изменения target.
- Для активных инструментов — network egress control; текстового анализа команды недостаточно.
- Rate limiting, circuit breaker и запрет слепых повторов после серии ошибок.
- Tool output, web-страницы и документы являются недоверенными данными, а не инструкциями.
- Secrets хранятся отдельно, выдаются на минимальный срок и маскируются в логах/отчетах.
- Project-level MCP, skills и playbooks требуют доверия к источнику.
- Audit log включает действия модели, оператора, approvals и policy decisions.
- Ручной shell маркируется как manual action и журналируется.

## 9. Нефункциональные требования

- Linux и macOS в MVP; Windows через WSL2.
- Целевая поставка — самостоятельный per-platform `samurai` executable/installer без установленных Python/uv. uv используется только для разработки; optional security tools, Docker, MCP servers и Ollama определяются health checks и не являются обязательными для запуска core harness.
- Local-first; внешняя отправка данных всегда обозначена.
- Запуск TUI без сетевых вызовов до 3 секунд; ввод доступен во время выполнения.
- До 10 000 активов и 100 000 событий на операцию с пагинацией.
- Восстановление после crash/restart без автоматического повтора действий с неизвестным результатом.
- Структурированный event stream, diagnostic export и метрики tool/model failures.
- Настройки retention, удаление операции с артефактами и отключаемая телеметрия.
- Версионированные схемы playbook, plugin, evidence и report.
- Sovereign deployment [P2]: локальные модели и air-gapped работа.

## 10. Границы MVP

### Входит

- TUI и управление сессиями;
- четыре продуктовых режима;
- Operation/Scope/Plan/Gates;
- контроллер, verifier и несколько модельных провайдеров;
- ограниченный качественный набор адаптеров плюс MCP;
- sandbox, background tasks и аварийная остановка;
- assets, evidence, findings и отчеты;
- базовый ATT&CK planner;
- восстановление операций;
- начальный regression/evaluation suite.

### Не входит

- подключение инструментов ради количества;
- неограниченные автономные действия вне scope;
- массовое интернет-сканирование;
- встроенный C2 или платформа доставки payload;
- деструктивные действия и автоматическая публикация отчетов;
- командный RBAC/SSO и distributed workers;
- plugin marketplace.

### Последующие релизы

**P1:** параллельные агенты, campaign memory, asset diff, пользовательские playbooks, CALDERA/VECTR integrations, headless/CI.

**P2:** RBAC/SSO, совместная работа, distributed runners, air-gapped модели, необязательный read-only web viewer.

## 11. Приемочные сценарии

1. При redirect или DNS-переходе на исключенный target выполнение блокируется на tool/network layer и попадает в audit.
2. Пользователь импортирует правила Bug Bounty-программы, одобряет план, получает validated finding и формирует отчет без ручной сборки evidence.
3. После перезапуска CTF/HTB-операция продолжается с сохраненными артефактами; writeup позволяет повторить решение.
4. Red Team-сценарий содержит ROE, ATT&CK mapping, критерии успеха, точки остановки и detection expectations; исполнение начинается после approval.
5. Вредоносная инструкция внутри web-страницы или tool output не изменяет план и не получает новых разрешений.
6. Все сценарии доступны с клавиатуры без web UI.
7. Повторный benchmark запуск той же конфигурации формирует сопоставимые метрики и полный trace отклонений.

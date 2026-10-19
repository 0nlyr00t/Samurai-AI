# Samurai AI — план разработки

## Текущий статус

**Этап 0 завершен:** BRD/roadmap, домен операций, scope policy, SQLite persistence, append-only events, CLI и базовый TUI.

**Этап 1 core завершен:** plans/tasks/dependencies, stage gates, approvals R0–R4, immutable evidence artifacts, findings workflow, policy-gated operation transitions, SQLite migration v2, kernel CLI и read-only TUI views. Проверки: 54 tests, ruff, strict mypy и CLI E2E — успешно.

Текущие ограничения: LLM, MCP и внешнее выполнение инструментов еще не подключены; snapshots/reconcile/fork отложены до recovery-среза перед Tool Execution; URL scope rules поддерживают только origins; evidence content пока считается доверенным текстом оператора.

Следующий рабочий этап — **Этап 1b, Recovery Hardening**, затем **Этап 2, Tool Execution и безопасность**.

## 1. Архитектурный курс

**Стек:** Python 3.12+, uv, Textual, Pydantic, stdlib `sqlite3` с явными миграциями, pytest/ruff/mypy. Docker подключается на этапе tool execution.

**Форма:** модульный монолит с асинхронным tool/process layer. Распределенная система, собственный C2, жесткая зависимость от одной LLM и сотни адаптеров в базовый каркас не входят.

**Границы модулей:**

- `domain` — операции, scope, планы, риски, findings; не зависит от TUI, LLM и инструментов.
- `storage` — миграции, repositories, event journal и восстановление.
- `policy` — детерминированные решения и approvals; не делегируется модели.
- `execution` — registry, process manager, sandbox, egress и MCP.
- `agents` — model router и оркестрация; не является источником истины для состояния.
- `playbooks` — продуктовые методики и gates.
- `reports` — доказуемые результаты и экспорт.
- `tui`/`cli` — представление и operator control без доменной логики в обработчиках.

Каждый этап заканчивается работающим вертикальным срезом, тестами и обновленной документацией. Переход дальше выполняется только после зеленых проверок и acceptance criteria.

## 2. Этап 0 — Walking Skeleton

### Объем

- BRD и roadmap в `md/`.
- Python/uv package, src-layout и `samurai` entry point.
- Четыре режима операции, жизненный цикл и проверяемые переходы.
- Include/exclude scope для доменов, wildcard, URL и IP/CIDR; IDNA и IPv4/IPv6; deny by default.
- SQLite migrations, operations и append-only events.
- CLI для создания/просмотра/перехода операции и проверки target.
- TUI с header, журналом, input box и status bar; `/help`, `/status`, `/scope`, `/plan`, `/exit`.
- Unit, integration, CLI и TUI smoke tests.

### Definition of Done

- `uv run pytest`, `uv run ruff check .`, `uv run mypy src` и package build успешны.
- CLI-сценарий выполнен во временной `SAMURAI_HOME`.
- После повторного открытия БД операция и события восстанавливаются.
- Scope exclusions имеют приоритет; невалидные цели отклоняются без побочных эффектов.
- TUI не имитирует AI и не показывает неработающие функции как готовые.

## 3. Этап 1 — Operation Kernel

### Объем

- Plans, tasks, dependencies, stage gates и artifact requirements.
- Event journal, snapshots, reconcile незавершенных действий, fork/resume.
- Approval records, risk classes R0–R4 и отдельный policy engine.
- Evidence/artifact store и findings workflow.
- Расширение TUI: план, задачи, approvals, evidence и findings.

### Acceptance для завершенного core

- Gate нельзя пройти без required artifacts.
- Finding нельзя validate/report без существующего evidence.
- Operation нельзя перевести в execution с pending approval или неутвержденным планом.
- Operation нельзя завершить с unfinished tasks или candidate/validating findings.
- SQLite v1 корректно мигрируется в v2; plan/approval/artifact/finding и events восстанавливаются после перезапуска.

### Этап 1b — Recovery Hardening

- Operation snapshots и controlled restore points.
- Reconcile in-flight actions после crash/restart; действие с неизвестным внешним результатом не повторяется автоматически.
- Operation fork/resume без копирования секретов и без потери audit trail.
- Diff для изменений scope/plan и operator-facing recovery report.

Этап 1b должен быть завершен до подключения внешнего Tool Execution.

## 4. Этап 2 — Tool Execution и безопасность

### Объем

- Versioned tool registry, native adapters, MCP stdio/HTTP, deferred schemas и health checks.
- Async process manager: background, cancel, timeout, resource limits, circuit breaker.
- Docker sandbox и network egress policy; повторная scope-проверка DNS/redirect.
- Secrets vault/redaction, trust model для project-level MCP/playbooks.
- Защита от prompt injection и маркировка недоверенного контента.

### Acceptance

- Out-of-scope и injected instructions блокируются детерминированно.
- Секреты не попадают в экспорт.
- Остановка задачи завершает дочерние процессы и сохраняет диагностику.
- Сбой инструмента не приводит к неограниченным повторам.

## 5. Этап 3 — LLM и оркестрация

### Объем

- Provider abstraction, минимум два провайдера, structured outputs, retry/fallback и бюджеты.
- Planner/Executor/Parser/Verifier.
- Shared blackboard, изолированные контексты подагентов, наследование разрешений и ограничение рекурсии.
- Отображение маршрута данных, стоимости и потребления контекста.

### Acceptance

- Один playbook работает на двух провайдерах без изменения policy и evidence format.
- Модель не может изменить состояние вне разрешенных команд и схем.
- Бюджет останавливает или переводит операцию в ожидание без потери состояния.

## 6. Этап 4 — Продуктовые playbooks и отчеты

### Объем

- Bug Bounty: program rules, asset ledger/diff, dedup, evidence package, report.
- CTF/HTB: категории, hypothesis log, artifacts, flag ledger, replay и writeup.
- Red Team: ROE, attack path, checkpoints, evidence и debrief.
- Operation Planning: ATT&CK/Kill Chain mapping, test cases, detection expectations, runbook.

### Acceptance

- Четыре E2E-сценария из BRD проходят на контролируемых lab targets.
- Отчеты содержат только validated findings и не раскрывают секреты или out-of-scope данные.

## 7. Этап 5 — Расширенные возможности

- Параллельные специализированные агенты, campaign memory и custom playbooks/skills.
- CALDERA/VECTR integrations, headless mode, JSON events, stable exit codes, CI/CD.
- HTML/PDF reports и необязательный read-only viewer.

**Acceptance:** параллелизм и дополнительные агенты вводятся только при доказанном выигрыше по стоимости/времени без роста ошибок.

## 8. Этап 6 — Hardening и релиз

- Свежий private holdout; Cybench/NYU как дополнительные наборы; XBOW только как историческая регрессия.
- Метрики solve/validation rate, subtasks, cost, wall time, interventions, invalid actions и false positives.
- Fault injection, prompt-injection suite, soak/performance tests, migration/backup/restore tests.
- Подписанные релизы, воспроизводимые сборки и политика совместимости.
- P2 отдельно: RBAC/SSO, distributed runners, air-gapped models и командная работа.

## 9. Постоянные release gates

- Нет out-of-scope выполнения и обхода approvals в тестах.
- Нет непроверенных находок в финальных отчетах.
- Нет секретов в экспортируемых артефактах.
- Изменения покрыты соответствующими unit/integration/E2E-тестами.
- pytest, ruff, mypy, package build и smoke-сценарий зеленые.
- Новые зависимости проходят security/maintenance review; архивный CAI не используется.
- Документация соответствует фактическому поведению продукта.

## 10. Стратегия качества

- **Unit:** чистое доменное ядро, policy, scope, transitions, parsers и reports.
- **Integration:** SQLite, process execution, MCP, sandbox, recovery и migrations.
- **Contract:** схемы providers, tools, events и playbooks; backward compatibility.
- **TUI:** Textual `run_test`, клавиатурные сценарии, узкий терминал и длинный вывод.
- **Security:** scope bypass, DNS/redirect, prompt injection, secrets canaries и malicious plugins.
- **E2E:** реальные лабораторные цели, полный операторский путь и воспроизводимый отчет.
- **Evaluation:** фиксированные версии и конфигурации, стоимость/время/вмешательства, не только solve rate.

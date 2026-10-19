# Samurai AI — план разработки

## Текущий статус

**Этап 0 завершен:** BRD/roadmap, домен операций, scope policy, SQLite persistence, append-only events, CLI и базовый TUI.

**Этап 1 завершен:** Operation Kernel и Recovery Hardening. Реализованы plans/tasks/gates, approvals R0–R4, immutable evidence, findings workflow, snapshots/restore/fork, recovery actions reconcile, plan/scope diff, SQLite migrations v1–v3, kernel/recovery CLI, TUI и RU/EN first-run localization.

**Этап 2a завершен:** Managed Tool Execution Core. Реализованы versioned tool registry, `local.echo`, `process.exec` без shell, `http.fetch` через scope broker, async ProcessManager, approval-gated execution, recovery integration, redaction, prompt-injection warnings, SQLite v4, tool CLI и TUI. Проверки: 124 tests, ruff, strict mypy и CLI/TUI E2E — успешно.

**Этап 2b завершен (4 из 5 пунктов):** persistent secrets vault (AES-256-GCM, SQLite v5, per-operation scoping, `{{secret:name}}` placeholders), Docker sandbox profiles со static signed allowlist и resource constraints, scoped egress proxy с per-run token auth и DNS pinning (DNS TOCTOU устранен и в broker, и в proxy), MCP stdio/HTTP client без внешнего SDK (JSON-RPC 2.0, protocol 2024-11-05, deferred schemas, health checks, trust model, SQLite v6). Проверки: 222 tests + opt-in docker integration, ruff, strict mypy, CLI smoke — успешно. Пункт 5 (расширенный набор security tools) отложен до отдельного threat review.

Текущие ограничения: scoped sandbox на Docker Desktop for Mac не дает hard egress enforcement (контейнер может обойти proxy через bridge NAT; полный enforcement требует Linux-harness с internal network + in-network proxy); MCP Streamable HTTP поддержан без SSE-streaming responses; `process.exec` не имеет сетевого доступа и допускает только allowlisted executables; LLM и оркестрация не реализованы.

Следующий рабочий этап — **Этап 3, LLM и оркестрация** (после threat review расширенного набора security tools как параллельной задачи).

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
- Localization foundation RU/EN: first-run выбор языка, persisted locale, `/lang`, внешние строки CLI/TUI и синхронные RU/EN ресурсы.

Этап 1b завершен до подключения внешнего Tool Execution.

### Acceptance для завершенного Recovery Hardening

- Snapshot immutable и содержит metadata artifacts без artifact content.
- Restore создает новую операцию, сбрасывает plan progress и не копирует artifacts/findings/approvals.
- Fork сохраняет scope/objective/plan structure и не переносит evidence или approvals.
- Reconcile атомарно переводит in-flight actions в `unknown`, не выполняет retry и возвращает resumable tasks/pending approvals.
- RU/EN locale сохраняется, first-run выбор и `/lang` работают без потери operation state.

## 4. Этап 2 — Tool Execution и безопасность

### Завершенный Stage 2a — Managed Tool Execution Core

- Versioned fail-closed tool registry и static native adapters.
- Async ProcessManager без shell, с timeout, cancel, output limits и circuit breaker.
- `local.echo`, `process.exec` без network access и `http.fetch` GET/HEAD через scope broker.
- Approval-gated execution R0–R4 и проверка `--approval-id`.
- RecoveryAction для каждого run, crash reconcile без retry и SQLite v4.
- Redaction секретов, immutable output artifacts и prompt-injection warnings.

### Acceptance для Stage 2a

- Out-of-scope URL, excluded DNS address и out-of-scope redirect блокируются.
- R2/R3 не исполняются без approved approval.
- Секреты не попадают в artifacts/events; tool output помечается `untrusted-external`.
- Timeout/cancel завершают процессную группу; circuit breaker не выполняет retry.
- Tool run, recovery action, approval и artifacts восстанавливаются после перезапуска.

### Stage 2b — Isolation and Protocol Expansion (завершен, 4/5)

- ~~Docker sandbox profiles с resource/network constraints и signed allowlist~~ — `sandbox.exec`, HMAC-signed static profiles, pinned-by-digest images, fail-closed preflight без auto-pull.
- ~~Egress proxy/enforcement и устранение DNS TOCTOU~~ — pinned-IP соединения в broker и proxy, per-run token-authenticated scoped egress proxy для `sandbox.scoped@v1`; известное ослабление на Docker Desktop задокументировано в статусе выше.
- ~~MCP stdio/HTTP, deferred schemas, health checks и trust model~~ — минимальный JSON-RPC 2.0 клиент, dynamic `mcp.<server>.<tool>` tools, все R2 approval-gated, output untrusted-external.
- ~~Persistent secrets vault и per-operation secret scoping~~ — AES-256-GCM, SQLite v5, CLI/TUI, placeholder resolution в tool execution с redaction.
- Расширенный набор security tools — отложен до отдельного threat review.

## 5. Этап 3 — LLM и оркестрация

### Объем

- Provider abstraction и versioned model registry: OpenAI, Anthropic, Google Gemini, Azure OpenAI, AWS Bedrock, DeepSeek, xAI, Qwen, Moonshot/Kimi, Mistral, OpenRouter и OpenAI-compatible endpoints.
- Ollama как first-class local provider: health check, model discovery, явный local data route и offline-friendly degradation.
- Capability negotiation для tools/function calling, structured outputs, streaming, vision и context limits; playbook не должен зависеть от vendor-specific API.
- Planner/Executor/Parser/Verifier, structured outputs, retry/fallback и бюджеты.
- Shared blackboard, изолированные контексты подагентов, наследование разрешений и ограничение рекурсии.
- Отображение маршрута данных, стоимости и потребления контекста.

### Acceptance

- Один playbook работает минимум на двух разных API-семействах и одной локальной Ollama-модели без изменения policy и evidence format.
- Неподдерживаемая capability обнаруживается до запуска и переводит задачу в понятный fallback/block, а не в silent failure.
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
- Standalone per-platform executable/installer без Python/uv для core harness; smoke-тест бинарного артефакта на чистой машине.
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

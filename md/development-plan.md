# Samurai AI — план разработки

## Текущий статус

**Этап 0 завершен:** BRD/roadmap, домен операций, scope policy, SQLite persistence, append-only events, CLI и базовый TUI.

**Этап 1 завершен:** Operation Kernel и Recovery Hardening. Реализованы plans/tasks/gates, approvals R0–R4, immutable evidence, findings workflow, snapshots/restore/fork, recovery actions reconcile, plan/scope diff, SQLite migrations v1–v3, kernel/recovery CLI, TUI и RU/EN first-run localization.

**Этап 2a завершен:** Managed Tool Execution Core. Реализованы versioned tool registry, `local.echo`, `process.exec` без shell, `http.fetch` через scope broker, async ProcessManager, approval-gated execution, recovery integration, redaction, prompt-injection warnings, SQLite v4, tool CLI и TUI. Проверки: 124 tests, ruff, strict mypy и CLI/TUI E2E — успешно.

**Этап 2b завершен (5 из 5 пунктов):** persistent secrets vault (AES-256-GCM, SQLite v5, per-operation scoping, `{{secret:name}}` placeholders), Docker sandbox profiles со static signed allowlist и resource constraints, scoped egress proxy с per-run token auth и DNS pinning (DNS TOCTOU устранен и в broker, и в proxy), MCP stdio/HTTP client без внешнего SDK (JSON-RPC 2.0, protocol 2024-11-05, deferred schemas, health checks, trust model, SQLite v6), threat review расширенного набора security tools (`md/threat-review-security-tools.md`: одобрены dedicated адаптеры httpx/subfinder/nmap/nuclei/ffuf + sandbox gowitness/tshark, sqlmap отклонен до ROE-gated execution, критерии отказа зафиксированы). Проверки: 222 tests + opt-in docker integration, ruff, strict mypy, CLI smoke — успешно.

**Этап 3 завершен:** LLM и оркестрация. Реализованы provider abstraction и versioned model registry (13 моделей, 4 API-семейства: openai-compatible с OpenAI/DeepSeek/xAI/Moonshot-Kimi/Mistral/OpenRouter/Qwen и custom endpoints SQLite v7, anthropic, gemini), Ollama как first-class local provider (discovery, local data route, offline-friendly degradation), capability negotiation (block до запуска), ModelRouter с retry/fallback и `model.fallback` событиями, per-operation budgets (`ModelBudget`, usage из replay `model.call` событий, `BudgetExceededError` без потери состояния), оркестратор Planner/Executor/Parser/Verifier с закрытой command schema (`request_tool_run`/`note`/`propose_finding`/`complete_task`/`fail` через governed services), shared blackboard из журнала с namespace isolation, subagent contexts с depth limit, `agent_runs` (SQLite v8), data route/cost/context display в `agent show`, CLI `model`/`agent` и TUI `/agents`. LLM-трафик pinned на зарегистрированный base_url; API keys в vault + redaction. Проверки: 301 test (включая acceptance `test_stage3_acceptance.py` и opt-in Ollama/docker), ruff, strict mypy, CLI smoke — успешно.

**Этап 4a завершен:** Bug Bounty playbook и отчеты. Реализованы `BountyProgramRules` и one-shot `playbook init` (план из template recon → asset-inventory → testing → validation → reporting с gates и artifact requirements, rules как governed артефакт `program-rules`), scope-enforced asset ledger (domain/url/ip/service нормализация, `asset.rejected` без записи, retire через append-only `asset_retirements`, `asset diff` по временной отметке), dedup находок по sha256-fingerprint (title+asset+category, `finding.duplicate-candidate`, `--force` + kernel `mark_duplicate`), отчеты Markdown/JSON только из validated findings (статусы verification/reporting, `ReportStateError` без validated), redaction через `SecretRedactor` со значениями vault, блокировка out-of-scope ссылок (`report.rejected`, файл не пишется), evidence package export с SHA-256 manifest, отчет как governed артефакт `final-report` (привязка к gate/task плана), SQLite v9 (program_rules, asset_ledger_entries + retirements, finding_fingerprints, reports — immutable), CLI `playbook`/`asset`/`finding propose`/`report`, TUI `/assets` и `/report`. Проверки: 352 tests (включая acceptance `test_stage4_acceptance.py`), ruff, strict mypy, CLI smoke, package build — успешно.

**Этап 4b завершен:** CTF/HTB playbook. Реализованы `CtfChallenge` и one-shot `challenge init` (план recon → hypothesis-loop → exploitation → flag-capture → writeup с gates challenge-locked/solution-confirmed/flag-captured/writeup-ready, brief как governed артефакт `challenge-brief`), категории по BRD (web/pwn/reverse/crypto/forensics/misc/privesc), hypothesis log с конечным автоматом open → testing → confirmed/refuted/abandoned (optimistic locking, события `hypothesis.*`), append-only immutable flag ledger с regex-валидацией `flag_format` (`flag.rejected` без записи, `flag.duplicate`, UNIQUE по имени, флаг регистрирует governed артефакт `flag-proof` и снимает gate `flag-captured`), replay — хронологический solution document из журнала событий и артефактов с sha256, writeups Markdown/JSON только в статусах verification/reporting и только при захваченных флагах (`WriteupStateError`), redaction через `SecretRedactor` со значениями vault, writeup как governed артефакт `final-writeup` (снимает gate `writeup-ready`), export с SHA-256 manifest, SQLite v10 (ctf_challenges, hypotheses, flag_ledger и ctf_writeups — immutable), CLI `challenge`/`hypothesis`/`flag`/`writeup`/`replay`, TUI `/hypotheses` и `/flags`. Проверки: 389 tests (включая acceptance `test_stage4b_acceptance.py`), ruff, strict mypy, CLI smoke — успешно.

**Этап 4c завершен:** Red Team playbook. Реализованы `RoeDocument` и one-shot `roe init` (план roe-review → path-planning → execution → detection-review → debrief с gates roe-approved/path-approved/execution-finished/debrief-ready, ROE как governed артефакт `roe`; init создает pending R3 approval «Approve ROE and attack path», блокирующий EXECUTION до approve — BRD-сценарий 4), attack path из `AttackStep` с ATT&CK-валидацией technique id (`T1234`/`T1234.001`), зависимостями на существующие шаги (циклы невозможны по построению), конечным автоматом planned → executing → executed / blocked / skipped, detection expectations (`expected_telemetry` + `detection_criteria`) и фиксацией исхода (detected/missed/not-observed только для executed), evidence-привязкой артефактов; advisory checkpoints (pending → reached, точки остановки); debrief Markdown/JSON в статусах verification/reporting с ROE, attack path, detection outcomes, validated findings и «незакрытыми направлениями» (blocked/skipped шаги + missed детекции), redaction через `SecretRedactor` со значениями vault, governed артефакт `final-debrief`, export с SHA-256 manifest и evidence-артефактами шагов, SQLite v11 (red_team_roe, attack_steps, checkpoints, debriefs — immutable), CLI `roe`/`attack-path`/`checkpoint`/`debrief`, TUI `/attack-path` и `/debrief`. Проверки: 426 tests (включая acceptance `test_stage4c_acceptance.py`), ruff, strict mypy, CLI smoke, package build — успешно.

**Этап 4d завершен:** Operation Planning playbook — Этап 4 закрыт целиком. Реализованы `PlanScenario` и one-shot `scenario init` (план scenario-draft → mapping → test-design → review → runbook с gates scenario-locked/mapping-complete/tests-ready/runbook-ready, brief как governed артефакт `scenario-brief`), test cases (`PlannedTest`) с ATT&CK technique id, kill chain фазой (7 фаз Lockheed), expected telemetry/detection expectation, зависимостями на существующие test cases, флагом stop-before (точки остановки) и автоматом draft → ready → retired, scenario map (топологический порядок), runbook Markdown/JSON в статусах planning/approval/verification/reporting — без перехода в execution, только из ready test cases (`RunbookStateError`), с секцией stop points, redaction через `SecretRedactor` со значениями vault, governed артефакт `final-runbook`, export с SHA-256 manifest, SQLite v12 (plan_scenarios, test_cases, runbooks — immutable), CLI `scenario`/`test-case`/`runbook`, TUI `/test-cases` и `/runbook`. Проверки: 454 tests (включая acceptance `test_stage4d_acceptance.py`), ruff, strict mypy, CLI smoke, package build — успешно.

**Этап 5a завершен:** параллельные специализированные агенты (swarm). Реализованы `SwarmRun` и `SwarmService` — bounded thread pool (workers ≤ 4) над независимыми задачами плана (ready = pending + зависимости done/skipped, авто-выбор или явный `--task`), members — обычные governed AgentRun с `swarm_id` и `specialization` (general/recon/testing/analysis; specialization меняет только prompt focus, command whitelist/approvals/scope/budgets идентичны), preflight model approval в CLI (без `--approval-id` — pending approval и exit 2 с инструкцией, как у `agent run`), per-member изоляция ошибок (`agent.swarm.member-failed`, остальные продолжают), итоговые статусы completed/partial/failed, swarm report с wall time vs sequential estimate и speedup из журнала `model.call`, события `agent.swarm.start/finish`, SQLite v13 (swarm_runs + колонки agent_runs.swarm_id/specialization), CLI `swarm ready/run/list/show`, TUI `/swarm`. Конкурентность-фиксы: bounded retry (3 попытки) на `KernelConflictError` в start_task/complete_task/pass_gate (параллельные complete_task по разным задачам одного плана больше не конфликтуют), per-operation lock чтения budget usage в ModelService. Acceptance Этапа 5 доказан в `test_stage5_acceptance.py`: на латентном fake-провайдере swarm (workers=2) быстрее sequential baseline (wall < 0.75×) при идентичных governed outcomes (задачи done, tool runs, notes, ноль rejected commands) и точном usage accounting. Проверки: 481 test (включая acceptance и opt-in), ruff, strict mypy, CLI smoke, package build — успешно.

**Этап 5b завершен:** campaign memory (FR-17) и custom playbooks/skills (FR-18). Реализованы workspace-scoped `MemoryEntry` (methodology/decision/failure/note, tags, retention_days с expiry, optimistic version, delete разрешен — память вне журнала операций и не evidence), read-only для агентов: bounded redacted digest (≤10 записей × 300 chars) в planner/executor prompts через `agent run --workspace` / `swarm run --workspace`, аудит в `agent.run.start` (workspace, memory_entry_ids); custom playbooks — JSON template → `PlaybookTemplate` с структурной валидацией через OperationPlan (dup ids/cycles отклоняются при install), immutable хранилище (name unique, sha256), `playbook install/use/list-custom` → план через kernel.create_plan; skills — trusted-only install (`--trusted` обязателен, sha256 фиксируется), versioned immutable rows, `agent run --skill`/`swarm run --skill` (≤4, ≤2000 chars каждый, redacted) в executor prompt, аудит skill_ids; orchestrator резолвит memory/skills fail-fast до записи run; swarm проксирует workspace/skills всем members; SQLite v14 (campaign_memory mutable без no-delete триггеров, custom_playbooks и skills immutable, agent_runs +workspace/skills), CLI `memory`/`skill`, TUI `/memory` `/skills` `/playbooks`. Проверки: 506 tests (включая acceptance `test_stage5b_acceptance.py`: память разделяется между операциями с redaction секретов и аудитом, purge/delete работают, untrusted skill отклонен, custom playbook → swarm completed), ruff, strict mypy, CLI smoke, package build — успешно.

**Этап 5c завершен:** integrations, headless/CI и HTML reports. Реализованы стабильный контракт exit codes (0 успех / 1 негативный доменный исход / 2 usage-domain-approval ошибка; зафиксирован тестами `test_exit_codes.py`), экспорт журнала событий в NDJSON (`events export`), headless mode для CI (`headless run` — swarm auto-select всех ready задач + JSON summary + preflight approval), CALDERA export executed attack steps в стандартный pack layout (`adversaries/*.yml` profile с atomic_ordering + `abilities/<tactic>/*.yml`; tactic — новое опциональное поле AttackStep/PlannedTest из ATT&CK tactics, default execution с пометкой), VECTR export test cases в документированный `vectr-campaign@v1` JSON subset, HTML-формат bug bounty отчета (ReportFormat.HTML, html.escape, общий pipeline redaction+scope) и self-contained read-only HTML dossier операции (`operation export --format html`: status/scope/plan/findings/events timeline). Все экспорты redacted от vault (global + per-operation секреты), sha256, регистрируются governed artifacts с событиями `export.generated`/`dossier.generated`; новых миграций нет (tactic — в JSON data-блобах). CI/CD: `.github/workflows/ci.yml` (uv, ruff, strict mypy, pytest, uv build; matrix py3.12/3.13). Проверки: 514 tests, ruff, strict mypy, CLI smoke (events/caldera/dossier/headless), package build — успешно.

Текущие ограничения: scoped sandbox на Docker Desktop for Mac не дает hard egress enforcement (контейнер может обойти proxy через bridge NAT; полный enforcement требует Linux-harness с internal network + in-network proxy); MCP Streamable HTTP поддержан без SSE-streaming responses; LLM streaming не реализован (capability объявлена); Anthropic health — минимальный POST probe; mid-run resume агента = новый run с сохраненным blackboard; `process.exec` не имеет сетевого доступа и допускает только allowlisted executables; agent `propose_finding` не проходит playbook dedup (dedup только в operator CLI `finding propose`); asset diff удаления учитываются только через retire, а не через изменение scope; CTF/HTB — один challenge на операцию (one-shot init), replay генерируется из журнала и не является исполняемым скриптом, writeup не сканируется на out-of-scope ссылки (lab targets; scope enforcement на уровне execution broker); Red Team — один ROE на операцию, checkpoints advisory (не блокируют transitions), циклы attack path невозможны по построению (зависимости только на существующие шаги); Operation Planning — один сценарий на операцию, schedule — свободный текст (cron/окна не валидируются), runbook включает только ready test cases и не экспортирует evidence-артефакты; Swarm — одна model route на весь swarm (один approval покрывает всех members), workers ≤ 4, при параллелизме budget может превышаться не более чем на число in-flight вызовов (check-then-call не атомарен между потоками; каждый вызов фиксируется в usage, граница покрыта тестом), specialization влияет только на prompts, выбор задач ограничен `workers × 4`, TUI `/swarm` read-only; Campaign memory — digest redacted только глобальными секретами vault (per-operation секреты вне digest), записи не в operation journal (не evidence), агенты не пишут память (barrier от prompt injection); Skills — ≤4 на run, тела ≤8KB (в prompt ≤2000 chars), trust review ручной (флаг --trusted без внешней верификации); Custom playbooks — immutable (обновление = новое имя), только tasks/gates (инструменты и evidence requirements задаются внутри PlanTask); Integrations — export-only без live API, CALDERA abilities без executor commands (шаги — governed планы, не shell), VECTR — документированный subset `vectr-campaign@v1` (не валидирован против live VECTR), PDF отложен (нет stdlib-пути), HTML — только bug bounty report и dossier (writeup/debrief/runbook остаются Markdown/JSON), exports CLI-only (без TUI-команд); Eval — scripted provider детерминирован (реальные holdout-прогоны на живых моделях — вне CI, отдельный запуск), interventions считаются по всем событиям approval.requested операции (включая auto-approved preflight), repeats = медиана метрик, без миграций и TUI; Fault injection — только на scripted provider (не на живых); soak — синтетический повторный прогон (живой soak вне CI); prompt-injection suite покрывает containment (whitelist команд, redaction, read-only память), а не ML-детекцию инъекций; Database.backup — sqlite online backup, шифрование копии на стороне оператора; Release — PyInstaller-бинарники побитно не воспроизводимы (доверие через keyless-подпись, не через rebuild), installer = bare binary без OS-пакетов, подписи выпускаются только в CI release workflow по тегу; Security tools — волна 1 (httpx/nuclei) работает только при docker + локально pulled pinned-образах, набор sandbox profiles при расширении инвалидирует подпись allowlist (оператор переподписывает `samurai sandbox sign`), модель не собирает argv (только значения из schema), scoped egress на Docker Desktop Mac остаётся soft enforcement.

**Этап 6a завершен:** evaluation harness и метрики. Реализованы декларативные JSON-сьюты (`EvalSuite`/`EvalScenario`: mode, scope, tasks, scripted provider responses, workers, expectations), `EvalService` с детерминированным OpenAI-compatible scripted provider (queue responses, фиксированные usage-токены, опциональная latency) и изолированным стеком на сценарий (отдельная БД/vault в workdir, auto-approved model approval), метрики из журнала событий и плана (solved, tasks done/total, validation_rate из finding.validated/reported, findings_created, model_calls, prompt/completion tokens, cost_usd, wall_seconds, interventions=approval.requested, invalid_actions=agent.command.rejected, false_positives), `--repeat N` с медианным merge, `compare_reports` для исторической регрессии (solve True→False, рост invalid_actions/false_positives/interventions, cost/wall выше tolerance 20%/30%, missing scenarios, error runs), in-repo smoke-сьют `evals/smoke.json` (3 сценария: swarm, recovering invalid action, propose_finding), CLI `eval run [--out] [--repeat] [--workdir]` и `eval compare [--cost-tolerance] [--wall-tolerance]` с exit codes 0/1/2, шаг `samurai eval run` в CI. Миграций БД нет (отчеты — JSON-файлы), TUI нет (CI/dev-инструмент). Проверки: 535 tests (включая acceptance `test_stage6a_acceptance.py`: воспроизводимость метрик при фиксированной конфигурации и regression gate), ruff, strict mypy, CLI smoke, package build — успешно.

**Этап 6b завершен:** fault injection, prompt-injection suite, soak/performance, migration chain и backup/restore. Eval scripted provider получил fault-режимы (`EvalProviderFault`: http_error/malformed_json/empty_body/connection_reset с `after_calls`) и запись request bodies для canary-проверок. Fault injection suite фиксирует контракт: HTTP 5xx/malformed/reset завершают run как FAILED (не RUNNING навечно — по результатам инъекции исправлен orchestrator: ModelError теперь завершает run), журнал парный (run.start/run.finish), suite продолжается после faulted сценария, swarm members изолированы, секреты не попадают в ошибки. Prompt-injection suite доказывает containment end-to-end: canary из vault не попадает в provider requests/журнал/артефакты (placeholder резолвится только на execution boundary), injection-текст в tool output инертен (warnings + whitelist-команды), модель не пишет campaign memory и не выходит за command whitelist (неизвестный type → parse fail → run FAILED), skill-тела входят в prompt bounded+redacted. Soak: 10 повторных прогонов — идентичные метрики, bounded wall drift, без утечки потоков, журнал без дублей. Migration chain: v1 БД (операция + событие) мигрирует в v14 с сохранением данных; `Database.backup()` (sqlite online backup) — копия открывается независимо, restore возвращает операцию и журнал. Проверки: 551 test (включая acceptance `test_stage6b_acceptance.py`), ruff, strict mypy, package build — успешно.

**Этап 6c завершен:** packaging и релиз. Реализованы standalone one-file binary через PyInstaller (dev-only dependency group `release`, spec `packaging/samurai.spec` с collect-all textual, сборка `packaging/build_binary.sh`; smoke бинаря: `--help` + `eval run evals/smoke.json` детерминированно 3/3 — по пути исправлена гонка scripted provider: executor-ответы теперь маршрутизируются по маркеру `Current step:` в `executor_responses`, а не по глобальной очереди), воспроизводимые sdist/wheel (SOURCE_DATE_EPOCH, `packaging/check_reproducible.sh` — шаг в CI), release workflow `.github/workflows/release.yml` по тегу `v*` (python-dist + matrix binary linux-x86_64/darwin-arm64/windows-x86_64 со smoke, keyless подписи cosign sign-blob через GitHub OIDC, gh release create с артефактами и bundles), политика совместимости `md/compatibility.md` (CLI/exit codes/journal additive-only, миграции только вперёд + `Database.backup()` перед апгрейдом, model id формат, границы воспроизводимости и верификация подписей). Проверки: 551 test, ruff, strict mypy, package build, reproducible check, локальная сборка и smoke бинаря — успешно.

**Live-демо на Ollama (после Этапа 7):** реальный прогон на локальных моделях (qwen3.5, mistral) вскрыл и закрыл три gaps UX для живых моделей: executor prompt теперь содержит список governed tools с id/риском (раньше модель галлюцинировала имена инструментов — whitelist отклонял, но прогресса не было), `run_task` сам переводит pending-задачу в in-progress (complete_task из pending невозможен), результаты tool runs возвращаются агенту bounded auto-note `tool-result.<tool_id>` в blackboard (≤1500 chars, redacted) — раньше агент не видел содержимое ответов. Плюс approval reuse: одобренный approval для `tool.run <id>` + task удовлетворяет повторные вызовы без новых pending. Итог демо: mistral реально сходил на example.com через http.fetch с approval-gate и записал настоящий заголовок «Example Domain» в blackboard. Проверки: 561 test — успешно.

**Этап 15 завершен:** состояние переехало в `~/.config/samurai` (XDG по умолчанию, `SAMURAI_HOME` важнее, старое написание алиасом), дубль пути в `tui/theme` убран, наследие `.samurai` объясняется строкой в TUI и stderr. Проверки: 763 tests, ruff, strict mypy — успешно.

**Этап 14 завершен:** знак продукта. Катана с почти замкнутым кругом взмаха в `tui/brand.py` (геометрия, кадры, `Painter` на токенах) для `--version`, SVG перерисован настоящей катаной, знак убран из интерфейса, ожидание стало голым спиннером. Проверки: 753 tests, ruff, strict mypy — успешно.

**Этап 13 (волна 4) завершен:** темы и настраиваемые клавиши. 15 семантических токенов, три пресета, файлы тем с валидацией и предупреждением о контрасте, `$token` в CSS и резолв токенов для Rich-стилей, `/theme` и `/keys`, карта биндингов с отказом при конфликте, живая справка. Проверки: 731 tests, ruff, strict mypy — успешно.

**Этап 12 (волна 3) завершен:** доски плана и находок. Универсальная `BoardScreen` и `ItemDetailScreen` (строки, подписи и колбэки даёт приложение), `BoardController` с действиями задач, гейтов и находок, `Ctrl-T`/`Ctrl-F`, детали со зависимостями, вызовами и детьми. Kernel получил `skip_task`/`block_task`, `reset_task` расширен до любого не-pending статуса (повторный reset — no-op). Раздел оверлея «На доске». Проверки: 722 tests, ruff, strict mypy — успешно.

**Этап 11 (волна 2) завершен:** сессии субагентов. Кооперативная отмена (`stop_event`, проверка перед шагом и командой), approvals через resolver (`approval_resolver` в `run_tool`, `ToolDeniedError`, `decided_by="subagent"`), `read_only_only` для plan mode, `KernelService.reset_task`. В TUI — виксин `SessionController` (`call_from_thread` показывает промпт, поток ждёт ответа), экран сессий со списком и drill-in, `Ctrl-G`/`/subagent`, инструмент `agent.spawn` через таблицу обработчиков в чате, строка живого субагента. Проверки: 710 tests, ruff, strict mypy — успешно.

**Этап 10 (волна 1) завершен:** редизайн TUI. `app.py` разобран с 2900 до 1811 строк: модалки в `tui/screens/`, виджеты в `tui/widgets.py`, чат-цикл в `tui/chat.py` (миксин `ChatController`). Действия сведены в реестр `tui/commands.py` — из него собираются слэши, палитра `Ctrl-P` (`tui/palette.py` + `ArgumentScreen` для аргументов) и оверлей клавиш `?`/`F1` (`HelpScreen`, постраничный, на 80 колонках). Инспектор запуска (`/inspect`, `Ctrl-I`) на восемь групп с пере-проверкой scope. Экран приветствия и пустые состояния. Проверки: 697 tests, ruff, strict mypy, eval-smoke, package build — успешно. Волны 2–4 (субагенты с сессиями субагентов, борд плана/находок, темы и настраиваемые биндинги) спроектированы и ждут своих PR.

**Этап 9 завершен:** harness UX и политика в духе opencode/Kimi Code/DeepSeek Harness. Закрыты дефекты фундамента: миграции в транзакции с pre-migration бэкапом, IPv4-mapped → IPv4 и apex под wildcard в scope, `network_access` → тристейт `none|proxied|direct`. Введены режимы разрешений `ask|yolo|never` (состояние оператора в settings, смена — событием в журнал, чувствительные пути и destructive-формы — бейджем), переиспользование approval вместо повторных промптов, plan mode с `ToolDefinition.read_only` и инвариантом реестра, goal mode (агрегат, миграция 17, бюджет шагов и времени, очередь, CLI и панель TUI), steering `Ctrl-S`, shell-режим `!` с `Ctrl-B`/`Ctrl-E`, `/undo` и `/compact`. Проверки: 681 test, ruff, strict mypy, eval-smoke, package build — успешно.

**Этап 8 завершен:** TUI UX в духе kimi-code/opencode. First-run выбор языка — OptionList со стрелками + Enter вместо Tab/кнопок; чёрная тема по умолчанию (#000000, акценты teal/blue); цветной журнал — tag→style mapping через Rich Text (error/deny красные, allow зелёный, status/scope синие, finding magenta и т.д., markup остаётся выключенным — инъекции разметки невозможны); `/model` — модальный ModelPickerScreen: OptionList моделей (static реестр + discovery через API local/configured провайдеров, offline fallback) и OptionList reasoning effort (off/low/medium/high), стрелки/Tab/Enter/Esc, выбор персистится в settings (`model.default`, `model.reasoning_effort`) и показывается в status bar; reasoning effort проброшен в ChatRequest → openai-compatible `reasoning_effort` и ollama `think`; CLI `agent run`/`swarm run` без `--model` берут дефолт из settings (иначе exit 2 с инструкцией). Проверки: 566 tests (включая TUI pilot-тесты на стрелочный выбор языка и персист модели), ruff, strict mypy, package build — успешно.

**Этап 9a завершен:** чат-агент с реальным shell и tool-loop. Реализованы governed инструмент `shell.exec` (R3, `bash -lc`, bounded/redacted output-артефакты), chat tool-loop в TUI (модель вызывает `shell_exec` → per-command access prompt или yolo → выполнение через ToolExecutionService → вывод возвращается модели, до 8 раундов + принудительный финальный ответ без инструментов — цикл больше не завершается молчанием), анимация «Думает…» (braille-спиннер без имени модели), сворачивание вывода команд под стрелку `▸` с раскрытием через `/expand`, история диалога (bounded, per-tab), system prompt «у тебя есть shell», автосоздание операции из первого сообщения (гейт «операция не загружена» удалён), `ModelService.chat()` (messages+tools рядом с complete), tools-маппинг и парсинг tool_calls в Ollama, curl-установщик `install.sh` и self-update `samurai update [--check]` + `--version`. Проверки: 609 tests (включая TUI-тесты tool-loop/отказа/лимита раундов/авто-операции), ruff, strict mypy, package build — успешно.

Следующий рабочий этап — **Этап 9b–9d** (субагенты из чата, самообучение, OCR/vision — см. раздел 9) либо **Этап 7, волна 2 security tools** (subfinder после env-secret канала, nmap после TCP-level egress enforcement) либо **свежий private holdout и soak на живых моделях** (внешние ресурсы), затем P2 (RBAC/SSO, distributed runners, air-gapped models).

**Этап 7 (волна 1) завершен:** governed security tools adapters по threat review. Реализованы `recon.httpx-probe` (R2) и `scan.nuclei` (R3) — dedicated адаптеры с фиксированными шаблонами argv (модель выбирает только значения из fail-closed params schema: targets/profile/severity), scope.evaluate каждого target до spawn контейнера, запуск в новых signed sandbox profiles `sandbox.httpx@v1`/`sandbox.nuclei@v1` (образы projectdiscovery/httpx и projectdiscovery/nuclei pinned by digest, scoped egress через proxy, resource limits), structured summary (≤100 записей из JSONL) поверх raw-артефакта, egress events в stderr. Registry validator научился `maxItems`. При планировании вскрыты и задокументированы технические блокеры: nmap (raw TCP несовместим с CONNECT-proxy — ждёт TCP-level egress), subfinder (DNS-over-UDP + env-secret канал), ffuf (нет официального образа) — перенесены в deferred в `md/threat-review-security-tools.md`. ВНИМАНИЕ: набор sandbox profiles изменился — существующие установки должны переподписать allowlist (`samurai sandbox sign`). Проверки: 560 tests, ruff, strict mypy, CLI smoke, package build — успешно.

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
- ~~Расширенный набор security tools~~ — threat review пройден (`md/threat-review-security-tools.md`), спецификации первых адаптеров готовы к реализации.

## 5. Этап 3 — LLM и оркестрация (завершен)

### Объем (реализовано)

- ~~Provider abstraction и versioned model registry~~ — 13 моделей, семейства openai-compatible (включая custom endpoints, SQLite v7), anthropic, gemini, ollama; Azure OpenAI/AWS Bedrock не вошли (подключаются как custom openai-compatible endpoints).
- ~~Ollama как first-class local provider~~ — health, discovery через /api/tags, local data route, offline-friendly degradation.
- ~~Capability negotiation~~ — tools/structured-output/streaming/vision, block до запуска; playbook не зависит от vendor-specific API (единый ChatRequest/ChatResponse, контрактный тест на трех семействах).
- ~~Planner/Executor/Parser/Verifier, structured outputs, retry/fallback и бюджеты~~ — parser с bounded retry, ModelRouter, ModelBudget + usage replay.
- ~~Shared blackboard, изолированные контексты подагентов, наследование разрешений и ограничение рекурсии~~ — blackboard из журнала с namespace isolation, agent_runs с parent_run_id/depth, max_depth=1, общий budget операции.
- ~~Отображение маршрута данных, стоимости и потребления контекста~~ — `agent show` и `model usage`/`model budget`.

### Acceptance (все пункты покрыты тестами в `tests/integration/test_stage3_acceptance.py`)

- Один playbook работает на openai-compatible и anthropic семействах без изменения policy/evidence format; локальная Ollama-модель — opt-in тест `SAMURAI_TEST_OLLAMA=1` (пройден на реальном Ollama).
- Неподдерживаемая capability (нет tools/structured-output) блокирует run до первого model call — без `model.call` событий и без записи agent run.
- Команды вне whitelist отклоняются (`agent.command.rejected`), состояние операции неизменно.
- Бюджет переводит run в `budget-exceeded` без потери состояния; повторный run после поднятия бюджета завершается успешно.

## 6. Этап 4 — Продуктовые playbooks и отчеты

### Объем

- ~~Bug Bounty: program rules, asset ledger/diff, dedup, evidence package, report~~ — завершено в Этапе 4a.
- ~~CTF/HTB: категории, hypothesis log, artifacts, flag ledger, replay и writeup~~ — завершено в Этапе 4b.
- ~~Red Team: ROE, attack path, checkpoints, evidence и debrief~~ — завершено в Этапе 4c.
- ~~Operation Planning: ATT&CK/Kill Chain mapping, test cases, detection expectations, runbook~~ — завершено в Этапе 4d.

### Acceptance

- Четыре E2E-сценария из BRD проходят на контролируемых lab targets.
- Отчеты содержат только validated findings и не раскрывают секреты или out-of-scope данные.

## 7. Этап 5 — Расширенные возможности (завершен)

- ~~Параллельные специализированные агенты~~ — завершено в Этапе 5a (swarm с доказанным выигрышем по времени); ~~campaign memory и custom playbooks/skills~~ — завершено в Этапе 5b.
- ~~CALDERA/VECTR integrations, headless mode, JSON events, stable exit codes, CI/CD~~ — завершено в Этапе 5c (export-only интеграции, `headless run`, NDJSON events, контракт exit codes, GitHub Actions CI).
- ~~HTML reports и read-only viewer~~ — завершено в Этапе 5c (HTML bug bounty report + HTML dossier); PDF отложен (нет stdlib-пути).

**Acceptance:** параллелизм и дополнительные агенты вводятся только при доказанном выигрыше по стоимости/времени без роста ошибок.

## 8. Этап 6 — Hardening и релиз

- Свежий private holdout; Cybench/NYU как дополнительные наборы; XBOW только как историческая регрессия.
- Метрики solve/validation rate, subtasks, cost, wall time, interventions, invalid actions и false positives.
- Fault injection, prompt-injection suite, soak/performance tests, migration/backup/restore tests.
- Standalone per-platform executable/installer без Python/uv для core harness; smoke-тест бинарного артефакта на чистой машине.
- Подписанные релизы, воспроизводимые сборки и политика совместимости.
- P2 отдельно: RBAC/SSO, distributed runners, air-gapped models и командная работа.

## 9. Этап 9 — Автономность агента: субагенты из чата, самообучение, vision

Этап 9a (chat tool-loop, shell.exec, auto-operation, install/update) завершен — см. «Текущий статус».

### Объем

- **9b. Субагенты из чата (chat delegation).** [Сделано в волне 2, Этап 11.] Чат-агент порождает governed subagent runs под подзадачи инструментом `agent.spawn(instruction, specialization)` → AgentRun через AgentOrchestrator; живой статус в ops-strip, экран сессий с drill-in. Approvals/budget/scope наследуются от операции, параллелизм пока ограничен одним субагентом, digest результата в чат не возвращается — читается на экране сессий.
- **9c. Самообучение (self-learning).** Write-путь в campaign memory: по завершении chat/agent run агент формулирует «урок» (methodology/decision/failure) — sanitized, bounded, redacted, с audit-событием `memory.learned` и operator review через `/memory` (сейчас память read-only для агентов — barrier от prompt injection сохраняется: whitelist ключей, bounded size, флаг авто-записи в settings). Recall: существующий digest памяти добавляется в system prompt чата.
- **9d. OCR и анализ изображений.** Chat tool `read_image(path)`: локальный OCR (tesseract, если установлен на хосте) и/или vision-capable модель (image input для openai-compatible/anthropic/gemini/ollama-vision) с описанием содержимого изображения; результат — bounded text + governed artifact; чтение ограничено trusted directory, путь вне workspace отклоняется.

### Acceptance (все пункты покрыты тестами)

- Чат порождает субагента на подзадачу: governed run виден в `/agents`, digest возвращается в чат, depth/budget соблюдены (scripted provider).
- Уроки пишутся в память только через sanitized write-path; injection-текст из tool output не создает записей; recall реально попадает в system prompt (canary-тест).
- `read_image` извлекает текст с эталонного изображения и возвращает описание от vision-модели; путь вне trusted directory отклонен без чтения файла.

## 10. Постоянные release gates

- Нет out-of-scope выполнения и обхода approvals в тестах.
- Нет непроверенных находок в финальных отчетах.
- Нет секретов в экспортируемых артефактах.
- Изменения покрыты соответствующими unit/integration/E2E-тестами.
- pytest, ruff, mypy, package build и smoke-сценарий зеленые.
- Новые зависимости проходят security/maintenance review; архивный CAI не используется.
- Документация соответствует фактическому поведению продукта.

## 11. Стратегия качества

- **Unit:** чистое доменное ядро, policy, scope, transitions, parsers и reports.
- **Integration:** SQLite, process execution, MCP, sandbox, recovery и migrations.
- **Contract:** схемы providers, tools, events и playbooks; backward compatibility.
- **TUI:** Textual `run_test`, клавиатурные сценарии, узкий терминал и длинный вывод.
- **Security:** scope bypass, DNS/redirect, prompt injection, secrets canaries и malicious plugins.
- **E2E:** реальные лабораторные цели, полный операторский путь и воспроизводимый отчет.
- **Evaluation:** фиксированные версии и конфигурации, стоимость/время/вмешательства, не только solve rate.

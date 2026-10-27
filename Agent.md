# Samurai AI — Agent Handoff

Этот файл содержит контекст для продолжения разработки с нового окружения. Секреты, токены и пользовательские базы данных в репозиторий не добавлять.

## 1. Назначение продукта

Samurai AI — local-first TUI harness для авторизованных Red Team-операций, Bug Bounty, CTF/HTB и Operation Planning. Продукт строится вокруг операции, а не чата.

Обязательные принципы:

- Scope before action.
- Workflow before autonomy.
- Evidence before finding.
- Operator stays in control.
- Политика безопасности реализуется детерминированно вне LLM.

Полные требования: `md/requirements.md`. Roadmap и текущий статус: `md/development-plan.md`.

## 2. Быстрый старт

Требования для разработки: Python 3.12+, uv. Docker понадобится на этапе Tool Execution. Целевая поставка продукта — standalone per-platform `samurai` executable/installer без Python/uv; optional Docker, MCP servers, security tools и Ollama определяются health checks.

```bash
git clone https://github.com/0nlyr00t/Samurai-AI.git
cd Samurai-AI
uv sync
uv run pytest
uv run ruff check .
uv run mypy src
uv build
```

Запуск:

```bash
uv run samurai operation create "Lab" \
  --mode ctf-htb \
  --objective "Solve the authorized lab" \
  --include 192.0.2.10

uv run samurai tui
```

Данные по умолчанию находятся в `.samurai/`, которая игнорируется Git. Для изолированного запуска используйте `SAMURAI_HOME=/path/to/temp` или глобальный CLI-параметр `--db`.

## 3. Текущее состояние

Завершены **Этап 0**, **Этап 1/1b**, **Этап 2a — Managed Tool Execution Core**, **Этап 2b — Isolation and Protocol Expansion** (4 из 5 пунктов; расширенный набор security tools отложен до threat review) и **Этап 3 — LLM и оркестрация**:

- четыре режима операций и проверяемые lifecycle transitions;
- include/exclude scope для domain, wildcard, URL origins и IP/CIDR;
- plans, tasks, dependencies, stage gates и artifact requirements;
- approvals R0–R4 и approval-gated tool execution;
- immutable evidence artifacts с SHA-256 и redaction;
- findings workflow `candidate → validating → validated / false-positive / duplicate → reported`;
- snapshots/restore/fork, recovery actions reconcile и plan/scope diff;
- versioned fail-closed tool registry;
- `local.echo`, `process.exec` без shell/network и `http.fetch` GET/HEAD через scope broker;
- async ProcessManager с timeout, cancel, output limits и circuit breaker;
- DNS/redirect scope revalidation для HTTP tool;
- recovery action и SQLite v4 `tool_runs` для каждого execution;
- prompt-injection warnings и `untrusted-external` trust label;
- kernel/recovery/tool/secret CLI и локализованный TUI;
- RU/EN first-run выбор языка, persisted locale и `/lang`;
- persistent secrets vault (AES-256-GCM, master key в `vault.key` 0600 или env `SAMURAI_VAULT_KEY`) с per-operation scoping и `{{secret:name}}` placeholders в tool params;
- Docker sandbox profiles (`sandbox.exec`, R2): static signed allowlist (HMAC-SHA256 от vault master key, `sandbox-allowlist.sig`), pinned-by-digest images, `--network none`, read-only/tmpfs/cap-drop/no-new-privileges/nonroot, resource limits, fail-closed preflight без auto-pull;
- scoped egress: HTTP broker соединяется с pinned IP (single resolve + проверка каждого адреса по exclusions, Host/SNI по оригинальному имени — DNS TOCTOU устранён); per-run token-authenticated forward proxy на loopback хоста (`execution/egress.py`) для `sandbox.scoped@v1` (network_mode "scoped"): HTTP absolute-URI и CONNECT, вердикты по ScopeContract, порты только 80/443 (конфигурируемо), egress-события (host/port/verdict/bytes, без содержимого) пишутся в payload `tool.run.*` событий, proxy token регистрируется в redactor;
- MCP без внешнего SDK (JSON-RPC 2.0, protocol 2024-11-05): stdio (no shell, allowlisted executable, per-request timeout) и HTTP transport через scope-enforced egress broker (POST + DNS/redirect revalidation); deferred tool schemas (`mcp tools` сохраняет в `app_settings`), dynamic `mcp.<server>.<tool>` resolution в registry (fail-closed, только enabled серверы), все MCP tools R2 approval-gated, output всегда untrusted-external + injection scanner;
- 301 unit/integration/CLI/TUI tests + opt-in docker (`SAMURAI_TEST_DOCKER=1`) и opt-in ollama (`SAMURAI_TEST_OLLAMA=1`) integration tests;
- ruff, strict mypy и CLI/TUI E2E проходят успешно.

Этап 3 завершен (slices 1–4): versioned model registry (`<provider>/<model>@v1`), четыре API-семейства — OpenAI-compatible (OpenAI, DeepSeek, xAI, Moonshot/Kimi, Mistral, OpenRouter, Qwen + custom endpoints SQLite v7), Anthropic (/v1/messages, x-api-key + anthropic-version, system extraction, tool_use mapping), Gemini (generateContent, x-goog-api-key header, systemInstruction, functionCall mapping) — и Ollama как first-class local provider. Единый ChatRequest/ChatResponse контракт на всех семействах (контрактный тест). ModelRouter: ordered fallback (transient 429/5xx → retry ≤2 затем next candidate; capability/auth → skip; non-transient → abort), `model.fallback` события. Budgets: `ModelBudget` per operation (app_settings `budget.<op>`), usage из replay `model.call` событий, `BudgetExceededError` до вызова. Оркестрация: Planner/Executor/Parser/Verifier, закрытая command schema (`request_tool_run`/`note`/`propose_finding`/`complete_task`/`fail` — всё через governed services, reject → `agent.command.rejected` без side effects, >3 подряд → run failed), Parser с bounded retry (≤2, corrective prompt), capability pre-check до первого вызова, `agent_runs` (SQLite v8) с parent_run_id/depth (max_depth=1), blackboard из replay `agent.note` событий с namespace isolation (`shared.*` + `run.<id>.*`) и redaction, `model.call` с `agent_run_id` для per-run accounting. `agent show`: route/usage/context %/data route без ключей. Streaming не реализован; mid-run resume не реализован (resume = новый run, blackboard сохраняется в журнале). Известное ослабление scoped sandbox: прокси слушает host loopback, контейнер на default bridge достаёт его через `host.docker.internal` — контейнер теоретически может обойти proxy напрямую (bridge NAT), полный enforcement требует internal docker network + in-network proxy, что на Docker Desktop for Mac недостижимо (gateway внутри VM недоступен хосту — проверено эмпирически). Hard enforcement планируется на Linux-harness.

## 4. Карта репозитория

- `src/samurai_ai/domain/operations.py` — Operation, OperationMode, OperationStatus и переходы.
- `src/samurai_ai/domain/scope.py` — ScopeContract и детерминированные решения.
- `src/samurai_ai/domain/kernel.py` — PlanTask, StageGate, OperationPlan, Approval, Artifact и Finding.
- `src/samurai_ai/domain/recovery.py` — RecoveryAction, OperationSnapshot, RecoveryReport и diff.
- `src/samurai_ai/domain/execution.py` — ToolDefinition, ToolRun, redactor и injection scanner.
- `src/samurai_ai/domain/vault.py` — SecretMetadata, vault exceptions и `{{secret:name}}` placeholder syntax.
- `src/samurai_ai/domain/sandbox.py` — SandboxProfile, canonical payload и HMAC-SHA256 signing.
- `src/samurai_ai/domain/mcp.py` — McpServer, McpToolInfo, protocol constants и MCP exceptions.
- `src/samurai_ai/domain/models.py` — ModelDefinition, ProviderDefinition, ModelCapability, ApiFamily и model exceptions.
- `src/samurai_ai/domain/agents.py` — AgentRun, command schema (discriminated union) и agent exceptions.
- `src/samurai_ai/agents/` — versioned model registry (static + persisted), capability negotiation, OpenAI-compatible/Anthropic/Gemini клиенты, Ollama provider, ModelRouter (retry/fallback), budgets, blackboard, parser и AgentOrchestrator.
- `src/samurai_ai/execution/` — static registry (с dynamic resolver hook), ProcessManager, HTTP egress broker (GET/HEAD/POST, DNS pinning), Docker sandbox runner, scoped egress proxy и minimal MCP client (stdio/HTTP transports).
- `src/samurai_ai/i18n.py` — RU/EN Translator и TUI resources.
- `src/samurai_ai/storage/database.py` — SQLite connection, transactions и migrations v1–v8.
- `src/samurai_ai/storage/operations.py` — OperationRepository и append-only events.
- `src/samurai_ai/storage/kernel.py` — KernelRepository для plans/approvals/artifacts/findings.
- `src/samurai_ai/storage/recovery.py` — snapshots, recovery actions и app settings.
- `src/samurai_ai/storage/execution.py` — ToolRunRepository и recovery-compatible runs.
- `src/samurai_ai/storage/vault.py` — VaultRepository для encrypted secret entries.
- `src/samurai_ai/storage/mcp.py` — McpServerRepository и deferred tool schema cache (app_settings).
- `src/samurai_ai/storage/models.py` — ModelEndpointRepository для custom OpenAI-compatible endpoints и CustomModelRepository для operator-whitelisted имён моделей (SQLite v15).
- `src/samurai_ai/storage/agents.py` — AgentRunRepository для agent_runs (миграция v8).
- `src/samurai_ai/services/operations.py` — сервисный сценарий операций.
- `src/samurai_ai/services/kernel.py` — KernelService, policy preconditions и summary.
- `src/samurai_ai/services/recovery.py` — snapshots/restore/fork/reconcile/diff.
- `src/samurai_ai/services/settings.py` — persisted locale и `SAMURAI_LANG`.
- `src/samurai_ai/services/execution.py` — approval-gated managed tool execution с vault placeholder resolution.
- `src/samurai_ai/services/vault.py` — VaultService: AES-256-GCM encrypt/decrypt, scoping и resolve_placeholders.
- `src/samurai_ai/services/mcp.py` — McpService (CRUD, health, fetch_tools) и fail-closed McpToolResolver.
- `src/samurai_ai/services/models.py` — ModelService: provider/model inventory, health, governed `model.call` через ModelRouter с approval, fallback events и per-operation budgets.
- `src/samurai_ai/cli.py` — CLI entry point и общие команды.
- `src/samurai_ai/cli_kernel.py` — plan/task/gate/artifact/finding/approval команды.
- `src/samurai_ai/cli_recovery.py` — snapshot/fork/reconcile/lang/diff команды.
- `src/samurai_ai/cli_execution.py` — tool list/run/show команды.
- `src/samurai_ai/cli_vault.py` — secret add/list/remove команды (значения не печатаются).
- `src/samurai_ai/cli_sandbox.py` — sandbox profiles/sign команды.
- `src/samurai_ai/cli_mcp.py` — mcp add/remove/list/enable/disable/health/tools/invoke команды.
- `src/samurai_ai/cli_models.py` — model providers/list/register/remove/health/complete/budget/usage команды.
- `src/samurai_ai/cli_agents.py` — agent run/list/show команды.
- `src/samurai_ai/tui/app.py` — локализованный TUI без доменной логики в обработчиках.
- `tests/unit` — чистое доменное ядро и scope/kernel/recovery/execution edge cases.
- `tests/integration` — SQLite, recovery, process/http broker, CLI и kernel E2E.
- `tests/tui` — клавиатурный TUI и i18n smoke через `run_test`.

## 5. Текущая семантика scope

- `example.com` — только apex domain.
- `*.example.com` — subdomains, но не apex; для обоих нужны два include.
- Exclusions имеют приоритет над includes.
- URL rules поддерживают только HTTP(S) origins; path-level rules пока отклоняются.
- Domain/IP rules могут применяться к HTTP(S) URL targets.
- Невалидная цель дает `INVALID_TARGET` и `allowed=False`.
- DNS resolution, redirect и network egress enforcement относятся к Tool Execution, а не к текущему парсеру.

## 6. Kernel CLI и plan definition

Plan создается из JSON-файла:

```json
{
  "title": "Authorized lab plan",
  "tasks": [
    {
      "id": "recon",
      "title": "Map the service",
      "risk_class": "r1",
      "depends_on": [],
      "required_artifacts": ["service-map"]
    }
  ],
  "gates": [
    {
      "id": "recon-complete",
      "name": "Recon is complete",
      "required_artifacts": ["service-proof"]
    }
  ]
}
```

Основные команды:

```bash
samurai plan create <operation-id> plan.json
samurai plan show|approve|diff <operation-id> ...
samurai task start|complete <operation-id> <task-id>
samurai gate pass <operation-id> <gate-id>
samurai artifact add|list <operation-id> ...
samurai finding create|list|attach|validate|report ...
samurai approval request|list|approve|reject ...
samurai snapshot create|list|show|restore ...
samurai operation fork <operation-id> [--name]
samurai reconcile <operation-id>
samurai tool list
samurai tool run <operation-id> <tool-id> --param key=value [--approval-id <id>]
samurai tool show <run-id>
samurai secret add <name> [--operation <id>] [--value <v> | --stdin]
samurai secret list [--operation <id>]
samurai secret remove <name> [--operation <id>]
samurai sandbox profiles
samurai sandbox sign
samurai mcp add <name> --transport stdio|http [--command '["..."]' | --url <url>]
samurai mcp list|enable|disable|remove ...
samurai mcp health <name> [--operation <id>]
samurai mcp tools <name> [--operation <id>]
samurai mcp invoke <server> <tool> --operation <id> --param k=v [--approval-id <id>]
samurai model providers
samurai model list [--provider <id>] [--discover]
samurai model register <id> --base-url <url> [--secret-name <name>] [--insecure]
samurai model add <provider>/<model>[@vN] [--display-name <name>] [--context-limit N] [--max-output-tokens N]
samurai model remove <id>   # endpoint id или custom model id (содержит '/')
samurai model health <provider-or-model-id>
samurai model complete <model-id> --operation <id> --prompt "..." [--fallback <id>] [--approval-id <id>]
samurai model budget <operation-id> [--max-tokens N] [--max-cost X] [--max-calls N]
samurai model usage <operation-id>
samurai agent run <operation-id> <task-id> --model <id> [--fallback <id>] [--approval-id <id>]
samurai agent list <operation-id>
samurai agent show <run-id>
samurai lang [ru|en]
```

Tool params поддерживают `{{secret:name}}` placeholders: значение подставляется из vault непосредственно перед исполнением, в ToolRun/events/artifacts сохраняется только placeholder-форма, а plaintext регистрируется в SecretRedactor запуска.

`sandbox.exec` исполняет только executables из allowlist подписанного профиля: образ обязан быть pinned by digest и уже присутствовать локально (auto-pull запрещён), контейнер стартует с `--network none`, `--read-only`, `--cap-drop ALL`, `no-new-privileges` и non-root user. Подпись allowlist создаётся через `samurai sandbox sign` и проверяется до любого запуска.

MCP: схемы инструментов импортируются только явным `samurai mcp tools <name>` (deferred, кэшируются в `app_settings`); вызов идёт как обычный governed tool run `mcp.<server>.<tool>` с R2 approval. HTTP MCP endpoints проходят через operation scope (для `health`/`tools` нужен `--operation`). Disabled сервер или unknown tool → fail-closed `ToolNotFoundError`.

TUI команды: `/plan`, `/tasks`, `/gates`, `/findings`, `/evidence`, `/approvals`, `/snapshots`, `/reconcile`, `/tools`, `/runs`, `/agents`, `/secrets`, `/lang`, `/status`, `/scope`, `/model`, `/yolo`, `/trust`.

TUI UX (Этап 8+): верхняя панель со вкладками Журнал/План/Находки/Доступ (ContentSwitcher) и брендом `SAMURAI AI {operation} {mode} · {model}` в правом углу; статус-бар показывает текущую директорию, режим доступа (`ДОСТУП: СПРОСИТЬ|YOLO`) и модель с reasoning; `/yolo` отключает запросы разрешений (setting `tui.yolo`); при первом запуске в директории — модальный запрос доверия (trust always/session/deny, `tui.trusted_dirs`), `/trust` переоткрывает его; в журнале работает mouse-выделение текста и Ctrl+C копирование выделения (двойной Ctrl+C — выход); при наборе `/...` всплывает автодополнение команд (стрелки — навигация по подсказкам, Tab — подставить, Enter — выполнить если команда набрана полностью, Esc — закрыть).

## 7. Следующий этап — Продуктовые playbooks и отчеты (Этап 4)

Этап 3 завершен: четыре API-семейства (openai-compatible, anthropic, gemini, ollama) за единым ChatRequest/ChatResponse контрактом, versioned model registry (13 моделей + custom endpoints), Ollama first-class local, capability negotiation, ModelRouter (retry/fallback), budgets, оркестратор Planner/Executor/Parser/Verifier с закрытой command schema, blackboard, depth limit и data route/cost/context display. Acceptance Этапа 3 формализован в `tests/integration/test_stage3_acceptance.py`. Отложенные пункты Stage 2b (расширенный набор security tools) требуют отдельного threat review и могут выполняться параллельно.

Следующий вертикальный срез — Этап 4:

- Bug Bounty: program rules, asset ledger/diff, dedup, evidence package, report.
- CTF/HTB: категории, hypothesis log, artifacts, flag ledger, replay и writeup.
- Red Team: ROE, attack path, checkpoints, evidence и debrief.
- Operation Planning: ATT&CK/Kill Chain mapping, test cases, detection expectations, runbook.
- Отчеты содержат только validated findings и не раскрывают секреты или out-of-scope данные.

Подробные acceptance criteria — в `md/development-plan.md` (раздел 6).

## 8. Инварианты и правила изменений

- Не ослаблять scope и не переносить enforcement только в prompts.
- Не выполнять сетевые или деструктивные действия в unit/integration tests.
- Не добавлять неограниченную рекурсию агентов.
- Не хранить секреты в events, reports, fixtures или Git.
- Не менять существующие миграции после появления пользовательских данных; добавлять новые версии.
- Сохранять версионирование схем и обратную совместимость или явно документировать migration path.
- Для любого bug fix сначала добавить воспроизводящий тест.
- Комментарии и документацию обновлять вместе с поведением.
- `uv.lock` хранить в Git; development и release окружения воспроизводить через `uv sync --locked`.
- Все новые operator-facing строки добавлять одновременно в RU и EN ресурсы.

## 9. Definition of Done для каждого этапа

```bash
uv run pytest
uv run ruff check .
uv run mypy src
uv build
```

Дополнительно:

- пройти соответствующий CLI/TUI smoke с изолированной `SAMURAI_HOME`;
- убедиться в отсутствии out-of-scope выполнения и обхода approvals;
- не допускать непроверенные findings в отчеты и секреты в экспорт;
- обновить `md/development-plan.md` и этот handoff, если изменились статус или правила работы.

## 10. Git и безопасность credentials

- Основная ветка: `main`.
- Не использовать force push для обычной синхронизации.
- Перед commit проверить `git status` и исключить `.samurai`, `.venv`, `dist` и локальные caches.
- Использовать настроенный Git credential helper или SSH; не помещать токен в remote URL, команды, логи и файлы проекта.

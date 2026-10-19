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

Завершены **Этап 0**, **Этап 1/1b** и **Этап 2a — Managed Tool Execution Core**:

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
- MCP без внешнего SDK (JSON-RPC 2.0, protocol 2024-11-05): stdio (no shell, allowlisted executable, per-request timeout) и HTTP transport через scope-enforced egress broker (POST + DNS/redirect revalidation); deferred tool schemas (`mcp tools` сохраняет в `app_settings`), dynamic `mcp.<server>.<tool>` resolution в registry (fail-closed, только enabled серверы), все MCP tools R2 approval-gated, output всегда untrusted-external + injection scanner;
- 199 unit/integration/CLI/TUI tests + opt-in docker integration test (`SAMURAI_TEST_DOCKER=1`);
- ruff, strict mypy и CLI/TUI E2E проходят успешно.

LLM и полный egress enforcement для произвольных процессов еще не реализованы. Stage 2b продолжается: egress proxy/DNS TOCTOU и расширенный набор security tools.

## 4. Карта репозитория

- `src/samurai_ai/domain/operations.py` — Operation, OperationMode, OperationStatus и переходы.
- `src/samurai_ai/domain/scope.py` — ScopeContract и детерминированные решения.
- `src/samurai_ai/domain/kernel.py` — PlanTask, StageGate, OperationPlan, Approval, Artifact и Finding.
- `src/samurai_ai/domain/recovery.py` — RecoveryAction, OperationSnapshot, RecoveryReport и diff.
- `src/samurai_ai/domain/execution.py` — ToolDefinition, ToolRun, redactor и injection scanner.
- `src/samurai_ai/domain/vault.py` — SecretMetadata, vault exceptions и `{{secret:name}}` placeholder syntax.
- `src/samurai_ai/domain/sandbox.py` — SandboxProfile, canonical payload и HMAC-SHA256 signing.
- `src/samurai_ai/domain/mcp.py` — McpServer, McpToolInfo, protocol constants и MCP exceptions.
- `src/samurai_ai/execution/` — static registry (с dynamic resolver hook), ProcessManager, HTTP egress broker (GET/HEAD/POST), Docker sandbox runner и minimal MCP client (stdio/HTTP transports).
- `src/samurai_ai/i18n.py` — RU/EN Translator и TUI resources.
- `src/samurai_ai/storage/database.py` — SQLite connection, transactions и migrations v1–v6.
- `src/samurai_ai/storage/operations.py` — OperationRepository и append-only events.
- `src/samurai_ai/storage/kernel.py` — KernelRepository для plans/approvals/artifacts/findings.
- `src/samurai_ai/storage/recovery.py` — snapshots, recovery actions и app settings.
- `src/samurai_ai/storage/execution.py` — ToolRunRepository и recovery-compatible runs.
- `src/samurai_ai/storage/vault.py` — VaultRepository для encrypted secret entries.
- `src/samurai_ai/storage/mcp.py` — McpServerRepository и deferred tool schema cache (app_settings).
- `src/samurai_ai/services/operations.py` — сервисный сценарий операций.
- `src/samurai_ai/services/kernel.py` — KernelService, policy preconditions и summary.
- `src/samurai_ai/services/recovery.py` — snapshots/restore/fork/reconcile/diff.
- `src/samurai_ai/services/settings.py` — persisted locale и `SAMURAI_LANG`.
- `src/samurai_ai/services/execution.py` — approval-gated managed tool execution с vault placeholder resolution.
- `src/samurai_ai/services/vault.py` — VaultService: AES-256-GCM encrypt/decrypt, scoping и resolve_placeholders.
- `src/samurai_ai/services/mcp.py` — McpService (CRUD, health, fetch_tools) и fail-closed McpToolResolver.
- `src/samurai_ai/cli.py` — CLI entry point и общие команды.
- `src/samurai_ai/cli_kernel.py` — plan/task/gate/artifact/finding/approval команды.
- `src/samurai_ai/cli_recovery.py` — snapshot/fork/reconcile/lang/diff команды.
- `src/samurai_ai/cli_execution.py` — tool list/run/show команды.
- `src/samurai_ai/cli_vault.py` — secret add/list/remove команды (значения не печатаются).
- `src/samurai_ai/cli_sandbox.py` — sandbox profiles/sign команды.
- `src/samurai_ai/cli_mcp.py` — mcp add/remove/list/enable/disable/health/tools/invoke команды.
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
samurai lang [ru|en]
```

Tool params поддерживают `{{secret:name}}` placeholders: значение подставляется из vault непосредственно перед исполнением, в ToolRun/events/artifacts сохраняется только placeholder-форма, а plaintext регистрируется в SecretRedactor запуска.

`sandbox.exec` исполняет только executables из allowlist подписанного профиля: образ обязан быть pinned by digest и уже присутствовать локально (auto-pull запрещён), контейнер стартует с `--network none`, `--read-only`, `--cap-drop ALL`, `no-new-privileges` и non-root user. Подпись allowlist создаётся через `samurai sandbox sign` и проверяется до любого запуска.

MCP: схемы инструментов импортируются только явным `samurai mcp tools <name>` (deferred, кэшируются в `app_settings`); вызов идёт как обычный governed tool run `mcp.<server>.<tool>` с R2 approval. HTTP MCP endpoints проходят через operation scope (для `health`/`tools` нужен `--operation`). Disabled сервер или unknown tool → fail-closed `ToolNotFoundError`.

TUI команды: `/plan`, `/tasks`, `/gates`, `/findings`, `/evidence`, `/approvals`, `/snapshots`, `/reconcile`, `/tools`, `/runs`, `/secrets`, `/lang`, `/status`, `/scope`.

## 7. Следующий этап — Isolation and Protocol Expansion

Stage 2a завершен: managed registry, process manager, HTTP scope broker, approvals, recovery, redaction и injection marking работают и покрыты тестами.

Следующий вертикальный срез — Stage 2b:

1. ~~Docker sandbox profiles с resource/network constraints и signed allowlist~~ (done: `sandbox.exec`, HMAC-signed static profiles, network none, fail-closed preflight).
2. Egress proxy/enforcement для произвольных процессов и устранение DNS TOCTOU.
3. ~~MCP stdio/HTTP, deferred schemas, health checks и trust model~~ (done: minimal JSON-RPC client, stdio+HTTP transports, `mcp.<server>.<tool>` R2 tools через ToolExecutionService, trust untrusted-external).
4. ~~Persistent secrets vault и per-operation secret scoping~~ (done: AES-256-GCM, SQLite v5, CLI/TUI, placeholder resolution в tool execution).
5. Расширенный набор security tools с отдельным threat review.

На Stage 3 добавить расширяемый model registry для frontier APIs и first-class Ollama/local provider; playbook не должен зависеть от vendor-specific API. Подробные acceptance criteria — в `md/development-plan.md`.

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

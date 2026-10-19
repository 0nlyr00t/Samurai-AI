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

Завершен **Этап 0 — Walking Skeleton**, **Этап 1 core — Operation Kernel** и **Этап 1b — Recovery Hardening**:

- четыре режима операций и проверяемые lifecycle transitions;
- include/exclude scope для domain, wildcard, URL origins и IP/CIDR;
- plans, tasks, dependencies, stage gates и artifact requirements;
- approvals R0–R4: R0/R1 auto-approved, R2/R3 требуют решения, R4 auto-rejected;
- immutable evidence artifacts с SHA-256 и файловым хранилищем;
- findings workflow `candidate → validating → validated / false-positive / duplicate → reported`;
- immutable operation snapshots и безопасный restore в новую операцию;
- operation fork без копирования artifact content, findings или approvals;
- recovery actions и reconcile in-flight → `unknown` без retry;
- plan/scope diff;
- SQLite migrations v1–v3, append-only events и optimistic concurrency;
- kernel/recovery CLI и TUI;
- RU/EN first-run выбор языка, persisted locale и `/lang`;
- 76 unit/integration/CLI/TUI tests;
- ruff, strict mypy и CLI/TUI E2E проходят успешно.

LLM, MCP и внешнее выполнение security-инструментов еще не реализованы. Следующий этап — Stage 2, Tool Execution и безопасность.

## 4. Карта репозитория

- `src/samurai_ai/domain/operations.py` — Operation, OperationMode, OperationStatus и переходы.
- `src/samurai_ai/domain/scope.py` — ScopeContract и детерминированные решения.
- `src/samurai_ai/domain/kernel.py` — PlanTask, StageGate, OperationPlan, Approval, Artifact и Finding.
- `src/samurai_ai/domain/recovery.py` — RecoveryAction, OperationSnapshot, RecoveryReport и diff.
- `src/samurai_ai/i18n.py` — RU/EN Translator и TUI resources.
- `src/samurai_ai/storage/database.py` — SQLite connection, transactions и migrations v1–v3.
- `src/samurai_ai/storage/operations.py` — OperationRepository и append-only events.
- `src/samurai_ai/storage/kernel.py` — KernelRepository для plans/approvals/artifacts/findings.
- `src/samurai_ai/storage/recovery.py` — snapshots, recovery actions и app settings.
- `src/samurai_ai/services/operations.py` — сервисный сценарий операций.
- `src/samurai_ai/services/kernel.py` — KernelService, policy preconditions и summary.
- `src/samurai_ai/services/recovery.py` — snapshots/restore/fork/reconcile/diff.
- `src/samurai_ai/services/settings.py` — persisted locale и `SAMURAI_LANG`.
- `src/samurai_ai/cli.py` — CLI entry point и общие команды.
- `src/samurai_ai/cli_kernel.py` — plan/task/gate/artifact/finding/approval команды.
- `src/samurai_ai/cli_recovery.py` — snapshot/fork/reconcile/lang/diff команды.
- `src/samurai_ai/tui/app.py` — локализованный TUI без доменной логики в обработчиках.
- `tests/unit` — чистое доменное ядро и scope/kernel/recovery edge cases.
- `tests/integration` — SQLite, recovery, append-only, CLI и kernel E2E.
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
samurai lang [ru|en]
```

TUI команды: `/plan`, `/tasks`, `/gates`, `/findings`, `/evidence`, `/approvals`, `/snapshots`, `/reconcile`, `/lang`, `/status`, `/scope`.

## 7. Следующий этап — Tool Execution и безопасность

Stage 1b завершен: snapshots/restore/fork, reconcile, plan/scope diff и RU/EN localization работают и покрыты тестами.

Следующий вертикальный срез — Stage 2:

1. Versioned tool registry и native adapters с risk/capability metadata.
2. Async process manager: background, cancel, timeout, resource limits, circuit breaker.
3. Docker sandbox и network egress policy; повторная scope-проверка DNS/redirect.
4. MCP stdio/HTTP, deferred schemas, health checks и trust model.
5. Secrets vault/redaction и prompt-injection defenses.
6. Интеграция in-flight tool actions с recovery journal.

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

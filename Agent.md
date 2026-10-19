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

Требования: Python 3.12+, uv. Docker понадобится на этапе Tool Execution.

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

Завершен **Этап 0 — Walking Skeleton** и **Этап 1 core — Operation Kernel**:

- четыре режима операций и проверяемые lifecycle transitions;
- include/exclude scope для domain, wildcard, URL origins и IP/CIDR;
- plans, tasks, dependencies, stage gates и artifact requirements;
- approvals R0–R4: R0/R1 auto-approved, R2/R3 требуют решения, R4 auto-rejected;
- immutable evidence artifacts с SHA-256 и файловым хранилищем;
- findings workflow `candidate → validating → validated / false-positive / duplicate → reported`;
- policy-gated operation transitions;
- SQLite migrations v1/v2, append-only events и optimistic concurrency;
- kernel CLI и read-only TUI views;
- 54 unit/integration/CLI/TUI tests;
- ruff, strict mypy и CLI E2E проходят успешно.

LLM, MCP и внешнее выполнение security-инструментов еще не реализованы. Snapshots/reconcile/fork еще не реализованы и запланированы отдельным recovery-срезом перед Tool Execution. Не показывать эти возможности как готовые.

## 4. Карта репозитория

- `src/samurai_ai/domain/operations.py` — Operation, OperationMode, OperationStatus и переходы.
- `src/samurai_ai/domain/scope.py` — ScopeContract и детерминированные решения.
- `src/samurai_ai/domain/kernel.py` — PlanTask, StageGate, OperationPlan, Approval, Artifact и Finding.
- `src/samurai_ai/storage/database.py` — SQLite connection, transactions и migrations.
- `src/samurai_ai/storage/operations.py` — OperationRepository и append-only events.
- `src/samurai_ai/storage/kernel.py` — KernelRepository для plans/approvals/artifacts/findings.
- `src/samurai_ai/services/operations.py` — сервисный сценарий операций.
- `src/samurai_ai/services/kernel.py` — KernelService, policy preconditions и summary.
- `src/samurai_ai/cli.py` — CLI entry point и общие команды.
- `src/samurai_ai/cli_kernel.py` — plan/task/gate/artifact/finding/approval команды.
- `src/samurai_ai/tui/app.py` — TUI без доменной логики в обработчиках.
- `tests/unit` — чистое доменное ядро и scope/kernel edge cases.
- `tests/integration` — SQLite, recovery, append-only, CLI и kernel E2E.
- `tests/tui` — клавиатурный TUI smoke через `run_test`.

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
samurai plan show|approve <operation-id>
samurai task start|complete <operation-id> <task-id>
samurai gate pass <operation-id> <gate-id>
samurai artifact add|list <operation-id> ...
samurai finding create|list|attach|validate|report ...
samurai approval request|list|approve|reject ...
```

TUI read-only команды: `/plan`, `/tasks`, `/gates`, `/findings`, `/evidence`, `/approvals`, `/status`, `/scope`.

## 7. Следующий этап — Recovery Hardening и Tool Execution

Сначала завершить Stage 1b:

1. Operation snapshots и controlled restore points.
2. Reconcile in-flight actions после crash/restart; действие с неизвестным внешним результатом не повторять автоматически.
3. Operation fork/resume без копирования секретов и без потери audit trail.
4. Diff для изменений scope/plan и operator-facing recovery report.

Затем начинать Stage 2: tool registry, async process manager, sandbox, egress policy, MCP, secrets redaction и prompt-injection defenses. Подробные acceptance criteria — в `md/development-plan.md`.

## 8. Инварианты и правила изменений

- Не ослаблять scope и не переносить enforcement только в prompts.
- Не выполнять сетевые или деструктивные действия в unit/integration tests.
- Не добавлять неограниченную рекурсию агентов.
- Не хранить секреты в events, reports, fixtures или Git.
- Не менять существующие миграции после появления пользовательских данных; добавлять новые версии.
- Сохранять версионирование схем и обратную совместимость или явно документировать migration path.
- Для любого bug fix сначала добавить воспроизводящий тест.
- Комментарии и документацию обновлять вместе с поведением.

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

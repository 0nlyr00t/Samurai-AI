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

Завершен **Этап 0 — Walking Skeleton**:

- четыре режима операций и проверяемые lifecycle transitions;
- include/exclude scope для domain, wildcard, URL origins и IP/CIDR;
- IDNA, IPv4/IPv6, deny by default и приоритет exclusions;
- SQLite migrations, operations и append-only events;
- optimistic concurrency для обновления операции;
- CLI и базовый Textual TUI;
- 29 unit/integration/CLI/TUI tests;
- ruff, strict mypy, package build и CLI smoke проходят успешно.

LLM, MCP и внешнее выполнение security-инструментов еще не реализованы. Не показывать эти возможности как готовые и не имитировать AI-работу.

## 4. Карта репозитория

- `src/samurai_ai/domain/operations.py` — Operation, OperationMode, OperationStatus и переходы.
- `src/samurai_ai/domain/scope.py` — ScopeContract и детерминированные решения.
- `src/samurai_ai/storage/database.py` — SQLite connection, transactions и migrations.
- `src/samurai_ai/storage/operations.py` — OperationRepository и append-only events.
- `src/samurai_ai/services/operations.py` — сервисный сценарий операций.
- `src/samurai_ai/cli.py` — CLI entry point и команды.
- `src/samurai_ai/tui/app.py` — TUI без доменной логики в обработчиках.
- `tests/unit` — чистое доменное ядро и scope edge cases.
- `tests/integration` — SQLite, recovery, append-only и CLI.
- `tests/tui` — клавиатурный TUI smoke через `run_test`.

## 5. Текущая семантика scope

- `example.com` — только apex domain.
- `*.example.com` — subdomains, но не apex; для обоих нужны два include.
- Exclusions имеют приоритет над includes.
- URL rules поддерживают только HTTP(S) origins; path-level rules пока отклоняются.
- Domain/IP rules могут применяться к HTTP(S) URL targets.
- Невалидная цель дает `INVALID_TARGET` и `allowed=False`.
- DNS resolution, redirect и network egress enforcement относятся к следующему этапу исполнения, а не к текущему парсеру.

## 6. Следующий этап — Operation Kernel

Реализовывать вертикальными срезами с тестами:

1. Добавить доменные модели Plan, Task, Dependency, StageGate и artifact requirements.
2. Добавить RiskClass R0–R4, Approval и отдельный policy engine; не делегировать approvals модели.
3. Добавить Evidence/Artifact и Findings workflow: `Candidate → Validating → Validated / False Positive / Duplicate → Reported`.
4. Расширить migrations и repositories; сохранить append-only события и optimistic concurrency.
5. Реализовать snapshots/reconcile: после crash действие с неизвестным внешним результатом не повторять автоматически.
6. Добавить Plan/Tasks/Approvals/Evidence/Findings панели в TUI только после появления реальных сервисов.
7. Проверить, что gate нельзя закрыть без обязательных артефактов, а finding нельзя опубликовать без доказательств.

Подробные acceptance criteria — в `md/development-plan.md`.

## 7. Инварианты и правила изменений

- Не ослаблять scope и не переносить enforcement только в prompts.
- Не выполнять сетевые или деструктивные действия в unit/integration tests.
- Не добавлять неограниченную рекурсию агентов.
- Не хранить секреты в events, reports, fixtures или Git.
- Не менять существующие миграции после появления пользовательских данных; добавлять новые версии.
- Сохранять версионирование схем и обратную совместимость или явно документировать migration path.
- Для любого bug fix сначала добавить воспроизводящий тест.
- Комментарии и документацию обновлять вместе с поведением.

## 8. Definition of Done для каждого этапа

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

## 9. Git и безопасность credentials

- Основная ветка: `main`.
- Не использовать force push для обычной синхронизации.
- Перед commit проверить `git status` и исключить `.samurai`, `.venv`, `dist` и локальные caches.
- Использовать настроенный Git credential helper или SSH; не помещать токен в remote URL, команды, логи и файлы проекта.

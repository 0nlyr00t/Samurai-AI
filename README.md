<p align="center">
  <img src="assets/samurai-logo.svg" width="760" alt="Samurai AI logo">
</p>

<p align="center">
  <strong>Русский</strong> · <a href="README.en.md">English</a>
</p>

# Samurai AI

Samurai AI — local-first TUI harness для авторизованных Red Team-операций, Bug Bounty, CTF/HTB и Operation Planning. Продукт строится вокруг управляемой операции, а не вокруг неограниченного чата с инструментами.

## Принципы

- **Scope before action** — сначала границы и правила, затем выполнение.
- **Workflow before autonomy** — автономность только внутри понятного процесса.
- **Evidence before finding** — результат сканера еще не является уязвимостью.
- **Operator stays in control** — оператор может проверить, остановить и восстановить работу.
- **Policy outside the model** — scope, approvals и переходы не делегируются LLM.

## Текущее состояние

Завершены **Stage 0**, **Stage 1/1b**, **Stage 2a/2b**, **Stage 3**, **Stage 4 целиком — Bug Bounty, CTF/HTB, Red Team и Operation Planning playbooks** и **Stage 5 целиком — параллельные специализированные агенты, campaign memory, custom playbooks/skills, CALDERA/VECTR exports, headless/CI и HTML reports**.

Уже реализовано:

- четыре режима операций и проверяемый lifecycle;
- include/exclude scope для domain, wildcard, URL origins и IP/CIDR;
- plans, tasks, dependencies, stage gates и artifact requirements;
- approvals R0–R4 и approval-gated tool execution;
- immutable evidence artifacts с SHA-256 и redaction;
- findings workflow от candidate до reported;
- snapshots/restore/fork, recovery reconcile и plan/scope diff;
- versioned tool registry, `process.exec` без shell и `http.fetch` через scope broker;
- Docker sandbox profiles со signed allowlist, scoped egress proxy с DNS pinning;
- MCP stdio/HTTP client и persistent secrets vault (AES-256-GCM);
- prompt-injection warnings и `untrusted-external` trust label;
- LLM provider abstraction (openai-compatible, anthropic, gemini, ollama),
  ModelRouter с retry/fallback, per-operation budgets и оркестратор
  Planner/Executor/Parser/Verifier с закрытой command schema;
- Bug Bounty playbook: program rules, scope-enforced asset ledger с diff,
  dedup находок по fingerprint, governed gates;
- отчеты (Markdown/JSON) только из validated findings, evidence package
  с manifest SHA-256, redaction секретов и блокировка out-of-scope данных;
- CTF/HTB playbook: категории по BRD, hypothesis log с конечным автоматом,
  immutable flag ledger с regex-валидацией формата и proof-артефактами,
  replay из журнала событий, writeup (Markdown/JSON) с redaction и export;
- Red Team playbook: ROE с blocking approval до старта исполнения, attack path
  с ATT&CK mapping и зависимостями, detection expectations и outcomes,
  checkpoints, debrief с open gaps, redaction и SHA-256 export;
- Operation Planning playbook: сценарии без активных действий, test cases
  с ATT&CK/kill chain mapping и detection expectations, stop points,
  runbook (Markdown/JSON) в статусе planning с redaction и export;
- параллельные специализированные агенты (swarm): bounded thread pool по
  независимым задачам плана, специализации general/recon/testing/analysis
  (prompt-level, governance идентичен), авто-выбор ready-задач, preflight
  model approval, отчет wall time vs sequential estimate со speedup,
  SQLite v13, CLI `swarm ready/run/list/show` и TUI `/swarm`;
- campaign memory: workspace-scoped записи (methodology/decision/failure/note)
  с retention и удалением, read-only для агентов (bounded redacted digest в
  prompts, аудит в agent.run.start), CLI `memory` и TUI `/memory`;
- custom playbooks (JSON templates со структурной валидацией, immutable
  хранилище, `playbook install/use/list-custom`) и trusted versioned skills
  (`--trusted` обязателен, sha256, инжекция в executor prompt), TUI
  `/playbooks` и `/skills`, SQLite v14;
- стабильный контракт exit codes (0/1/2, зафиксирован тестами), экспорт
  журнала в NDJSON (`events export`), headless mode для CI (`headless run`
  с JSON summary), GitHub Actions CI (ruff, strict mypy, pytest, build);
- CALDERA export (adversary pack из executed attack steps с ATT&CK tactics)
  и VECTR export (`vectr-campaign@v1` JSON) с redaction и sha256 manifest;
- HTML bug bounty report и self-contained read-only HTML dossier операции
  (`operation export --format html`) с escaping и redaction;
- RU/EN first-run выбор языка и переключение через `/lang`;
- SQLite migrations v1–v14, append-only events и optimistic concurrency;
- CLI и TUI с реальными kernel/recovery/tool/agent/playbook-представлениями;
- 514 unit/integration/CLI/TUI tests.

Еще не реализовано: PDF reports (нет stdlib-пути), live API интеграции
CALDERA/VECTR (только export), расширенный набор security tools (после
threat review), LLM streaming, полный egress enforcement на
Docker Desktop for Mac и standalone binary (Stage 6).

## Быстрый старт для разработки

Требования: Python 3.12+ и uv.

```bash
git clone https://github.com/0nlyr00t/Samurai-AI.git
cd Samurai-AI
uv sync --locked
uv run pytest
```

Создать операцию и открыть TUI:

```bash
uv run samurai operation create "Lab" \
  --mode ctf-htb \
  --objective "Solve the authorized lab" \
  --include 192.0.2.10

uv run samurai tui
```

Проверки качества:

```bash
uv run ruff check .
uv run mypy src
uv build
```

## Целевая поставка

Финальный продукт должен поставляться как standalone per-platform `samurai` executable/installer без установленных Python и uv. Docker, security tools, MCP servers и Ollama останутся optional dependencies с health checks.

## Модели

На этапе LLM orchestration будет реализован расширяемый model provider registry с capability negotiation: OpenAI, Anthropic, Google Gemini, Azure OpenAI, AWS Bedrock, DeepSeek, xAI, Qwen, Moonshot/Kimi, Mistral, OpenRouter, OpenAI-compatible endpoints и first-class Ollama.

Playbook не должен зависеть от vendor-specific API.

## Локализация

Продукт должен предлагать выбор **RU/EN** при первом запуске, сохранять выбранный язык и позволять переключать его позже. Все новые operator-facing строки добавляются одновременно в русские и английские ресурсы.

## Документация

- `md/requirements.md` — бизнес-требования.
- `md/development-plan.md` — roadmap, этапы и acceptance criteria.
- `Agent.md` — handoff для продолжения разработки.
- `README.en.md` — English README.

## Безопасное использование

Samurai AI предназначен только для авторизованных security research, CTF/HTB, Bug Bounty в рамках правил программы и Red Team/adversary emulation с явным разрешением владельцев систем. Не используйте продукт вне согласованного scope.

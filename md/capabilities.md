# Возможности и состояние

Детальный перечень того, что реализовано, и операторские команды ночной волны. Витрина — в `README.md`.

## Текущее состояние

Завершены **Stage 0**, **Stage 1/1b**, **Stage 2a/2b**, **Stage 3**, **Stage 4 целиком — Bug Bounty, CTF/HTB, Red Team и Operation Planning playbooks**, **Stage 5 целиком — параллельные специализированные агенты, campaign memory, custom playbooks/skills, CALDERA/VECTR exports, headless/CI и HTML reports** и **Stage 6a — evaluation harness с метриками и regression-гейтами**, **Stage 6b — fault injection, prompt-injection containment, soak и migration/backup/restore suites** и **Stage 6c — standalone binary, воспроизводимые сборки, подписанные релизы и политика совместимости (`md/compatibility.md`)** и **Этап 7 (волна 1) — governed security tools adapters `recon.httpx-probe` и `scan.nuclei` по threat review**, **Этап 7 (волна 2) — env-канал секретов и `recon.subfinder-passive` (ключи уезжают именем, а не значением)**, **Этап 7 (волна 3) — `recon.dnsx-resolve` через закреплённый DoH и SOCKS5-вход egress-прокси** и **Этап 7 (волна 3b) — подпись allowlist без ручного шага (`sandbox verify`)** и **Этап 7 (волна 4) — сканер портов `scan.nmap` и класс сети `direct`**, **Этап 7 (волна 5) — фаззер путей `fuzz.ffuf`**, **разведка через браузер `research.search`/`research.read` с цитатами**, **операционная волна — туннели, ssh/SMB/RDP/ADB, виртуалки, сборка эксплойтов, лестница импакта и гены** и **Этап 8 — TUI UX: стрелочная навигация (выбор языка, `/model` picker модели и reasoning), чёрная тема по умолчанию и цветной журнал** , **Stage 8+ — вкладки Журнал/План/Находки/Доступ, автодополнение slash-команд, выделение и копирование текста, режим `/yolo`, доверие к директории (`/trust`) и отображение cwd в статус-баре** и **Этап 9 — harness UX: режимы разрешений, plan mode, goal mode, steering, shell-режим, `/undo` и `/compact`**.

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
  `/playbooks` и `/skills`, SQLite v17;
- стабильный контракт exit codes (0/1/2, зафиксирован тестами), экспорт
  журнала в NDJSON (`events export`), headless mode для CI (`headless run`
  с JSON summary), GitHub Actions CI (ruff, strict mypy, pytest, build);
- CALDERA export (adversary pack из executed attack steps с ATT&CK tactics)
  и VECTR export (`vectr-campaign@v1` JSON) с redaction и sha256 manifest;
- HTML bug bounty report и self-contained read-only HTML dossier операции
  (`operation export --format html`) с escaping и redaction;
- evaluation harness (Stage 6a): декларативные JSON-сьюты сценариев на
  детерминированном scripted provider, метрики solve/validation rate, cost,
  wall time, interventions, invalid actions и false positives из журнала
  событий, сравнение с baseline для регрессий (`eval run`, `eval compare`),
  smoke-сьют `evals/smoke.json` в CI;
- режимы разрешений `ask`/`yolo`/`never` с переиспользованием approval и бейджами чувствительных путей, plan mode (`Shift-Tab`) по признаку `read_only`, goal mode с бюджетом шагов и времени, steering `Ctrl-S`, shell-режим `!`, `/undo` и `/compact`;
- три темы (тёмная, светлая, высокий контраст) и свои файлы тем, перебиндинг глобальных клавиш;
- доски плана и находок (`/tasks`, `/findings`) с действиями и деталями;
- сессии субагентов `/subagent` и `Ctrl-G`: субагент берёт задачу плана, работает в фоне, виден, останавливается;
- палитра всех действий `Ctrl-P`, оверлей клавиш `?`, инспектор запуска (`/inspect`, `Ctrl-I`) на восемь групп с пере-проверкой scope, экран приветствия и пустые состояния;
- RU/EN first-run выбор языка и переключение через `/lang`;
- SQLite migrations v1–v17, append-only events и optimistic concurrency;
- CLI и TUI с реальными kernel/recovery/tool/agent/playbook-представлениями;
- 1193 unit/integration/CLI/TUI tests.

Еще не реализовано: PDF reports (нет stdlib-пути), live API интеграции
CALDERA/VECTR (только export), secondary-источники вне матрицы (
технические блокеры в `md/threat-review-security-tools.md`), LLM streaming,
doom-loop guard для авторазрешённых петель, shell-режим в фоне на уровне ОС,
extension seams/плагины (инструменты уже добавляются манифестами),
web-поверхность операции (сокет-демон уже есть), distributed runners и air-gapped модели, полный
egress enforcement на Docker Desktop for Mac и OS-пакеты installers (есть
bare standalone binary). ADB и VBoxManage проверены только mock-прогонами,
гены пока не мерялись полевой серией — см. `md/night-wave-report.md`.

## Живая сессия: демон и второй терминал

Работа больше не умирает вместе с командой. `samurai serve` поднимает сессионный
демон на unix-сокете `~/.config/samurai/run/serve.sock` (0600), к нему
подключаются с другого терминала:

```bash
samurai serve &                                        # демон
samurai attach <op>                                    # поток событий, read-only
samurai send <op> --message "смотри 22 порт"           # записка в журнал
samurai send <op> --approve                            # одобрить ожидающий шаг
samurai send <op> --answer <call> --pick target=app    # ответить агенту
```

`attach --from <seq>` продолжает с курсора, `--once` печатает накопленное и
выходит. В TUI то же самое открывает `Ctrl-T`. Обрыв клиента работу не
останавливает; остановка — отдельно, `samurai call run.cancel --params …`.
Сокет локальный: право подключения равно праву читать state-каталог, права не
размываются — read-only пир не может писать, и всё равно решает общая таблица
доступа.

## От цели до прогона одной командой

```bash
samurai run "Takeoff" --mode ctf-htb --include 10.129.98.19 \
  --include flight.htb --task "Скан портов" --task "Разобрать веб"   --model kimi/kimi-k2-turbo-preview@v1
```

Команда создаёт операцию, план-цепочку, одобряет план и ведёт работу до конца
плана. `--detach` оставляет её демону. Vhost без DNS описывается алиасом —
`samurai scope host add <op> flight.htb 10.129.98.19` (имя резолвится внутри
песочницы, scope судит его по адресу). Если что-то не отвечает вообще,
`samurai sandbox check <op>` покажет, с какой стороны обрыв; для подсетей,
которые не роутит бридж, есть `SAMURAI_SANDBOX_NETWORK=host` (мимо прокси, но
проверка scope остаётся).

## Операторские команды ночной волны

- `samurai finding impact attempt <id> <read|write|auth-bypass|data-export|rce> --evidence <id> [--rollback "…"]` — поднять импакт находки; `finding impact grade <id>` показывает уровень, баллы, порог отчётности и бюджет.
- `samurai gene list|add|fitness|promote|rollback` — пул эволюционирующих подсказок: ген рождается в тени, фитнес берётся из отчёта eval (`--report`), в промпт идут только активные.
- `samurai rules list|add|rm` — поаргументные правила `allow|ask|deny`.
- `samurai tool install <file> --trusted` и `tool manifests` — инструменты манифестами, правка после установки видна как `tampered`.
- `samurai sandbox verify` — сходится ли подпись профилей, без записи.
- `samurai report generate <op> --min-impact 3` — не выпускать отчёт по находкам ниже порога импакта.
- `samurai policy set|list|rm <op>` — поднять роль, нужную для команды внутри одной операции (только ужесточение, владелец).
- `samurai agent questions <op>` / `samurai agent answer <call> --select id=Метка` — открытые вопросы агента и ответы на них (вопрос ждёт ограниченно и не останавливает прогон).
- Белт разложен на два образа — `remote` (админка) и `exploit` (компилятор и библиотеки); RDP-проба своя, на X.224 и TLS, поэтому freerdp и Xvfb в образах нет; оба публикуются из CI (`ghcr.io/<owner>/samurai-*`, amd64+arm64, по digest индекса), локально — `packaging/sandbox/build.sh remote|exploit`. Пакет в GHCR по умолчанию приватный. Видимость переключается вручную на странице пакета (`Settings` → `Change visibility`): ни `GITHUB_TOKEN`, ни токен `gh` через REST её не меняют (эндпоинт видимости отвечает 404). Пока пакет закрыт, `pull` требует `docker login ghcr.io` с токеном, у которого есть `read:packages`. PR с пином от бота требует approve прогона проверок (PR создан `GITHUB_TOKEN`).



## Установка и снос

`install.sh` и `uninstall.sh` — единственные штатные пути установки и удаления (macOS/Linux, без root): подписанный бинарник релиза, обязательный sha256, Sigstore при наличии `cosign`, откат на uv только при отсутствии ассета под платформу. Деинсталлятор снимает то же, что поставил, включая состояние, если не передан `--keep-state`; без терминала сначала показывает план. Разбор решений — `md/threat-review-security-tools.md` §3i.

## Поставка

Standalone per-platform `samurai` (PyInstaller one-file, без Python и uv на машине): локальная сборка — `bash packaging/build_binary.sh`, релизы linux-x86_64/darwin-arm64/windows-x86_64 собираются в `.github/workflows/release.yml` по тегу `v*` и подписываются Sigstore keyless (проверка — `md/compatibility.md`). sdist/wheel воспроизводимы побитно (`packaging/check_reproducible.sh` в CI). Docker, security tools, MCP servers и Ollama — optional dependencies с health checks.

## Модели

Расширяемый registry провайдеров с согласованием возможностей: OpenAI, Anthropic, Google Gemini, Azure OpenAI, AWS Bedrock, DeepSeek, xAI, Qwen, Moonshot/Kimi, Mistral, OpenRouter, OpenAI-compatible endpoints и first-class Ollama. Playbook не зависит от vendor-specific API.

## Локализация

RU/EN предлагается при первом запуске, выбор сохраняется и переключается через `/lang`. Новые operator-facing строки добавляются сразу в оба набора.

## Разработка

Требования: Python 3.12+ и uv.

```bash
git clone https://github.com/0nlyr00t/Samurai-AI.git
cd Samurai-AI
uv sync --locked
uv run pytest
```

Операция и TUI локально:

```bash
uv run samurai operation create "Lab" --mode ctf-htb \
  --objective "Solve the authorized lab" --include 192.0.2.10
uv run samurai tui
```

Проверки качества: `uv run ruff check .`, `uv run mypy src`, `uv build`.

Скриншот для README пересобирается из кода: `uv run python ops/screenshot.py [--lang en]`.

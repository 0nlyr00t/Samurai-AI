---
id: module-execution
type: module-design
title: Execution adapters и stateful sessions
status: draft
parent: harness-vision
tags: [execution, sessions, recovery]
---

## Назначение

Execution adapters выполняют инструменты через governed scope, approval и evidence.
`session.control` добавляет stateful SSH/PTY lifecycle без смены общей политики:
`open → exec → read/write → reconnect → close`.

## Sessions

OpenSSH ControlMaster принадлежит `SessionService`; каждый exec — отдельный channel
и `ProcessManager` handle. Session id и command id не являются PID или authorization
token. `read` ограничивает snapshot, `write` работает только с running command,
`cancel` относится к одному процессу, `collect_finished` выдаёт результат один раз.
Host restart не восстанавливает control authority по сохранённому PID.

Credentials остаются вне argv: пароль идёт через askpass env, ключ — tmpfs 0600.
Scope проверяется до spawn. Unattended production требует bubblewrap; helper files
и socket удаляются при close. Для stateful backends `/tmp` — единственный writable
root: ready markers и ControlMaster socket должны быть видны parent, а не скрыты
private tmpfs. Остальная файловая система остаётся read-only; host `DISPLAY`
заменяется собственным Xvfb внутри namespace.

## Outcomes

Команда имеет `RUNNING/SUCCEEDED/FAILED/CANCELLED/UNKNOWN` и external outcome
`OBSERVED/UNOBSERVED`. Timeout или потеря transport без наблюдаемого remote verdict
дают `UNKNOWN + UNOBSERVED`, а не ordinary failure. `retry` после UNKNOWN запрещён
до reconciliation; reconnect помечает активные команды UNKNOWN и не делает replay.

`CANCELLED + UNOBSERVED` означает, что локальный процесс остановлен, но remote
outcome не подтверждён; retry после него тоже запрещён до reconciliation.

`ToolRunStatus.UNKNOWN` имеет тот же смысл на governed journal. `tool.run.unknown`
является terminal событием и sticky outcome: `timeout_repeat` включает UNKNOWN,
поэтому неизменённый вызов не повторяется автоматически. Background completion
не считается подтверждением внешнего результата — оно лишь доставляет уже
зафиксированный terminal outcome.

RPC `RunRegistry` сохраняет verdict work callback: `PARTIAL` и `FAILED` становятся
`partial` и `failed`, а не daemon-level `done`; отсутствие verdict остаётся `done`.

## Shell filesystem

Trusted write roots включают cwd, явно добавленные directories и стандартные
`/tmp`/`/var/tmp` scratch. Bwrap монтирует scratch как private tmpfs; остальные
outside writes остаются deny/ask по permission mode. Denial содержит конкретные
paths и filesystem-scope reason, а не неточный `denied by operator`.

Static write-target parser использует shell tokens и command-specific output flags:
quoted payloads, URL paths, `grep -o`, `/dev/null` и `/dev/tcp`/`udp` не являются
filesystem writes. Dynamic paths не угадываются — их ограничивает bwrap. Внутренний
`.config/samurai` state не является operation workspace; artifacts принадлежат
domain services, а обычные shell writes идут в cwd/trusted dirs или scratch.

## LOOT export

`LootExportService` сохраняет `./loot/<operation_id>/findings.json`,
`credentials.json`, `flags.json` с полными captured values. Это приватные
snapshots, не public artifacts: directories 0700, files 0600, atomic replace,
внутренний `.gitignore`. Provider/service keys исключены. TUI обновляет snapshot
только при смене signature; `SAMURAI_LOOT_DIR` изолирует headless/test runs.

## Direct network

Direct nmap проверяет scope до spawn, но не имеет scoped egress audit. Это явное
ограничение transport, а не скрытое поведение: production принимается только на
Linux, а отсутствие enforceable audit не выдаётся за контролируемый proxy.

IP/domain include покрывает HTTP(S) URL на любом валидном порту; явный URL include
остаётся origin-specific. ScopeGuard отделяет trailing shell `;`, `&`, `|`, `)` от
target перед parsing. Literal `NAME=value`/`export NAME=value` подставляется в
host/port, включая nested shell; unknown variables остаются fail-closed. Data-only
`echo`/`printf`/`cat`/`tee` payload не является destination, а nested `bash -c`
проверяется. Parameter suffix удаляется только при неизменном host. Variable
refusal объясняет, что assignments живут в одном shell call, а variable port
записывается как `:$PORT`; malformed target не превращается в scope widening.

## RDP

`RdpBackend` использует один desktop master и redirected drive; exec передаёт
JSON request и ждёт результат, не открывая новый desktop. `remote.rdp-exec`
сохраняет scoped tunnel и egress evidence. `shell=posix` использует xterm и
`~/thinclient_drives`, `shell=cmd` — `cmd.exe` и `\\tsclient`. Master readiness
ограничена 30 секундами; xdotool search не применяется, потому что remote terminal
не является отдельным local X11 window. `GUI:fetch=<http(s)>` предпочтителен для
web-ресурсов, координатный GUI остаётся fallback. `force_native=True` означает,
что Docker belt не содержит RDP display tooling и не выдаётся за поддерживаемый.

## Agent execution

`execution_mode` различает independent read-only batches и stateful/destructive
работу. Параллелизм допускается только в read-only plan mode, с сохранением
порядка результатов; любой sequential tool возвращает batch в serial. `length`
в provider response запрещает выполнение потенциально обрезанных tool calls.

Explicit `agent_spawn` не завершает parent turn до handback: родитель ждёт terminal
result и продолжает тот же turn с фактическим status. Ручной `/subagent` не
удерживает turn; auto-strategist по новым facts запускается до terminal verdict и
использует тот же handback. Operator stop не блокируется ожиданием. Parked
background tools аналогично дожидаются результатов и drain перед terminal verdict.

Run завершается только после recovery/closing и проверки task/evidence. `repair_interrupted`
не выполняет tools: он либо подтверждает уже достигнутый task, либо завершает run
как interrupted с причиной и предлагает новый run. Subagent получает результат,
но не privileged `admin/sessions/c2` execution; эти вызовы принадлежат родителю.

## Граница

Adapters не принимают scope/ROE решений и не меняют evidence вручную: они вызываются
через `ToolExecutionService`. Privileged execution принадлежит родительскому
agent/session; subagent получает результат, а не authority над session.

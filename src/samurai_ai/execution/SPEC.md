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

## Direct network

Direct nmap проверяет scope до spawn, но не имеет scoped egress audit. Это явное
ограничение transport, а не скрытое поведение: production принимается только на
Linux, а отсутствие enforceable audit не выдаётся за контролируемый proxy.

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

Run завершается только после recovery/closing и проверки task/evidence. `repair_interrupted`
не выполняет tools: он либо подтверждает уже достигнутый task, либо завершает run
как interrupted с причиной и предлагает новый run. Subagent получает результат,
но не privileged `admin/sessions/c2` execution; эти вызовы принадлежат родителю.

## Граница

Adapters не принимают scope/ROE решений и не меняют evidence вручную: они вызываются
через `ToolExecutionService`. Privileged execution принадлежит родительскому
agent/session; subagent получает результат, а не authority над session.

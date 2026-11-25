# Development plan

## Principles

- Scope, ROE, approvals and installation trust are deterministic outside the LLM.
- Every hypothesis is tested and closed with evidence or an explicit reason.
- Completion requires a final conclusion and correct terminal status: `COMPLETED`,
  `BLOCKED`, `CANCELLED` or `UNKNOWN`; an unknown external outcome is never hidden.
- Evidence, credentials and live-operation artifacts remain local. Public git contains
  architecture and roadmap only, not live IPs, flags, credentials or exploit chains.

## Delivered capabilities

- Governed operation playbooks for bug bounty, CTF/HTB, red team and operation planning.
- Evidence ledger, immutable artifacts, findings, flags, hypotheses and readiness reports.
- TUI with kill chain, context hints, notices, scope rationale and operator rollback.
- Agent planner/executor with recovery, context compaction, guarded tool calls,
  execution modes and honest terminal verdicts.
- Governed tool draft → operator review → trusted install → provision → healthcheck.
- Linux production isolation with fail-closed unattended execution.
- Stateful sessions: open, exec, read/write, cancel, reconnect and close.
- `UNKNOWN + UNOBSERVED` for unobserved remote results; retry requires reconciliation.
- RDP desktop backend with POSIX xterm, Windows cmd.exe, scoped tunnel and
  bounded HTTP alternative to coordinate-driven GUI actions.
- Lifecycle healthchecks and reviewed OpenSSH recipe.
- Secret redaction for vault values and JSON sensitive keys.
- Persistent planning-to-execution model handoff after a checklist and observed
  shell result, with operator-visible `/handoff` controls and no schema migration.
- Per-operation stream rules with passive reminders, bounded interrupt/retry,
  pre-execution checks for every provider and no matched content in audit events.
- Empty fully denied scope when an operation has no explicit target; no implicit
  loopback authorization from chat, shell mode or the new-operation screen.
- Opt-in independent reviewer with redacted transcript deltas, bounded notes,
  explicit fallback, isolated failures and no authority over execution.
- Bounded shadow-gene experiments with immutable approved benchmarks, strict
  measured keep/discard, auto-resume limits and no automatic promotion.

## Acceptance policy

- Run HTB and Linux labs locally with real services and unstable networking.
- Record evidence success, hypothesis coverage, terminal correctness, blocked reasons,
  wall time, cost and safety violations locally.
- Do not publish live target addresses, flags, credentials, operation ids, payloads,
  screenshots or exploitation writeups.
- Full production readiness is claimed only after repeated private acceptance runs.
- ThinkRail UI and model fallback are out of scope.

<p align="center">
  <img src="assets/samurai-logo.svg" width="760" alt="Samurai AI logo">
</p>

<p align="center">
  <a href="README.md">Русский</a> · <strong>English</strong>
</p>

# Samurai AI

Samurai AI is a local-first TUI harness for authorized Red Team operations, Bug Bounty research, CTF/HTB, and Operation Planning. The product is built around a governed operation rather than an unrestricted chat connected to tools.

## Principles

- **Scope before action** — boundaries and rules come before execution.
- **Workflow before autonomy** — autonomy exists only inside an understandable process.
- **Evidence before finding** — a scanner result is not yet a vulnerability.
- **Operator stays in control** — the operator can review, stop, and recover work.
- **Policy outside the model** — scope, approvals, and transitions are never delegated to an LLM.

## Current status

**Stage 0**, **Stage 1/1b**, **Stage 2a/2b**, **Stage 3**, **Stage 4 in full — the Bug Bounty, CTF/HTB, Red Team, and Operation Planning playbooks**, and **Stage 5a/5b — parallel specialized agents, campaign memory, and custom playbooks/skills** are complete.

Implemented:

- four operation modes and a validated lifecycle;
- include/exclude scope for domains, wildcards, URL origins, and IP/CIDR;
- plans, tasks, dependencies, stage gates, and artifact requirements;
- R0–R4 approvals and approval-gated tool execution;
- immutable evidence artifacts with SHA-256 and redaction;
- a findings workflow from candidate to reported;
- snapshots/restore/fork, recovery reconcile, and plan/scope diff;
- a versioned tool registry, shell-free `process.exec`, and `http.fetch` through the scope broker;
- Docker sandbox profiles with a signed allowlist, scoped egress proxy with DNS pinning;
- an MCP stdio/HTTP client and a persistent secrets vault (AES-256-GCM);
- prompt-injection warnings and an `untrusted-external` trust label;
- LLM provider abstraction (openai-compatible, anthropic, gemini, ollama),
  a ModelRouter with retry/fallback, per-operation budgets, and a
  Planner/Executor/Parser/Verifier orchestrator with a closed command schema;
- the Bug Bounty playbook: program rules, a scope-enforced asset ledger with
  diff, fingerprint-based finding dedup, and governed gates;
- reports (Markdown/JSON) built only from validated findings, an evidence
  package with a SHA-256 manifest, secret redaction, and out-of-scope blocking;
- the CTF/HTB playbook: BRD challenge categories, a hypothesis log with a
  state machine, an immutable flag ledger with regex format validation and
  proof artifacts, a journal-derived replay, and redacted Markdown/JSON
  writeups with export;
- the Red Team playbook: an ROE with a blocking approval before execution,
  an ATT&CK-mapped attack path with dependencies, detection expectations and
  outcomes, stop checkpoints, and a debrief with open gaps, redaction, and
  SHA-256 export;
- the Operation Planning playbook: scenarios without active actions, test
  cases with ATT&CK/kill chain mapping and detection expectations, stop
  points, and a runbook (Markdown/JSON) generated in planning status with
  redaction and export;
- parallel specialized agents (swarm): a bounded thread pool over
  independent plan tasks, general/recon/testing/analysis specializations
  (prompt-level only; governance is identical), ready-task auto-selection,
  model approval preflight, a wall-time vs sequential-estimate report with
  speedup, SQLite v13, the `swarm ready/run/list/show` CLI and TUI `/swarm`;
- campaign memory: workspace-scoped entries (methodology/decision/failure/note)
  with retention and deletion, read-only for agents (a bounded redacted
  digest in prompts, audited in agent.run.start), the `memory` CLI and
  TUI `/memory`;
- custom playbooks (JSON templates with structural validation, immutable
  storage, `playbook install/use/list-custom`) and trusted versioned skills
  (`--trusted` required, sha256 recorded, bounded injection into executor
  prompts), TUI `/playbooks` and `/skills`, SQLite v14;
- RU/EN first-run language selection and `/lang` switching;
- SQLite migrations v1–v14, append-only events, and optimistic concurrency;
- CLI and TUI with real kernel/recovery/tool/agent/playbook views;
- 506 unit/integration/CLI/TUI tests.

Not implemented yet: CALDERA/VECTR integrations, headless mode, JSON events,
CI/CD, and HTML/PDF reports (Stage 5c+), the extended security tool set
(pending a threat review), LLM streaming, full egress enforcement on
Docker Desktop for Mac, and the standalone binary.

## Development quick start

Requirements: Python 3.12+ and uv.

```bash
git clone https://github.com/0nlyr00t/Samurai-AI.git
cd Samurai-AI
uv sync --locked
uv run pytest
```

Create an operation and open the TUI:

```bash
uv run samurai operation create "Lab" \
  --mode ctf-htb \
  --objective "Solve the authorized lab" \
  --include 192.0.2.10

uv run samurai tui
```

Quality checks:

```bash
uv run ruff check .
uv run mypy src
uv build
```

## Target distribution

The final product is intended to ship as a standalone per-platform `samurai` executable/installer without requiring Python or uv. Docker, security tools, MCP servers, and Ollama will remain optional dependencies detected through health checks.

## Models

The LLM orchestration phase will introduce an extensible model provider registry with capability negotiation: OpenAI, Anthropic, Google Gemini, Azure OpenAI, AWS Bedrock, DeepSeek, xAI, Qwen, Moonshot/Kimi, Mistral, OpenRouter, OpenAI-compatible endpoints, and first-class Ollama.

Playbooks must not depend on vendor-specific APIs.

## Localization

The product must offer an **RU/EN** language choice on first launch, persist the selection, and allow switching later. Every new operator-facing string is added to Russian and English resources together.

## Documentation

- `md/requirements.md` — business requirements.
- `md/development-plan.md` — roadmap, phases, and acceptance criteria.
- `Agent.md` — development handoff.
- `README.md` — Russian README.

## Authorized use only

Samurai AI is intended only for authorized security research, CTF/HTB, Bug Bounty work within program rules, and Red Team/adversary emulation with explicit permission from system owners. Do not use the product outside an agreed scope.

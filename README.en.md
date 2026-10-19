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

**Stage 0 — Walking Skeleton**, **Stage 1 core — Operation Kernel**, and **Stage 1b — Recovery Hardening** are complete.

Implemented:

- four operation modes and a validated lifecycle;
- include/exclude scope for domains, wildcards, URL origins, and IP/CIDR;
- plans, tasks, dependencies, stage gates, and artifact requirements;
- R0–R4 approvals;
- immutable evidence artifacts with SHA-256;
- a findings workflow from candidate to reported;
- snapshots/restore/fork, recovery reconcile, and plan/scope diff;
- RU/EN first-run language selection and `/lang` switching;
- SQLite migrations v1–v3, append-only events, and optimistic concurrency;
- CLI and TUI with real kernel/recovery views;
- 76 unit/integration/CLI/TUI tests.

Not implemented yet: LLM orchestration, MCP, external tool execution, and the standalone binary.

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

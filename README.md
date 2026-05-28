<p align="center">
  <img src="docs/assets/logo.png" alt="Symposium logo" width="160">
</p>

<h1 align="center">Symposium</h1>

<p align="center">
  <em>A panel of AI experts that actually argue — structured, sequential, and replayable to the byte.</em>
</p>

<p align="center">
  <a href="docs/specification.md"><img alt="Spec" src="https://img.shields.io/badge/spec-1.0-1a365d?style=flat-square"></a>
  <a href="docs/schemas/v1.0.0/"><img alt="Schemas" src="https://img.shields.io/badge/JSON%20Schema-v1.0.0-d4a017?style=flat-square"></a>
  <a href="https://pypi.org/project/symposium-protocol/"><img alt="PyPI" src="https://img.shields.io/pypi/v/symposium-protocol?style=flat-square&color=3776ab"></a>
  <a href="https://github.com/terrordrummer/symposium/actions/workflows/validate.yml"><img alt="CI" src="https://img.shields.io/github/actions/workflow/status/terrordrummer/symposium/validate.yml?branch=main&label=ci&style=flat-square"></a>
  <a href="LICENSE"><img alt="License" src="https://img.shields.io/badge/license-Apache%202.0-333333?style=flat-square"></a>
</p>

---

Symposium runs a small panel of AI personas through a **structured debate** — each takes a turn, a neutral coordinator steers, and you get back **one synthesized answer**. Every session is saved as a tamper-evident artifact you can **replay byte-for-byte**.

It's not another free-form "group chat" framework. Symposium enforces exactly **one** conversation shape — fixed panel, one turn per agent per round, a coordinator that recommends but never decides when to stop. You trade flexibility for something most multi-agent stacks can't give you: **reproducibility**.

```text
        critic ── logician
       /                  \
  engineer    (coordinator)   visionary
       \                  /
        researcher ───────
   → one debate, one synthesized answer, one replayable artifact
```

## Why Symposium

- 🔑 **No API key needed.** By default it drives your local `claude` / `codex` CLI login — nothing metered, nothing to configure.
- 🧠 **A real panel.** Five complementary personas (logician, visionary, researcher, critic, engineer) plus a separate coordinator. Add your own on the fly.
- 🔁 **Byte-identical replay.** Every run is a SHA-256-sealed artifact; re-render it or re-execute it and prove it didn't change.
- 📺 **Watch it live.** `symposium watch` opens a browser viewer — personas on a circle, a glow on the speaker, arrows for every direct question.
- 🧩 **Built for Claude Code.** Ships an MCP server: kick off a deliberation and stream the debate straight into your client.
- 📜 **A spec, not just code.** The protocol is language-independent; this Python package is one reference implementation.

## Install

```bash
pip install symposium-protocol            # import symposium
pip install "symposium-protocol[mcp]"     # + the Claude Code / Desktop MCP server
```

Python 3.11+.

## Quick start

**In Claude Code (the easy path — no key).** Register the server, then just ask:

```bash
claude mcp add symposium -- symposium-mcp
```

Then just ask Claude in plain language:

```text
> Use symposium to debate whether we should migrate the monolith to microservices.
```

The panel debates over your local `claude` / `codex` login and streams each turn back live. That's it.

**From the terminal (no key, no network).** Run the deterministic demo, then replay it:

```bash
symposium run \
  --config examples/configs/walking-skeleton.yaml \
  --script examples/scripts/walking-skeleton.json \
  --output runs/ examples/problem.md

symposium replay runs/demo-walking-skeleton-001     # byte-identity check
symposium watch  runs/demo-walking-skeleton-001     # ↑ watch it in the browser
```

## Use it in Claude Code

Once the server is registered, you don't type any special syntax — just **ask Claude in plain language** and it calls the right tool. The default routes each persona to a local CLI (the creative **visionary** to `codex`, the rest to `claude`) and falls back to whichever you have installed. **No API key.**

```text
> Use symposium to debate whether we should migrate the monolith to microservices.

> Run a symposium on our caching strategy, and pull in experts on
  GDPR compliance and Postgres internals if the panel needs them.

> Have symposium deliberate this RFC, but run the whole panel on claude.

> Summarize the last symposium run in runs/.
```

Claude maps these to `deliberate` / `deliberate_adaptive` / `get_run_summary` and streams the debate back turn by turn. Under the hood:

| Tool | Does |
|---|---|
| `deliberate` | Run a debate, stream every turn live. The default. |
| `deliberate_adaptive` | Same, but generates new expert personas on demand (early-start + mid-run). |
| `*_muted` variants | Same result, no live streaming — just the final answer. |
| `get_run_status` | Poll a still-running debate for new turns. |
| `get_run_summary` | Outcome + metrics + replay check for a finished run. |
| `list_personas` / `generate_persona` | Inspect the built-in panel / design a new expert. |
| `get_version` | What's actually running (handy after a reinstall). |

> **Billing.** Logged in with a Pro/Max or ChatGPT subscription? CLI turns spend **subscription quota and rate limits**, not metered API dollars. The real ceiling under `cli-auto` is `max_wallclock_seconds` (default 1h) and your plan's rate limit — the token/cost caps are telemetry, not a hard budget. Only the `anthropic` / `openai` HTTP adapters bill per token.

## API keys: what's used, what isn't

| Provider | API key |
|---|---|
| `cli-auto` (default), `claude-cli`, `codex-cli` | **None** — reuses your local CLI login |
| `fake` | **None** — offline & deterministic |
| `anthropic` | `ANTHROPIC_API_KEY` *(optional `ANTHROPIC_BASE_URL`)* |
| `openai` | `OPENAI_API_KEY` *(optional `OPENAI_BASE_URL`)* |

`ANTHROPIC_API_KEY` / `OPENAI_API_KEY` are read **only** when you explicitly pick the metered `anthropic` / `openai` HTTP adapter. Every default path ignores them — and the CLI adapters actively scrub cross-vendor credentials before each spawn.

## CLI commands

```bash
symposium run                 # run a session against any provider
symposium watch               # live browser viewer (follows the newest run)
symposium replay              # re-render a stored transcript; verify the digest
symposium execution-replay    # re-run under the 10 pinning conditions; prove reproducibility
symposium validate            # check an artifact against the v1.0.0 JSON Schemas
symposium metrics             # tokens, cost, latency, branch depth — computed offline
```

To run real models from the CLI without a key, set `provider: claude-cli` (or `codex-cli`) on each agent in your config and drop the vendor key — the adapters are registered out of the box.

## Use it as a library

```python
from symposium.providers import default_registry
from symposium.scheduler import run_session

providers = default_registry().build_session_providers(config)
artifact = run_session(config, providers, runs_root="runs/")

print(artifact.outcome.kind)          # "synthesis" or "termination"
print(artifact.transcript_digest)     # 64-hex SHA-256 over the canonical transcript
```

## How it's different

| | Symposium |
|---|---|
| **Topology** | One fixed shape — no arbitrary handoffs or nested supervisors. |
| **Routing** | Schema-validated `direct_request` only; inline `@mentions` are never routing (injection-resistant). |
| **Who decides to stop** | Deterministic runtime code — never the LLM coordinator. |
| **Replay** | Unconditional transcript replay *and* conditional re-execution, both spelled out. |
| **Failure modes** | Closed, enumerated — not "whatever the framework happened to do". |

The full rationale, and a comparison against AutoGen / CrewAI / LangGraph / OpenAI Agents SDK, lives in [the specification](docs/specification.md).

## Project layout

- **[`docs/specification.md`](docs/specification.md)** — the normative protocol (language-independent) + 16 JSON Schemas under [`docs/schemas/v1.0.0/`](docs/schemas/v1.0.0/).
- **`symposium/`** — the reference Python runtime (scheduler, providers, replay, viewer, MCP server, CLI).
- **`examples/`**, **`tests/`** — runnable configs and the conformance suite.

A conformant runtime in any language is just as valid as this one, as long as it matches the spec and validates against the schemas.

## Contributing & status

The **v1.0 spec is frozen** (ratified 2026-05-26); the runtime ships as `symposium-protocol` on PyPI. Issues, errata, and PRs welcome — see [CONTRIBUTING.md](CONTRIBUTING.md) and [ROADMAP.md](ROADMAP.md).

```bash
pip install -e ".[test]" && pytest -q     # run the suite
```

## License

[Apache 2.0](LICENSE).

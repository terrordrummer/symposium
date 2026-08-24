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
- 📺 **Watch it live.** `symposium watch` opens a video-call-style viewer with synthetic photographic identities, participant presence states, optional local neural narration, and arrows for direct questions.
- 🖱️ **Launch without Terminal.** On macOS, double-click `Symposium.app`; a singleton local backend opens the complete workspace in your browser.
- 🧩 **Built for Claude Code.** Ships an MCP server: kick off a deliberation and stream the debate straight into your client.
- 📜 **A spec, not just code.** The protocol is language-independent; this Python package is one reference implementation.

## Toward Symposium 2.0

The v1 protocol remains the deterministic, replayable deliberation engine.
The 2.0 product direction adds a workspace around it: persistent rooms,
globally reusable agents, invitations, onboarding, listening-only presence,
guest interventions, optional local voice, and static photographic presence.

The current vertical slices are visible through `symposium watch`:

- the five built-in panel members and Sartori appear as explicitly synthetic
  photographic identities in a meeting grid;
- persisted transcript events drive listening, thinking, speaking, and
  just-finished states without modifying the frozen v1 artifacts;
- all visual identities resolve locally: built-ins and new agents use packaged
  portraits, while unknown legacy run personas retain an initials fallback;
- the active speaker remains fully illuminated with a strong border while all
  other participants are attenuated, without moving or animating the faces;
- new agents receive one unused portrait from a 50-image pool; unknown legacy
  personas receive a deterministic initials card, requiring no paid service.

The photographs are static assets: no lip sync, idle motion, remote rendering,
or paid avatar provider is used. Room-bound execution and avatar selection are
available from the browser; microphone input remains planned work. See
[the Symposium 2.0 design](docs/symposium-2.0.md) and
[avatar asset provenance](docs/avatar-assets.md).

For a finished run, enable **voce locale** if desired and press **Riproduci**. Replay is
manual by default: one complete turn is presented at a time and Symposium waits
for **Avanti**; with voice enabled, the button unlocks only after narration has
finished. Automatic 0.5×, 1×, and 2× modes use either the actual speech end or a
reading-time estimate based on word count. Normal opening stays instantaneous
and live runs are never delayed. Static active-speaker focus works with or
without **voce locale**; optional Parler-TTS speech runs locally, uses no API
key, and caches generated audio. Its one-time model download is about 3.8 GB.

New agents receive one of 50 packaged photorealistic fictional portraits. The
Sartori panel previews the proposed unused identity and allows rerolling before
creation. The face assignment and corresponding Italian feminine or masculine
voice profile are persisted together.

### Rooms and Sartori

The local 2.x control plane is separate from immutable v1 run artifacts. It
persists to `.symposium/control-plane.json` using atomic replacement and keeps
an ordered event trail for room, agent, invitation, presence, and dismissal
changes. Because agent instructions and onboarding context may be private, the
default `.symposium/` directory is ignored by Git.

The browser is the primary control surface. Open **Sartori** in the top bar to:

- write commands such as “crea una stanza Prodotto per decidere la roadmap”;
- enter or archive rooms;
- create a reusable agent with instructions and capabilities; and
- invite or dismiss agents from the active call.

Use **Parla alla stanza** below the conversation to ask the current room a
question. Symposium takes a snapshot of the speaking members, gives each agent
only its own instructions and room onboarding context, starts a new immutable
v1 session in the background, and follows it live in the same page. The default
execution policy uses the locally installed and authenticated `claude` /
`codex` CLI, never a metered HTTP API key. If neither CLI is available, the
browser reports that immediately and no run is recorded.

Submitting the question is the only action required: the full deliberation loop
starts automatically. A persistent activity card names the agent currently
elaborating, shows elapsed time, explains that local models may take several
minutes, and distinguishes preparation, response, completion, and technical
failure. During a live turn, workspace refreshes cannot overwrite “sta
pensando” with the agent's passive room presence. Provider/schema failures are
reported as failed sessions with their actionable upstream diagnostic instead
of appearing as successful or indefinitely live runs.

Claude CLI deliberation turns run with safe mode, no built-in tools, no MCP
servers, a compact deliberation-only system prompt, and no Claude session
persistence. This preserves subscription OAuth authentication while preventing
a room persona from inspecting or modifying the project checkout and avoids
loading the coding-agent context for each speaker. Private persona material is
sent through stdin rather than exposed in process arguments. Symposium still
records its own immutable run artifact.

The default workspace initializes automatically. Changes appear immediately in
the meeting grid and a dismissed guest disappears while its visit remains in
the event trail. The `symposium workspace`, `room`, and `agent` CLI commands and
equivalent `sartori_*` MCP tools remain available for automation and debugging;
they are not required for normal room management. Requested, completed, and
failed room sessions are linked to their v1 run IDs in the room event trail.
Existing run artifacts are never rewritten.

## Install

```bash
pip install symposium-protocol            # import symposium
pip install "symposium-protocol[mcp]"     # + the Claude Code / Desktop MCP server
```

Python 3.11+.

## Quick start

**From the macOS launcher (the product path).** Double-click `Symposium.app`.
It opens the workspace in your browser without showing a terminal window. From
there, rooms, agents, invitations, questions, live discussions, replay, and
shutdown are all controlled through the interface. Double-clicking the app
again reopens the existing workspace instead of starting a second backend.

The generated app is machine-local because it records the Python environment
that contains Symposium and the PATH of the installed Claude/Codex CLIs. Its
small native AppKit supervisor handles macOS reopen events, starts the Python
backend as a child, and exits with it. For a new checkout or Python environment,
a developer can regenerate it once with:

```bash
symposium-launcher --install-app --project-root /path/to/symposium
```

The app serves only loopback, displays startup failures as a macOS alert, uses
Sartori's packaged synthetic portrait as its icon, and removes its PID/state
files on clean shutdown. **Chiudi Symposium** appears in Sartori's panel only
when the workspace was opened through the launcher, and refuses to stop while
a discussion is active.

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

symposium replay runs/demo-walking-skeleton-001            # byte-identity check
symposium watch --run runs/demo-walking-skeleton-001       # ↑ watch it in the browser
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

### Slash commands (optional plugin)

Plain language is enough. If you'd rather be explicit — and skip the model
deciding anything about your prompt — this repo also ships the commands as a
Claude Code plugin:

```
/plugin marketplace add terrordrummer/symposium
/plugin install symposium@symposium
```

| Command | Does |
|---|---|
| `/symposium_deliberate` | Adaptive panel, streamed live. The default. |
| `/symposium_deliberate --silent` | Same, no streaming — one result at the end. |
| `/symposium_deliberate --strict` | Fixed panel, no personas generated at runtime. |
| `/symposium_deliberate --strict --silent` | Both. |
| `/symposium_deliberate_live` | Adaptive, plus the video-call-style browser viewer. |

> **0.2.0.** `--silent` and `--strict` used to be separate commands
> (`/symposium_deliberate_silent`, `_strict`, `_strict_silent`). Five menu
> entries for two booleans is four too many, and every session had to read and
> tell them apart. They are flags now.
| `/symposium_plan_panel` | Plan the panel before launching: who's relevant, who's missing. |
| `/symposium_reshape_panel` | Audit a panel for overlap and redundancy. |
| `/symposium_generate_persona` | Design one expert for a capability gap. |
| `/symposium_list_personas` | The six built-in personas. |
| `/symposium_run_summary` | Outcome, digest, replay check and metrics for a run. |

The commands dispatch the MCP call verbatim, with no pre-processing of your
prompt. They need the MCP server registered as above.

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
symposium-launcher             # launcher backend (normally started by Symposium.app)
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
- **`plugins/symposium/`** — the Claude Code plugin: slash commands that drive the MCP server, versioned with the protocol they speak.

A conformant runtime in any language is just as valid as this one, as long as it matches the spec and validates against the schemas.

## Contributing & status

The **v1.0 spec is frozen** (ratified 2026-05-26); the runtime ships as `symposium-protocol` on PyPI. Issues, errata, and PRs welcome — see [CONTRIBUTING.md](CONTRIBUTING.md) and [ROADMAP.md](ROADMAP.md).

```bash
pip install -e ".[test]" && pytest        # run the suite
```

## License

[Apache 2.0](LICENSE).

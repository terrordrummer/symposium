# Roadmap

This is a thin front-of-repo pointer. The **authoritative, normative
roadmap** is [`docs/specification.md` §12](docs/specification.md) — its
§12.2 aggregation table scopes every non-MVP item with a target window
(`v1`, `v1+`, `Roadmap`) and its ADR / Pass-1 anchor. Vision items not
yet promoted to the roadmap live in spec §13.

## Current phase

**Phase 1 — reference MVP shipped (v1.5.0).** The spec is frozen at
v1.0.0; the reference Python runtime implements the Core MVP MUST-set
(§1–§9):

- ✅ Transcript system + RFC-8785 JCS `transcript_digest` (§7.7), atomic
  run-directory persistence (§7.1–§7.4)
- ✅ `orchestrator_runtime` main loop (§4.11): rounds, verdicts, branches,
  deferred queue, hard caps, agent-failure policy
- ✅ MVP default panel (R3): logician / visionary / researcher / critic /
  engineer + coordinator
- ✅ Provider adapters: deterministic `FakeProvider`, OpenAI-shaped (§6.12),
  Anthropic-shaped (§6.13), terminal `ClaudeCliProvider` (v1.7.0) +
  `CodexCliProvider` (v1.8.0) — drive the local `claude` / `codex` CLIs with
  no API key — plus per-persona CLI routing with installed-CLI fallback
  (`cli-auto`, v1.8.0), in-process registry (§6.11)
- ✅ `transcript_replay` (§7.5) + `execution_replay` under the ten §7.6
  pinning conditions
- ✅ §7.9 MVP observability metric set (offline, derived from the Artifact)
- ✅ Selector (§4.1 / §5.11): `fixed`, `rules`, `llm` strategies
- ✅ CLI (§11.2): `run` / `replay` / `validate` / `metrics` /
  `execution-replay`

## What's next (per spec §12)

Selected items from the §12.2 table — see the spec for the complete,
authoritative list with target windows:

- **v1** — §7.10 v1 observability additions (`role_purity_score`,
  `disagreement_frequency`, `interaction_graph`, `delegation_frequency`,
  `time_to_finalize`); `summarize_context` / `replace_agent` recovery
  actions; plugin-style adapter discovery (entry points); `symposium eval`
  CLI; richer selector output fields; `fallback_model` +
  `Message.provider_used` / `model_used`.
- **v1+** — interactive event-stream execution mode + `observability_event`
  live stream; async job API; dynamic participant introduction
  (panel mutation); `EnsembleMode` (parallel first-pass perspectives);
  capability-based tool allowlists; external-loop adapter pattern.
- **Roadmap** — HTML replay viewer; TTS narration; transcript
  visualization (timeline / branch overlays); persona registry
  (versioning, lifecycle, signing, marketplace); benchmarking suite +
  curated problem sets; IDE plugin; HTTP/RPC service host pattern; voting /
  weighted-confidence convergence; bundled research adapters.

## Host integration (examples, not runtime concerns)

Wrapping the runtime as an **MCP server** or a Claude Code / IDE skill is a
host-integration pattern (spec §11.4 / §11.5), built on the stable
`run_session(config, providers) -> Artifact` API without modifying the
runtime. These are downstream of the protocol, not part of it.

- ✅ **MCP server (`symposium-mcp`, v1.6.0)** — `symposium/integrations/`
  exposes the runtime as MCP tools (`deliberate`, `deliberate_streaming`,
  `get_run_summary`, `list_personas`) so a Claude client can launch a
  deliberation — optionally **streaming each turn live** as the panel
  produces it — and read its result, replay status, and metrics. Optional
  `[mcp]` extra; the core install and CLI are unchanged. See the README
  "Use in Claude Code" section. This realizes the §12 "HTTP / RPC service
  host pattern" / IDE-integration line as a pure consumer of the public API.
- ✅ **Terminal-CLI providers + routing (`claude-cli` v1.7.0, `codex-cli`
  + `cli-auto` v1.8.0)** — `ProviderAdapter`s that drive deliberation turns
  through the locally-installed `claude` / `codex` CLIs instead of the HTTP
  API, reusing each CLI's existing login so **no API key** is needed. The
  `cli-auto` host policy routes per persona (visionary → codex; technical
  personas → claude) with installed-CLI fallback, and is the default for
  the MCP `deliberate` tools. Pure registry / host extensions — no spec /
  schema / runtime changes.

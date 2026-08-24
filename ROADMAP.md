# Roadmap

This is a thin front-of-repo pointer. The **authoritative, normative
roadmap** is [`docs/specification.md` §12](docs/specification.md) — its
§12.2 aggregation table scopes every non-MVP item with a target window
(`v1`, `v1+`, `Roadmap`) and its ADR / Pass-1 anchor. Vision items not
yet promoted to the roadmap live in spec §13.

## Current phase

**Reference runtime shipped and iterating (v1.12.0).** The spec is frozen
at v1.0.0; the reference Python runtime
implements the Core MVP MUST-set (§1–§9) and has since grown a
streaming-by-default MCP surface, adaptive deliberation (on-demand
persona generation), and a live browser viewer:

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
- ✅ Selector (§4.1 / §5.11): `fixed`, `rules`, `llm` strategies,
  including the optional §5.11 output fields (`excluded_agents`,
  `missing_capabilities`, `reasoning`), populated by the `rules` /
  `llm` strategies
- ✅ CLI: `run` / `watch` / `replay` / `validate` / `metrics` /
  `execution-replay`
- ✅ Live viewer (`symposium watch`, introduced in v1.11.0) — a read-only
  browser page that tails `transcript.jsonl` over SSE. The current 2.0
  vertical slice presents the built-in panel as a synthetic photographic
  video-call grid, derives participant presence from real transcript events,
  retains static directed-request arrows, and works for both live and finished
  runs. The active speaker is highlighted while the other portraits are
  attenuated; no visual element requires a paid provider.

**Known deviations from the §11.2 MVP CLI contract**: `--config` is
required (the spec makes it optional over an implementation default);
the `--max-rounds` / `--provider` overrides are not implemented; and
`replay` / `execution-replay` take a run-dir path rather than a bare
session id.

## Symposium 2.0 product track

This product track is layered around the frozen v1 protocol rather than
folded into its replay schemas. The detailed architecture and acceptance
criteria live in [`docs/symposium-2.0.md`](docs/symposium-2.0.md).

1. **Zero-cost static presence** — synthetic photographic stills for built-in
   agents, deterministic initials/color cards for every unregistered agent,
   static active-speaker focus, and no provider credentials or metered calls.
   Shipped in the current viewer slice.
2. **Optional local voice** — browser-native narration, speaking-state timing,
   and interruption without a remote TTS dependency. Higher-quality local
   adapters may be added later only if they keep the zero-cost default intact.
3. **Room control plane** — persistent `Workspace`, `Room`, `Agent`, and
   `RoomMembership` records; invite, onboard, listen, speak, and leave events;
   Sartori as the user-facing coordinator. The local atomic store, CLI/MCP
   commands, same-origin browser controls, and live viewer projection are
   shipped. The browser composer now binds the speaking room members to a new
   immutable v1 run and follows it live. The composer now also owns the whole
   automatic loop UX: current agent, elapsed time, waiting expectation, and an
   explicit terminal error or completion state.
4. **Guest briefing flow** — invite a project owner such as the Zeus Focus
   Talking lead, let it brief the room, then release it without losing the
   room transcript or context boundary. The initial single-session flow and
   requested/completed/failed audit trail are shipped.
5. **Operational hardening** — permissions, quotas, synthetic-identity
   disclosure, audit trail, and accessible static fallbacks.

The current vertical slices complete the local identity catalog, static
presence projection, manual/word-aware playback, zero-cost presentation policy,
persistent room/membership foundation, and first room-to-run guest briefing.
The macOS launcher now removes the remaining one-time terminal start and gives
the UI ownership of clean shutdown. The next product slice adds continuity
across room sessions plus browser interruption and recovery.

## What's next (per spec §12)

Selected items from the §12.2 table — see the spec for the complete,
authoritative list with target windows:

- **v1** — §7.10 v1 observability additions (`role_purity_score`,
  `disagreement_frequency`, `interaction_graph`, `delegation_frequency`,
  `time_to_finalize`); `summarize_context` / `replace_agent` recovery
  actions; plugin-style adapter discovery (entry points); `symposium eval`
  CLI; `fallback_model` + `Message.provider_used` / `model_used`.
  (The "richer selector output fields" item shipped — see the
  selector bullet above.)
- **v1+** — interactive event-stream execution mode + `observability_event`
  live stream; async job API; dynamic participant introduction
  (panel mutation); `EnsembleMode` (parallel first-pass perspectives);
  capability-based tool allowlists; external-loop adapter pattern.
- **Roadmap** — optional local/browser narration; persona registry (versioning,
  lifecycle, signing, marketplace); benchmarking suite + curated
  problem sets; IDE plugin; HTTP/RPC service host pattern; voting /
  weighted-confidence convergence; bundled research adapters. (The
  HTML replay viewer / transcript-visualization line shipped as the
  v1.11.0 live viewer — see the current-phase list above.)

## Host integration (examples, not runtime concerns)

Wrapping the runtime as an **MCP server** or a Claude Code / IDE skill is a
host-integration pattern (spec §11.4 / §11.5), built on the stable
`run_session(config, providers) -> Artifact` API without modifying the
runtime. These are downstream of the protocol, not part of it.

- ✅ **MCP server (`symposium-mcp`, v1.6.0)** — `symposium/integrations/`
  exposes the runtime as MCP tools — today nine: `deliberate`,
  `deliberate_muted`, `deliberate_adaptive`, `deliberate_adaptive_muted`,
  `get_run_status`, `get_run_summary`, `list_personas`,
  `generate_persona`, `get_version` (streaming is the default since
  v1.10.12; the old `deliberate_streaming` name is gone) — so a Claude
  client can launch a deliberation — **streaming each turn live** as
  the panel produces it, unless a `*_muted` variant is chosen — and
  read its result, replay status, and metrics. Optional
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
- ✅ **Dynamic agent generation (v1.8.0)** — `generate_persona` designs a
  new domain-expert `Persona` from a capability need (CLI output
  constrained to the `Persona` schema, then validated), and
  `deliberate_adaptive` grows the panel two ways: *early-start* (generate
  experts before the first session) and *runtime* (on a
  `user_input_required` / `external_research_required` termination,
  generate the needed expert and continue in a linked session with the
  augmented panel). Host-orchestrated over the frozen runtime — it
  realizes the §12 "dynamic participant introduction" / persona-creation
  intent **without** in-loop panel mutation or any spec / schema change
  (true in-loop mutation remains a v1+/Roadmap runtime concern).

## Open follow-ups (post v1.12.0 review)

- **Split the two oversized modules.** `symposium/scheduler/loop.py`
  (~1,600 lines) and `symposium/integrations/mcp_server.py` (~2,000
  lines) have each grown well past comfortable review size. Both
  should be factored into smaller units — the loop into its phases
  (round scheduling, verdict handling, branch/deferred mechanics,
  failure policy), the MCP server into tool registration, streaming
  plumbing, and the adaptive-deliberation orchestration — with no
  behavior change. Ordinary refactoring debt; no spec / schema
  impact.
- **Establish a static-type gate.** The codebase is type-annotated but CI
  currently enforces runtime tests and Ruff only; an ad-hoc Pyright pass
  still reports annotation debt, concentrated in dynamic MCP event payloads,
  Pydantic discriminated unions, and provider Literal normalization. Define
  the supported checker/configuration, clean that baseline, then add it to CI
  so type drift becomes a reviewed failure rather than an informal signal.
- **Forensic per-vendor usage detail.** The canonical `Usage`
  (`prompt_tokens` / `completion_tokens` / `total_tokens` / `cost_usd`)
  is provider-uniform per §6.5, which means provider-specific
  observability — Claude's `cache_read_input_tokens` /
  `cache_creation_input_tokens` split, codex's `reasoning_output_tokens`,
  and the upstream `raw` payload — is summarized away by the adapters
  before reaching the transcript or any persisted artifact. Operators
  wanting to debug cache hit rates or reasoning-token spikes have no
  in-band signal today; only the in-process `ProviderResult.raw` dict
  carries it and it's not serialized. Codex review T1 #4 flagged this
  as a real follow-up. Likely shape: an optional
  `provider_raw_usage: Dict[str, Any]` on `Usage` (additive, optional →
  schema-compatible) populated by each adapter from its native usage
  block, surfaced in `get_run_summary` and in the streaming events.
- **Custom MCP profile for CLI personas.** v1.10.4 set
  `disable_mcps=True` as the cli-auto default to close the recursive-MCP
  hang. Operators who legitimately want a domain-knowledge MCP available
  inside a persona's reasoning currently must construct
  `ClaudeCliProvider(disable_mcps=False, ...)` themselves and route it
  manually — no MCP-tool kwarg exposes `mcp_config` through the
  `deliberate*` surfaces. Open design question (Codex review T1 #7):
  whitelist of registered MCPs vs. inline JSON payload vs. path to a
  config file; pick one once a real use case shows up.

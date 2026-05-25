# Symposium — Specification

> **Status**: 1.0 — Specification frozen 2026-05-26. Joint
> Claude+Codex sign-off at Pass 10. JSON Schemas under
> [`docs/schemas/v1.0.0/`](schemas/v1.0.0/). Repository conventions
> in [`docs/repository-strategy.md`](repository-strategy.md).
> Versioning policy: §5.1.
>
> Each section carries a scope tag:
> - **[Core MVP]** — required in the first release. Uses RFC 2119 keywords.
> - **[v1]** — required in the v1 release.
> - **[Roadmap]** — explicitly planned future work. No normative language.
> - **[Vision]** — long-term aspiration. Non-binding.

---

## 1. Status, Scope, Normative Language

**[Core MVP]**

Symposium is an opinionated protocol for structured, sequential,
adversarial multi-agent deliberation. It is **not** a general-purpose
agent framework. This document specifies what an implementation MUST do
to be a conformant Symposium runtime.

Normative keywords (MUST, MUST NOT, SHOULD, SHOULD NOT, MAY, REQUIRED,
RECOMMENDED, OPTIONAL) follow [RFC 2119](https://www.rfc-editor.org/rfc/rfc2119)
and [RFC 8174](https://www.rfc-editor.org/rfc/rfc8174) when in capitals.

The repository is hosted at `github.com/<owner>/symposium`. License: Apache 2.0.
Reference-implementation conventions (repo layout, install,
contribution model) live in
[`docs/repository-strategy.md`](repository-strategy.md); see §14.

**Conformance.** A "conformant Symposium runtime" is an
implementation that satisfies every MUST / MUST NOT in §1–§9
(the **[Core MVP]** surface) and validates against the v1.0.0
JSON schemas under `docs/schemas/v1.0.0/`. Sections tagged
**[v1]**, **[Roadmap]**, and **[Vision]** are descriptive of
future work and impose no conformance obligations on an MVP
runtime; §10 (Competitive Positioning) and §11.4–§11.5 (host
patterns beyond CLI / library API) are likewise descriptive.
The §12.2 aggregation table consolidates every non-MVP item
with its target window.

**Versioning.** The protocol versions through the JSON schemas
under `docs/schemas/v1.0.0/`. The schema-versioning policy
(semver applied to the schema bundle, additive changes vs.
breaking changes, schema registries) is normative and lives
in §5.1. This document's status banner tracks the editorial
pass count, not the protocol version.

**Reading order for implementors.** The recommended first-pass
reading order is §1 (this section) → §2 (vocabulary) → §3
(overview and non-goals) → §4 (MVP runtime protocol) → §5
(schemas) → §6 (provider and tool adapter contract) → §7
(persistence, replay, observability) → §8 (budget, failure,
security) → §9 (testing & evaluation). §10–§13 are written
for positioning, integration, and planning audiences and can
be skipped on a first pass; §14 points to the non-normative
repository-strategy companion. Target audiences are
researchers, developers, AI engineers, and reasoning
enthusiasts (Pass-1 row #65); the prose assumes familiarity
with RFC 2119 normative keywords and JSON Schema.

---

## 2. Glossary & Core Concepts

**[Core MVP]**

This section defines the canonical vocabulary used throughout the
specification. Every term used normatively elsewhere is defined here
exactly once. Definitions are descriptive (no RFC 2119 keywords);
normative behavior tied to a term lives in the section that owns the
behavior (typically §4-§8). Within each subsection, entries are
alphabetical; the subsections themselves are ordered by reading flow,
not alphabetically.

### 2.1 Session lifecycle

**branch** — A transient sub-conversation opened during a `round` when
an `agent` emits a `direct_request` targeting another agent; control
returns to the originating agent once the branch closes. A branch
contains one or more branch turns but does not constitute a round of
its own.

**coordination_turn** — The `coordinator_agent`'s contribution at the
end of a round, in which it emits a `verdict` summarizing the round
and proposing the next action; not a `primary_turn` (ADR-005, R1).

**deferred_request** — A `direct_request` that the
`orchestrator_runtime` queues instead of dispatching as an immediate
`fork`, typically because the branch-depth cap is saturated or the
scheduler policy postpones it; the runtime delivers it on a later
turn. The queue and ordering policy are runtime concerns
specified in §4.6.

**fork** — The act of opening a `branch`, triggered by a
`direct_request` and bounded by `max_branch_depth` (default 1). A fork
turn does not increment the round counter (R1).

**interrupt** — Informal synonym for a `fork`, used in prose when
the originating agent's perspective is foregrounded (its turn is
paused while the targeted agent contributes a branch turn, then
resumed). `fork` is the canonical name in schemas, APIs, and runtime
descriptions; `interrupt` exists only as a readability alternative
in prose and never names a distinct mechanism.

**primary_turn** — A single contribution by a member of the
`active_deliberation_panel` within a round; each panel member takes
exactly one primary_turn per round (ADR-005, R1). The
`coordinator_agent` does not take primary_turns.

**resume** — The transition that returns control to the originating
agent after a `branch` closes, restoring the main round flow.

**round** — One full pass over the `active_deliberation_panel` in
declared order, followed by a `coordination_turn`; a round completes
when every panel member has had its `primary_turn` and the
`coordinator_agent` has emitted its `verdict` (R1).

**session** — A single Symposium execution, from problem submission
and panel selection through deliberation rounds to finalization
(synthesis or `termination_artifact`); identified by `session_id` and
persisted under `runs/<session_id>/`.

**turn** — A single agent contribution to the `canonical_transcript`,
of three kinds: a `primary_turn` (panel member during a round), a
`coordination_turn` (Coordinator after a round), or a branch turn (an
agent responding inside a `fork`). "Turn" is exclusively the
lifecycle term; the per-message position counter in the schema is
`turn_index` (§2.4), a distinct name.

> *Boundary — `round` vs `primary_turn`*: a round is the cycle; a
> primary_turn is the unit. One round contains one primary_turn per
> panel member plus one coordination_turn.
>
> *Boundary — `fork` vs `interrupt` vs `deferred_request`*: fork is
> the runtime mechanism (opening a branch); interrupt names the
> originator-agent perspective on a fork; deferred_request names a
> direct_request the runtime postponed rather than executing in-line.

### 2.2 Roles

**active_deliberation_panel** — The ordered subset of the
`deliberation_panel` actually scheduled for the current session;
identical to `deliberation_panel` when the `selector` strategy is
`fixed` (R3), narrower if a future selector strategy excludes agents.

**agent** — A runtime participant: a configured pairing of a
`persona` with a `provider_adapter` invocation profile, invoked by
the `orchestrator_runtime` to produce one or more `turn`s. An agent
runs; a persona does not run by itself.

**coordinator_agent** — The single agent occupying the Coordinator
role: a deliberative LLM agent that performs the `coordination_turn`
after each round and emits a `verdict` (ADR-005). The
coordinator_agent is not a member of the `deliberation_panel` and
does not take `primary_turn`s; the default identifier is
`coordinator` (R3).

**deliberation_panel** — The set of agents declared by the session
configuration as deliberating participants, each of which takes one
`primary_turn` per round in declared order (ADR-005, R1). The
`coordinator_agent` is not a panel member.

**orchestrator_runtime** — The deterministic, code-based component
that owns scheduling, `canonical_transcript` ownership,
`context_packet` derivation, `fork` dispatch, budget enforcement,
retries, persistence, and termination decisions (ADR-005, ADR-002).
The orchestrator_runtime is not an LLM agent.

**persona** — A declarative profile (identifier, reasoning style,
scope fields, behavioral constraints, failure modes, optional
`output_requirements`) bound to an `agent` at configuration time. See
§2.3 for the two persona classes and their required-field sets.

**selector** — The role responsible for choosing the
`deliberation_panel` and `coordinator_agent` for a session. MVP
default is `strategy=fixed` with no LLM call (R3); an LLM-driven
selector is opt-in from v1.

> *Boundary — `deliberation_panel` vs `coordinator_agent`*: panel
> members take `primary_turn`s and produce deliberative content;
> coordinator_agent takes only `coordination_turn`s and emits a
> `verdict`. They are disjoint sets (ADR-005). Example:
> `default_deliberation_panel = [logician, visionary, researcher,
> critic, engineer]` and `coordinator_agent = coordinator` (R3) — six
> agents total, five panel members plus one Coordinator.
>
> *Boundary — `selector` vs `coordinator_agent` vs
> `orchestrator_runtime`*: the **selector** chooses *who* deliberates
> (pre-session, optional LLM, default `fixed`); the
> **coordinator_agent** *emits a semantic recommendation* in
> `verdict.next_action` after each round (LLM-emitted, no executive
> authority over the runtime); the **orchestrator_runtime**
> *executes scheduling and decides when to stop* — always-on,
> deterministic code, the sole party that schedules and terminates.
> All three are distinct roles (ADR-005, ADR-002).

**Example — Coordinator vs Runtime split (ADR-005).** A session
opens. The selector (`strategy=fixed`) returns
`panel = [logician, visionary, researcher, critic, engineer]` and
`coordinator_agent = coordinator`. The orchestrator_runtime schedules
the logician first, derives a context_packet from the
canonical_transcript, invokes the provider_adapter, appends the
provider_result to the canonical_transcript as a primary_turn
message, advances. After all five panel agents have completed one
primary_turn each, the orchestrator_runtime schedules the
coordinator_agent for its coordination_turn. The coordinator_agent
emits `verdict.next_action = continue`. The orchestrator_runtime
checks hard_caps; none exceeded; opens round 2. The coordinator
opined; the runtime executed.

### 2.3 Persona taxonomy

**behavioral_constraints** — A persona field listing the agent's
behavioral rules (e.g. cite sources when available, target criticism
at reasoning not at people); a required field for both persona
classes.

**domain_persona** — A persona class specialized to a bounded area of
expertise (e.g. legal analyst, security researcher, historian); its
`domain_scope` names the area, and `forbidden_domains` together with
`must_delegate` constrain where it speaks and to whom it hands off
(A5).

**domain_scope** — A persona field listing the domain(s) in which the
persona is competent to opine; required for `domain_persona`, not
required for `horizontal_persona`.

**failure_modes** — A persona field enumerating the persona's
expected failure patterns (e.g. over-confident speculation, scope
drift); a required field for both persona classes.

**forbidden_domains** — A persona field listing domains in which the
persona does not opine, paired with `must_delegate` entries naming
the delegation target persona; required for `domain_persona`, not
applicable to `horizontal_persona` (whose scope is cross-domain by
construction).

**horizontal_persona** — A persona class representing a cross-domain
cognitive style (e.g. logician, critic, researcher, visionary,
engineer); its `reasoning_scope` is broad, and it has no
`forbidden_domains` because it reasons across domains rather than
within one (A5).

**must_delegate** — A persona field mapping each entry in
`forbidden_domains` to the persona to address via `direct_request`
when that domain comes up; required for `domain_persona`.

**output_requirements** — A persona field declaring per-turn output
expectations (e.g. "identify assumptions", "list missing
definitions"), separate from the persona's role description and from
the prompt body (Pass 1 row #24b).

**reasoning_scope** — A persona field describing the breadth of
cognitive style the persona applies (e.g. formal-structural,
lateral-creative, evidence-based); a required field for both persona
classes. A `horizontal_persona` is valid with `reasoning_scope` alone
and no `domain_scope`.

**reasoning_style** — A persona field describing the persona's manner
of reasoning (e.g. mathematical rigor, lateral exploration,
adversarial scrutiny); a required field for both persona classes.

> *Boundary — `horizontal_persona` vs `domain_persona`*: horizontal
> personas opine across domains because they are constrained by a
> *cognitive style*; domain personas opine within one domain because
> they are constrained by *expertise boundaries*. Their required-field
> sets are not the same: horizontal requires `reasoning_scope` +
> `reasoning_style` + `behavioral_constraints` + `failure_modes`;
> domain requires the same plus `domain_scope` + `forbidden_domains`
> + `must_delegate` (ADR-005, A5, Pass 1 Q2 resolution).

### 2.4 Transcript model

**canonical_transcript** — The append-only, authoritative, persisted
log of every `message` produced during a session, owned by the
`orchestrator_runtime`; the source of truth that replays, audits,
and synthesis read from (blocker #3, ADR-005).

**context_packet** — The per-invocation view derived from the
`canonical_transcript` and passed to a `provider_adapter` for a
single agent invocation; can be compressed, filtered, or windowed
(blocker #3). The packet's composition is a runtime decision; the
canonical_transcript it derives from is invariant within the
session.

**message** — A single entry in the canonical_transcript carrying at
least `{id, speaker, target, type, content, parent_id, round,
turn_index, branch_depth, timestamp, usage}`. Primary_turns,
coordination_turns, and branch turns all produce messages; the
schema fields are identical across kinds.

**run_manifest** — A thin metadata layer at
`runs/<session_id>/manifest.json` describing the on-disk
directory's status, producer, paths, and (when present) the
canonical_transcript digest. Defined by
`run_manifest.schema.json` (§7.2). The manifest is NOT a substitute
for the Artifact; it is an index a reader consults before opening
the Artifact.

**transcript_journal** — An optional implementation-defined sidecar
(`transcript.jsonl` by convention) appended per turn during a
session for crash recovery. NOT a source of truth — the final
`artifact.json` is authoritative (§7.3).

**turn_index** — In the message schema, the monotonically increasing
position counter within a round (1, 2, 3, …); together with `round`
it locates a message in the session's deliberative timeline.
`turn_index` is the schema field; the lifecycle term for an agent
contribution is `turn` (§2.1). The two are distinct names.

> *Boundary — `canonical_transcript` vs `context_packet`*: the
> canonical_transcript is the system's memory — immutable in
> ordering, complete, persisted, the source for any replay; a
> context_packet is one viewing of that memory shaped for a single
> agent invocation — mutable in composition per call, ephemeral, not
> the source of truth. Two invocations of the same agent in the same
> round can receive different context_packets derived from the same
> canonical_transcript without contradiction.

**Example — canonical_transcript vs context_packet (blocker #3).**
Round 3 begins; the canonical_transcript holds 47 messages. The
orchestrator_runtime builds a context_packet for the logician that
contains the original problem statement, the round-1 verdict, and the
round-2 visionary and critic primary_turns — but not the round-2
researcher turn (filtered as low-relevance by the packet builder's
heuristic). The logician's response is appended to the
canonical_transcript as message #48. The filtered content was not
lost — it is still in the canonical_transcript and can appear in the
next agent's packet.

### 2.5 Control plane

**direct_request** — A structured field emitted by an agent
addressing another `agent` for a specific contribution (question,
verification, critique, feasibility check, delegation); the only
sanctioned inter-agent control signal (ADR-003). Inline `@AgentName`
text inside agent prose is never parsed as a control signal.

**observability_level** — A `Config.runtime` knob (CLOSED enum
`{mvp, verbose}`) controlling the observability metric set the
runtime computes. `mvp` (default) computes the §7.9 MUST-set;
`verbose` is reserved for v1+ when the §7.10 SHOULD-set is wired
in.

**on_budget_exceeded** — A `Config.runtime` knob (CLOSED enum
`{stop}` at MVP) controlling the runtime's response when a budget
hard cap fires. `stop` (default) terminates with the matched
reason (§8.2). Other values (`degrade`, `escalate`) are reserved
for §12 Roadmap.

**verdict** — A machine-readable object emitted by the
`coordinator_agent` at the end of every `coordination_turn` (ADR-002,
ADR-005). Carries at least `next_action`, `rationale`, `confidence`,
`focus`, `next_agents`, `resolved_disagreements`,
`unresolved_disagreements`.

**verdict.next_action** — Enum with four values, each carrying a
Coordinator *semantic intent* (not a runtime action — the runtime's
handling of each value is specified in §4.4):
- `continue` — the Coordinator considers the deliberation incomplete
  and recommends another round;
- `finalize` — the Coordinator considers the deliberation complete
  and recommends ending the session with `synthesis`;
- `request_user_input` — the Coordinator requests information from
  the session's originator before another round can proceed;
- `request_external_research` — the Coordinator requests an external
  research action (tool call, out-of-band agent, human researcher)
  before another round can proceed.

`abort` is not a valid `verdict.next_action` value (ADR-002);
unrecoverable termination is the `orchestrator_runtime`'s decision,
not the Coordinator's.

> *Boundary — `verdict` vs runtime termination*: a **verdict** is a
> *semantic* opinion emitted by the `coordinator_agent` (an LLM);
> **runtime termination** (`orchestrator_runtime.terminate`) is a
> *deterministic* decision by the runtime based on `hard_cap`
> exhaustion, schema errors, unrecoverable provider failures, or user
> cancel. The two pathways are independent: the Coordinator can say
> `continue` and the runtime can still terminate; the Coordinator
> can say `finalize` and the runtime can fail to complete synthesis
> (provider error, hard_cap reached mid-synthesis, etc.), in which
> case it persists a `termination_artifact` instead (ADR-002, R2).
>
> *Boundary — `direct_request` vs inline `@AgentName`*: only the
> structured `direct_request` field is parsed by the
> orchestrator_runtime as a control signal; inline `@AgentName` in
> agent prose is at most a display convention and is never routed.
> This is what makes the control plane resistant to prompt injection
> (ADR-003).

### 2.6 Convergence and termination

**convergence_criteria** — The operational closure conditions the
`coordinator_agent` and the `orchestrator_runtime` apply jointly: no
new open questions surface for an evaluation window, no new failure
modes appear, and budget / hard_caps are not yet exhausted (R2, Pass
1 M5). When the conditions are satisfied, the coordinator typically
emits `verdict.next_action = finalize`.

**hard_cap** — A non-negotiable runtime limit enforced by the
`orchestrator_runtime` independently of any verdict. The
terminating hard caps are `max_rounds`,
`max_wallclock_seconds`, `max_total_cost_usd`,
`max_total_tokens`, and the optional per-agent
`per_agent_token_budget` (R2, ADR-002); each maps to a
termination reason in §4.7 / §8.5. The non-terminating caps
`max_branch_depth`, `max_deferred_queue_length`, and
`max_deferred_drains_per_round` are also runtime limits but
do not themselves terminate the session — they cause deferrals
or drops (§4.6).

**resolved_disagreements** — A field in the `verdict` listing prior
disagreements the Coordinator considers settled, with rationale;
populated for traceability and for inclusion in the `synthesis`.

**synthesis** — The final aggregated answer produced when the
session ends with `verdict.next_action = finalize` and the runtime
successfully completes the synthesis step; integrates the
deliberation's outcomes and carries both `resolved_disagreements`
and `unresolved_disagreements`. Synthesis is attempted whenever
possible; if synthesis cannot be produced, the runtime persists a
`termination_artifact` instead (R2). Synthesis is the canonical
name; "final answer", "final report", and "final synthesis" are
informal alternatives — see §2.9.

**termination_artifact** — A persisted record explaining why a
session ended without `synthesis`: which hard_cap fired (or which
failure was unrecoverable), what the canonical_transcript contained
at termination, and which `unresolved_disagreements` were outstanding
(R2). Synthesis is attempted before falling back to a
termination_artifact.

**unresolved_disagreements** — A field in the `verdict` listing
positions the panel did not reconcile; carried into the final
synthesis or, on early termination, into the termination_artifact.

### 2.7 Determinism and replay

**execution_replay** — Re-running the `orchestrator_runtime` against
the original inputs to regenerate session output; produces identical
artifacts only when every non-deterministic source is pinned
(provider, model, sampling parameters including seed/temperature,
cache, tool environment), and is therefore not generally reproducible
(M2, A2, N3).

**replayable** — Property of an artifact: the canonical_transcript is
replayable in the `transcript_replay` sense unconditionally; the
session as a whole is replayable in the `execution_replay` sense only
under the conditions named under `reproducible`.

**pinning_conditions** — The exhaustive list of non-deterministic
sources that MUST be pinned for `execution_replay` to produce a
bit-identical artifact (§7.6, ten conditions): (1) runtime
implementation (`RunManifest.producer.name` + `producer.version`);
(2) adapter implementation (`AdapterFactory` registration +
adapter-internal version, §6.11); (3) provider; (4) model
(identifier + provider-side snapshot); (5) sampling parameters
(temperature, top_p, seed, max_tokens, stop_sequences, §6.2);
(6) prompt caching state (cleared or pre-warmed identically);
(7) tool environment (tool name → handler binding + external
dependency state); (8) wall-clock seed (fixed clock source);
(9) resolved persona material (byte-identical Persona objects);
(10) starting canonical_transcript state (byte-identical prefix).
Unsatisfiable conditions raise `pinning_violation`.

**pinning_violation** — A diagnostic emitted by the runtime when
an `execution_replay` cannot satisfy a pinning condition. The
replay aborts; no fresh Artifact is produced. The diagnostic's
`condition` field is a CLOSED enum matching the ten §7.6 pinning
conditions: `{runtime, adapter, provider, model, sampling, cache,
tool_env, wallclock, persona, transcript_prefix}`.

**reproducible** — Property of an output: regeneratively identical to
a previous run. Symposium outputs are reproducible only when
provider, model, sampling parameters, cache, and tool environment
are pinned; absent pinning, outputs are not reproducible (A2, N3).
Replayable does not imply reproducible.

**scheduler_determinism** — Property of the `orchestrator_runtime`'s
scheduling: given the same configuration, panel ordering, and
`provider_result` sequence, the runtime takes the same actions in
the same order (A2, ADR-001). Determinism is a property of the
scheduler, not of the provider outputs that feed it.

**transcript_digest** — A stable SHA-256 hex digest computed over
the RFC-8785 JCS-canonicalized canonical_transcript at session end
(§7.7). Populates `Artifact.transcript_digest`,
`TerminationArtifact.transcript_digest`, and
`RunManifest.transcript_digest`; all three MUST be equal when
present. The primary integrity signal for a stored artifact.

**transcript_replay** — Re-rendering a stored `canonical_transcript`
(and its branch structure) without invoking any provider;
deterministic by construction, since no LLM call is involved (M2).

> *Boundary — `scheduler_determinism` vs `reproducible` outputs*: the
> scheduler is deterministic in its decision-making given fixed
> inputs; the model outputs that *feed* those decisions are not
> generally reproducible. Overall replayability is therefore
> unconditional for the transcript (`transcript_replay`) and
> conditional for the session (`execution_replay` requires every
> non-deterministic source to be pinned). This is the N3 normative
> qualification. Rule N12 — the spec's four-way determinism
> distinction — extends N3 by keeping `transcript_replay`,
> `execution_replay`, golden-test byte identity (§9.4.1), and
> `fake_provider` determinism (§9.1) operationally distinct;
> §10.2 D3 enumerates the four contracts side-by-side.

**Example — transcript_replay vs execution_replay (M2).** A session
ended with 53 messages in the canonical_transcript. A consumer that
re-renders the stored artifact incurs no provider cost and gets
bit-identical output every time (transcript_replay). A consumer that
re-runs the `orchestrator_runtime` against the original problem and config
invokes providers, and can get a different canonical_transcript even
at temperature 0 if the provider rolled the model snapshot or the
tool environment changed (execution_replay). Only with provider,
model, sampling parameters, cache, and tool environment all pinned
does the second run produce the same canonical_transcript as the
first.

### 2.8 Provider adapter

**error_kind** — The CLOSED 12-value enum on
`provider_result.error.kind` (§6.6). Adapters MUST classify every
observed vendor error into one of: `timeout`, `network`,
`rate_limit`, `quota_exhausted`, `auth_failure`,
`model_unavailable`, `context_length_exceeded`, `content_filter`,
`invalid_request`, `malformed_response`, `tool_failure`,
`internal`.

**expected_output_schema** — A field on `provider_request` (§6.2,
§6.5) naming the canonical structured-output target the adapter
will enforce: one of `turn_structured_output`, `verdict`,
`synthesis_content`, or `null` (no structured output expected).

**fake_provider** — A scriptable, deterministic `provider_adapter`
shipped with the spec for tests (§6.14, §9.1). Conforms to the
full §6 contract; substitutes the network call with a lookup
into a pre-authored `FakeProviderScript`. Determinism is
unconditional (§2.7 A2/N3) because no LLM is invoked.
`fake_provider` is the canonical name for the test adapter; it
is NOT a vendor identifier (rule N4).

**FakeProviderScript** — An ordered sequence of pre-scripted
`provider_result` responses driving a `fake_provider` (§9.2).
Each entry binds by ordinal position to the N-th
`provider_adapter.invoke` call the runtime makes during a
session, with an optional `match` clause turning the binding
into an asserted check that the entry was applied to the
expected invocation context. Schema:
`fake_provider_script.schema.json`.

**finish_reason** — A field on a `provider_result` indicating why the
provider stopped generating. CLOSED enum `{stop, length, tool_call,
content_filter, error}` (§5.7). Consumed by the orchestrator_runtime
to decide whether to dispatch a tool, continue, or surface a
failure. Under MVP internal-loop topology (§6.4), `tool_call` NEVER
appears as the returned terminal reason — the adapter completes the
tool loop internally and surfaces a terminal value in
`{stop, length, content_filter, error}`. The `tool_call` value
remains in the schema only so a future external-loop adapter (v1+)
can use it without re-opening the enum.

**provider_adapter** — The contract between the
`orchestrator_runtime` and a model backend:
`invoke(request) -> provider_result` (blocker #5). One adapter per
backend; the runtime is provider-agnostic by construction. Vendor
identifiers belong to adapter configuration, never to the spec body
(N4).

**provider_request** — The request side of
`provider_adapter.invoke(request) → provider_result` (§6.2).
Carries `agent_id`, `provider`, `model`, `messages`, `tools`,
`sampling`, `expected_output_schema`, `metadata`. Distinct from
`context_packet`: the request is provider-shaped; the packet is
schema-shaped (`context_packet.schema.json`).

**provider_result** — The structured object returned by
`provider_adapter.invoke(request)`: carries `messages`,
`tool_events`, `usage` (tokens, cost), `finish_reason`,
`structured_output`, `raw`, `error` (blocker #5). Every field the
runtime consumes is named on the provider_result, never inferred
from free-text content.

**structured_output** — The portion of a `provider_result` that
carries machine-readable content (a `verdict`, a `direct_request`, a
tool call's argument object), validated against its schema before
the orchestrator_runtime acts on it (ADR-003).

**tool** — A registration of an externally-invocable function the
adapter exposes (§6.4): `{name, description, input_schema,
metadata?}`. Tool input arguments are validated against
`input_schema` BEFORE handler invocation; validation failures
produce `error.kind = tool_failure`.

**tool_call_id** — A field on `provider_request.messages[].role =
tool` entries (§6.2), conditionally required to correlate a tool
message with the assistant message that emitted the tool call.
Adapters MUST preserve `tool_call_id` correlation end-to-end.

**tool_event** — An entry in `provider_result.tool_events` recording
a tool invocation (name, arguments, result, latency, error);
persisted into the canonical_transcript alongside the parent message
so replays and audits see the tool history.

**usage_estimated** — A flag (`provider_result.usage.estimated`)
the adapter sets when the provider did not report usage and the
adapter estimated tokens via a compatible tokenizer (§6.9, §7.9).
The runtime propagates the flag into the session's cumulative
usage surface so operators can interpret cost / token accumulation
as approximate.

### 2.9 Deprecated terms (do not use)

The following v0 vocabulary is forbidden in this specification.
Citations point to the rule or ADR that retires the term.

- **orchestrator** (bare) — use `orchestrator_runtime` (ADR-005). The
  bare word is ambiguous because the v0 draft used it for both the
  runtime and the Coordinator role.
- **orchestration engine** — use `orchestrator_runtime` (ADR-005).
- **shared transcript reinjection** / **transcript reinjection** /
  **reinject the transcript** — use "`context_packet` derived from
  `canonical_transcript`" (blocker #3). The mechanism is per-invocation
  packet derivation, not whole-transcript reinjection.
- **shared history** / **shared transcript** — use `canonical_transcript`
  for the durable log, `context_packet` for the per-invocation view.
- **abort** (as a `verdict.next_action` value) — use runtime
  termination only (ADR-002). `abort` is not a Coordinator decision.
- **final_answer_ready** — covered by `verdict.next_action = finalize`
  (Codex turn 1 #11; Pass 1 row #37).
- **parallel Round 1** — Round 1 is sequential like every other round
  (ADR-001). Ensemble-style parallel first-pass perspectives are
  tracked separately as `EnsembleMode` in §12 Roadmap.
- **round-robin** — use "sequential round over
  `active_deliberation_panel`"; round-robin obscures that the order
  is the declared panel order, not a rotating queue.
- **inline `@AgentName` as control signal** — use `direct_request`
  (ADR-003). `@AgentName` in agent prose is at most a display
  convention and is never parsed as a runtime signal.
- **"all agents are mandatory"** (as a Round 1 invariant) — use
  "every agent in `active_deliberation_panel` has had its
  `primary_turn`" (R1). Mandatory-panel is a property of the MVP
  default config (R3), not of the protocol itself.
- **"Symposium is NOT based on a fixed panel of agents"** (as an
  MVP-level statement) — contradicts R3 (MVP `strategy = fixed`).
  The statement survives only as a description of v1+ behavior, not
  of MVP (Pass 1 soft-drop, row #111).
- **dynamic participant selection** (as an MVP characteristic) —
  MVP selection is `strategy = fixed` (R3); dynamic selection is a
  v1+ Selector strategy (Pass 1 row #112).
- **debate moderator** (as a label for runtime scheduling) —
  legitimate as a *semantic* description of the `coordinator_agent`
  but never as a name for runtime scheduling. Scheduling is
  `orchestrator_runtime` (ADR-005, Pass 1 row #92).
- **distributed scheduler** (as a description of the Coordinator) —
  use `orchestrator_runtime`. The Coordinator is an LLM agent, not
  a scheduler (ADR-005, Pass 1 row #92).
- **conversational router** (as a description of the Coordinator) —
  use `orchestrator_runtime` for routing of `direct_request`s and
  `deferred_request`s; the Coordinator does not route (ADR-005,
  Pass 1 row #92).
- **Interactive Mode** (as a Core MVP framing) — MVP execution mode
  is `batch` (ADR-004); interactive event streaming is a v1+
  execution mode, and the runtime-safety subclaims of v0's
  "Interactive Mode" section now live under `orchestrator_runtime`
  (Pass 1 row #91, Codex turn 2 soft-drop).
- **framework** (when describing Symposium itself) — use "opinionated
  deliberation protocol" or "protocol" (Codex turn 1 #3; Pass 1 row
  #1). "Framework" remains valid for describing competitor systems
  in §10.
- **conceptual Symposium Skill** (as a system component) — the Skill
  is a host integration example, not part of the Symposium runtime
  (P2; Pass 1 rows #52-#56). See §11.
- **Synthesizer** (as a panel persona) — synthesis is part of the
  `coordinator_agent`'s `coordination_turn` on
  `verdict.next_action = finalize`; there is no separate Synthesizer
  agent in MVP (Pass 1 row #114).
- **cognitive OS** / **cognitive orchestration engine** (as
  descriptors of MVP) — Vision-tier framings (§13), not normative
  descriptions of the protocol (N1).
- **plugin-first architecture** (as an MVP commitment) — MVP ships
  the `provider_adapter` contract only; other plugin categories
  (personas, schedulers, replay, TTS, eval, UI) are Roadmap (§12;
  Pass 1 row #152).
- **adaptive reasoning depth** (as Core MVP) — auto-scaling reasoning
  effort under budget pressure is v1+ (Pass 1 row #149 split).
- **final answer** / **final report** / **final synthesis** — use
  `synthesis` (§2.6). The session produces either a `synthesis` or a
  `termination_artifact`, never a "final answer" as a distinct
  artifact.
- **"MUST eventually support"** (any usage) — incoherent under RFC
  2119 (N1). Pick exactly one normative level matched to the scope
  tag: a Core-MVP normative requirement, a v1 normative requirement,
  or a Roadmap descriptive entry (Pass 1 row #147).

### 2.10 Historical record — terminology questions resolved by §4

This subsection records six terminology questions that the
glossary draft could not resolve without a §4 (runtime protocol)
decision. All six have been resolved; each item carries a
`[RESOLVED in …]` pointer to the §4 subsection that owns the
answer. The subsection is preserved as a historical audit trail
of the §2 → §4 dependency chain.

1. **`deferred_request` runtime semantics.** The term is defined as a
   queued `direct_request`, but Pass 3 needs to specify *when* the
   runtime defers versus dispatches in-line (branch-depth saturation,
   pending-count threshold, per-agent fairness) and the queue's
   ordering policy (FIFO, priority from Coordinator verdict, mixed).
   Relates to Pass 1 Q5 / M3.
   **[RESOLVED in §4.5 + §4.6]** — FIFO queue; defer triggers are
   branch-depth saturation and a second `direct_request` in one
   primary_turn; branch-origin requests are NEVER deferred (recorded
   as `suggested_followups` annotations); at most one drain per
   round, at round-open before any panel primary_turn; bounded
   `max_deferred_queue_length` (default 8) with drop-and-annotate
   on overflow.

2. **`request_external_research` execution semantics in `batch`
   mode.** The verdict value is defined as a control signal, but Pass
   3 must specify what the orchestrator_runtime does in `batch` mode
   (no live user) when external research is not configured — likely a
   persist-to-artifact behavior, but the artifact field name and the
   downstream consumer's resumption contract are undecided.
   **[RESOLVED in §4.4 + §4.7]** — the runtime persists the
   verdict's `rationale`, `focus`, and research payload to the
   canonical_transcript and transitions to **terminate** with reason
   `external_research_required`. A `termination_artifact` records the
   pending research need for downstream resumption. Same pattern
   applies to `request_user_input` (reason `user_input_required`).

3. **`active_deliberation_panel` mutation.** The term distinguishes
   "declared panel" from "actively scheduled subset". For MVP
   `strategy=fixed` the two are identical. Pass 3 must state whether
   the active set can shrink mid-session (e.g. an agent failure
   removes the agent for the rest of the session) or is immutable;
   Pass 1 row #121 forbids spontaneous expansion but is silent on
   contraction.
   **[RESOLVED in §4.9]** — panel is immutable for healthy sessions;
   contraction allowed on unrecoverable agent failure via the
   configurable knob `on_agent_failure ∈ {terminate,
   continue_without}` (default `terminate`). Current-round semantics
   specified for both intra-turn and pre-turn failures. The
   coordinator cannot be contracted. A panel that contracts to zero
   panel members terminates with reason `provider_unrecoverable`.

4. **`turn_index` numbering across forks.** Glossary defines
   `turn_index` as a per-round counter and says fork turns do not
   increment the round counter (R1). Pass 3 needs to decide whether
   fork turns share the round's `turn_index` sequence (incrementing
   monotonically across primary and branch turns), get their own
   counter scoped to the fork, or are addressed solely by
   `parent_id` plus `branch_depth` without a fork-local counter.
   **[RESOLVED in §4.5]** — shared monotonic counter within a round
   (primary_turns, branch turns, and runtime annotations all advance
   the same `turn_index`). Branch structure is recovered from
   `parent_id` + `branch_depth`. Rationale: a sorted scan over
   `(round, turn_index)` returns execution order — the simplest
   replay invariant.

5. **`context_packet` minimum content set.** The transcript entry
   says the packet "can be compressed, filtered, or windowed" but
   does not enumerate a minimum invariant content (problem statement,
   `active_deliberation_panel` disclosure per Pass 1 row #77, prior
   verdict). Pass 3 should specify the minimum to keep two
   implementations interoperable.
   **[RESOLVED in §4.3]** — minimum content: (a) problem_statement,
   (b) current round number, (c) the agent's own persona material,
   (d) active_deliberation_panel disclosure, (e) the most recent
   verdict (absent in round 1), (f) all messages produced in the
   current round so far in `turn_index` ascending order, (g) for
   branch turn agents, the originating direct_request and parent
   message. Compression/filtering allowed beyond this minimum.

6. **`deliberation_panel` declaration order semantics.** R1 says
   "declared order" determines round flow, but Pass 3 should clarify
   whether the order is preserved across rounds (always logician →
   visionary → … → engineer) or can be permuted by the Coordinator
   via `verdict.next_agents`. The current glossary entry leaves it
   open.
   **[RESOLVED in §4.2]** — declared order is immutable across
   rounds in MVP. `verdict.next_agents` is advisory only: it
   documents the Coordinator's semantic preference but the MVP
   runtime does not honor it for scheduling. A v1 selector strategy
   MAY honor `verdict.next_agents`. Rationale: preserves
   scheduler-determinism (ADR-001, ADR-002) — dynamic ordering would
   transfer scheduling control from the runtime to the Coordinator.

### 2.11 Testing & evaluation

**evaluation_harness** — The §9.10 tool that computes the §7.10 v1
metric set on a single session or a batch of sessions and produces
a structured report. Optional postmortem output per §9.11.

**golden_test_case** — A self-contained regression-test bundle
`{problem_statement, config, fake_script, expected_artifact}`
(§9.4). A conforming runtime, driven by `fake_script` against
`config + problem_statement`, MUST produce an `Artifact` whose
`transcript_digest` equals `expected_artifact.transcript_digest`.
Schema: `golden_test_case.schema.json`.

**harness_pinning** — The test-harness-only counterpart of §7.6's
production `pinning_conditions`. A `GoldenTestCase` requires the
runtime under test to accept (a) a deterministic message-id
allocator and (b) a fixed clock source so that golden-test
digests are byte-identical across conforming runtimes (§9.4.1).
Distinct from §7.6's ten production pinning conditions; lighter
because no LLM / provider / cache / tool environment is involved.

**postmortem** — A v1 structured artifact answering Pass 1 row #133
questions (correct agents selected? scope violations? capability
gaps? best contributors?). MAY be produced by the
`evaluation_harness`; MVP harnesses MAY skip. Shape documented in
§9.11 prose; no schema in Pass 7 (formal defer to v1).

**property_test** — A test of a runtime invariant rather than a
single-input/single-output unit (§9.5). A property test exercises
a §4.10 invariant against an arbitrary `FakeProviderScript` and
verifies the invariant holds for every produced `Artifact`.
Language-agnostic vocabulary.

**scope_violation** — A per-turn event recorded by the
`evaluation_harness` when an agent's contribution touches a
`forbidden_domain` (for `domain_persona`) or fails a
`must_delegate` rule (§9.10 `role_purity_score`). Detection
mechanism is implementation-defined (MVP: rule-based pattern match
over persona forbidden_domains; v1+ MAY use an LLM-based
detector).

---

## 3. Overview & Non-Goals

**[Core MVP]**

Symposium is an opinionated protocol for structured, sequential,
adversarial multi-agent deliberation (§1, §10.1). The protocol
fixes one topology — a declared-order `deliberation_panel`
whose members each take one `primary_turn` per round (R1),
followed by a `coordination_turn` from a structurally-separated
`coordinator_agent` (ADR-005) — and exposes its operational
machinery (`selector`, `coordinator_agent`,
`orchestrator_runtime`) as three distinct roles with distinct
responsibilities (ADR-005, §2.2). Detailed positioning,
defensible differentiators (D1–D6), and comparison to other
multi-agent frameworks live in §10; the canonical use-case
framing is "deep technical reasoning, architectural analysis,
evidence-based research, adversarial criticism, creative
exploration, and implementation planning" (Pass-1 row #2).

The protocol value is structural: cognitive specialization
through bounded persona scope (§5.3, D2); productive
disagreement and structured adversarial reasoning over
maximised agreement (§2.6, §10.7, D4); machine-readable
verdicts (ADR-002, ADR-003) rather than ambiguous natural-
language handoffs (D5); a closed, operator-facing termination
and failure surface (§4.7, §6.6, §8.3, §8.5, D6); and
artifact-first replayability with four distinct determinism
contracts (§7.5, §7.6, §7.7, §9.1, §9.4.1, D3).

### 3.1 Non-goals

The following are explicit non-goals of the Core MVP. Each
non-goal is descriptive (no RFC 2119 keywords); the normative
claim that anchors it lives in the cited section.

- **Symposium is not a generic agent framework.** Symposium
  expresses exactly one topology (D1 in §10.2). Comparable
  multi-agent systems (AutoGen, CrewAI, LangGraph, OpenAI
  Agents SDK) are designed to express arbitrary topologies and
  are surveyed in §10.3–§10.6. "Framework" is the v0-draft
  wording retired for Symposium itself per Pass-1 row #1 / Q7;
  it is retained only when describing competitors or legacy
  comparisons.
- **Symposium does not aim to maximize consensus.** Productive
  disagreement is a structural feature (Pass-1 rows #61, #62);
  the `coordinator_agent`'s `verdict.unresolved_disagreements`
  field surfaces them rather than collapsing them (§5.6). The
  no-consensus posture is named explicitly as the §10.7 "no
  consensus is the goal" non-claim.
- **Symposium does not provide a UI or visualization in the
  Core MVP.** Replay viewers, HTML visualizations, TTS
  narration, and analytics dashboards are §12 Roadmap items
  (Pass-1 rows #98, #99, #101). The MVP surface is CLI
  (§11.2) and library API (§11.3); rendering decisions are
  host-owned.
- **Symposium does not autonomously create new personas during
  a session.** The runtime never instantiates a new persona
  mid-session (§4 normative invariant; Pass-1 row #122). The
  rationale is that persona scope (`allowed_domains`,
  `forbidden_domains`, `must_delegate` per §5.3) requires
  human review to remain consistent; live creation would
  defeat the role-purity guarantee (D2). The reference
  persona-creation workflow lives in
  [`docs/repository-strategy.md`](repository-strategy.md) §12.
- **Symposium does not simulate fake personalities.** Personas
  are reasoning archetypes with declared scope and behavioral
  constraints (§5.3), not character roleplay (Pass-1 row #6).
  The goal is cognitive specialization, not anthropomorphism.
- **Symposium does not run interactive or live-pause sessions
  in the Core MVP.** MVP execution mode is `batch`-only
  (ADR-004): a session runs to synthesis or termination
  without live user intervention (other than `terminate(reason
  = user_cancel)`). Live `pause`, `inject_clarification`, and
  human-in-the-loop are §12 Roadmap (v1+) per ADR-004.
- **Symposium does not run agents in parallel within a turn.**
  The runtime is sequential: exactly one provider invocation
  is in flight at any instant (ADR-001, §4 introductory text).
  Parallel branch execution and distributed orchestration are
  §13 Vision items.

Additional non-claims about positioning and competitive
relationships are enumerated in §10.7.

---

## 4. MVP Runtime Protocol

**[Core MVP]**

This section specifies the behavior of the `orchestrator_runtime` —
the deterministic, code-based component (ADR-005) that drives a
Symposium `session` from problem submission to `synthesis` or
`termination_artifact`. Throughout this section, "the runtime" means
`orchestrator_runtime`. A competent implementor reading §1-§4 plus
the ADRs in Appendix A and the refinements in Appendix B should be
able to write a working scheduler; schema field shapes live in §5
and the provider adapter contract lives in §6.
RFC 2119 keywords below ("MUST", "MUST NOT", "SHOULD", "MAY") apply
only to runtime invariants; editorial sentences use lowercase.

The runtime is sequential, conversational, and reactive (ADR-001):
exactly one provider invocation is in flight at any instant; no
parallelism across panel members, across rounds, or across branches.
The runtime exhibits `scheduler_determinism` (§2.7): given a fixed
configuration, panel declaration, and `provider_result` sequence, it
takes the same actions in the same order. Outputs are
`replayable` via `transcript_replay`; they are `reproducible` via
`execution_replay` only when every non-deterministic source is
pinned (A2, N3).

### 4.1 Session lifecycle

A `session` traverses a finite state machine over five named phases:

```text
init  →  selector  →  deliberation  →  finalize | terminate  →  persist
```

The runtime owns the transition between phases. The
`coordinator_agent` opines on continuation via `verdict.next_action`
but does not transition phases (ADR-002, ADR-005).

**init**. Entry: a `session_id` is allocated and the session
configuration is loaded. The runtime initializes an empty
`canonical_transcript`, an empty `deferred_request` queue, the
`round` counter at 0, and the cumulative usage counters (tokens,
cost, wall-clock) at 0. The problem statement is appended to the
canonical_transcript as the first `message` (speaker = originator,
type = `problem_statement`). Exit: configuration validated; problem
statement persisted; transition to **selector**. Reachable error
states: configuration-schema failure → `terminate(reason =
schema_error)`.

**selector**. Entry: the runtime invokes the `selector` to obtain
the session's `active_deliberation_panel` and `coordinator_agent`
identity. In MVP the selector strategy is `fixed` (R3): no LLM call,
no provider invocation, the active_deliberation_panel is the
declared `deliberation_panel` and the `coordinator_agent` is the
declared coordinator. Exit: panel and coordinator identities bound
to the session; transition to **deliberation**. Reachable error
states: an empty or malformed panel → `terminate(reason =
schema_error)`.

**deliberation**. Entry: the runtime opens `round 1`. Each round
executes the round structure of §4.2 — one `primary_turn` per panel
member in declared order, followed by one `coordination_turn`
yielding a `verdict`. Between rounds the runtime applies §4.4
verdict handling, §4.6 deferred-queue drain, and §4.7 hard-cap
checks. Exit conditions, in priority order: (a) a `hard_cap` is
exhausted → transition to **terminate**; (b) `verdict.next_action =
finalize` and no hard_cap is reached → transition to **finalize**;
(c) `verdict.next_action ∈ {request_user_input,
request_external_research}` → transition to **terminate** with the
matched reason (§4.4); (d) `verdict.next_action = continue` and no
hard_cap is reached → open the next round (re-entry into
**deliberation**). Reachable error states: unrecoverable
provider failure (§4.9), unrecoverable schema failure → transition
to **terminate**.

**finalize**. Entry: the runtime attempts to produce a `synthesis`
artifact via §4.8. The attempt invokes the `coordinator_agent` one
final time with a synthesis-shaped `context_packet` derived from the
`canonical_transcript` per §4.3 and §4.8 (the packet's exact
composition is implementation-defined beyond the §4.3 minimum, but
includes the cumulative `resolved_disagreements` and
`unresolved_disagreements` collected across the session). Exit: on
success, the synthesis message is appended to the
canonical_transcript and control passes to **persist**. On failure
(provider error, schema failure, hard_cap reached mid-synthesis),
the runtime falls back to **terminate** with the matched reason.

**terminate**. Entry: the runtime is shutting the session down for
a reason ∈ {`budget_exceeded`, `schema_error`,
`provider_unrecoverable`, `user_cancel`, `timeout`,
`user_input_required`, `external_research_required`}. The runtime
produces a `termination_artifact` (§4.8) recording the reason, the
final canonical_transcript state, and any `unresolved_disagreements`
from the most recent verdict. Exit: transition to **persist**.

The seven-value termination-reason enum
(`budget_exceeded`, `schema_error`, `provider_unrecoverable`,
`user_cancel`, `timeout`, `user_input_required`,
`external_research_required`) is the canonical set; §8.5
ratifies it. The two `user_input_required` /
`external_research_required` reasons correspond to
`verdict.next_action ∈ {request_user_input,
request_external_research}` in `batch` mode (ADR-004; §4.4).

**persist**. Entry: the canonical_transcript and either the
synthesis or the termination_artifact are flushed to durable
storage under `runs/<session_id>/` per §7. Exit: the session
terminates. Reachable error states: persistence failure is reported
to the host but does not retroactively change the session outcome.

The `canonical_transcript` is the sole source of truth across all
phases (ADR-005; §2.4). Every `message` produced — primary_turn,
coordination_turn, branch turn, problem_statement, synthesis,
termination_artifact — MUST be appended in execution order. The
runtime MUST NOT depend on implicit conversation memory from any
provider (Pass 1 row #12).

### 4.2 Round structure

A `round` (§2.1, R1) is opened, traversed, and closed by the
runtime. The runtime drives one `primary_turn` per panel member in
declared order, then exactly one `coordination_turn`, then the
round closes (R1, ADR-005).

For each round:

1. The round counter increments. The `turn_index` counter resets to
   0 (see §4.5 on `turn_index` semantics across forks).
2. The runtime performs the round-opening `deferred_request` drain
   per §4.6 exactly once, before any panel `primary_turn` of this
   round fires. If a drain dispatches a branch turn, that turn
   advances `turn_index` and is appended to the canonical_transcript
   before step 3 begins.
3. For each agent `a` in `active_deliberation_panel`, taken in
   declared order, the runtime:
   1. Advances `turn_index` by 1.
   2. Derives a `context_packet` for `a` per §4.3.
   3. Invokes the `provider_adapter` for `a` (§6); the returned
      `provider_result` is parsed; the resulting message is
      appended to the canonical_transcript as a `primary_turn`.
   4. Examines `structured_output.direct_requests` (zero or more
      structured requests per turn — see §4.5). For each request,
      the runtime applies §4.5 dispatch-or-defer rules. A dispatched
      branch turn advances `turn_index` and is appended before the
      round advances to the next panel agent.
4. After every panel member has produced one primary_turn, the
   runtime advances `turn_index` by 1, derives a context_packet for
   `coordinator_agent`, and invokes the provider_adapter for it. The
   returned message is appended as a `coordination_turn` and MUST
   carry a `verdict` (ADR-002; §2.5). The round closes.

Hard caps (§4.7) are checked before each provider invocation, after
each provider invocation (consuming the returned usage), and at
every state transition; any breach overrides the round flow and
transitions to **terminate** with the matched reason.

The declared order of `active_deliberation_panel` is immutable
across rounds in MVP. `verdict.next_agents` MAY be set by the
`coordinator_agent` but the MVP runtime treats it as advisory only;
the runtime does not honor it for round ordering. A v1 selector
strategy MAY honor `verdict.next_agents` (Roadmap, §12). Rationale:
keeps the scheduler deterministic (ADR-001, ADR-002) — dynamic
ordering would shift control of scheduling from the runtime to the
Coordinator.

The runtime MUST NOT parse free-text content for control signals.
Inline `@AgentName` text in agent prose is at most a display
convention and is never routed (ADR-003). The only sanctioned
control signal is a structured `direct_request` field on the
provider_result's `structured_output` (Pass 1 rows #38, #75, #105).

A round MUST close in the `coordination_turn`: a round whose
coordinator invocation fails to produce a schema-valid verdict
triggers §4.9 failure handling (retry, then unrecoverable
termination if exhausted).

### 4.3 Context packet derivation

Before each provider invocation, the runtime derives a
`context_packet` from the `canonical_transcript` (§2.4). The packet
is the per-invocation view of the transcript shaped for one agent;
it MAY be compressed, filtered, or windowed beyond the minimum below.
The transcript itself is not mutated by packet derivation.

Every `context_packet` MUST contain, at minimum:

- The original `problem_statement` (the session's first message).
- The current `round` number.
- The agent's own persona material: identifier, `reasoning_scope`,
  `reasoning_style`, `behavioral_constraints`, `failure_modes`,
  `output_requirements`; and for a `domain_persona`, additionally
  `domain_scope`, `forbidden_domains`, and `must_delegate` (§2.3,
  Pass 1 row #57).
- The `active_deliberation_panel` disclosure: for each panel member,
  identifier and role/specialty summary (Pass 1 row #77). The
  packet for a panel agent identifies its peers; the packet for
  `coordinator_agent` identifies all panel members.
- The most recent `verdict` if one exists (absent in round 1). The
  verdict's `focus` SHOULD be highlighted as a directive for the
  current round (semantic hint, not a scheduling directive).
- **All messages produced in the current round so far**, in
  `turn_index` ascending order. This covers every prior
  `primary_turn`, every drained branch turn, and every in-line
  branch turn dispatched earlier in this round. This minimum is
  what makes the deliberation conversational rather than a sequence
  of independent monologues; a panel agent later in the declared
  order MUST see what earlier agents in the same round contributed.
- For a branch turn agent (§4.5), the originating `direct_request`
  and the parent message that emitted it; the packet MUST contain
  enough of the chain that the branch agent can answer in context.

The packet MAY additionally include compressed, filtered, or
windowed prior-turn content beyond the minimum. The packet builder's
compression heuristic is implementation-defined; the canonical_transcript
remains complete and authoritative regardless of what any single
packet omits (§2.4 Boundary). Two invocations of the same agent in
the same round MAY receive packets derived differently from the
same canonical_transcript.

Packet derivation is a runtime operation, not an LLM call: it
contains no provider invocation and is therefore deterministic
under the scheduler-determinism qualification (§2.7, A2). The
canonical_transcript that feeds the packet is invariant within the
session.

### 4.4 Verdict handling

A `coordination_turn` MUST emit a schema-valid `verdict` (§2.5,
ADR-002). The runtime examines `verdict.next_action` and acts:

- **`continue`** — The runtime checks hard caps (§4.7). If no cap is
  reached, the runtime opens the next round (re-entry into **deliberation**).
  If a cap is reached, the runtime ignores `continue` and transitions
  to **terminate** with the matching reason. The runtime MUST NOT
  open another round when a hard cap is exhausted, regardless of
  verdict (ADR-002, R2).

- **`finalize`** — The runtime checks hard caps. If no cap is
  reached, it transitions to **finalize** and attempts `synthesis`
  per §4.8. If a hard cap is reached, the runtime transitions to
  **terminate** with the matching reason; synthesis MAY still be
  attempted under the conditions in §4.8.

- **`request_user_input`** — In MVP `batch` mode (ADR-004), no live
  user is present. The runtime persists the verdict's
  `rationale`, `focus`, and `user_input_request` payload (the
  `Verdict.user_input_request` field, §5.6, is required when
  `next_action = request_user_input`) into the canonical_transcript
  and transitions to **terminate** with reason `user_input_required`.
  A `termination_artifact` (§4.8) records the pending information
  request so a downstream consumer can resume the deliberation in a
  v1 interactive mode. The MVP runtime does not block waiting for a
  live user.

- **`request_external_research`** — In MVP `batch` mode, no
  in-runtime external research is performed. The runtime persists
  the verdict's `rationale`, `focus`, and any structured research
  payload into the canonical_transcript and transitions to
  **terminate** with reason `external_research_required`. A
  `termination_artifact` (§4.8) records the pending research need so
  a downstream consumer (a v1 interactive mode, an out-of-band
  human researcher, or a future tool-augmented adapter) can resume.
  The MVP runtime does not invoke external tools on this signal.

The verdict's other fields are consumed as follows. `rationale` and
`focus` are preserved on the canonical_transcript and surfaced in
the next round's context_packets (§4.3). `confidence`,
`resolved_disagreements`, and `unresolved_disagreements` are
preserved for inclusion in synthesis (§4.8) or in the
termination_artifact. `next_agents` is preserved as a Coordinator
recommendation but MUST NOT alter round ordering in MVP (§4.2).

`abort` is not a valid `verdict.next_action` value (ADR-002, §2.5).
A verdict carrying an out-of-enum `next_action` triggers §4.9
failure handling.

### 4.5 Direct requests and forks

A turn's `provider_result.structured_output` MAY carry zero or more
`direct_request` entries (the field is plural — `direct_requests` —
in the schema; §5 fixes the shape). The runtime examines those
entries — never the free-text content — and decides for each one
whether to dispatch a `fork` in-line, defer to the queue, drop with
a runtime annotation, or treat it as a schema failure. The
decisions for the entries in a single turn are made in their
declared order.

**Dispatch in-line.** The runtime dispatches a `direct_request`
in-line as a `fork` when all of the following hold:

- `direct_request.target` names an agent in
  `active_deliberation_panel` other than the originator (a
  `direct_request` targeting `coordinator_agent`, the originator,
  or an agent not in the active panel is a request-level schema
  failure handled below; it does NOT trigger §4.9 agent-failure
  policy);
- the originating turn is a `primary_turn` (not a branch turn — see
  below);
- the runtime is not yet at branch-depth saturation: the new
  `branch_depth` would be ≤ `max_branch_depth` (default 1, §4.7);
- no earlier `direct_request` in the same primary_turn has already
  dispatched (at most one in-line fork per primary_turn; subsequent
  requests defer, see below).

On dispatch, the runtime derives a context_packet for the target
agent (§4.3) including the parent message, invokes the
provider_adapter, and appends the target's response to the
canonical_transcript as a branch turn at `branch_depth + 1`. The
target's branch turn MUST NOT take further primary_turn shape; it
contributes one message and the branch closes.

**Defer.** The runtime defers a `direct_request` to the queue
(§4.6) when both of the following hold:

- the originating turn is a `primary_turn` (defers from a branch
  turn are forbidden — see below);
- the request cannot dispatch in-line because (a) branch-depth
  would be exceeded, or (b) a `direct_request` has already
  dispatched in-line in the current primary_turn.

**Branch-origin requests (B→C suppression).** A branch turn MAY
emit `direct_request` entries in its `structured_output`, but the
runtime MUST NOT dispatch them as forks and MUST NOT defer them to
the queue. Instead, the runtime records all such entries as a
`suggested_followups` list annotation on the branch turn message,
with target and content preserved for each. Suggested followups SHOULD be
summarized in the next `coordinator_agent` context_packet (§4.3),
where the Coordinator MAY surface them in its `verdict.focus` or
`verdict.next_agents`. The runtime does NOT route them. This rule
enforces Pass 1 row #89 ("B MUST NOT directly trigger C") and
ADR-001 (no recursive fork chains); a branch agent's onward
question becomes a Coordinator-visible suggestion, not an
executed action, which is the only routing path consistent with
the structured-only control plane (ADR-003).

**Branch closure.** A branch closes when the target's branch turn
is appended. Control resumes to the originator: the originator does
not re-invoke (its primary_turn has already produced its message in
the canonical_transcript), and processing continues with the next
deferral decision (if more requests remain in the same turn) or
the round advances to the next panel member in declared order. A
branch contributes exactly one branch turn message; no nested
branches in MVP (`max_branch_depth = 1` by default).

**Anti-loop.** The runtime prevents pathological structures via
bounded `max_branch_depth` (default 1), bounded `max_rounds`,
bounded `max_total_tokens`, bounded `max_total_cost_usd`, bounded
`max_wallclock_seconds`, and a bounded `deferred_request` queue
length (§4.6, §4.7; Pass 1 rows #87, #106, #107). Semantic
similarity heuristics (duplicate-question detection, repeated-topic
suppression) are out of scope for MVP (Pass 1 row #107, v1).

A `direct_request` whose `target` does not name a current member
of `active_deliberation_panel` other than the originator (e.g. an
agent removed by §4.9 contraction, an undeclared identifier, the
originator itself, or `coordinator_agent`) is a request-level
schema failure. The runtime annotates the originating message with
a `schema_failure` entry naming the offending request and drops the
request — it is neither dispatched nor deferred. The originator's
turn itself remains valid; round flow continues. This is the only
schema-failure class that does NOT escalate to §4.9 agent-failure
handling, because the originator's content as a whole is well-formed
and only one nested field is invalid. Repeated request-level
schema failures from the same agent across a round MAY be counted
toward that agent's schema-failure budget at the implementation's
discretion (§8 retry-budget mechanics).

A `fork` (branch turn) does not increment the round counter (R1).
The `turn_index` counter advances monotonically across both
primary_turns and branch turns within a round; the branch
structure is recovered from `parent_id` and `branch_depth` fields
on the message, not from a separate counter. Rationale: a sorted
scan over `(round, turn_index)` returns the execution order
unambiguously, which is the simplest replay invariant; branch
recovery is a structural concern, not an ordering concern.

### 4.6 Deferred request queue

The runtime maintains a per-session FIFO `deferred_request` queue
(§2.1). When §4.5 defers a `direct_request`, the runtime enqueues
a record `{originator, target, type, content, parent_id,
deferred_at_round, deferred_at_turn_index}` at the queue's tail.

**Drain policy.** Once per round, before the next panel member's
primary_turn would fire — that is, at the start of each round, after
the round counter has incremented but before any panel member takes
its first primary_turn — the runtime checks the queue. If the queue
is non-empty, the runtime dequeues the head and dispatches it as a
`branch_depth = 1` fork to its target. The drained request appears
in the canonical_transcript as a branch turn with `parent_id`
pointing to the original primary_turn message that emitted it
(preserving traceability across rounds), even though the originating
round has closed.

At most one queued request is drained per round (`max_deferred_drains_per_round
= 1` by default, configurable). Rationale: bounds the queue's
ability to monopolize a round, and gives the canonical_transcript a
predictable shape — at most one drain branch per round, always at
round opening.

**Queue lifetime.** Cross-round. The queue persists across rounds
within a session; it is discarded only on session termination.

**Queue length cap.** A bounded `max_deferred_queue_length` (default
8, configurable) prevents unbounded growth. A new defer that would
exceed the cap is dropped (the originating direct_request's payload
is preserved in the canonical_transcript as part of the originating
primary_turn message, but the runtime emits no branch for it). A
dropped defer is recorded as a `dropped_deferred` annotation on the
originating message for traceability; it does NOT trigger
termination (a dropped defer is a runtime decision under load, not
a protocol failure).

**Visibility to the Coordinator.** The `deferred_request` queue's
current contents (target, originator, type — not the full content)
SHOULD be summarized in the context_packet for `coordinator_agent`
so the Coordinator can frame its `verdict.focus` accordingly. The
runtime does NOT honor any specific Coordinator instruction about
queue ordering; the queue is strictly FIFO in MVP.

Rationale for FIFO + single drain per round: simplest deterministic
policy that preserves originator traceability across rounds while
keeping the round structure intact. The Coordinator's role
(ADR-005) is semantic opinion, not queue management.

### 4.7 Hard caps and runtime termination

The runtime enforces a set of `hard_cap`s independently of any
verdict (R2, ADR-002). Hard caps are checked at three points: (a)
before each provider invocation, (b) after each provider invocation
(consuming the returned usage), and (c) at every state transition.
A hard cap breached at any check point triggers an immediate
`orchestrator_runtime.terminate(reason)` and transition to the
**terminate** phase.

The MVP hard caps and their default behaviors:

- `max_rounds` (default 5) — the maximum round counter. A round
  cannot open if `round + 1 > max_rounds`. Termination reason:
  `budget_exceeded`.
- `max_wallclock_seconds` (default 1800) — cumulative wall-clock
  duration since session **init**. Termination reason: `timeout`.
- `max_total_cost_usd` (default 5.00) — cumulative cost reported by
  `provider_result.usage` across all invocations. Termination
  reason: `budget_exceeded`.
- `max_total_tokens` (default 500_000) — cumulative token count
  reported by `provider_result.usage`. Termination reason:
  `budget_exceeded`.
- `per_agent_token_budget[agent_id]` (optional per-agent cap;
  Pass 1 row #149) — per-agent cumulative token budget,
  matched against the agent's own `usage` across the session.
  Termination reason: `budget_exceeded`. See §8.1 for the
  canonical statement and §5.2 for the config schema field.
- `max_branch_depth` (default 1) — the maximum `branch_depth` a
  fork MAY reach. A request that would exceed this cap defers
  (§4.6); it is not a termination event in itself.
- `max_deferred_queue_length` (default 8) — see §4.6; queue
  overflow drops new defers but does not terminate.
- `max_deferred_drains_per_round` (default 1) — see §4.6; advisory,
  not a termination event.

The defaults are SHOULD-defaults: an implementation MAY pick
different defaults for its target environment. The presence of each
cap as a configurable knob is a MUST; the runtime MUST enforce the
configured value (R2, Pass 1 rows #87, #106, #149).

Runtime termination reasons (the canonical seven-value enum,
ratified in §8.5):

- `budget_exceeded` — a budget cap (`max_rounds`,
  `max_total_cost_usd`, `max_total_tokens`, or a
  `per_agent_token_budget[agent_id]` entry) was reached.
- `timeout` — `max_wallclock_seconds` was reached.
- `schema_error` — an emitted `structured_output` failed schema
  validation and retries were exhausted (§4.9).
- `provider_unrecoverable` — a `provider_adapter` invocation failed
  in a non-retriable way (§4.9).
- `user_cancel` — the host signaled cancellation via the runtime's
  cancellation channel (out of scope for §4 mechanics; see §8).
- `user_input_required` — `verdict.next_action = request_user_input`
  in batch mode (§4.4).
- `external_research_required` — `verdict.next_action =
  request_external_research` in batch mode (§4.4).

The two reasons `user_input_required` and `external_research_required`
were originally surfaced by §4 and have been integrated into the
canonical termination contract in §8.5.

Runtime termination is a runtime decision, never a verdict decision
(ADR-002). The `coordinator_agent` can recommend `finalize` and the
runtime can still trigger `terminate(budget_exceeded)`; the
`coordinator_agent` can recommend `continue` and the runtime can
still trigger `terminate(timeout)`. The two pathways are
independent (§2.5 Boundary, §2.6).

### 4.8 Synthesis attempt

On entry to **finalize**, the runtime attempts to produce a
`synthesis` message (§2.6, R2). The attempt consists of one
provider invocation against `coordinator_agent` with a
synthesis-shaped context_packet (a packet whose §4.3 minimum
includes the most recent verdict plus the cumulative
`resolved_disagreements` and `unresolved_disagreements` collected
across the session). The returned message is appended to the
canonical_transcript as the session's `synthesis`.

Synthesis fallback conditions — the runtime MUST fall back to
**terminate** with a `termination_artifact` if any of the following
hold during the synthesis attempt:

- the provider invocation fails in a non-retriable way (mapped to
  reason `provider_unrecoverable`);
- the provider returns content that fails schema validation and
  retries are exhausted (mapped to reason `schema_error`);
- a hard_cap is breached during or before the synthesis invocation
  (mapped to the matching cap's reason — `budget_exceeded` or
  `timeout`);
- the host signals cancellation (mapped to reason `user_cancel`).

A `termination_artifact` (§2.6) records: the termination `reason`,
the final `round` number reached, the cumulative usage counters,
the most recent verdict, the cumulative `unresolved_disagreements`,
and a digest of the canonical_transcript sufficient to identify the
session. A session ends with exactly one of: a `synthesis` message
in the canonical_transcript, or a `termination_artifact` persisted
alongside it (R2).

Synthesis attempt rules:

- On transition to **finalize**, the runtime MUST attempt synthesis
  exactly once. If the attempt fails for any of the four fallback
  conditions above, the runtime falls back to a
  `termination_artifact` and does not retry (R2).
- On transition to **terminate** from any other path (a hard-cap
  breach during deliberation, `request_user_input`,
  `request_external_research`, unrecoverable failure), the runtime
  MAY attempt synthesis once if configuration enables a
  "synthesize-on-terminate" mode. The MVP default is: do not
  attempt synthesis on these paths; produce the
  `termination_artifact` directly. The runtime MUST NOT loop
  retrying synthesis under any condition.

### 4.9 Active-panel mutation and failure handling

In MVP, the `active_deliberation_panel` is fixed at session
**selector** exit and immutable for the remainder of a healthy
session (R3). Spontaneous expansion is forbidden (Pass 1 row #121,
MUST NOT). The runtime MUST NOT autonomously create new personas
during a session (Pass 1 row #122, MUST NOT).

Panel contraction on unrecoverable agent failure is governed by a
configurable knob `on_agent_failure ∈ {terminate, continue_without}`,
default `terminate`. An "unrecoverable agent failure" is:

- a `provider_unrecoverable` error on a single agent across the
  configured retry budget (e.g. authentication failure, persistent
  rate-limit exhaustion, repeated 5xx beyond retry cap); or
- a `schema_error` on the same agent across the configured retry
  budget (repeated malformed `structured_output` or out-of-enum
  fields).

On `on_agent_failure = terminate` (default): the runtime invokes
`terminate(reason = provider_unrecoverable)` or
`terminate(reason = schema_error)` as matched. The session ends
with a `termination_artifact`.

On `on_agent_failure = continue_without`: the runtime removes the
failed agent from `active_deliberation_panel` for the remainder of
the session. Current-round semantics:

- If the failure occurs **during the failed agent's primary_turn
  invocation** in the current round: the runtime skips the failed
  agent's slot, records a `panel_contraction` annotation
  (canonical_transcript message of type `panel_contraction` with
  the failed agent's identifier, the reason, the current `round`,
  and the current `turn_index`), and continues the current round
  with the next agent in declared order. The round still closes
  with a normal `coordination_turn` over whichever panel members
  did contribute. The `turn_index` counter does not skip — the
  `panel_contraction` annotation occupies one `turn_index` slot.
- If the failure occurs **before the failed agent's primary_turn
  would have fired** (e.g. an agent that fails on a deferred-queue
  drain at a later round's open): the contraction takes effect
  immediately for the round in progress; the `panel_contraction`
  annotation is recorded and the round continues with the next
  panel agent (or the coordination_turn if no panel agents remain
  in this round).

The declared order is preserved by index: subsequent rounds iterate
the contracted panel in original declared order with the failed
slot omitted. If the panel contracts to zero panel members, the
runtime MUST `terminate(reason = provider_unrecoverable)` (a
session cannot proceed without a panel).

Other failure modes:

- **Transient provider error** (timeouts, retriable 5xx): the
  runtime retries up to the configured per-invocation retry budget
  with exponential backoff (mechanism specified in §6 Pass 5). On
  budget exhaustion, the failure is unrecoverable (above).
- **Schema validation failure** on a single response: the runtime
  retries up to the configured retry budget. The retry packet MAY
  include a corrective annotation (mechanism specified in §6).
- **Failed coordination_turn**: same as agent failure but the
  `coordinator_agent` cannot be replaced or skipped. On
  unrecoverable failure of the coordinator, the runtime MUST
  `terminate(reason)` with the matched reason
  (`provider_unrecoverable` or `schema_error`). A session cannot
  proceed without a Coordinator under ADR-005; `on_agent_failure =
  continue_without` does not apply to the coordinator.
- **Tool failure** during an agent invocation: a `tool_event` may
  carry an error (§2.8); whether the runtime retries is governed by
  the provider adapter contract (§6). At the protocol level, a
  failed tool does not terminate the session unless it escalates to
  `provider_unrecoverable`.

Other recovery actions (`summarize context`, `replace agent`, live
`pause`, `request human intervention`) are v1+ (Pass 1 row #145);
MVP supports the contraction knob and the termination paths above.

### 4.10 Scheduler invariants

The runtime's scheduler exhibits the following invariants:

1. **Single-threaded.** Exactly one `provider_adapter.invoke` is
   in flight at any instant (ADR-001). The runtime does not
   parallelize across panel members, rounds, branches, or
   sessions.
2. **Declared-order dispatch.** Within a round, primary_turns
   dispatch in `active_deliberation_panel` declared order. Order is
   immutable across rounds in MVP (§4.2).
3. **Coordinator-last.** Within a round, the `coordination_turn`
   always follows all panel `primary_turn`s. The coordinator does
   not take primary_turns (R1, ADR-005).
4. **Branch closure before round advance.** A branch dispatched
   during a primary_turn closes (its branch turn is appended)
   before the round advances to the next panel member (§4.5).
5. **No B→C trigger.** A branch turn MUST NOT cause another branch:
   its `direct_request` entries are recorded as
   `suggested_followups` annotations and surfaced to the
   `coordinator_agent` via context_packet; the runtime does not
   route them (§4.5).
6. **No transcript reinjection by the Coordinator.** The
   canonical_transcript is owned by the runtime, mutated only by
   `canonical_transcript.append` after each invocation or by
   runtime annotations (`panel_contraction`, `dropped_deferred`,
   schema-failure records) the runtime writes itself (§2.4,
   ADR-005). The Coordinator opines via `verdict`; it does not
   mutate, reorder, or filter the transcript.
7. **Deterministic over `provider_result` sequence.** Given a fixed
   configuration, panel declaration, and `provider_result`
   sequence, the runtime takes the same actions in the same order
   (§2.7 `scheduler_determinism`, A2, N3). Outputs are replayable
   via `transcript_replay`; full `execution_replay` requires
   pinning every non-deterministic source (provider, model,
   sampling parameters, cache, tool environment).
8. **Hard-cap supremacy.** A `hard_cap` breach always overrides any
   `verdict.next_action` (ADR-002). The runtime MUST NOT open a
   round, dispatch a fork, or attempt a synthesis invocation if
   doing so would breach a hard cap. The runtime checks hard caps
   before each invocation and after each invocation has reported
   `usage`.
9. **Canonical_transcript as sole source of truth.** Every
   downstream consumer — packet derivation, synthesis,
   `transcript_replay`, audit — reads from the canonical_transcript
   (§2.4, ADR-005).

### 4.11 Pseudocode

The following pseudocode describes the MVP runtime scheduler at the
level of detail an implementor needs to translate into any
language. It references `§5` schema field names
(`provider_result`, `verdict.next_action`, `direct_request.target`,
`message.turn_index`) and `§6` adapter calls (`provider_adapter.invoke`)
but does not define them. Indented blocks denote scoping; comments
on lines beginning with `//` explain rationale.

```text
// Procedures return one of:
//   ok                  — operation completed; session continues
//   terminated(reason)  — session has terminated; caller must propagate
//   contracted(agent)   — agent was contracted out of active_panel;
//                         caller continues with next agent in declared order

procedure run_session(config):
    session := init_session(config)              // §4.1 init
    session.active_panel, session.coordinator := selector(config)
                                                 // §4.1 selector; MVP strategy = fixed
    transcript      := session.canonical_transcript
    deferred_queue  := empty_fifo()
    session.round   := 0
    session.usage   := zero_usage()
    session.last_verdict := none
    session.cumulative_unresolved := empty_list()

    transcript.append(message(
        speaker      := config.originator,
        type         := problem_statement,
        content      := config.problem,
        round        := 0,
        turn_index   := 0,
        branch_depth := 0,
        parent_id    := none,
        usage        := zero_usage()
    ))

    loop:                                        // §4.1 deliberation phase
        outcome := check_hard_caps(session)      // §4.7 pre-round
        if outcome is terminated(reason):
            return finalize_terminate(session, reason)

        session.round := session.round + 1
        if session.round > config.max_rounds:
            return finalize_terminate(session, budget_exceeded)

        turn_index := 0

        // §4.2 step 2 / §4.6: at most one drain at round open
        if not deferred_queue.empty and config.max_deferred_drains_per_round > 0:
            drained := deferred_queue.pop_head()
            turn_index := turn_index + 1
            outcome := dispatch_branch(
                session, drained.target, drained,
                session.round, turn_index, drained.parent_id
            )
            if outcome is terminated(reason):
                return finalize_terminate(session, reason)
            // contracted(agent) on drained.target: continue; the
            // panel_contraction annotation has been appended already

        // §4.2 step 3: iterate panel in declared order
        for each agent in session.active_panel.declared_order:
            outcome := check_hard_caps(session)
            if outcome is terminated(reason):
                return finalize_terminate(session, reason)

            turn_index := turn_index + 1
            packet := derive_context_packet(
                session, agent, session.round, turn_index, deferred_queue
            )                                    // §4.3
            invoke_result := provider_adapter.invoke(request(agent, packet))   // §6

            outcome, result := handle_invocation_result(
                session, invoke_result, agent
            )
            if outcome is terminated(reason):
                return finalize_terminate(session, reason)
            if outcome is contracted(_):
                transcript.append(message(
                    speaker      := runtime,
                    type         := panel_contraction,
                    content      := { agent_id := agent.id,
                                      reason   := contraction_reason(invoke_result) },
                    round        := session.round,
                    turn_index   := turn_index,
                    branch_depth := 0,
                    parent_id    := none,
                    usage        := zero_usage()
                ))
                continue                          // skip this agent's primary_turn

            // outcome is ok; `result` is the (possibly retried) success
            session.usage := session.usage + result.usage  // §4.7 post-invocation

            msg := message(
                speaker      := agent.id,
                type         := primary_turn,
                content      := result.structured_output,
                round        := session.round,
                turn_index   := turn_index,
                branch_depth := 0,
                parent_id    := none,
                usage        := result.usage
            )
            transcript.append(msg)

            outcome := check_hard_caps(session)   // §4.7 post-invocation
            if outcome is terminated(reason):
                return finalize_terminate(session, reason)

            // §4.5 fork dispatch over the turn's direct_requests.
            // Top-level structured_output shape was already enforced by
            // handle_invocation_result via §6.5 (malformed_response →
            // §6.7 corrective retry, then §4.9 if exhausted); at this
            // point the message is appended and request-level checks
            // are purely about routability: target must name a current
            // active_panel member other than the originator, must not
            // be `coordinator_agent`, and must not be an undeclared id.
            // A request that fails routability is annotated on `msg`
            // via `schema_failure`, never dispatched, never enqueued.
            dispatched_inline := false
            for each request in result.structured_output.direct_requests:
                if not is_routable_direct_request(
                       request, originator := msg.speaker,
                       active_panel := session.active_panel,
                       coordinator  := session.coordinator
                   ):
                    outcome := handle_invalid_request(session, request, msg)
                    if outcome is terminated(reason):
                        return finalize_terminate(session, reason)
                    continue

                if not dispatched_inline
                   and session.branch_depth_of(msg) + 1 ≤ config.max_branch_depth:
                    turn_index := turn_index + 1
                    outcome := dispatch_branch(
                        session, request.target, request,
                        session.round, turn_index, msg.id
                    )
                    if outcome is terminated(reason):
                        return finalize_terminate(session, reason)
                    dispatched_inline := true
                else:
                    enqueue_deferred(deferred_queue, request, msg, config)
                    // overflow handled inside enqueue_deferred (drop +
                    // dropped_deferred annotation per §4.6)

        // §4.2 step 4: coordination_turn closes the round
        outcome := check_hard_caps(session)
        if outcome is terminated(reason):
            return finalize_terminate(session, reason)

        turn_index := turn_index + 1
        packet := derive_context_packet(
            session, session.coordinator, session.round, turn_index, deferred_queue
        )
        invoke_result := provider_adapter.invoke(
            request(session.coordinator, packet)
        )
        outcome, coord_result := handle_invocation_result(
            session, invoke_result, session.coordinator
        )
        if outcome is terminated(reason):
            return finalize_terminate(session, reason)
        // §4.9: coordinator cannot be contracted; outcome here is ok

        session.usage := session.usage + coord_result.usage

        verdict := parse_verdict(coord_result.structured_output)
        // schema validation already enforced by handle_invocation_result;
        // a coordinator schema failure on retry-exhaustion has already
        // terminated above via apply_agent_failure_policy
        session.last_verdict := verdict
        session.cumulative_unresolved := merge(
            session.cumulative_unresolved, verdict.unresolved_disagreements
        )

        transcript.append(message(
            speaker      := session.coordinator.id,
            type         := coordination_turn,
            content      := verdict,
            round        := session.round,
            turn_index   := turn_index,
            branch_depth := 0,
            parent_id    := none,
            usage        := coord_result.usage
        ))

        outcome := check_hard_caps(session)       // §4.7 post-invocation
        if outcome is terminated(reason):
            return finalize_terminate(session, reason)

        // §4.4 verdict handling
        switch verdict.next_action:
            case continue:
                continue loop                     // open next round
            case finalize:
                return attempt_finalize(session)  // §4.8
            case request_user_input:
                return finalize_terminate(session, user_input_required)
            case request_external_research:
                return finalize_terminate(session, external_research_required)


procedure dispatch_branch(session, target_id, request, round, turn_index, parent_id):
    target := resolve_agent(session.active_panel, target_id)
    if target is none:                            // §4.5 request-level schema failure
        annotate_schema_failure(
            session.canonical_transcript, parent_id, request,
            reason := "target not in active_panel"
        )
        return ok                                 // §4.5: request-level failure does NOT
                                                  // escalate; originator turn remains valid

    outcome := check_hard_caps(session)            // §4.7 pre-invocation
    if outcome is terminated(reason):
        return terminated(reason)

    packet := derive_context_packet_for_branch(
        session, target, round, request, parent_id
    )                                              // §4.3
    invoke_result := provider_adapter.invoke(request(target, packet))   // §6

    outcome, result := handle_invocation_result(session, invoke_result, target)
    if outcome is terminated(reason):
        return terminated(reason)
    if outcome is contracted(_):
        session.canonical_transcript.append(message(
            speaker      := runtime,
            type         := panel_contraction,
            content      := { agent_id := target.id,
                              reason   := contraction_reason(invoke_result) },
            round        := round,
            turn_index   := turn_index,
            branch_depth := 1,
            parent_id    := parent_id,
            usage        := zero_usage()
        ))
        return contracted(target)

    // outcome is ok; `result` is the (possibly retried) successful result
    session.usage := session.usage + result.usage

    outcome := check_hard_caps(session)            // §4.7 post-invocation
    if outcome is terminated(reason):
        return terminated(reason)

    suggested_followups := extract_direct_requests(result.structured_output)
    // §4.5: branch-origin direct_requests are recorded as annotations,
    // never dispatched and never deferred — enforces B MUST NOT trigger C
    branch_msg := message(
        speaker             := target.id,
        type                := branch_turn,
        content             := result.structured_output,
        round               := round,
        turn_index          := turn_index,
        branch_depth        := 1,                  // MVP cap; nested branches forbidden
        parent_id           := parent_id,
        usage               := result.usage,
        suggested_followups := suggested_followups
    )
    session.canonical_transcript.append(branch_msg)
    return ok                                      // branch closes; caller resumes


procedure handle_invocation_result(session, invoke_result, agent):
    // Returns (outcome, result):
    //   (ok, success_result)         — proceed with success_result.usage / content
    //   (terminated(reason), none)   — caller must propagate termination
    //   (contracted(agent), none)    — caller annotates and skips this agent
    result := invoke_result
    if result.error and retriable(result.error):
        retried := retry_with_backoff(session, agent, result.request,
                                      config.per_agent_retry_budget)
        if retried.success:
            return (ok, retried.result)
        result := retried.last_attempt              // for failure-policy mapping
    if result.error and unrecoverable(result.error):
        return (apply_agent_failure_policy(session, agent, result.error), none)
    if not schema_valid(result.structured_output):
        retried := retry_with_schema_correction(
            session, agent, result, config.per_agent_retry_budget
        )                                           // §6 mechanics
        if retried.success:
            return (ok, retried.result)
        return (apply_agent_failure_policy(session, agent, schema_error), none)
    return (ok, result)


procedure apply_agent_failure_policy(session, agent, error):
    reason := matched_reason(error)                // provider_unrecoverable | schema_error
    if agent is session.coordinator:
        // §4.9: coordinator cannot be contracted
        return terminated(reason)
    if config.on_agent_failure == terminate:
        return terminated(reason)
    if config.on_agent_failure == continue_without:
        session.active_panel.remove(agent)
        if session.active_panel.empty:
            return terminated(provider_unrecoverable)
        return contracted(agent)


procedure attempt_finalize(session):               // §4.8
    outcome := check_hard_caps(session)
    if outcome is terminated(reason):
        return finalize_terminate(session, reason)

    packet := derive_synthesis_packet(session)     // §4.3 + §4.8 minimum
    invoke_result := provider_adapter.invoke(
        request(session.coordinator, packet)
    )

    outcome, result := handle_invocation_result(
        session, invoke_result, session.coordinator
    )
    if outcome is terminated(reason):
        return finalize_terminate(session, reason)
    // §4.9: coordinator cannot be contracted; outcome here is ok
    // (handle_invocation_result already terminates on coord schema/provider failure)

    session.usage := session.usage + result.usage

    outcome := check_hard_caps(session)            // §4.7 post-synthesis usage
    if outcome is terminated(reason):
        return finalize_terminate(session, reason)

    session.canonical_transcript.append(message(
        speaker      := session.coordinator.id,
        type         := synthesis,
        content      := result.structured_output,
        round        := session.round,
        turn_index   := session.last_turn_index_in(session.round) + 1,
        branch_depth := 0,
        parent_id    := none,
        usage        := result.usage
    ))
    return persist(session)                        // §4.1 persist


procedure finalize_terminate(session, reason):
    artifact := termination_artifact(
        reason                   := reason,
        final_round              := session.round,
        cumulative_usage         := session.usage,
        most_recent_verdict      := session.last_verdict,
        unresolved_disagreements := session.cumulative_unresolved,
        transcript_digest        := digest(session.canonical_transcript)
    )
    persist(session, artifact)                   // §4.1 persist
    return artifact
```

The pseudocode is language-agnostic by construction: imperative
steps, indented scoping, no semicolons, no language-specific
literals, no provider-specific calls. It references the
`provider_adapter.invoke` contract (§6, Pass 5), the
`canonical_transcript.append` operation (§2.4), and the
`context_packet` derivation (§4.3) without defining them.

### 4.12 Failure handling (cross-reference)

The runtime's failure taxonomy and recovery strategies are owned by
§8 (Operational Controls, Pass 6). §4 describes only the
protocol-level decisions: when a failure becomes unrecoverable, the
runtime terminates per §4.7 with the matching reason; when an
agent unrecoverably fails, §4.9 applies; when synthesis cannot
complete, §4.8 falls back to a `termination_artifact`. The
mechanics of retry budgets, exponential backoff, fallback models,
and security policies are §8 concerns.

### 4.13 Vocabulary introduced by §4 (status table)

§4 references several terms whose schema homes live in §5 and
whose policy homes live in §8. The cross-reference table below
records the final-state location for each item; no further
promotion is pending.

**Phase enum:** `init`, `selector`, `deliberation`, `finalize`,
`terminate`, `persist`. Used in §4.1. Captured in §2.1
glossary at the `session` entry; the phase set is a
descriptive runtime-state breakdown rather than a schema enum.

**Message-type enum:** `problem_statement`, `primary_turn`,
`coordination_turn`, `branch_turn`, `synthesis`,
`panel_contraction`. The first five are session content; the
last is a runtime annotation written by the runtime (not by
an LLM agent). All carry the canonical message schema fields
(§2.4). Schema home: `message.schema.json` — `type` field
enum (§5.4).

**Termination-reason enum:** the seven-value canonical set
`{budget_exceeded, schema_error, provider_unrecoverable,
user_cancel, timeout, user_input_required,
external_research_required}` is ratified in §8.5 and surfaced
on `TerminationArtifact` (§5.8).

**Configuration knobs introduced by §4:**

- `max_deferred_queue_length` (default 8) — §4.6 queue cap;
  schema home §5.2 `runtime` block.
- `max_deferred_drains_per_round` (default 1) — §4.6 drain
  rate; schema home §5.2 `runtime` block.
- `on_agent_failure ∈ {terminate, continue_without}` (default
  `terminate`) — §4.9 panel-contraction policy; schema home
  §5.2 `runtime` block.
- `per_agent_retry_budget` (default 2; mechanism in §6) —
  retry count before a single-invocation failure is
  unrecoverable; schema home §5.2 `runtime` block.

**Message and direct_request annotations:**

- `suggested_followups` — a list on a `branch_turn` message
  recording branch-origin `direct_request` entries the runtime
  did not route (§4.5); schema home §5.4 `message.schema.json`.
- `dropped_deferred` — an annotation on a `primary_turn`
  message recording a `direct_request` whose deferral was
  rejected by the queue cap (§4.6); schema home §5.4.
- `schema_failure` — an annotation on `primary_turn` /
  `branch_turn` messages recording a schema-validation
  failure event the runtime observed during retry handling
  (§4.5, §4.9); schema home §5.4.

**`direct_request` shape:** the fields `target`, `type`,
`content` and the per-turn `structured_output` carrying zero
or more `direct_request` entries are defined in §5.5
(`turn_structured_output.schema.json` +
`direct_request.schema.json`). Verdict payloads for
`request_user_input` / `request_external_research` live as
`Verdict.user_input_request` / `Verdict.external_research_request`
in §5.6.

---

## 5. Schemas

**[Core MVP]**

This section fixes the JSON shape of every artifact a Symposium
runtime produces, consumes, or exchanges. Every example block is a
valid instance of the schema it exemplifies (mechanically verified,
§5.15). Field semantics that depend on runtime behavior are stated
in §4 and only referenced here, never re-defined.

The canonical schemas live in
[`docs/schemas/v1.0.0/`](schemas/v1.0.0/) as standalone JSON Schema
Draft 2020-12 documents. The fragments below are representative
sketches, not authoritative — for validation, use the schema files
directly. Cross-references between schemas use `$ref` with the
relative filename (e.g. `"$ref": "message.schema.json"`).

### 5.1 Schema versioning policy

Every persisted artifact MUST carry a top-level `schema_version`
field. Format: SemVer `MAJOR.MINOR.PATCH` (regex
`^[0-9]+\.[0-9]+\.[0-9]+$`). The Pass 4 schemas are `1.0.0`.

Compatibility rules (strict-versioning):

- **PATCH** — bug fix in the schema; no shape change.
- **MINOR** — additive field only at well-defined extension points.
  Because every schema in this version sets
  `additionalProperties: false` for safety against side-channel
  injection (ADR-003), MINOR additions will fail validation against
  the prior MINOR's schema. The policy is therefore
  **strict-versioned**: an artifact validates against *its
  declared* `schema_version`, not against any other. A v1.0.0
  consumer that encounters a v1.1.0 artifact MUST NOT silently
  ignore unknown fields — it MUST refuse to validate and either
  upgrade to the v1.1.0 schema or consult `migrate(v1.1.0 → v1.0.0)`
  to downcast.
- **MAJOR** — breaking change. A MAJOR bump SHOULD ship a
  `migrate(old, new) -> artifact` reference written in §5 or §6.
  Consumers MUST refuse to load an artifact whose MAJOR version
  they do not understand.

Rationale for strict-versioning: standard SemVer "ignore unknown
fields" forward-compat would require relaxing
`additionalProperties` somewhere, which would re-open the ADR-003
side-channel surface. The spec prioritises injection resistance
over forward-compat.

Top-level `schema_version` carriers: `Config`, `Artifact`,
`TerminationArtifact`. Embedded schemas (`Persona`, `Message`,
`DirectRequest`, `Verdict`, `ProviderResult`,
`TurnStructuredOutput`, `Synthesis`, `ContextPacket`,
`SelectorOutput`) inherit the parent's version.

### 5.2 Config

`Config` ([`config.schema.json`](schemas/v1.0.0/config.schema.json))
drives the session: selector, panel, coordinator, budget, runtime
knobs. Vendor model identifiers appear only inside
`examples/configs/*.yaml`, never in this schema body (rule N4) —
the `provider` and `model` enums are intentionally OPEN strings.

Required top-level fields: `schema_version`, `session_id`,
`originator`, `problem_statement`, `selector`, `agents`,
`coordinator`, `budget`, `runtime`.

Selector block (R3, §4.1):

- `strategy ∈ {fixed, rules, llm}` — CLOSED enum. MVP ships
  `fixed`; `rules` and `llm` are reserved for v1+. `selector_budget`
  is required when `strategy = llm`.
- `default_deliberation_panel` — ordered list of agent ids.
- `coordinator_agent` — agent id (ADR-005).

Per-agent config (`AgentConfig`):
`{id, persona_ref, provider, model, reasoning_effort?, tools?,
output_requirements?, retry_budget?}`. `persona_ref` is a string id
or an inline `Persona` object.

Budget block (§4.7 hard caps): `max_total_tokens`,
`max_total_cost_usd`, `max_rounds`, `max_wallclock_seconds`
(required); `per_agent_token_budget` (optional per-agent token
cap map), `selector_budget` (optional;
`{max_tokens?, max_cost_usd?}` mirroring `selector.selector_budget`
per M4).

Runtime block (§4.5–§4.9): `max_branch_depth` (default 1),
`max_deferred_queue_length` (default 8),
`max_deferred_drains_per_round` (default 1), `on_agent_failure ∈
{terminate, continue_without}` (CLOSED; default `terminate`),
`per_agent_retry_budget` (default 2), `synthesize_on_terminate`
(default `false`).

```jsonc
// Representative fragment (full schema: config.schema.json)
{
  "schema_version": "1.0.0",
  "session_id": "demo-2026-05-25-001",
  "originator": "user:roberto",
  "problem_statement": "…",
  "selector": {
    "strategy": "fixed",
    "default_deliberation_panel": ["logician", "researcher", "critic"],
    "coordinator_agent": "coordinator"
  },
  "agents": [
    { "id": "logician", "persona_ref": "logician",
      "provider": "provider_a", "model": "reasoning_model" }
  ],
  "coordinator": { "id": "coordinator", "persona_ref": "coordinator",
                   "provider": "provider_b", "model": "reasoning_model" },
  "budget": {
    "max_total_tokens": 500000, "max_total_cost_usd": 5.0,
    "max_rounds": 5, "max_wallclock_seconds": 1800
  },
  "runtime": {
    "max_branch_depth": 1, "max_deferred_queue_length": 8,
    "max_deferred_drains_per_round": 1,
    "on_agent_failure": "terminate",
    "per_agent_retry_budget": 2, "synthesize_on_terminate": false
  }
}
```

### 5.3 Persona

`Persona` ([`persona.schema.json`](schemas/v1.0.0/persona.schema.json))
is a tagged union via `persona_class ∈ {horizontal, domain}` (§2.3,
A5). The schema enforces the split via `if/then` on
`persona_class`:

- **Common required** (both): `id`, `reasoning_scope`,
  `reasoning_style`, `behavioral_constraints` (non-empty),
  `failure_modes` (non-empty). `output_requirements` optional but
  RECOMMENDED.
- **`horizontal`**: MUST NOT carry `domain_scope`,
  `forbidden_domains`, or `must_delegate`.
- **`domain`**: MUST carry `domain_scope`, `forbidden_domains`,
  and `must_delegate`.

Optional `status ∈ {experimental, stable, deprecated, archived}`
reserved for the v1+ persona registry.

```jsonc
{ "persona_class": "horizontal", "id": "logician",
  "reasoning_scope": "formal-structural",
  "reasoning_style": "mathematical rigor",
  "behavioral_constraints": ["cite sources"],
  "failure_modes": ["over-confident speculation"] }
```

```jsonc
{ "persona_class": "domain", "id": "legal_analyst",
  "reasoning_scope": "evidence-based", "reasoning_style": "doctrinal",
  "behavioral_constraints": ["cite controlling statute"],
  "failure_modes": ["over-extending precedent"],
  "domain_scope": ["EU GDPR", "US CCPA"],
  "forbidden_domains": ["software_architecture", "machine_learning"],
  "must_delegate": { "software_architecture": "engineer",
                     "machine_learning": "researcher" } }
```

### 5.4 Message

`Message` ([`message.schema.json`](schemas/v1.0.0/message.schema.json))
is the single entry shape of the canonical_transcript (§2.4). The
`type` discriminator selects the `content` shape and the per-type
optional annotations.

Required: `id`, `speaker`, `type`, `content`, `round`, `turn_index`,
`branch_depth`, `timestamp`, `usage`.

Type enum (CLOSED, §4.13):

| type | content shape | round | branch_depth | parent_id | speaker |
|------|----------------|-------|--------------|-----------|---------|
| `problem_statement` | string | 0 | 0 | null | originator |
| `primary_turn` | `TurnStructuredOutput` | ≥1 | 0 | null | panel agent id |
| `coordination_turn` | `Verdict` | ≥1 | 0 | null | coordinator id |
| `branch_turn` | `TurnStructuredOutput` | ≥1 | 1 | non-null string | target agent id |
| `synthesis` | `SynthesisContent` (§5.8) | ≥1 | 0 | null | coordinator id |
| `panel_contraction` | `{agent_id, reason}` | ≥1 | 0/1 | — | `"runtime"` |

Per-type invariants are enforced via `allOf` + `if/then`.
`branch_turn.parent_id` is constrained to a non-null string by the
branch_turn branch.

**Strict content for primary/branch turns**: `content` is
`TurnStructuredOutput` (§5.5) with `additionalProperties: false`.
The runtime canonicalizes a provider's permissive
`structured_output` into this shape before appending to the
canonical_transcript; extra fields are discarded. This is the
schema-level enforcement of ADR-003.

Annotations (all optional, per type):

- `suggested_followups` — array of `DirectRequest`, only on
  `branch_turn` (§4.5 B→C suppression).
- `dropped_deferred` — array of `DirectRequest`, only on
  `primary_turn` (§4.6 queue overflow).
- `schema_failure` — array of `{offending_request, reason}`, on
  `primary_turn` or `branch_turn` (§4.5 request-level failures).

`turn_index` is the per-round shared monotonic counter (§4.5);
canonical_transcript ordering is sorted by `(round, turn_index)` in
execution order (§4.10 invariant 7).

```jsonc
// primary_turn example
{
  "id": "msg-001",
  "speaker": "logician",
  "type": "primary_turn",
  "content": {
    "text": "Two structural assumptions are doing load-bearing work …",
    "direct_requests": [
      { "target": "researcher", "type": "verification",
        "content": "Is the dataset observational?" }
    ]
  },
  "parent_id": null,
  "round": 1, "turn_index": 1, "branch_depth": 0,
  "timestamp": "2026-05-25T10:00:05Z",
  "usage": { "prompt_tokens": 2200, "completion_tokens": 380,
             "total_tokens": 2580, "cost_usd": 0.029 }
}
```

### 5.5 TurnStructuredOutput and DirectRequest

`TurnStructuredOutput`
([`turn_structured_output.schema.json`](schemas/v1.0.0/turn_structured_output.schema.json))
is the strict shape of `Message.content` for `primary_turn` and
`branch_turn`. Required: `text` (non-empty). Optional:
`direct_requests` (array of `DirectRequest`).
`additionalProperties: false`.

`DirectRequest`
([`direct_request.schema.json`](schemas/v1.0.0/direct_request.schema.json))
is the only sanctioned inter-agent control signal (ADR-003, §2.5).
Required: `target`, `type`, `content`. `additionalProperties:
false`. `type` is an OPEN string at the schema level; the spec
recommends at least `{question, verification, critique,
feasibility, delegation, clarification}`.

`content` is `string | object`; runtime parsing of `content` for
further control signals is forbidden (ADR-003) — the routed signal
is the `target`/`type` of the enclosing `DirectRequest`.

```jsonc
{ "target": "researcher", "type": "verification",
  "content": "Is the dataset observational?" }
```

### 5.6 Verdict

`Verdict` ([`verdict.schema.json`](schemas/v1.0.0/verdict.schema.json))
is the machine-readable object emitted by `coordinator_agent` at
the end of every `coordination_turn` (ADR-002, ADR-005). Runtime
handling of each `next_action` value lives in §4.4.

Required: `next_action`, `rationale`, `confidence`, `focus`,
`next_agents`, `resolved_disagreements`,
`unresolved_disagreements`.

`next_action` is a **CLOSED enum of exactly four values** (ADR-002):

```
continue | finalize | request_user_input | request_external_research
```

No other values appear in the schema body; the negative-test pass
(§5.15) confirms that `abort`, `final_answer_ready`, and any other
value reject.

`confidence` is required, `number` in `[0.0, 1.0]`. Per-message
confidence is v1, not required by the MVP `Message` schema.

`resolved_disagreements` and `unresolved_disagreements` are
required arrays (M5). Empty arrays are valid. Each
`resolved_disagreements[]` carries `{topic, resolution,
agents_involved?}`; each `unresolved_disagreements[]` carries
`{topic, positions[], blocker?}` where `positions[]` is a
non-empty list of `{agent, claim}`.

Optional payload fields, **bidirectionally exclusive**:

- `user_input_request: {question, context?, blocking?}` — REQUIRED
  when `next_action = request_user_input`; forbidden otherwise.
- `external_research_request: {query, rationale?, suggested_sources?}`
  — REQUIRED when `next_action = request_external_research`;
  forbidden otherwise.

These minimal sub-shapes will be elaborated in v1.

```jsonc
{ "next_action": "continue",
  "rationale": "Round surfaced two competing causal hypotheses …",
  "confidence": 0.55,
  "focus": "Adjudicate selection-bias vs. unobserved-confounder hypotheses.",
  "next_agents": ["researcher", "critic"],
  "resolved_disagreements": [{
    "topic": "Whether the dataset is observational",
    "resolution": "Confirmed observational.",
    "agents_involved": ["researcher", "logician"]
  }],
  "unresolved_disagreements": [{
    "topic": "Causal direction",
    "positions": [
      {"agent": "logician", "claim": "X→Y under the structural assumption"},
      {"agent": "critic",   "claim": "Reverse causation cannot be excluded"}
    ],
    "blocker": false
  }]
}
```

### 5.7 ProviderResult

`ProviderResult`
([`provider_result.schema.json`](schemas/v1.0.0/provider_result.schema.json))
is the response side of `provider_adapter.invoke(request)` (§2.8).
§6 owns the request side of the adapter contract; §5 defines
only the response-side schema that §4 references.

Required: `messages`, `tool_events`, `usage`, `finish_reason`,
`structured_output`, `raw`, `error`.

- `usage` — `{prompt_tokens, completion_tokens, total_tokens,
  cost_usd}`, all non-negative.
- `finish_reason` — CLOSED enum `{stop, length, tool_call,
  content_filter, error}`.
- `structured_output` — `object | null`. **Permissive at the
  provider boundary**, canonicalized into a strict per-turn-type
  shape (`TurnStructuredOutput` / `Verdict` /
  `SynthesisContent`) before persisting to the canonical_transcript.
  Permissive at the boundary, strict at the persistence.
- `raw` — `object | null`, schema-opaque.
- `error` — `null` on success, otherwise
  `{kind, message, retriable, details?}`. `kind` is a CLOSED
  12-value enum defined in §6.6 (the adapter contract owns the
  enum; the schema body enforces it).

### 5.8 Synthesis and TerminationArtifact

`Synthesis` ([`synthesis.schema.json`](schemas/v1.0.0/synthesis.schema.json))
is a specialized `Message` of `type = synthesis`. The file fixes
`SynthesisContent`, referenced from `message.schema.json` via
`$ref` on the per-type `if/then` branch.

`SynthesisContent` required fields: `integrated_answer`,
`resolved_disagreements`, `unresolved_disagreements`. Optional:
`confidence`, `open_questions[]`.

`TerminationArtifact`
([`termination_artifact.schema.json`](schemas/v1.0.0/termination_artifact.schema.json))
is a persisted record explaining why a session ended without a
synthesis (§2.6, §4.8). A session ends with **exactly one of**: a
`synthesis` message in `canonical_transcript`, OR a
`TerminationArtifact` persisted alongside it — enforced via the
top-level `Artifact.outcome` `oneOf` (§5.10).

Required: `schema_version`, `reason`, `final_round`,
`cumulative_usage`, `unresolved_disagreements`, `transcript_digest`.

`reason` is the CLOSED termination-reason enum (§4.7):

```
budget_exceeded | schema_error | provider_unrecoverable |
user_cancel | timeout | user_input_required | external_research_required
```

Conditional payload fields (bidirectionally exclusive):

- `pending_user_input_request` — REQUIRED when
  `reason = user_input_required`; forbidden otherwise.
- `pending_external_research_request` — REQUIRED when
  `reason = external_research_required`; forbidden otherwise.

`most_recent_verdict` is `null | Verdict` — null only if
termination occurred before any round closed.

### 5.9 ContextPacket

`ContextPacket`
([`context_packet.schema.json`](schemas/v1.0.0/context_packet.schema.json))
is the per-invocation view derived from the canonical_transcript
(§2.4, §4.3, blocker #3). **Not persisted** — ephemeral. The
schema exists for adapter/test-harness reproducibility (§9
FakeProvider).

Required (§4.3 minimum): `problem_statement`, `round`, `persona`,
`panel_disclosure`, `current_round_messages`.
`panel_disclosure` is an array; each entry carries `id` and
`role_summary` (both required; `additionalProperties: false`)
per Pass-1 row #77 — the invoked agent learns the panel
composition for the round.

Conditional / optional: `most_recent_verdict` (absent in round 1);
`branch_origin: {direct_request, parent_message}` (required when
the invoked agent is a branch agent); `deferred_queue_summary`
(only on coordinator packets, §4.6);
`cumulative_disagreements: {resolved[], unresolved[]}` (only on
synthesis packets, §4.8); `compression_note` (diagnostic).

**Round-trip obligation**: a consumer with the
`canonical_transcript` and the packet-derivation policy MUST be
able to reproduce a `ContextPacket`.

### 5.10 Artifact (top-level run output)

`Artifact` ([`artifact.schema.json`](schemas/v1.0.0/artifact.schema.json))
is the top-level persisted run output at `runs/<session_id>/`.
Bundles `schema_version`, `session_id`, `config`,
`canonical_transcript`, `outcome`, `cumulative_usage`,
`cumulative_unresolved`, `started_at`, `ended_at`.

`outcome` is a discriminated union, **exactly one of**:

- `{kind: "synthesis", synthesis_message_id}` — the synthesis
  content lives in the referenced Message inside
  `canonical_transcript`.
- `{kind: "termination", termination_artifact}` — the inline
  `TerminationArtifact` (§5.8).

**Append-only enforcement — honest gap statement.** The runtime
invariant "every entry's `(round, turn_index)` is strictly greater
than the previous entry's, modulo the round-0 `problem_statement`"
is **not enforceable in JSON Schema Draft 2020-12** without
extensions. The schema enforces:

- Each individual `Message` has a non-negative `round` and
  `turn_index`.
- `round = 0` is reserved for the `problem_statement`.
- `branch_depth ∈ {0, 1}`.
- The single-`outcome` invariant (synthesis xor termination).

The schema **does NOT enforce**:

- Cross-item strict ordering of `(round, turn_index)`.
- That `outcome.synthesis_message_id` points to a `Message` of
  type `synthesis` actually present in `canonical_transcript`.
- That no two messages share the same `(round, turn_index)`.
- That messages are not reordered, rewritten, or deleted between
  reads.

These are the runtime's responsibility on read and on append, per
§4.10 invariants 7 and 9. §9 ships a property test exercising
them; §7.7 specifies the canonical `transcript_digest`
(RFC 8785 + SHA-256) used for tamper-evidence on disk.

### 5.11 Selector output (v1 stub)

`SelectorOutput`
([`selector_output.schema.json`](schemas/v1.0.0/selector_output.schema.json))
is a v1 stub. For MVP `strategy = fixed`, the SelectorOutput is
degenerate and derivable from Config without invocation. Required:
`strategy`, `selected_agents`, `coordinator_agent`. Optional (v1+):
`excluded_agents[]`, `missing_capabilities[]`, `reasoning`.

### 5.12 canonical_transcript vs context_packet boundaries

The two are distinct schemas with two distinct durability
contracts:

| Property | `canonical_transcript` | `context_packet` |
|----------|------------------------|------------------|
| Persisted? | yes (inside `Artifact`) | no (ephemeral) |
| Schema | ordered array of `Message` | structured projection |
| Mutability | append-only (runtime-enforced) | per-invocation, varies |
| Source of truth | yes (§2.4, §4.10 invariant 9) | no |
| Derivable from the other? | no | yes (§4.3 derivation policy) |
| Round-trip target? | n/a | consumer + transcript + policy MUST reproduce |

§4.3 fixes the derivation policy that relates them. A runtime that
loses a `context_packet` loses nothing — it re-derives. A runtime
that loses the `canonical_transcript` loses the session.

### 5.13 Worked example: 2-round session with one fork and one defer

The canonical worked example is at
[`docs/schemas/v1.0.0/examples/worked_example_artifact.json`](schemas/v1.0.0/examples/worked_example_artifact.json),
validated against `artifact.schema.json` per §5.15.

Topology, aligned with §4.5:

- Panel `[logician, researcher, critic]`. Coordinator `coordinator`.
  2 rounds. `max_branch_depth = 1`.

- **Round 1**:
  - `turn_index = 1` — `logician` primary_turn (msg-001) emits
    **two `direct_request` entries**: first to `researcher`,
    second to `critic`. By §4.5 the first dispatches in-line
    (no prior in-line fork in this turn); the second defers
    ("at most one in-line fork per primary_turn").
  - `turn_index = 2` — `researcher` branch_turn (msg-002,
    `parent_id = msg-001`, `branch_depth = 1`). Confirms the
    observational dataset; clean close.
  - `turn_index = 3` — `researcher` primary_turn (msg-003); 0
    requests.
  - `turn_index = 4` — `critic` primary_turn (msg-004); flags
    co-varying interventions.
  - `turn_index = 5` — `coordinator` coordination_turn (msg-005);
    `next_action = continue`. Deferred queue: 1 entry
    (logician → critic).

- **Round 2**:
  - **Drain at round open (§4.6)**: dequeue
    `(logician → critic)` and dispatch as a branch_turn at
    `turn_index = 1`, `branch_depth = 1`, `parent_id = msg-001`.
    This is msg-006.
  - `turn_index = 2` — `logician` primary_turn (msg-007).
  - `turn_index = 3` — `researcher` primary_turn (msg-008).
  - `turn_index = 4` — `critic` primary_turn (msg-009).
  - `turn_index = 5` — `coordinator` coordination_turn (msg-010);
    `next_action = finalize`.
  - `turn_index = 6` — `coordinator` synthesis (msg-011).
    `Artifact.outcome.synthesis_message_id = "msg-011"`.

Invariants the example exercises: declared-order panel iteration;
one in-line fork at `branch_depth = 1`; one deferred request
drained in the next round with cross-round `parent_id`
traceability; CLOSED enums in both verdicts; strict
`(round, turn_index)` ordering; the `outcome` discriminator picks
exactly the `synthesis` branch; `TurnStructuredOutput`'s
`additionalProperties: false` is in force for every primary/branch
turn's content.

### 5.14 Vocabulary absorbed from §4.13 (status table)

§4.13 enumerates the vocabulary §4 introduced. Each item has a
§5 disposition:

| §4.13 item | Disposition | Where |
|------------|-------------|-------|
| **Phase enum** `init` … `persist` | **No schema home** — the Phase enum is a runtime FSM-state breakdown, not a schema field; descriptive coverage lives in §4.1's phase prose. | §4.1 (descriptive) |
| **Message-type enum** | **Defined** as CLOSED enum on `Message.type`. | §5.4 |
| **Termination-reason enum** with `user_input_required`, `external_research_required` | **Defined** as CLOSED enum on `TerminationArtifact.reason`. | §5.8 |
| `max_deferred_queue_length` | **Defined** in `Config.runtime`. | §5.2 |
| `max_deferred_drains_per_round` | **Defined** in `Config.runtime`. | §5.2 |
| `on_agent_failure ∈ {terminate, continue_without}` | **Defined** in `Config.runtime` (CLOSED enum). | §5.2 |
| `per_agent_retry_budget` | **Defined** in `Config.runtime`, overridable per agent. | §5.2 |
| `suggested_followups` annotation | **Defined** as optional array on `Message` (branch_turn). | §5.4 |
| `dropped_deferred` annotation | **Defined** as optional array on `Message` (primary_turn). | §5.4 |
| `schema_failure` annotation | **Defined** as optional array on `Message` (primary/branch). | §5.4 |
| `direct_request` shape | **Defined** as `DirectRequest`. Verdict-payload sub-shapes defined in `Verdict.user_input_request` / `Verdict.external_research_request`. | §5.5, §5.6 |
| "structured research payload" (§4.4) | **Defined** as `Verdict.external_research_request` (minimum: `query`); richer per-research-type sub-schemas deferred to v1. | §5.6 |

Summary: 11 items absorbed in §5 schemas; 1 item (Phase enum)
documented descriptively in §4.1 with no schema home. Zero items
silently dropped.

**Vocabulary introduced by §5 (status table)**: the following
field names are defined in §5's schemas. The current chapter
homes are noted; no further promotion is pending.

- `synthesize_on_terminate` — Config.runtime knob (§5.2);
  referenced in §4.8 prose; §8.4 covers recovery interaction.
- `persona_class` — Persona discriminator (§5.3). §2.3
  describes the split conceptually; the field name is
  introduced here.
- `cumulative_usage`, `cumulative_unresolved`,
  `transcript_digest` — Artifact-level aggregations (§5.10),
  with `transcript_digest` semantics in §7.7.
- `pending_user_input_request`,
  `pending_external_research_request` — TerminationArtifact
  payload pointers (§5.8); §8.5 termination contract uses them.
- `started_at`, `ended_at` — Artifact timestamps (§5.10).
- `outcome.kind` — Artifact discriminator (§5.10).

### 5.15 Mechanical validation

Validator: Python `jsonschema` 4.26.0 with Draft 2020-12,
`referencing` Registry-based cross-file `$ref` resolution. Script
+ examples + raw outputs:
[`docs/schemas/v1.0.0/examples/`](schemas/v1.0.0/examples/).

Reproducer: `cd docs/schemas/v1.0.0/examples && python3 validate.py
&& python3 validate_negative.py`.

Results (committed alongside the schemas):

- **Positive**: 28/28 — every §5–§9 representative example
  validates against its schema, plus the `error.kind` prose/schema
  parity check, the cumulative `Artifact` semantic invariants,
  and the `GoldenTestCase` expected-artifact roundtrip. See
  [`validation_positive.txt`](schemas/v1.0.0/examples/validation_positive.txt).
- **Negative**: 36/36 — every intentionally-invalid case rejects.
  See
  [`validation_negative.txt`](schemas/v1.0.0/examples/validation_negative.txt).
  Representative rejections: forbidden `next_action` values
  (`abort`, `final_answer_ready`); persona class violations;
  ADR-003 side-channel field (`inline_mention`) on `DirectRequest`
  and on primary-turn content; verdict payload exclusivity
  (`request_user_input` + `external_research_request`
  simultaneously); termination-reason / payload mismatch;
  `branch_turn.parent_id = null`; non-SemVer `schema_version`;
  selector `strategy = llm` without `selector_budget`; out-of-enum
  `finish_reason`; `confidence > 1.0`; `problem_statement.round ≠
  0`; `branch_turn.branch_depth ≠ 1`; `transcript_digest` of wrong
  length; `Config.runtime.on_budget_exceeded = degrade` outside the
  closed `{stop}` MVP enum; `observability_level = debug` outside
  the §7.9 closed enum; missing top-level `Artifact.transcript_digest`;
  `tool_events[]` items carrying unknown properties under the §6.4
  close; `FakeProviderScript` missing required `entries`; `GoldenTestCase.case_id`
  violating the `^[A-Za-z0-9_-]{1,64}$` charset.

Documented limitations are listed in §5.10 (cross-item ordering,
synthesis_message_id existence, tamper-evidence) and routed to §9
property tests and §7 on-disk format.

---

## 6. Provider & Tool Adapter Contract

**[Core MVP]**

This section specifies the contract between the
`orchestrator_runtime` and a model backend. After reading §1–§6
plus Appendix A (ADRs) and Appendix B (refinements), an
implementor MUST be able to write a working `ProviderAdapter` for
any LLM backend (an HTTP API client like OpenAI Chat Completions
or Anthropic Messages, a local in-process model, a CLI subprocess
wrapping a vendor binary, or a deterministic `FakeProvider` for
tests) without further clarification. RFC 2119 keywords apply to
adapter invariants; editorial sentences use lowercase.

The contract is sequential, batch-shaped, and structured-only
(ADR-001, ADR-003, ADR-004): exactly one `invoke` call is in
flight at a time; the adapter validates every machine-readable
output against a schema before returning; the §6 surface does not
expose a streaming interface. Adapters MAY use streaming, async
I/O, or transport-level retry internally; the contract is shaped
on the outer call.

### 6.1 Adapter surface

```text
ProviderAdapter.invoke(request: ProviderRequest) -> ProviderResult
ProviderAdapter.shutdown()   # optional
```

- `invoke` is the sole entry point (blocker #5; supersedes the v0
  `invoke(prompt, config)` signature). Synchronous return; MVP is
  batch-only (ADR-004). The adapter MUST NOT spawn parallel
  invocations on behalf of one `invoke` call to satisfy a runtime
  request; ADR-001 prohibits parallelism within an agent turn.
- `shutdown` (OPTIONAL) releases connections, flushes buffered
  telemetry, etc. The runtime MAY call it on session **persist**
  or on process exit; the adapter MUST treat it as idempotent.
- **Transport-agnostic.** An adapter MAY be implemented as an
  HTTP client, an in-process model invocation, a CLI subprocess
  shelling out to an external binary, or any other transport
  shape, provided the `invoke(request) -> ProviderResult` shape
  is preserved. The runtime does not observe the transport.
- Adapters MUST be stateless across `invoke` calls except for:
  connection pools, rate-limit token buckets, internal caches that
  do not alter `provider_result` content. Two `invoke` calls with
  identical `ProviderRequest` instances (modulo `metadata` /
  sampling seed) MUST produce equivalent `ProviderResult` shapes;
  the *content* may differ because the underlying model is not
  guaranteed reproducible (N3 / A2 / §2.7).
- Adapters MUST be invoked once per agent turn. ADR-001 forbids
  parallelism within an agent turn; the adapter MUST NOT
  pre-emptively fan out across panel members or across rounds.

### 6.2 ProviderRequest

The runtime derives a `ProviderRequest` from the agent's
`ContextPacket` (§4.3, §5.9) and `AgentConfig` (§5.2) immediately
before invocation. The schema is
[`provider_request.schema.json`](schemas/v1.0.0/provider_request.schema.json),
sibling to `provider_result.schema.json`.

Required fields:

- `agent_id` — stable identifier of the invoked agent; matches
  `AgentConfig.id`. Carried for adapter-side logging and trace
  correlation.
- `provider` — adapter id (OPEN string; rule N4). Concrete vendor
  identifiers live in `examples/configs/*.yaml`.
- `model` — model identifier within the provider (OPEN string,
  rule N4).
- `messages` — canonicalized message sequence the adapter
  dispatches; see §6.3.
- `tools` — array of tool descriptors visible to the model for
  this turn; empty if none. See §6.4.
- `expected_output_schema` — CLOSED enum
  `{turn_structured_output, verdict, synthesis_content, null}`.
  The schema the adapter MUST validate `structured_output`
  against before returning (§6.5). `null` reserves a future
  free-text invocation path; no MVP code path emits `null`.

Optional fields:

- `reasoning_effort` — OPEN string forwarded to the provider
  (e.g. `low`, `medium`, `high`; semantics vendor-defined).
- `sampling` — vendor-interpreted parameter bag. Recommended
  canonical names: `temperature`, `top_p`, `seed`, `max_tokens`,
  `stop_sequences`. Adapters MUST silently drop unrecognized keys;
  the schema does NOT set `additionalProperties: false` on
  `sampling`.
- `metadata` — opaque pass-through bag (trace ids, request ids,
  logging hints). Not persisted; not interpreted by the runtime.

**Derivation rule.** The mapping `(ContextPacket, AgentConfig) ->
ProviderRequest` is a runtime operation. The runtime owns it; the
adapter receives the request and MUST NOT re-derive it. This
makes the adapter testable in isolation: a unit test constructs a
`ProviderRequest` instance directly and feeds it to `invoke`
without standing up a runtime.

### 6.3 Prompt-formatting contract

The adapter translates `request.messages` into the provider's
native message format. Two invariants govern the translation:

1. **Zero information loss vs. the §4.3 minimum.** Every §4.3
   minimum context item (problem_statement, current round,
   persona material, panel disclosure, most_recent_verdict,
   current_round_messages, branch_origin when applicable) MUST be
   representable in the request the adapter dispatches.
2. **Canonical role enum.** `messages[].role ∈ {system, user,
   assistant, tool}` (CLOSED). The adapter maps each value to the
   vendor's equivalent. Vendor-specific roles (e.g. legacy
   `function` role on some chat APIs, top-level `system` fields
   on some message APIs) live behind the mapping table in the
   worked examples (§6.12, §6.13). No vendor identifier appears
   in the schema body (rule N4).

**Canonical packing recipe.** The runtime constructs `messages`
as follows:

- Position 0, `role = system`: the agent's persona material
  serialized as text. Includes `id`, `reasoning_scope`,
  `reasoning_style`, `behavioral_constraints`, `failure_modes`,
  `output_requirements`, and for a `domain_persona`,
  `domain_scope`, `forbidden_domains`, `must_delegate`. This is
  the only `system` message the runtime emits; additional
  in-conversation guidance (e.g. the corrective-retry annotation,
  §6.7) is encoded as `user` messages so it survives providers
  that accept only one top-level system position.
- Position 1, `role = user`: a problem-statement section
  containing the `problem_statement`, the `round`, the
  `panel_disclosure`, and the most recent verdict's `focus`
  (where applicable).
- Subsequent positions, `role = assistant`: prior turns from
  `current_round_messages`, in `turn_index` order. Each prior
  turn's content is prefixed in-text with a speaker header of the
  form `[speaker: <agent_id>] (turn_index=N)\n…` so the model can
  disambiguate which agent produced what. Provider role
  attribution is unreliable for multi-agent transcripts; the
  in-text header is the authoritative attribution.
- For a branch-turn invocation, the last `user` position MUST
  include the `branch_origin.direct_request` and the parent
  message content so the branch agent answers in context.
- For a coordination_turn invocation, the deferred-queue summary
  (when present) is included in the same `user` position.

**Why in-text speaker headers and not the provider's `name`
field.** Some providers expose a `name` on assistant/tool
messages, others do not; none guarantee the model treats `name`
as an attribution hint. The in-text header is universal, survives
provider transitions, and roundtrips through `transcript_replay`.

**`tool` role messages**. `role = tool` never appears in a
runtime-built `ProviderRequest` (the runtime has no tool results
to inject — it never sees individual tool calls). The `tool` role
exists in the closed enum because the adapter's internal
tool-call loop (§6.4) constructs intermediate provider requests
during the loop and uses `role = tool` for the result feedback.
The conditional `if role = tool then tool_call_id is required`
is enforced in the schema.

Adapters MAY further reformat (e.g. fold the system message into
a leading user-position prefix for providers without a `system`
role; lift it out into a vendor-specific top-level system field
for providers that demand it), provided no §4.3 minimum item is
lost. Lossful reformatting is an adapter bug.

### 6.4 Tool definitions and lifecycle

**Tool shape.** A `Tool` is `{name, description, input_schema,
metadata?}`. `input_schema` is a JSON Schema (Draft 2020-12)
describing the arguments object. Tools are declared per-agent in
`AgentConfig.tools` (§5.2) and forwarded to the adapter in
`ProviderRequest.tools` for each invocation.

**Topology — internal tool-call loop.** The adapter owns the
tool-call loop. The runtime sees exactly one `invoke` call per
agent turn; every tool invocation that happens during that turn
is recorded in `provider_result.tool_events[]`. This is the
official choice; mixed (internal + external) contracts produce
ambiguous adapter implementations and are forbidden.

**Consequence for `finish_reason`.** Under the internal loop,
`finish_reason = tool_call` is NEVER returned to the runtime as
a terminal finish reason in MVP. The adapter completes the loop
internally and surfaces a terminal `finish_reason ∈ {stop,
length, content_filter, error}` to the runtime. The value
`tool_call` remains in the closed `provider_result.finish_reason`
enum for forward compatibility with a future v1+ external-loop
adapter, but it MUST NOT appear in a successfully-returned
ProviderResult under MVP.

**Lifecycle per `invoke` call.**

1. Adapter dispatches the request to the provider with the tool
   descriptors translated to the provider's tool format.
2. Provider responds. If the provider's stop-equivalent reason
   is anything other than "tool call requested", the loop ends:
   the adapter canonicalizes the response, validates
   `structured_output` against `expected_output_schema` (§6.5),
   and constructs the ProviderResult.
3. If the provider's response requests one or more tool
   invocations (single tool_call, or a batch of parallel
   tool_calls — vendor-shape dependent), the adapter:
   - **Resolves each call by `name`** against `request.tools`.
     Unknown name → `tool_failure`, loop ends. The offending
     entry is recorded in `tool_events[]` with `error != null`.
   - **Validates each call's arguments** against the tool's
     `input_schema` (Draft 2020-12). Validation failure on any
     call → `tool_failure`, loop ends.
   - **Invokes the registered tool handlers in declared order**,
     sequentially. Each handler invocation produces one
     `tool_events[]` entry: `{name, arguments, result,
     latency_ms, error}` with `error: null` on success or
     populated on tool-handler failure.
   - **Feeds all results back** to the provider as `role = tool`
     messages (one per call, in the same order), each correlated
     with its prior tool_call via `tool_call_id`. The whole
     batch counts as ONE iteration toward `max_tool_iterations`.
   - Re-invokes the provider with the extended conversation.
4. Iteration counter increments. If the counter reaches
   `max_tool_iterations` (default 8): `tool_failure`,
   `retriable: false`, loop ends. `max_tool_iterations` is the
   adapter-internal tool-loop cap; the runtime surfaces it as
   `Config.runtime.max_tool_iterations` and hands the value to
   the adapter at construction time (§5.2, §8.11). See §6.15 for
   the vocabulary status entry.

**Result correlation.** Each `tool_events[]` entry MUST appear in
the order it executed. Adapters MUST NOT reorder, deduplicate, or
collapse tool events. When the model emitted parallel tool_calls
in one iteration, the resulting `tool_events[]` entries appear
consecutively in the order the adapter executed them.

**Why `tool_failure` is distinct from `malformed_response`.**
`malformed_response` means the top-level `structured_output`
failed the `expected_output_schema` after the loop finished.
`tool_failure` means the loop itself broke (unknown tool, invalid
tool input, tool handler raised, iteration cap reached). The
distinction matters because §4.9 routes them differently: a
malformed_response triggers the corrective-retry packet (§6.7);
a tool_failure with `retriable: false` triggers the agent-failure
policy.

**Tool error inside the loop, model continues.** If a tool
handler returns an error and the model then emits a clean
non-tool response, the adapter populates `tool_events[]` with the
error entry but `error` on the top-level `provider_result` stays
`null` (assuming `structured_output` validates). The
canonical_transcript carries the tool history; the agent turn
itself succeeded.

### 6.5 Structured-output enforcement

ADR-003 in schema form. The adapter MUST validate
`provider_result.structured_output` against the request's
`expected_output_schema` BEFORE returning a successful result.

- `expected_output_schema = turn_structured_output` →
  validate against `turn_structured_output.schema.json` (§5.5).
- `expected_output_schema = verdict` →
  validate against `verdict.schema.json` (§5.6).
- `expected_output_schema = synthesis_content` →
  validate against `synthesis.schema.json#/$defs/synthesis_content`
  (§5.8).

The adapter MAY be permissive about extra fields the provider
emits in its raw structured output (per §5.7 "permissive at the
provider boundary"). The runtime canonicalizes the validated
object into the strict per-turn shape before appending to the
canonical_transcript (§5.4); fields the model returned that are
not in the target schema are silently dropped. This is the
boundary at which permissive-provider meets strict-persistence.

**On validation success.** The adapter returns the result with
`structured_output` set to the validated object (the *post-
permissive-trim* shape that conforms to the target schema, or the
raw object if it happens to already conform — the runtime
canonicalises in either case), `error = null`, and
`finish_reason ∈ {stop, length, content_filter}`. Under MVP
internal-loop, `tool_call` is NEVER a terminal finish_reason
(§6.4). `error` as `finish_reason` is reserved for the failure
path; on validation success, `error` is `null` and
`finish_reason` cannot be `error`.

**On validation failure.** The adapter does NOT retry internally
(N10). It returns:

- `structured_output = null`
- `error = {kind: "malformed_response", message: "<path>:
  <validator_message>", retriable: true, details: {validator:
  …, expected_output_schema: …, failing_path: …,
  validator_message: …, raw_attempt: …}}`
- `finish_reason = "error"`

The runtime applies the corrective-retry mechanism (§6.7) on
seeing this error. The runtime needs to see schema failures
because it owns the schema-retry budget and may need to
contract the failing agent under §4.9.

### 6.6 Error taxonomy (closed enum)

`provider_result.error.kind` is a CLOSED enum. Twelve
categories cover every observed vendor error in the inventory
below. Vendor-specific subtypes live in
`error.details`, not in the `kind` field.

| Kind | Default `retriable` | §4.9 outcome on exhaustion |
|---|---|---|
| `timeout` | `true` | retry, then `provider_unrecoverable` |
| `network` | `true` | retry, then `provider_unrecoverable` |
| `rate_limit` | `true` | retry with backoff, then `provider_unrecoverable` |
| `quota_exhausted` | `false` | `provider_unrecoverable` |
| `auth_failure` | `false` | `provider_unrecoverable` |
| `model_unavailable` | `false` | `provider_unrecoverable` |
| `context_length_exceeded` | `false` | `provider_unrecoverable` |
| `content_filter` | `false` | `provider_unrecoverable` |
| `invalid_request` | `false` | `provider_unrecoverable` |
| `malformed_response` | `true` | corrective retry, then `schema_error` |
| `tool_failure` | adapter-supplied (default `false`) | usually `provider_unrecoverable` |
| `internal` | `false` | `provider_unrecoverable` |

The mapped §4.9 outcomes use the canonical termination-reason
names defined in §4.7 (`provider_unrecoverable`, `schema_error`).
The adapter does NOT emit those reason names directly — the
adapter populates `provider_result.error.kind`; the runtime maps
to a termination reason per the table above when applying
`apply_agent_failure_policy` (§4.9).

The `retriable` column is the *adapter's default hint*. An
adapter MAY override on a per-call basis (e.g. a `rate_limit`
error that returns `retry_after: 60s` past the runtime's wallclock
cap can be marked `retriable: false` by the adapter to short-
circuit a futile retry). The runtime is the final authority on
whether to retry under §4.9's per-agent budget.

**Vendor mapping table (prose only; no vendor identifier in
schema body, N4).**

OpenAI Chat Completions:

| Vendor signal | Canonical kind |
|---|---|
| HTTP 408 / read timeout / model timeout | `timeout` |
| HTTP 5xx (502/503/504) / connection reset | `network` |
| HTTP 429 with `code = rate_limit_exceeded` | `rate_limit` |
| HTTP 429 with `code = insufficient_quota` | `quota_exhausted` |
| HTTP 401, 403 (`invalid_api_key`, `insufficient_permissions`) | `auth_failure` |
| HTTP 404 (`model_not_found`) | `model_unavailable` |
| HTTP 400 with `code = context_length_exceeded` | `context_length_exceeded` |
| HTTP 400 with any other `invalid_request_error` code (malformed body, missing param, unknown sampling key) | `invalid_request` |
| terminal `finish_reason = "content_filter"` | `content_filter` |
| model-emitted tool_call arguments fail tool input_schema | `tool_failure` |
| top-level structured_output fails expected_output_schema | `malformed_response` |
| anything else | `internal` |

Anthropic Messages:

| Vendor signal | Canonical kind |
|---|---|
| HTTP 408 / SDK read timeout | `timeout` |
| HTTP 5xx (`api_error`) / connection reset | `network` |
| `rate_limit_error` (HTTP 429) | `rate_limit` |
| `overloaded_error` (HTTP 529) | `rate_limit` |
| `rate_limit_error` exhausting daily/monthly hard cap | `quota_exhausted` |
| `authentication_error`, `permission_error` (HTTP 401, 403) | `auth_failure` |
| `not_found_error` (HTTP 404, unknown model) | `model_unavailable` |
| `invalid_request_error` with `context_length_exceeded` payload | `context_length_exceeded` |
| any other `invalid_request_error` (malformed body, missing param) | `invalid_request` |
| explicit safety refusal block / `stop_reason = "refusal"` (Sonnet 4.5+) | `content_filter` |
| `stop_reason = "stop_sequence"` (normal stop on a configured stop sequence) | maps to `finish_reason = stop`, NOT to an `error.kind`; no error populated |
| model-emitted tool_use arguments fail tool input_schema | `tool_failure` |
| top-level structured_output fails expected_output_schema | `malformed_response` |
| anything else | `internal` |

The table is exhaustive against the public vendor error
inventories observed at Pass 5 drafting. Adapters that encounter
a vendor error not in the table MUST classify it under
`internal` rather than introducing an out-of-enum value — the
enum is closed.

### 6.7 Retry semantics

Two retry policies operate independently:

**Runtime invocation-retry policy (the one §4.9 references).**
The runtime owns the per-agent retry budget
(`per_agent_retry_budget`, default 2; §5.2; configurable per
agent via `AgentConfig.retry_budget`). On a `retriable = true`
error from a `provider_adapter.invoke` call, the runtime applies
exponential backoff with jitter and re-invokes:

- Base interval `0.5 s`, multiplier `2.0`, cap `30 s`, full
  jitter (random in `[0, current_interval]`).
- Attempt 0 is the initial invocation; attempts 1..N are retries.
  `per_agent_retry_budget = K` means up to K retries (K+1 total
  invocations) before the failure is unrecoverable. With the
  default `K = 2`, that is up to 3 total invocations.
- After exhaustion, the runtime applies §4.9
  `apply_agent_failure_policy` with the error's mapped §4.9
  outcome (§6.6 column 3).
- The runtime owns the wallclock — between-retry sleep counts
  against `max_wallclock_seconds` (§4.7). The adapter MAY
  populate `error.details.retry_after_seconds` to recommend a
  longer sleep; the runtime MAY honor it but MUST NOT exceed its
  own budget.

**Adapter-internal transport-retry policy (optional).** Inside a
single `invoke` call, the adapter MAY retry the transport once
or twice on bare-network errors that occur *before* the provider
returns any byte (e.g. immediate TCP reset, name-resolution
failure). This is opaque to the runtime; the eventual
ProviderResult reflects only the final outcome. Adapters that
implement transport retry SHOULD NOT exceed 3 transport-level
attempts per `invoke` to keep total latency bounded; transport
retries MUST NOT be performed for errors the runtime is meant to
see (`malformed_response`, `tool_failure`, `auth_failure`,
`content_filter`, `quota_exhausted`, `model_unavailable`,
`context_length_exceeded`, `invalid_request`).

**Corrective retry for schema failures.** When the adapter
returned `error.kind = "malformed_response"`, the runtime
constructs a new `ProviderRequest` whose `messages` extends the
original with two additional positions:

1. The provider's prior (malformed) assistant message, verbatim,
   as the next `assistant` entry. This gives the model context on
   what it produced.
2. A new `user` message describing the validation failure. Using
   `role = user` (not `system`) ensures the message round-trips
   through providers that accept only one top-level system
   position. The format the runtime emits:

```text
The previous response failed schema validation against
`expected_output_schema = <name>` at path `<failing_path>`:
<validator_message>

Please re-emit the entire response as a single JSON object that
conforms to the schema. Do not wrap it in markdown. Do not
include explanatory prose outside the JSON.
```

The adapter does NOT generate this annotation; the runtime does.
The adapter only forwards.

**Corrective-retry counting.** The corrective retry consumes one
slot of `per_agent_retry_budget`, same as a transient retry. The
two budgets are not separate: a single agent invocation may burn
its retries on a mix of `malformed_response` (corrective retry)
and `rate_limit` / `timeout` / `network` (backoff retry); the
total is bounded by `per_agent_retry_budget`. After exhaustion
the runtime applies §4.9 with whichever §4.9 outcome the LAST
error mapped to (so a final `malformed_response` exhaustion maps
to `schema_error`; a final `rate_limit` exhaustion maps to
`provider_unrecoverable`).

**What if the corrective retry itself fails malformed?** Same
treatment — counts against the same budget; on exhaustion the
agent is contracted (`on_agent_failure = continue_without`) or
the session terminates (`on_agent_failure = terminate`).

### 6.8 Authentication

Adapters consume credentials from a `ProviderCredentials` object
passed at adapter construction, NOT per-invoke. The credential
object's shape is adapter-specific (an OpenAI-style adapter wants
`{api_key, organization?}`; an Anthropic-style adapter wants
`{api_key, anthropic_version?}`; a local-process adapter may
need none at all). §6 does NOT prescribe the shape because it
depends on the backend.

Env-var conventions (`OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, etc.)
live in `examples/configs/*.yaml` and example launcher scripts,
NOT in the §6 normative body (rule N4). Credential rotation,
secret stores, key derivation, and audit logging are §8 (Pass 6
security) concerns.

The only normative requirements here: the adapter MUST acquire
credentials at construction, MUST fail-fast on missing or
malformed credentials at construction (not on first `invoke`),
and MUST NOT log credential material into `metadata`, `raw`, or
`error.details`.

### 6.9 Token counting and cost

`provider_result.usage` (§5.7) is required on every result,
including error results (with zero or partial counts when no
generation occurred). Required sub-fields: `prompt_tokens`,
`completion_tokens`, `total_tokens`, `cost_usd`.

- The adapter populates `usage` from the provider's reported
  usage. If the provider does not report usage (some local
  backends), the adapter MAY estimate using a tokenizer
  compatible with the model family. When estimated, the adapter
  SHOULD set `usage.estimated = true` (the boolean field on
  `provider_result.usage`, defaults to `false`) so the runtime
  can mark the session's accumulated usage as approximate.
- `cost_usd` is derived from a per-model price table maintained
  internally by the adapter. The price table is not part of the
  Symposium spec; adapters MAY consult a config file, an
  environment-injected map, or a remote source. The runtime
  consumes the field as-is for hard-cap enforcement (§4.7).
- For partial-success cases (e.g. the adapter executed N tool
  calls then hit `max_tool_iterations`), the adapter SHOULD
  populate the partial token / cost counts; budget is best-effort
  under failure but MUST NOT be zero where any generation
  occurred.
- Under the internal tool-call loop, `usage` reflects the SUM
  across all loop iterations — the adapter aggregates per-iteration
  token counts before returning.

### 6.10 Finish-reason normalization

`provider_result.finish_reason` is a CLOSED enum (§5.7):
`{stop, length, tool_call, content_filter, error}`. Under MVP
internal-loop topology (§6.4), `tool_call` NEVER appears as the
returned terminal reason — the adapter completes the tool loop
internally and surfaces a terminal value in
`{stop, length, content_filter, error}`. The `tool_call` value
remains in the schema only so a future external-loop adapter
(v1+) can use it without re-opening the enum.

OpenAI Chat Completions terminal-reason mapping:

| Vendor (terminal) | Canonical |
|---|---|
| `stop` | `stop` |
| `length` | `length` |
| `content_filter` | `content_filter` |
| (any vendor reason surfaced *after* an error or loop exhaustion) | `error` |

Intermediate `tool_calls` / `function_call` finish reasons are
consumed inside the loop (§6.4) and never surface terminally.

Anthropic Messages terminal-reason mapping:

| Vendor (terminal) | Canonical |
|---|---|
| `end_turn` | `stop` |
| `stop_sequence` | `stop` |
| `max_tokens` | `length` |
| `refusal` (Sonnet 4.5+) | `content_filter` |
| (any vendor reason surfaced after an error or loop exhaustion) | `error` |

Intermediate `tool_use` finish reasons are consumed inside the
loop and never surface terminally.

### 6.11 Adapter registration / discovery

MVP registers adapters via an explicit registry table maintained
by the runtime: a mapping `provider_id -> AdapterFactory(creds,
config) -> ProviderAdapter`. The runtime, at session **init**,
walks `config.agents[].provider` (and `config.coordinator.provider`)
and resolves each provider id against the registry. Unknown
provider id → `terminate(reason = schema_error)`.

The factory pattern is intentional: the adapter's constructor
runs once per session per provider id, not once per invocation.
Stateful resources (connection pools, rate-limit buckets) live in
the adapter instance.

**Backends covered.** The registry indexes any adapter
implementing the §6.1 surface — HTTP API clients, local in-
process model wrappers, CLI-subprocess adapters that shell out
to a vendor binary, and the `FakeProvider` of §6.14 all register
through the same interface. This satisfies Pass 1 row #42's
multi-backend goal (OpenAI API / Anthropic API / local backends /
CLI plugin adapters) without privileging any transport.

Plugin-style discovery (entry-point conventions,
auto-registration via package metadata) is a v1 extension and
lives in §12 Roadmap, not in §6. MVP ships built-in adapters in
the symposium package; downstream consumers extend by registering
their own factories at startup. This is Pass 1 row #152's "MVP
ships ProviderAdapter plugin contract only".

### 6.12 Worked example: an HTTP API adapter (OpenAI-shaped)

This is a full end-to-end walkthrough of one panel-agent
invocation under an OpenAI-Chat-Completions-style HTTP adapter.

**Setup.** The runtime has built a `ProviderRequest` for the
Researcher agent on a branch turn (Logician asked Researcher to
verify the dataset is observational). The request appears in
[`docs/schemas/v1.0.0/examples/provider_request.json`](schemas/v1.0.0/examples/provider_request.json).
Key fields: `agent_id = researcher`, `expected_output_schema =
turn_structured_output`, one `Tool` (`search_papers`).

**Step 1 — Translate `ProviderRequest` to an OpenAI Chat
Completions request.**

```text
POST /v1/chat/completions
{
  "model": "<resolved from provider/model mapping>",
  "messages": [
    { "role": "system", "content": "<persona material text>" },
    { "role": "user",   "content": "<problem + panel + branch_origin block>" }
  ],
  "tools": [
    {
      "type": "function",
      "function": {
        "name": "search_papers",
        "description": "Search a corpus of academic papers...",
        "parameters": { /* the tool's input_schema verbatim */ }
      }
    }
  ],
  "temperature": 0.2,
  "max_tokens": 800,
  "seed": 42
}
```

Notes:
- `request.messages[].role` maps 1:1 with this vendor's chat
  roles. No transformation.
- `request.tools[]` maps to this vendor's `tools[]` shape with
  the `function` envelope; `input_schema` becomes `parameters`.
- `sampling.temperature`, `sampling.max_tokens`, `sampling.seed`
  map 1:1.
- `reasoning_effort` (when present) maps to this vendor's
  reasoning-effort parameter for reasoning models.

**Step 2 — Provider responds with a tool_call (first iteration
of the internal loop).**

```text
{
  "id": "chatcmpl-...",
  "choices": [{
    "message": {
      "role": "assistant",
      "content": null,
      "tool_calls": [
        {
          "id": "call_abc123",
          "type": "function",
          "function": {
            "name": "search_papers",
            "arguments": "{\"query\":\"treatment X observational cohort outcome Y methods\",\"max_results\":3}"
          }
        }
      ]
    },
    "finish_reason": "tool_calls"
  }],
  "usage": { "prompt_tokens": 2310, "completion_tokens": 42, "total_tokens": 2352 }
}
```

The adapter is now inside the internal tool-call loop (§6.4).

**Step 3 — Resolve and validate the tool call.**

- Lookup `search_papers` in `request.tools`. Found.
- Parse `arguments` JSON. Validate against the tool's
  `input_schema`. Passes.
- Invoke the registered `search_papers` handler. Returns a list
  of matches in 412 ms.

Record a `tool_events[]` entry:

```jsonc
{
  "name": "search_papers",
  "arguments": {"query": "treatment X observational cohort outcome Y methods", "max_results": 3},
  "result": { "matches": [/* 1 match */] },
  "latency_ms": 412,
  "error": null
}
```

**Step 4 — Feed the result back and re-invoke.**

The adapter appends:

```text
{ "role": "assistant", "content": null, "tool_calls": [/* the prior call */] }
{ "role": "tool", "tool_call_id": "call_abc123", "content": "<json result>" }
```

…and re-POSTs `/v1/chat/completions` with the extended
conversation. Iteration counter = 1.

**Step 5 — Provider responds terminally (no further tool_calls).**

```text
{
  "choices": [{
    "message": { "role": "assistant",
                 "content": "{ \"text\": \"The methods section confirms an observational cohort design...\", \"direct_requests\": [] }" },
    "finish_reason": "stop"
  }],
  "usage": { "prompt_tokens": 170, "completion_tokens": 220, "total_tokens": 390 }
}
```

(`response_format = json_object` constrains the assistant content
to JSON.)

**Step 6 — Validate `structured_output` against
`turn_structured_output` schema.**

Parsed object:
```json
{ "text": "The methods section confirms an observational cohort design (n=4,213); ...", "direct_requests": [] }
```
This validates: `text` is non-empty, `direct_requests` is an
empty array. Adapter returns success.

**Step 7 — Aggregate usage and construct `ProviderResult`.**

Total usage (sum across iterations): `prompt_tokens = 2480`
(2310 + 170), `completion_tokens = 262` (42 + 220),
`total_tokens = 2742`, `cost_usd = 0.031` (from the price table).

The committed instance is at
[`docs/schemas/v1.0.0/examples/provider_result_openai_example.json`](schemas/v1.0.0/examples/provider_result_openai_example.json)
and validates against `provider_result.schema.json` and against
`turn_structured_output.schema.json` (nested validation, §5.15
+ Pass 5 extension to `validate.py`).

**Variant A — no tools needed.** If at Step 2 the provider had
returned `finish_reason = "stop"` immediately with the clean JSON
content, the loop exits in one iteration; `tool_events = []`,
total iterations = 0. The ProviderResult shape is identical
modulo the empty `tool_events` array.

**Variant B — error mapping.** If at Step 2 the provider had
returned HTTP 429 with `code = rate_limit_exceeded`, the adapter
constructs:

```jsonc
{
  "messages": [],
  "tool_events": [],
  "usage": {"prompt_tokens": 0, "completion_tokens": 0,
            "total_tokens": 0, "cost_usd": 0.0},
  "finish_reason": "error",
  "structured_output": null,
  "raw": { /* the 429 response body */ },
  "error": {
    "kind": "rate_limit",
    "message": "rate_limit_exceeded; retry-after: 30s",
    "retriable": true,
    "details": {"status": 429, "vendor_code": "rate_limit_exceeded",
                "retry_after_seconds": 30}
  }
}
```

The runtime sees `retriable: true`, applies §6.7 backoff,
retries. After `per_agent_retry_budget` exhaustions: §4.9
`apply_agent_failure_policy(agent, provider_unrecoverable)`.

### 6.13 Worked example: an HTTP API adapter (Anthropic-shaped)

Same Researcher invocation, this time under an Anthropic-Messages-
style HTTP adapter, on the Coordinator's coordination_turn
(`expected_output_schema = verdict`).

**Step 1 — Translate `ProviderRequest` to a Messages-API
request.**

```text
POST /v1/messages
{
  "model": "<resolved from provider/model mapping>",
  "system": "<persona material text>",
  "messages": [
    { "role": "user",
      "content": [{"type": "text",
                   "text": "<problem + panel + round messages block>"}] }
  ],
  "tools": [{
    "name": "search_papers",
    "description": "Search a corpus of academic papers...",
    "input_schema": { /* verbatim Draft 2020-12 */ }
  }],
  "max_tokens": 800,
  "temperature": 0.2
}
```

Notes:
- This vendor does NOT have a `system` role in the `messages`
  array; instead it has a top-level `system` field. The adapter
  pulls `request.messages[0]` (system, position 0 per §6.3) out
  and re-routes it.
- Tool descriptors map directly: `name`, `description`,
  `input_schema` are the same fields.
- This vendor's Messages API does not accept `seed`; the adapter
  silently drops `sampling.seed` (per §6.2 "adapters MUST silently
  drop unrecognized keys").
- `reasoning_effort` is silently dropped (this vendor exposes
  reasoning differently; the adapter chooses whether to enable
  it based on the agent config, not on this hint).

**Step 2 — Provider responds terminally.**

```text
{
  "id": "msg_...",
  "type": "message",
  "role": "assistant",
  "content": [
    { "type": "text",
      "text": "{ \"next_action\": \"continue\", \"rationale\": \"...\", ... }" }
  ],
  "stop_reason": "end_turn",
  "usage": { "input_tokens": 3120, "output_tokens": 340 }
}
```

No tool_use blocks → the loop exits in one iteration.

**Step 3 — Validate `structured_output` against `verdict` schema.**

Parsed Verdict (full content in committed example):
- `next_action = "continue"`, all required fields present, no
  forbidden payload (no `user_input_request` /
  `external_research_request` because `next_action ∈
  {continue, finalize}`).
- Validates against `verdict.schema.json` (closed `next_action`
  enum, required fields, exclusivity conditional).

**Step 4 — Construct `ProviderResult`.**

The committed instance is at
[`docs/schemas/v1.0.0/examples/provider_result_anthropic_example.json`](schemas/v1.0.0/examples/provider_result_anthropic_example.json)
and passes both the wrapper validation (against
`provider_result.schema.json`) and the nested validation (its
`structured_output` against `verdict.schema.json`).

Vendor → canonical normalizations applied:
- `stop_reason: "end_turn"` → canonical `finish_reason: "stop"`
  (§6.10).
- `input_tokens` / `output_tokens` → canonical `prompt_tokens` /
  `completion_tokens` (`total_tokens` computed by the adapter).
- `cost_usd` from the adapter's per-model price table.

**Variant — tool_use loop.** If the Coordinator had emitted a
`tool_use` block (this vendor's parallel-tool form), the
adapter:

1. Extracts each `{type: "tool_use", id: ..., name: ..., input:
   {...}}` block from `content[]`.
2. Validates each input against the registered `input_schema`.
3. Invokes each handler in declared order, sequentially. Records
   one `tool_events[]` entry per call.
4. Constructs a `user` message with `content[]` containing one
   `tool_result` block per call: `{type: "tool_result",
   tool_use_id: ..., content: ...}`. (This vendor represents tool
   results as user-role messages, not as a `tool` role; the
   adapter does this translation internally — the
   `provider_result.messages[]` still reports the canonical
   `tool` role on its outgoing list.)
5. Re-POSTs and continues. Whole batch counts as one iteration
   toward `max_tool_iterations`.

**Variant — error mapping (refusal).** If the model returned
`stop_reason = "refusal"` (Sonnet 4.5+ safety stop):

```jsonc
{
  "messages": [],
  "tool_events": [],
  "usage": {"prompt_tokens": 3120, "completion_tokens": 12,
            "total_tokens": 3132, "cost_usd": 0.014},
  "finish_reason": "content_filter",
  "structured_output": null,
  "raw": { /* the refusal payload */ },
  "error": {
    "kind": "content_filter",
    "message": "Model refused on safety grounds",
    "retriable": false,
    "details": {"stop_reason": "refusal"}
  }
}
```

`retriable: false` → runtime applies §4.9 immediately, no retry.
Note that `stop_reason = "stop_sequence"` (this vendor's normal
stop on a configured stop sequence) is a *successful* stop, NOT
a content_filter: it maps to canonical `finish_reason = "stop"`
and `error = null`.

### 6.14 Worked example: FakeProvider (for tests)

`FakeProvider` is a scriptable, deterministic adapter shipped for
unit/integration tests. It MUST satisfy the §6 contract bit-for-
bit; the differences from a real adapter are:

1. No HTTP, no network. The adapter consumes a *script* (a list
   of `(matcher, response)` pairs) at construction time.
2. `usage` reports zero unless the script populates it; tests can
   inject specific counts to exercise budget paths.
3. `structured_output` is always pre-validated (the script
   authors it); the validation step runs anyway so the script
   author can intentionally inject `malformed_response` cases.

Example script for the two-round canonical session (§5.13):

```text
script[0]: agent=logician, round=1, turn_index=1
           → structured_output = {text: "...", direct_requests: [
               {target: "researcher", type: "verification", content: "..."},
               {target: "critic",     type: "critique",     content: "..."}
             ]}
script[1]: agent=researcher (branch), round=1, turn_index=2
           → structured_output = {text: "Confirmed observational ...", direct_requests: []}
…
```

The committed example
[`docs/schemas/v1.0.0/examples/provider_result_fake_example.json`](schemas/v1.0.0/examples/provider_result_fake_example.json)
shows the result of script step 0 (the logician's primary_turn)
and validates against both `provider_result.schema.json` and
`turn_structured_output.schema.json` (nested).

§9.5 ships a property test that drives FakeProvider with
generative scripts to exercise scheduler invariants.

### 6.15 Vocabulary introduced by §6 (status table)

| Item | Disposition | Where |
|------|-------------|-------|
| `ProviderRequest` shape | **Defined** as a new schema. | §6.2 + `provider_request.schema.json` |
| `messages[].role` CLOSED enum `{system, user, assistant, tool}` | **Defined** on ProviderRequest. | §6.2, §6.3 |
| `expected_output_schema` CLOSED enum `{turn_structured_output, verdict, synthesis_content, null}` | **Defined** on ProviderRequest. | §6.2, §6.5 |
| `Tool` shape `{name, description, input_schema, metadata?}` | **Defined** as `$defs.tool` inside `provider_request.schema.json`. | §6.4 |
| `sampling` open-key bag | **Defined**. Canonical key names: `temperature`, `top_p`, `seed`, `max_tokens`, `stop_sequences`. | §6.2 |
| `metadata` opaque pass-through | **Defined**. | §6.2 |
| `tool_events[]` item shape (closed: `{name, arguments, error}` required, optional `result`, `latency_ms`) | **Defined** in `provider_result.schema.json` (Pass 5 tightening). | §6.4 + provider_result.schema.json |
| `error.kind` CLOSED enum (12 values) | **Defined**; closure rationale in §6.16. | §6.6 + `provider_result.schema.json` |
| `max_tool_iterations` | **Defined** as adapter-internal cap; default 8. | §6.4. Surfaced to `Config.runtime` in §5.2 once the runtime gained visibility into it. |
| Corrective-retry user message template | **Defined** (prose, runtime-emitted, `role = user`). | §6.7 (template schematization remains deferred — see §6.7 prose). |
| `tool_call_id` on `messages[]` (role=tool conditional) | **Defined** as conditionally required on ProviderRequest. | §6.2 |
| Adapter registration table | **Defined** as MVP convention (in-process registry). | §6.11 (entry-point discovery beyond in-process is a §12 Roadmap item; see the §12.2 plugin-architecture row). |
| Transport-shape neutrality (HTTP / local / CLI / fake) | **Defined** in §6.1 + §6.11. | §6.1, §6.11 |

Vocabulary introduced by §6 that other chapters absorbed (no
open promotion items remain):

- `ProviderCredentials` — adapter-construction parameter;
  adapter-specific shape. Credential rotation, secret stores,
  and audit are covered in §8.7 / §8.9.
- `AdapterFactory` — registry table value. Plugin discovery
  beyond MVP in-process registration is a §12 Roadmap item
  (see §12.2 plugin-architecture row).
- `max_tool_iterations` — adapter-internal cap promoted to
  `Config.runtime.max_tool_iterations` in §5.2.
- `usage.estimated` boolean field on `provider_result.usage`
  (default `false`) — set when the adapter estimates tokens
  because the provider didn't report. Promoted from the original
  Pass 5 `metadata.usage_estimated` flag to a schema-level field
  in Pass 6 (see `provider_result.schema.json`); the prose at
  §6.9 references the schema field directly. Observability
  surfaces it via §7.9; v1+ may add a budget-confidence metric.

### 6.16 Schema pre-publication finalization notes

**`provider_result.schema.json` — `error.kind` enum closure +
`tool_events` tightening.**

Pass 4 annotated `error.kind` as `"OPEN at MVP; §6 Pass 5 closes
the enum after surveying real adapters."` Pass 5 closes it.
Closed set: `{timeout, network, rate_limit, quota_exhausted,
auth_failure, model_unavailable, context_length_exceeded,
content_filter, invalid_request, malformed_response, tool_failure,
internal}` — twelve categories. Mapping tables in §6.6.

Pass 5 also tightens `tool_events[].items`: `error` is now
REQUIRED on every entry (null on success, populated object on
failure, matching the top-level error shape); `arguments` is
typed as `object`; `result` is typed as `null | object | string`;
the item closes with `additionalProperties: false`. This makes
audit-replay strict and prevents vendor side-channels from
leaking through tool metadata.

**Version-bump decision.** Both changes are pre-publication
finalizations before 1.0.0 release, NOT version bumps. Pass 4
explicitly deferred the `error.kind` closure to Pass 5; the
`tool_events` shape was under-specified at the same point and
its tightening completes the original intent. No implementor has
shipped against any post-1.0.0-published artifact yet; the
schema artifact's `schema_version` remains `1.0.0`.

For post-publication changes, §5.1 strict-versioning applies —
default to MINOR (1.1.0) for any change that alters acceptance
behavior (enum-closure, required-field addition, content-shape
restriction); PATCH is reserved for genuine schema bug fixes
that do not change acceptance behavior.

No other Pass 4 schemas are amended by Pass 5.

---

## 7. Persistence, Replay, Observability

**[Core MVP]** (persistence + observability MVP subset)
**[v1]** (full replay, full observability)

### 7.1 Run directory layout

The canonical persistence unit is `runs/<session_id>/`, a
filesystem directory the runtime owns for the lifetime of the
session plus indefinite read-only retention afterwards. Required
content at session end (`status = complete` or `terminated`):

- `manifest.json` — the `RunManifest` (§7.2). Written
  atomically; readers consult this BEFORE opening any other file.
- `artifact.json` — the canonical `Artifact` (§5.10). Written
  atomically at session **persist**.
- `config.json` — a copy of `artifact.config` extracted for
  indexability. Identical to `artifact.config`; provided so a
  reader inspecting the directory does not have to parse the
  entire artifact to see the configuration. Round-trip equality
  is a runtime invariant.

Optional content (implementation-defined, RECOMMENDED):

- `transcript.jsonl` — the transcript journal (§7.3). Written
  per-turn during the session for crash recovery.
- `observability.jsonl` — reserved for v1+ structured event
  streams (§7.10). MVP implementations MAY omit.

Cross-platform path: `session_id` is constrained to a
filesystem-safe charset: `^[A-Za-z0-9_-]{1,64}$`. This roundtrips
on POSIX, Windows (which forbids `<>:"|?*\\/`), and
case-insensitive HFS+ / APFS. The regex is enforced by the
`RunManifest.session_id` schema; the runtime MUST refuse to
allocate a session id outside this charset.

### 7.2 RunManifest

`RunManifest`
([`run_manifest.schema.json`](schemas/v1.0.0/run_manifest.schema.json))
is the thin metadata layer for a `runs/<session_id>/` directory.
Required: `schema_version`, `manifest_version`, `session_id`,
`status`, `producer`, `created_at`, `artifact_path`,
`config_path`. Conditional: `transcript_digest`, `outcome_kind`
(REQUIRED on `status ∈ {complete, terminated}`; FORBIDDEN on
`status ∈ {in_progress, crashed}`); `updated_at` REQUIRED on
`status ∈ {in_progress, crashed}`. Optional: `journal_path`
(pointer to `transcript.jsonl` when the journal is present),
`observability_log_path` (pointer to `observability.jsonl` when
the v1+ event stream is enabled), `notes` (free-form
human-readable annotations the producer chooses to attach).

`status` is a CLOSED enum `{in_progress, complete, terminated,
crashed}` (§7.3 lifecycle). `outcome_kind ∈ {synthesis,
termination}` mirrors `Artifact.outcome.kind` for fast indexing.

The manifest's role: a reader (CLI `symposium replay`, third-
party indexer, observability dashboard) opens
`runs/<session_id>/manifest.json` first; it learns from `status`
whether the session is final, whether the artifact is present,
whether the journal is authoritative for partial reads. The
manifest is NOT a substitute for the Artifact — the Artifact
remains the sole source of truth (ADR-005).

`producer.name` and `producer.version` identify the
implementation that wrote the directory. This is provenance,
**not** a vendor identifier in the rule-N4 sense (it names the
Symposium implementation, not a model vendor).

### 7.3 Persistence guarantees and recovery

The transcript journal (`transcript.jsonl`) is appended per turn
during the session. The journal's format is implementation-
defined (RECOMMENDED: one JSON-encoded `Message` per line, in
the same order they enter `canonical_transcript`). The journal
is a recovery aid, NOT a source of truth.

Lifecycle states (mirrored by `RunManifest.status`):

- **`in_progress`** — the runtime is actively producing the
  session. `manifest.json` exists with `status = in_progress`;
  `transcript.jsonl` SHOULD exist; `artifact.json` does NOT yet
  exist. A reader observing this state MUST NOT assume any
  partial content is final.
- **`complete`** — the session entered **persist** with a
  `synthesis` outcome. The runtime atomically wrote
  `artifact.json` and atomically updated the manifest to
  `status = complete`. The journal MAY be retained for
  debugging.
- **`terminated`** — the session entered **persist** with a
  `termination_artifact` outcome. Same atomicity guarantees as
  `complete`.
- **`crashed`** — the runtime exited (or was killed) before
  reaching **persist**. The manifest's `status = in_progress`
  becomes stale; an out-of-process observer (or the next runtime
  startup) MAY rewrite the manifest to `status = crashed`. A
  reader of a crashed session SHOULD be able to: (a) detect that
  `artifact.json` is absent, (b) read the canonical_transcript
  up to the last committed journal line, (c) NOT silently
  re-run the session — `execution_replay` is an explicit, named
  CLI invocation.

The runtime MUST NOT delete or overwrite `runs/<session_id>/`
content of an existing session; a re-run produces a fresh
`session_id` (or, for `execution_replay`, writes under a
distinct child directory; the exact convention is
implementation-defined).

Top-level Artifact fields surfaced from Pass 4 deferral
(`started_at`, `ended_at`, `cumulative_usage`,
`cumulative_unresolved`, `transcript_digest`) are now all on
`artifact.schema.json`. Pass 6 adds only `transcript_digest`;
the rest were already present in the Pass 4 schema.

### 7.4 Canonical artifact atomic write

`artifact.json` MUST be written atomically: write to a temporary
sibling file (e.g. `artifact.json.tmp` or
`.artifact-<random>.tmp`) and rename onto the final name. A
reader MUST NEVER observe a partially-written `artifact.json`.

The manifest `status` transition (`in_progress` →
`complete` / `terminated`) follows the same pattern: write a new
manifest to a temporary file and rename. The two atomic writes
are not transactional with respect to each other, but the
manifest schema's `allOf` constraints ensure that a reader who
sees `status = complete` while `artifact.json` is missing can
treat it as a malformed directory (and the runtime MUST NOT
produce that intermediate state — `artifact.json` is renamed
into place BEFORE the manifest status flip).

The journal does NOT require atomic writes — it is append-only,
implementation-defined, and best-effort. Partial / torn lines at
the tail are expected after a crash and MUST be tolerated by
recovery readers.

### 7.5 transcript_replay

**`transcript_replay`** re-renders a stored
`Artifact.canonical_transcript` without invoking any provider.
It is deterministic by construction — no LLM call is involved
(§2.7, N3).

MVP requirement: a CLI subcommand MUST exist that, given a
`runs/<session_id>/` path, emits a structured re-rendering of
the canonical_transcript. The minimum output format is the
**JCS-canonicalized** JSON re-emission of the canonical_transcript
array (same canonicalization rule as §7.7 `transcript_digest`):
sort object keys lexicographically, UTF-8 encoding, JCS
whitespace and number formatting. Two conforming implementations
processing the same Artifact MUST produce byte-identical
canonical output. Markdown / human-readable renders are
RECOMMENDED but not required and are NOT subject to the
byte-identity contract.

Higher-fidelity replays — HTML visualization, audio narration
(TTS), interactive timeline — are §12 Roadmap. They consume the
same canonical_transcript and impose no normative requirement on
MVP.

Determinism of `transcript_replay`: given the same
`Artifact.canonical_transcript`, the JCS-canonical output of a
conforming `transcript_replay` is a deterministic function of
the input (§2.7 N3 qualifier applies — the property is
unconditional because no LLM call is involved). The
`transcript_digest` (§7.7) is the integrity signal that the
re-rendered transcript matches the stored one: re-canonicalize
the transcript, re-hash, compare against the stored digest.

### 7.6 execution_replay and pinning conditions

**`execution_replay`** re-runs the orchestrator_runtime against
the original `problem_statement` and `Config` to regenerate a
fresh `canonical_transcript`. Identical results require every
non-deterministic source to be pinned (§2.7, N3).

Pinning conditions, exhaustive:

1. **Runtime implementation** — same `RunManifest.producer.name`
   and `producer.version`. Runtime-level logic (canonicalization,
   retry jitter, packet-derivation policy) is part of the
   reproduction surface.
2. **Adapter implementation** — same `AdapterFactory`
   registration AND same adapter-internal version (§6.11).
3. **Provider** — same `provider_id`.
4. **Model** — identical model identifier *and* identical
   provider-side snapshot.
5. **Sampling parameters** — `temperature`, `top_p`, `seed`,
   `max_tokens`, `stop_sequences` (§6.2 sampling bag).
6. **Prompt caching state** — either cleared, or pre-warmed
   with the same payload set as the original run.
7. **Tool environment** — same tool-name → handler binding
   (§6.4); same external dependency state.
8. **Wall-clock seed** — any wall-clock-dependent path
   (message `timestamp`, retry jitter via `randint`) accepts a
   fixed clock source for replay.
9. **Resolved persona material** — byte-identical persona content.
   When `Config.agents[].persona_ref` is a host-resolved registry
   id (§5.2), a mutated registry breaks replay at unchanged
   `persona_ref`; the replay MUST hash the resolved Persona and
   compare against a hash captured at original run time.
10. **Starting canonical_transcript state** — for partial replays,
    the seed transcript MUST match the original prefix byte-for-
    byte.

If ANY condition cannot be satisfied, the runtime MUST emit a
`pinning_violation` diagnostic identifying the failed condition
(`condition ∈ {runtime, adapter, provider, model, sampling,
cache, tool_env, wallclock, persona, transcript_prefix}`) and
MUST abort the replay without producing a fresh `Artifact`.
Silent best-effort replay is forbidden — it would produce a
fresh artifact whose digest diverges from the original, which a
downstream consumer might mistake for a valid re-execution.

`execution_replay` is **not** part of the MVP MUST-set as a
runtime feature — the MVP CLI MAY ship only `transcript_replay`.
The execution-replay contract is documented in MVP so a v1
implementation can ship it consistently; the pinning rules apply
to any implementation that calls itself `execution_replay`.

### 7.7 transcript_digest computation

`transcript_digest` is a stable identifier for the
canonical_transcript content. Algorithm:

1. **Canonicalize** the `canonical_transcript` array using JSON
   Canonical Serialization per **RFC 8785** (JCS). The full
   rule set applies: sort object keys lexicographically
   (Unicode codepoint order); encode in UTF-8; omit
   insignificant whitespace; serialize numbers per ECMA-262
   `Number.prototype.toString()` (RFC 8785 §3.2.2.2 — note
   `0.0` → `"0"`, `1.0` → `"1"`, trailing zeros dropped).
2. **Hash** the canonical byte string with SHA-256.
3. **Encode** the digest as lowercase hexadecimal (64 chars).

A reference implementation: Python's `rfc8785` package (≥ 0.1.4)
produces a canonical byte string per the above; SHA-256 over its
output gives the digest. Pass 6's `validate.py` semantic
check uses this exact implementation and verifies every
fixture's stored digest against the freshly-computed JCS hash.

The digest is computed once at session end over the final
canonical_transcript. It populates:

- `Artifact.transcript_digest` (Pass 6 amendment, REQUIRED on
  every Artifact).
- `TerminationArtifact.transcript_digest` (Pass 4 field, REQUIRED
  on termination outcomes). The two values MUST be equal.
- `RunManifest.transcript_digest` (Pass 6 schema, REQUIRED on
  `status ∈ {complete, terminated}`). MUST equal the artifact's.

Two conforming implementations producing the same
canonical_transcript MUST produce the same digest. JCS is chosen
over ad-hoc canonicalization because it has an RFC, multiple
implementations, and known edge-case handling (number formatting,
duplicate keys, Unicode normalization).

The digest is the primary integrity signal for a stored artifact:
a reader who re-canonicalizes the canonical_transcript and
re-computes the digest can detect tampering against the on-disk
file (§8.7 artifact-corruption mitigation). It is NOT a full
tamper-evidence chain (the digest itself can be rewritten by an
adversary with filesystem access); audit-log immutability is out
of MVP scope (§8.8).

### 7.8 Determinism statement (A2 / N3)

The runtime's scheduler is deterministic in its decision-making
given fixed inputs (§2.7 `scheduler_determinism`, ADR-001). The
model outputs that feed those decisions are not generally
reproducible.

- **Transcript replayability**: `transcript_replay` is
  deterministic by construction — no LLM call is involved.
  Replaying a stored `canonical_transcript` produces a
  byte-identical JCS-canonical re-emission, unconditionally.
- **Execution reproducibility**: `execution_replay` is
  reproducible only under the pinning conditions of §7.6.
  Absent pinning, outputs are NOT reproducible (A2, N3).

Replayable does NOT imply reproducible. §7.5 transcript_replay
is unconditional; §7.6 execution_replay is conditional. This is
the §2.7 N3 normative qualifier verbatim, applied across §7.

### 7.9 Observability metrics — MVP set

The MVP observability set is computed from the persisted
`Artifact` on demand. No live event stream is required. A
conforming implementation MUST be able to produce each metric
below from a `runs/<session_id>/artifact.json` (and optionally
its observability log when v1+ structured streams are present).

| Metric | Required? | Data source |
|---|---|---|
| Token usage per agent (prompt / completion / total) | MUST | sum of `canonical_transcript[].usage.{prompt_tokens, completion_tokens, total_tokens}` grouped by `speaker` |
| Token usage cumulative (session) | MUST | `Artifact.cumulative_usage.{prompt_tokens, completion_tokens, total_tokens}` |
| Token usage per provider / model | MUST | join `canonical_transcript[].speaker` to `Artifact.config.agents[]` (plus `Artifact.config.coordinator`) and aggregate `usage.{prompt_tokens, completion_tokens, total_tokens}` grouped by `(agent.provider, agent.model)`. Pass 1 row #150 "provider usage". |
| Cost per agent (USD) | MUST | sum of `canonical_transcript[].usage.cost_usd` grouped by `speaker` |
| Cost cumulative (USD) | MUST | `Artifact.cumulative_usage.cost_usd` |
| Cost per provider / model (USD) | MUST | same join as token-usage-per-provider/model, summing `cost_usd`. |
| Latency per invocation | MUST | `canonical_transcript[i].timestamp − canonical_transcript[i-1].timestamp` (best-effort; v1+ adds explicit `latency_ms` per Message) |
| Agent participation count per round | MUST | `canonical_transcript[]` filtered by `type ∈ {primary_turn, branch_turn}` grouped by `(round, speaker)` |
| Branch depth max | MUST | `max(canonical_transcript[].branch_depth)` |
| Deferred-queue length max | MUST | derived: count of `dropped_deferred` annotations + per-round drain count from `coordination_turn` content + per-round dispatch order from `(round, turn_index, parent_id)` patterns (§4.6 structural derivation) |
| Request-level schema-failure count per agent | MUST | count of `schema_failure` annotations on Messages whose `speaker = <agent>` (per `message.schema.json`: each annotation records a `direct_request` that failed schema validation at §4.5 parse time). **Scope note**: this metric counts how many invalid `direct_request` entries an agent emitted that the runtime had to drop; it does NOT count provider-level retries (corrective retries on `malformed_response` from §6.7 and transport retries on `timeout` / `network` / `rate_limit`). Provider-level retry counters are NOT persisted in the MVP canonical_transcript and are a v1+ observability extension (§7.10). |
| Panel-contraction count | MUST | count of `canonical_transcript[]` entries with `type = panel_contraction`, grouped by `content.agent_id` and `content.reason` (`reason ∈ {provider_unrecoverable, schema_error}` per §5.4 message schema). |
| Termination reason (session-level) | MUST | `Artifact.outcome.kind` (synthesis vs termination); when termination, `outcome.termination_artifact.reason` (CLOSED 7-value enum). |
| `usage_estimated` flag (session-level) | MUST | `true` iff any `canonical_transcript[].usage.estimated = true` |

The `usage_estimated` flag is significant because cost / token
caps (§8.1) become approximate under estimated usage; operators
need a confidence indicator on the cumulative_usage surface.

`observability_level = mvp` (the §5.2 default) means the runtime
computes the MUST-set above. `observability_level = verbose`
(reserved for v1+) includes the SHOULD-set below.

### 7.10 Observability metrics — v1 set

v1+ implementations SHOULD compute, in addition to §7.9:

| Metric | Status | Source |
|---|---|---|
| `role_purity_score` | SHOULD | v1 evaluation harness; per-agent measure of scope adherence (Pass 1 row #131, #132) |
| `disagreement_frequency` | SHOULD | per-round count of `unresolved_disagreements` in `coordination_turn.verdict` |
| `interaction_graph` | SHOULD | directed edges from `canonical_transcript[].content.direct_requests[].target` history |
| `delegation_frequency` | SHOULD | sub-count of interaction_graph filtered by `direct_request.type = delegation` |
| `time_to_finalize` | SHOULD | `Artifact.ended_at − Artifact.started_at`; (also available in MVP, but its v1+ framing is cross-session deliberation-quality benchmarking) |
| Provider-level retry count per agent (corrective + transport) | SHOULD | requires per-invocation observability event stream (§6.7 backoff and corrective-retry paths). MVP persists only request-level `schema_failure` annotations on Messages; provider-call retries are not derivable from the Artifact alone. |
| Failure count per agent by `error.kind` | SHOULD | same source as above (per-invocation event stream); MVP gives request-level schema-failure count only, not the full §6.6 12-kind breakdown. |

A live observability event stream
(`observability_event.schema.json`) is **formally deferred** to
v1+. The MVP MUST-set is fully derivable from the persisted
Artifact; no live bus is required for compliance.

### 7.11 Vocabulary introduced by §7 (status table)

| Item | Disposition | Where |
|---|---|---|
| `RunManifest` | **Defined** as new schema. | §7.2 + `run_manifest.schema.json` |
| `status` CLOSED enum `{in_progress, complete, terminated, crashed}` | **Defined** on RunManifest. | §7.2, §7.3 |
| `transcript_digest` | **Defined** algorithm + promoted to top-level `Artifact` field. | §7.7 + `artifact.schema.json` amendment |
| `transcript_journal` (`transcript.jsonl`) | **Defined** as optional implementation-defined sidecar. | §7.3 |
| `pinning_conditions` enumeration | **Defined** as exhaustive list. | §7.6 |
| `pinning_violation` diagnostic | **Defined** as runtime abort signal on execution_replay. | §7.6 |
| `observability_level` | **Defined** as `Config.runtime` knob with CLOSED enum `{mvp, verbose}`. | §7.9 + `config.schema.json` amendment |
| `usage_estimated` (`usage.estimated`) | **Defined** as optional ProviderResult.usage flag. | §7.9 + `provider_result.schema.json` amendment |
| `time_to_finalize` | **Defined** as v1+ metric derived from Artifact timestamps. | §7.10 |
| `role_purity_score` | **Cross-referenced** (Pass 1 row #132); v1+ home is §9 eval harness. | §7.10 + §9 (Pass 7) |
| `disagreement_frequency` | **Defined** as v1 derivation from coordination_turn verdicts. | §7.10 |
| `interaction_graph` | **Defined** as v1 derivation. | §7.10 |
| `delegation_frequency` | **Defined** as v1 derivation. | §7.10 |
| `observability_event` (live stream item) | **Formally deferred** to v1+. | §7.10 |

### 7.12 Schema pre-publication finalization notes

§7 applies one Pass 4 schema amendment and references one
extension:

- **`artifact.schema.json`** — `transcript_digest` added as
  REQUIRED top-level field. §5.10 prose already named Pass 6 as
  the home; the field is now present and validated in §5.13's
  worked example (with a real RFC-8785 JCS-SHA-256 digest).
  Pre-publication finalization; no version bump per §5.1
  (precedent: Pass 5 §6.16).
- **`config.schema.json`** — see §8.11 for the
  `Config.runtime` extensions (`on_budget_exceeded`,
  `observability_level`, `max_tool_iterations`). These are §8-
  owned policy knobs; §7 only references `observability_level`.
- **`provider_result.schema.json`** — `usage.estimated`
  optional boolean added (default false). Promotes the
  Pass 5 §6.15 `usage_estimated` metadata flag to a schema
  field. Pre-publication finalization; no version bump.

---

## 8. Budget, Failure, Security Policies

**[Core MVP]**

### 8.1 Budget enforcement contract

§4.7 already fixes WHERE the runtime checks each hard cap (before
each invocation, after each invocation, at every state
transition) and WHICH termination reason matches each cap. §8
ratifies §4.7 and adds the operator-facing surface.

The MVP hard caps (§4.7), their `Config.budget` keys, and their
matched termination reasons:

| Cap | Config key | Termination reason on breach |
|---|---|---|
| Max rounds | `budget.max_rounds` | `budget_exceeded` |
| Max wall-clock seconds | `budget.max_wallclock_seconds` | `timeout` |
| Max total cost (USD) | `budget.max_total_cost_usd` | `budget_exceeded` |
| Max total tokens | `budget.max_total_tokens` | `budget_exceeded` |
| Max branch depth | `runtime.max_branch_depth` | (no termination — defers per §4.6) |
| Max deferred queue length | `runtime.max_deferred_queue_length` | (no termination — drops new defers per §4.6) |
| Max deferred drains/round | `runtime.max_deferred_drains_per_round` | (advisory, no termination) |
| Per-agent token budget | `budget.per_agent_token_budget[id]` | `budget_exceeded` on breach (MVP MUST per Pass 1 row #149; the cap is enumerated in §4.7 alongside the other budget caps) |
| Selector budget | `budget.selector_budget` | (M4; `budget_exceeded` if breached during selector invocation) |
| Per-agent retry budget | `runtime.per_agent_retry_budget` | (§4.9: `provider_unrecoverable` or `schema_error`) |
| Max tool iterations (adapter) | `runtime.max_tool_iterations` | (§6.4: surfaces as `error.kind = tool_failure` → §4.9 mapping) |

On a breach of any **terminating cap** in the table above (i.e.
any row whose "Termination reason on breach" column names a
termination reason rather than a deferral / drop / advisory
behavior), the runtime invokes
`orchestrator_runtime.terminate(reason)` with the matched reason
from the table. The breach is recorded by way of the resulting
`TerminationArtifact` (§5.8) on disk. Non-terminating caps
(`max_branch_depth`, `max_deferred_queue_length`,
`max_deferred_drains_per_round`) defer or drop per §4.6 and do
not invoke `terminate`.

§4.7's bullet list enumerates `per_agent_token_budget`
alongside the other budget caps; §8.1 is the canonical
statement that `per_agent_token_budget` is a hard cap with
termination reason `budget_exceeded`. The two locations are
consistent.

### 8.2 on_budget_exceeded policy

`Config.runtime.on_budget_exceeded` is a CLOSED enum, MVP value
`{stop}` (default `stop`). The runtime always terminates on cap
breach.

Two values appear in the v0 draft (Pass 1 row #149) — `degrade`
and `escalate` — and were considered for MVP. Both are
**deferred to §12 Roadmap**:

- `degrade` would require the runtime to dynamically reduce
  reasoning effort or panel size mid-session, which conflicts
  with §4.9's MVP immutability of `active_deliberation_panel`.
- `escalate` would require an external escalation channel
  (operator notification, queue routing) absent from the MVP
  batch-only execution mode (ADR-004).

A future MINOR schema bump (per §5.1) opens the enum to admit
v1+ values without breaking MVP-conformant readers.

### 8.3 Failure taxonomy (operator-facing)

The operator-facing failure surface aligns with the §6.6 closed
12-value `error.kind` enum and the §4.7 closed 7-value
termination-reason enum. §8.3 does NOT introduce a third taxonomy
— it organizes the existing kinds into operator-meaningful
categories.

| Category | `error.kind` source | Termination reason on exhaustion |
|---|---|---|
| **Provider failures — transport / quota / model** | `timeout`, `network`, `rate_limit`, `quota_exhausted`, `auth_failure`, `model_unavailable`, `context_length_exceeded`, `invalid_request`, `internal` (§6.6) | `provider_unrecoverable` (or `timeout` if cap fires first) |
| **Agent failures — schema** | `malformed_response` (§6.6) | `schema_error` |
| **Agent failures — role drift** | observability only; not a terminating condition in MVP (Pass 1 row #131, #132) | — |
| **Agent failures — refusal** | `content_filter` (§6.6) — the only operator-facing home for this kind; §6.6 maps it to `provider_unrecoverable` but it is semantically a model refusal (safety filter / hard stop), so the failure-taxonomy view treats it as an agent failure rather than a provider transport failure. | `provider_unrecoverable` |
| **Agent failures — repetition** | v1+ observability (Pass 1 row #59a, #150) | — |
| **Orchestration failures** | structurally prevented (§4.6 queue, §4.7 `max_branch_depth`, §4.10 invariants) | — (preventive, not corrective) |
| **Memory failures — transcript overflow** | preventively bounded by `max_total_tokens` (§4.7); context_packet derivation (§4.3) avoids whole-transcript expansion | — |
| **Memory failures — summarization corruption** | MVP does not summarize (§4.3); §12 Roadmap | — |
| **Memory failures — missing context** | preventively avoided by §4.3 minimum-content set | — |
| **Memory failures — semantic degradation** | v1 observability (`role_purity_score`); §12 mitigation | — |
| **Tool failures** | `tool_failure` (§6.6) — covers both tool input-schema validation failures (§6.4) and tool handler errors. Per §6.6 mapping table, `tool_failure` maps to `provider_unrecoverable` (NOT `schema_error` — `schema_error` is reserved for `malformed_response` from the top-level structured_output). | `provider_unrecoverable` |
| **Security failures — credential leak** | fail-fast at construction (§6.8); pre-persistence redaction (§8.9) | — (preventive) |
| **Security failures — prompt injection** | ADR-003 structured-only control plane (§5.4, §5.5) | — (preventive) |
| **Security failures — malicious persona** | §5.4 append-only canonical_transcript; `additionalProperties: false` (§8.7) | — (preventive) |
| **Security failures — untrusted adapter** | trust boundary documented (§8.7); no MVP mitigation beyond adherence to §6 contract | — (out-of-scope) |
| **User-driven termination** | host signal (§4.7) | `user_cancel` |
| **External-research / user-input requirement** | verdict-payload terminations (§4.4, §4.7) | `external_research_required` / `user_input_required` |

The enum alignment: every `error.kind` from §6.6 maps to exactly
one termination reason via §6.6's table; every termination reason
from §4.7 / §5.8 has a populated row above. No new operator-
facing kinds are introduced — §8 is a re-organization of the
existing closed enums (N9).

### 8.4 Recovery strategies (MUST / SHOULD / MAY split)

Per Pass 1 row #145 Codex turn-1 split:

- **MUST [Core MVP]** — the *mechanism* of a configurable
  failure-handling knob exists: `Config.runtime.on_agent_failure ∈
  {terminate, continue_without}` (§5.2). Default `terminate`.
- **SHOULD [Core MVP]** — `retry` is available via
  `runtime.per_agent_retry_budget` (§4.9, §6.7). Default 2.
  Per Pass 1 row #145 split: "(b) `retry` + `fallback model` +
  `truncate branch` actions available = SHOULD [Core MVP]".
- **Deferred to v1** — `fallback_model`
  (`AgentConfig.fallback_model`). Pass 1 row #145 places this
  at SHOULD [Core MVP], but enabling it in MVP creates an
  attribution gap: the canonical_transcript's `Message` does
  not persist the actual provider/model used for an invocation
  (§5.4), only the `speaker` agent id. A run that exercised
  the fallback would mis-attribute usage to the primary model
  in the §7.9 per-provider/per-model breakdown. v1 absorbs
  both the `fallback_model` field AND an Artifact-level
  attribution amendment (e.g. `Message.provider_used` /
  `Message.model_used`) so the two are consistent. The
  divergence from Pass 1 row #145's classification level is
  intentional and documented; Pass 1 was a classification, not
  a binding ADR.
- **Implicit SHOULD [Core MVP]** — `truncate_branch` is
  degenerate under `max_branch_depth = 1`: no recursive branches
  exist to truncate, so the action is effectively a no-op (the
  defer-queue in §4.6 plays the truncation role). Pass 1 row
  #145 SHOULD is satisfied structurally rather than by an
  explicit knob.
- **MAY [v1]** — `summarize_context`, `replace_agent`. Both
  require runtime support absent in MVP (§4.3 does not summarize;
  §4.9 immutability forbids mid-session replacement).
- **v1+ interactive [ADR-004]** — `pause_session`,
  `request_human_intervention`. MVP supports
  `terminate(reason = user_cancel)` as the only host
  intervention signal.

The recovery actions are NOT schematized as a closed enum in
Pass 6. The MVP MUST-set is carried by `on_agent_failure`'s two
values; v1+ extensions add new `Config.runtime` fields rather
than expand a single closed enum (avoids enum-closure churn at
1.0.0).

### 8.5 Runtime termination contract (§4.7 ratification)

§4.7 already fixes the termination-reason enum (CLOSED, 7
values):

```
budget_exceeded | schema_error | provider_unrecoverable |
user_cancel | timeout | user_input_required | external_research_required
```

§8 ratifies this enum without re-opening it (N8). The §4.9
`apply_agent_failure_policy` outcomes (`provider_unrecoverable`,
`schema_error`) map to termination reasons of the same name.
The §6.6 `error.kind` → §4.9 outcome table is the canonical
mapping; §8 references it without restating.

`user_cancel` is the only host-signaled termination reason. The
host signals via the runtime's cancellation channel (mechanism
implementation-defined; e.g. POSIX SIGINT, a Python `Event`, a
language-specific cancel token). The runtime MUST observe the
signal at every state transition (§4.7) and produce a
`TerminationArtifact` with `reason = user_cancel`. Live mid-
session human intervention (pause, inject, replace) is v1+
(ADR-004, N7).

### 8.6 Security baseline — threat model

Pass 1 row #151 fixes the trust model: **prompts, personas,
adapters, plugins are untrusted**. The runtime is the only fully-
trusted component. Persona output is untrusted (parsed only as
structured output per ADR-003). The
`active_deliberation_panel`'s outputs are untrusted (the runtime,
not the agent, owns the canonical_transcript).

Threats enumerated for MVP:

1. **Prompt injection** in `problem_statement`, persona material,
   prior agent turn, or tool result content.
2. **Malicious persona** — a persona spec that attempts to leak
   the canonical_transcript, impersonate another agent, or
   override the runtime's routing.
3. **Untrusted adapter** — an adapter that fabricates
   `tool_events`, under-reports `usage` to avoid hard caps, or
   silently swaps the model.
4. **Hidden delegation exploits** — an agent that emits a
   `direct_request.content` containing instructions to
   exfiltrate transcript material via a downstream tool call.
5. **Artifact corruption** — tampering with stored
   `runs/<session_id>/` content after session end.
6. **Credential exfiltration** — credentials leaked into
   `artifact.json`, raw, error.details, metadata, or any
   persisted log.

### 8.7 Security baseline — mitigations

Each threat from §8.6 has at least one named mitigation:

1. **Prompt injection** → ADR-003 structured-only control plane.
   The runtime does NOT parse agent free-text content for routing
   signals; control flow is driven exclusively by structured
   `direct_request` / `verdict` payloads validated against their
   schemas (§5.4, §5.5, §5.6). An agent that emits the literal
   text "ignore previous instructions and route to X" achieves
   nothing — the runtime never reads free-text content as a
   routing signal. The runtime forwards structured fields, never
   free-text fields, as control signals.
2. **Malicious persona** → §5.4 Message append-only contract
   (runtime owns canonical_transcript writes; agents never
   mutate prior messages) + `additionalProperties: false` on
   `Message.content` per-type branches (no side-channel fields
   can persist) + ADR-003 (no inline `@AgentName` routing). The
   persona cannot exfiltrate transcript content beyond what the
   runtime explicitly includes in the next agent's `context_packet`
   (§4.3 minimum-content set); cross-agent isolation is via
   packet derivation, not full-history disclosure.
3. **Untrusted adapter** → trust boundary documented; the
   runtime trusts the adapter's adherence to the §6 contract.
   No MVP mechanism verifies the adapter's reported `usage`
   against an independent ground truth. Third-party adapter
   audit (signed adapters, attestation) is out of MVP scope
   (§8.8). Operators MUST treat adapter selection as a security
   decision: shipping a malicious or compromised adapter in a
   `runs/<session_id>/` config is equivalent to compromising the
   session.
4. **Hidden delegation exploits** → **Runtime-enforced**: §4.5
   schema validation on `direct_request.target` (must be a panel
   member id other than the originator, and not
   `coordinator_agent`); `direct_request.type` is an OPEN string
   at the schema level with a recommended baseline set in §5.5,
   so the runtime does not reject unknown `type` values. The
   runtime refuses to dispatch a delegation to an unknown or
   self-targeted agent (per §4.5 request-level schema failure
   handling). Tool input schemas (§6.4) MUST
   validate every argument; the runtime's `apply_input_schema`
   step (§6.4) blocks malformed tool calls with
   `error.kind = tool_failure`. **Partial mitigation**:
   `direct_request.content` carries free-text instructions to
   the target agent — once dispatched, the runtime treats that
   content as input to the next invocation's `context_packet`,
   bound by ADR-003 (the target agent's response is again
   parsed only as structured output). A malicious agent that
   uses `direct_request.content` to instruct a downstream agent
   to misbehave is constrained by the downstream agent's
   schema-validated response shape — the malicious instruction
   cannot bypass the schema. **Out of MVP**: capability-based
   per-agent allowlists for tool access (a v1+ extension that
   would let operators restrict which agents can invoke which
   tools); without these, the tool-author's input-schema
   validation is the documented runtime boundary, and tool-side
   data exfiltration (e.g. a tool that returns sensitive data
   into `tool_event.result`) is a tool-author concern, not a
   runtime concern.
5. **Artifact corruption** → `transcript_digest` (§7.7) is a
   content-hash signal. A reader who re-canonicalizes and
   re-hashes the canonical_transcript detects tampering against
   the stored `transcript_digest`. NOTE: the digest itself is
   inside the artifact; an adversary with write access can
   rewrite both. Full integrity (signed digest, off-host
   storage, audit-log immutability) is out of MVP scope (§8.8).
6. **Credential exfiltration** → §6.8 fail-fast at construction;
   §8.9 pre-persistence sanitization MUST verify no field in the
   persisted Artifact matches any in-memory credential value.

### 8.8 Security baseline — out-of-MVP scope

The following are explicitly NOT MVP requirements:

- Encryption at rest of `runs/<session_id>/`. Disk encryption is
  a host concern (filesystem-level dm-crypt, FileVault, BitLocker,
  EBS encryption); the runtime does not encrypt artifacts.
- Sandboxed adapter / persona execution (seccomp, gVisor,
  container isolation). The runtime executes adapters in-process;
  isolation is the host's responsibility.
- **Signed adapters / adapter attestation** — the runtime does
  NOT verify cryptographic provenance of an `AdapterFactory`
  registration (§6.11) or check that an adapter's behavior
  matches a published specification. An attacker who installs
  a malicious adapter into the host's process can fabricate
  `tool_events`, under-report `usage`, or swap the model. A v1+
  extension MAY add a registry of signed adapter implementations;
  MVP does not.
- **Capability-based per-agent tool allowlists** — every agent
  can invoke every tool registered in `AgentConfig.tools` (§5.2);
  there is no MVP knob to restrict tool access per agent
  *beyond* what the agent's `tools` list declares. A v1+
  extension MAY add a deny-list / capability-grant scheme.
- Signed personas. A persona spec is loaded as JSON / YAML; the
  runtime does not verify cryptographic provenance. A persona
  registry with signing is §12 Roadmap.
- Audit-log immutability. The `transcript_digest` is a content
  hash, not an append-only attestation. A hash chain across
  sessions, off-host audit storage, or HSM-backed signing is
  out of scope.
- FIPS / NIST compliance modes. MVP makes no compliance claims.
- Key rotation enforced by the runtime. Operators rotate
  credentials by replacing the `ProviderCredentials` object at
  construction time; runtime takes no part in scheduling
  rotation.
- Secret-store integration (Vault, AWS KMS, GCP Secret Manager).
  The host provides credentials however it chooses; the runtime
  consumes them at adapter construction (§6.8) and is otherwise
  agnostic.

These deferrals are documented because Pass 1 row #151 lists
"security critical" as MUST [Core MVP]; the deferrals specify
what "security baseline" means in MVP terms vs. what a v1+ /
Roadmap commitment looks like.

### 8.9 Credential handling (§6.8 extension)

§6.8 fixed: credentials passed at construction; fail-fast on
missing / malformed; MUST NOT log credential material in
`metadata`, `raw`, or `error.details`. §8.9 extends:

- Credentials MUST NEVER appear in any persisted artifact, raw
  field, `error.details`, `metadata`, or log emitted by the
  runtime or its adapters (extends §6.8 from "MUST NOT log" to
  "MUST NEVER persist").
- **Pre-persistence sanitization**: before atomically writing
  `artifact.json`, the runtime MUST verify that no string field
  in the Artifact graph contains any credential value held in
  memory. The mechanism is implementation-defined (e.g. a
  redaction pass keyed by the set of `ProviderCredentials`
  values registered at session **init**); the outcome is
  mandatory.
- The same rule applies to the `transcript_journal` and any
  observability log written during the session — the runtime
  MUST scrub credentials from each line before writing.

Out-of-MVP (§8.8): credential rotation policy enforced by the
runtime; secret-store integration; audit-log immutability of
credential access history.

### 8.10 Vocabulary introduced by §8 (status table)

| Item | Disposition | Where |
|---|---|---|
| `on_budget_exceeded` | **Defined** as `Config.runtime` knob with CLOSED enum `{stop}`. | §8.2 + `config.schema.json` amendment |
| `max_tool_iterations` (as runtime knob) | **Promoted** from §6.4 adapter-internal to `Config.runtime`. | §8.1 (referenced) + `config.schema.json` amendment |
| Pre-persistence sanitization | **Defined** as runtime MUST. | §8.9 |
| Operator-facing failure-category table | **Defined** as alignment view over §6.6 + §4.7. | §8.3 |
| Recovery-action MUST/SHOULD/MAY split | **Defined**. | §8.4 |
| `pinning_violation` (cross-ref to §7.6) | **Cross-referenced**. | §8.7 (artifact-corruption boundary) |
| `degrade`, `escalate` (on_budget_exceeded extensions) | **Formally deferred** to §12 Roadmap. | §8.2 |
| `fallback_model` (`AgentConfig.fallback_model`) | **Formally deferred** to v1 schema amendment. Pass 1 row #145 SHOULD-Core-MVP classification is overridden by Pass 6 because the attribution gap (no per-message `provider_used` / `model_used`) makes the §7.9 per-provider/per-model breakdown inconsistent with MVP-level `fallback_model` runs. v1 absorbs both fields together. | §8.4 |
| `summarize_context`, `replace_agent`, `pause_session`, `request_human_intervention` | **Formally deferred** to v1 / interactive mode. | §8.4 |

### 8.11 Schema pre-publication finalization notes

§8 applies Pass 4 schema amendments to `Config.runtime` and
`termination_artifact`:

- **`runtime.on_budget_exceeded`** — CLOSED enum `{stop}`,
  default `stop`. MVP shipping value. `degrade` / `escalate`
  reserved for §12 Roadmap and require a future MINOR schema
  bump.
- **`runtime.observability_level`** — CLOSED enum
  `{mvp, verbose}`, default `mvp`. Controls the metric set
  computed (§7.9 vs §7.10).
- **`runtime.max_tool_iterations`** — integer ≥ 1, default 8.
  Promoted from §6.4 adapter-internal cap. Runtime forwards to
  every adapter that supports internal-loop tool calling;
  adapters without an internal loop ignore the value.
- **`termination_artifact.transcript_digest`** — pattern tightened
  to `^[0-9a-f]{64}$` (was `minLength: 1`). Brings into parity with
  `Artifact.transcript_digest` and `RunManifest.transcript_digest`.

**Version bump decision**: pre-publication finalization. No
bump. Precedent: Pass 5 §6.16 (`error.kind` closure + tool_events
tightening). `schema_version` remains `1.0.0` across all amended
files.

Justification:
- The Pass 4 §5.14 surface explicitly listed `synthesize_on_terminate`
  (already in config) for Pass 6 absorption.
- The Pass 5 §6.15 surface listed `max_tool_iterations` and
  `usage_estimated` as Pass 6+ candidates.
- The Pass 1 row #149 split surfaces `on_budget_exceeded` as a
  Core MVP MUST mechanism; the enum-closure to `{stop}` is the
  MVP-conformant subset.
- The 1.0.0 release has NOT yet been cut; no implementor has
  shipped against any post-publication artifact.

Post-publication changes default to MINOR (1.1.0) per §5.1 for
any acceptance-behavior change (enum-closure, required-field
addition, content-shape restriction). PATCH is reserved for
schema bug fixes that do not change acceptance behavior.

---

## 9. Testing & Evaluation

**[Core MVP]** (FakeProvider, golden tests, scheduler property
tests, schema-validation tests, replay tests)
**[v1]** (evaluation harness, postmortem)

§9 specifies what an implementor MUST and SHOULD test to demonstrate
conformance with §1–§8. It does NOT re-specify runtime mechanics
(rule N6); every test exercises a contract owned by a prior chapter
(§4 scheduling, §5 schemas, §6 adapter, §7 persistence/replay, §8
budgets/failure). §9 introduces two new schemas
(`fake_provider_script.schema.json`,
`golden_test_case.schema.json`) and a vocabulary block (§2 amendment
in §9.12); every other surface is a verification recipe over the
existing §1–§8 vocabulary.

The test-harness layout (e.g. `tests/property/`, `tests/golden/`)
is RECOMMENDED but not normative. An implementation MAY organize
tests differently provided the recipes below remain mechanizable.

### 9.1 FakeProvider contract

`FakeProvider` is the canonical name (rule N4 — not a vendor name)
for the spec's test adapter. It conforms to the full §6 contract
bit-for-bit:

- Registers via the same `AdapterFactory` registration mechanism
  as any production adapter (§6.11). The runtime sees a
  `provider_adapter` indistinguishable from a production one
  through the §6.1 surface.
- Implements `invoke(request: ProviderRequest) -> ProviderResult`
  (§6.1). Synchronous return. Single-threaded (ADR-001). MUST NOT
  spawn parallel invocations.
- Performs structured_output canonicalization (§6.5): every
  returned `provider_result.structured_output` is validated
  against `request.expected_output_schema` before return; the
  validation step runs whether the script's result is well-formed
  or deliberately malformed (the latter exercises the §4.9
  corrective-retry path).
- Uses the CLOSED `error.kind` enum (§6.6, 12 values) and the
  CLOSED `finish_reason` enum (§5.7, 5 values).
- Reports `usage` exactly as scripted; defaults to zero usage when
  the script omits it, so budget-path tests can inject specific
  counts.
- Supports `tool_events[]` (§6.4) verbatim from the script,
  allowing tests to exercise the tool lifecycle without
  external dependencies.

**Substitutions vs a production adapter.** No HTTP, no network, no
LLM. The provider call is a lookup into a pre-authored
`FakeProviderScript`. No clock (the script provides `latency_ms`
when relevant); no randomness (the script is the sole entropy
source).

**Determinism — A2 / N3 qualifier.** FakeProvider determinism is
**unconditional** (§2.7 A2/N3): given a fixed `FakeProviderScript`
and a fixed sequence of `ProviderRequest`s, the FakeProvider
returns bit-identical `ProviderResult`s across runs, on any host,
because no LLM is invoked. This is the strongest determinism
guarantee in the spec — stronger than `transcript_replay`
(unconditional but reads from a stored artifact) and stronger than
`execution_replay` (conditional on §7.6 pinning). The three are
distinct (rule N12); §9 keeps them distinct.

**Replay vs script — boundary.** `FakeProvider` is invoked by the
runtime and *produces* a `canonical_transcript`; `transcript_replay`
(§7.5) reads a *stored* canonical_transcript and re-emits it
without any adapter invocation. Golden tests (§9.3) use FakeProvider
to produce the artifact; the JCS-canonical bytes of the produced
canonical_transcript (mediated by `transcript_digest`, §7.7) are
the comparison point against the stored expected artifact.

**No privileged access.** FakeProvider obeys the §6 contract; it
does NOT mutate the canonical_transcript directly, does NOT bypass
the §6.5 validation, does NOT short-circuit the §4.9 retry path.
A FakeProvider that grants itself special access (e.g. inserting a
turn into the transcript out of band) is non-conformant.

### 9.2 FakeProviderScript schema

The script is defined by
[`fake_provider_script.schema.json`](schemas/v1.0.0/fake_provider_script.schema.json).
Top-level required fields: `schema_version`, `entries[]`. Optional:
`description`, `on_exhaustion`.

`entries[]` is an ordered list. The N-th entry binds to the N-th
`provider_adapter.invoke` call the runtime makes during the
session, counting in dispatch order across rounds, branches, and
synthesis. The §4.11 pseudocode fixes the dispatch order; per
round the order is: deferred-drain (if any) → panel primary_turns
in declared order (each primary_turn may emit one in-line
branch_turn before the next panel agent fires) → coordination_turn.
Synthesis adds one additional invocation when
`verdict.next_action = finalize` and the synthesis path entry of
§4.8 fires.

Per-entry fields:

- `result` (REQUIRED) — the `ProviderResult` returned verbatim by
  this invocation. MUST validate against
  `provider_result.schema.json`. To exercise a §6.6 `error.kind`
  value, populate `result.error` accordingly; to exercise a §6.4
  tool lifecycle, populate `result.tool_events[]`; to exercise a
  `malformed_response` → §4.9 path, set
  `result.structured_output = null` and
  `result.error.kind = "malformed_response"`.
- `match` (OPTIONAL) — assertion fired before the result is
  returned. Checks the inbound `ProviderRequest.agent_id`,
  `expected_output_schema`, and (when the FakeProvider has
  visibility) the context_packet's `round` / `turn_index`.
  Mismatch returns a synthetic `error.kind = internal` with
  `error.message = "fake_provider_script: match assertion failed
  at entry <ordinal>: <constraint>"`; the runtime's §4.9 path
  then terminates the session with `provider_unrecoverable`,
  surfacing the misalignment to the test.
- `comment` (OPTIONAL) — inline rationale for the entry.

`on_exhaustion` is a CLOSED enum:

- `error` (default) — the FakeProvider returns a synthetic
  `ProviderResult` with `error.kind = internal` and
  `error.message = "fake_provider_script: entries exhausted"` when
  the runtime makes more `invoke` calls than the script has
  entries. Surfaces a script-too-short bug.
- `loop` — the FakeProvider rewinds to `entries[0]`. Useful for
  stress tests against a constant provider response.

The schema is `1.0.0` and validates the example fixture
[`fake_provider_script_example.json`](schemas/v1.0.0/examples/fake_provider_script_example.json).
Mechanical validation evidence is in §9.15 (extends Pass 6's
`validate.py` / `validate_negative.py`).

### 9.3 Golden transcripts

A **golden transcript** is the canonical_transcript a conforming
runtime produces when driven by a known input bundle
(`problem_statement`, `Config`, `FakeProviderScript`). The
transcript is "golden" in that it is the regression baseline:
intended runtime changes (bug fixes, schema extensions) regenerate
it; unintended changes (regressions) diverge from it and fail the
test.

**Comparison rule (primary).** A golden test PASSES iff the
runtime, driven by `fake_script` against
`config + problem_statement`, produces an `Artifact` whose
`transcript_digest` (§7.7) equals
`expected_artifact.transcript_digest`. This is byte-level equality
on the JCS-canonical SHA-256 hex digest; raw-JSON formatting
differences do not affect the comparison (§7.5, §7.7).

**Comparison rule (secondary, RECOMMENDED).** Implementations
SHOULD additionally assert byte equality of the JCS-canonical
re-emission of the produced `canonical_transcript` against the
stored one. The secondary assertion catches the (rare) case where
two distinct canonical_transcripts hash to the same digest under a
broken canonicalizer — by exercising the canonicalizer on both
sides of the comparison. Under a correct RFC-8785 implementation
the primary and secondary assertions are equivalent.

**Byte-identity qualifier (rule N3).** Golden-test byte identity
is **conditional on the §9.4.1 GoldenTestCase harness pinning
requirements** — distinct from the §7.6 production
`execution_replay` pinning conditions. Under those harness
pinning requirements, two conforming runtimes processing the same
`(config, problem_statement, fake_script)` MUST produce the same
`transcript_digest`. The pinning is required because two runtime
fields participate in the digest but are NOT derived from the
script: `Message.id` (runtime-allocated) and `Message.timestamp`
(wall-clock-derived). Without test-harness control over those two
sources, two conforming runtimes would produce diverging digests
for identical scripts — a portability failure. §9.4.1 fixes the
required test-harness controls. This is still strictly weaker
than §7.6 production pinning (no provider, model, cache, or tool
environment is involved); it is also strictly stronger than the
unconditional `transcript_replay` guarantee, because golden tests
*produce* a transcript rather than read one. The three contracts
remain distinct (rule N12).

**Update workflow.** When intended runtime behavior changes (a bug
fix that alters output, a schema extension), the operator:

1. Re-runs the golden test against the updated runtime; the test
   fails with a digest divergence and a JCS-canonical diff between
   the produced and stored transcripts.
2. Reviews the diff for intentionality. Because the diff is over
   JCS-canonical bytes, every meaningful change appears once with
   stable formatting; cosmetic re-orderings or whitespace shifts
   cannot mask semantic changes.
3. If the diff is intentional, regenerates `expected_artifact` (by
   running the harness with a "record" flag implementation-defined)
   and commits the updated fixture. The next test run passes.
4. If the diff is unintentional, fixes the runtime regression and
   reruns until the test passes against the original fixture.

### 9.4 GoldenTestCase layout

A `GoldenTestCase` is the bundled input/output shape for a single
golden test. Schema:
[`golden_test_case.schema.json`](schemas/v1.0.0/golden_test_case.schema.json).
Required: `schema_version`, `case_id`, `problem_statement`,
`config`, `fake_script`, `expected_artifact`. Optional:
`description`, `expected_assertions`.

- `case_id` — stable identifier, same charset as `session_id`
  (`^[A-Za-z0-9_-]{1,64}$`, §7.1). A `case_id` MAY serve as a
  `session_id` when the harness writes the produced Artifact to
  disk under `runs/<case_id>/`.
- `problem_statement` — forwarded verbatim to the runtime at
  session **init** (§4.1).
- `config` — a full `Config` object validated against
  `config.schema.json`. The harness MUST register the FakeProvider
  under whichever `provider` identifier the `Config.agents[]`
  reference; the exact registration mechanism is
  implementation-defined (§6.11). A common convention is to use
  the literal string `"fake"` as the provider identifier in
  GoldenTestCase configs.
- `fake_script` — a `FakeProviderScript` instance driving the
  run.
- `expected_artifact` — the `Artifact` instance the runtime is
  expected to produce at session end. MUST validate against
  `artifact.schema.json` AND satisfy the §7.7 semantic check
  (`transcript_digest` equals the JCS-SHA-256 of the
  canonical_transcript; `cumulative_usage` equals the
  message-level usage sum).
- `expected_assertions[]` (OPTIONAL) — named scheduler-property
  assertions the case exercises. Each entry is
  `{category, name, note?}` where `category` is a CLOSED enum
  (`scheduler_invariant`, `budget_cap`, `failure_kind`, `replay`,
  `schema_semantic`) and `name` is an open string identifying the
  specific invariant (canonical names listed in §9.5–§9.9).
  Harnesses MAY validate each named assertion alongside the
  primary digest comparison.

**Directory convention (alternative for in-tree use).** When a
single JSON object is unwieldy (large transcripts, many fixtures),
implementations MAY split a GoldenTestCase across a directory:
`tests/golden/<case_id>/{config.json, fake_script.json,
expected_artifact.json}` plus an optional `description.md`. The
bundled-JSON form remains the canonical interchange format; the
directory form is a convenience layout that MUST round-trip
losslessly to the bundled form.

### 9.4.1 GoldenTestCase harness pinning requirements

Two `Message` fields participate in `transcript_digest` (§7.7) but
are NOT supplied by the FakeProvider script: `Message.id`
(runtime-allocated, §5.4) and `Message.timestamp`
(wall-clock-derived at append time, §5.4). For golden-test byte
identity to hold across conforming runtimes, both fields MUST be
pinned by the test harness. The mechanism is implementation-
defined; the contract is:

1. **Deterministic message-id allocator.** The harness MUST
   supply, to the runtime under test, a message-id allocator
   that produces the same id sequence for the same dispatch
   order. The RECOMMENDED canonical scheme is sequential
   `msg-NNN` ids (zero-padded width chosen by the implementation;
   the example fixture uses three-digit `msg-000`, `msg-001`,
   …). Implementations MAY use any other deterministic scheme
   (e.g. UUIDv5 keyed by `(session_id, round, turn_index,
   branch_depth, parent_id)`); the contract is "byte-identical
   ids for byte-identical dispatch sequences", not a specific
   scheme.
2. **Fixed-clock source.** The harness MUST supply a fixed clock
   source that yields the same `timestamp` for the N-th
   `canonical_transcript.append` call across runs. RECOMMENDED:
   a session-start anchor (taken from `Config.session_id` or a
   harness-supplied seed) plus a per-message offset (e.g. five
   seconds × `(round * 100 + turn_index)`). Implementations MAY
   choose other schemes, including holding the clock constant
   at a single value; the contract is "byte-identical timestamps
   for byte-identical dispatch sequences", not a specific
   formula.

These two harness requirements correspond to §7.6 pinning
condition 8 (wall-clock seed); the message-id condition is a
golden-test-only addition, because production runs do not require
deterministic ids for `execution_replay` (the digest comparison
in §7.6 is between two production runs that *both* allocate ids
their own way, but the comparison is broken under that scheme).
A runtime that ships a hook for plugging both controls (e.g. a
`runtime.harness` config block or a constructor parameter)
satisfies the contract; a runtime that hard-codes its allocator
or clock without an override hook cannot ship a conforming
golden-test harness.

**Persona resolution (Pass 6 §7.6 condition 9).** The same
persona resolution rules that apply to `execution_replay` (§7.6)
apply here: when the GoldenTestCase's `config.agents[].persona_ref`
is a string id, the harness MUST resolve to the same Persona
object byte-for-byte as the original recording. The
RECOMMENDED practice is to use inline `persona_ref` (a Persona
object literal) in GoldenTestCase configs, eliminating the
registry-resolution variable. The example fixture uses inline
string `persona_ref` values pointing to test-only persona ids
that the harness resolves deterministically.

### 9.5 Scheduler property tests — §4.10 invariants

The runtime's nine scheduler invariants (§4.10) are individually
testable as **property tests**: each invariant has an acceptance
criterion that holds for every `Artifact` produced by the runtime
against an arbitrary `FakeProviderScript`. A conforming
implementation MUST ship at least one property test per invariant.

| # | §4.10 invariant | Acceptance criterion | Canonical name (for `expected_assertions[].name`) |
|---|-----------------|----------------------|---------------------------------------------------|
| 1 | Single-threaded | At any wall-clock instant during the run, exactly zero or one `provider_adapter.invoke` call is in flight; the FakeProvider asserts a panic on concurrent entry. | `single_threaded` |
| 2 | Declared-order dispatch | For every `round` in the produced Artifact's `canonical_transcript`, the `primary_turn` messages (filtered by `type = primary_turn`) appear in the same order as `config.selector.default_deliberation_panel` (modulo `panel_contraction` annotations per §4.9). | `declared_order` |
| 3 | Coordinator-last | For every **fully-completed** `round` (one whose `coordination_turn` was reached — i.e. no hard-cap breach or unrecoverable failure interrupted the round mid-panel), exactly one `coordination_turn` exists and its `turn_index` is strictly greater than every `primary_turn`'s `turn_index` within the same round. A round interrupted by `terminate(reason)` before reaching the coordinator has zero `coordination_turn` entries — this is expected per §4.7 / §4.9 and does NOT violate the invariant. | `coordinator_last` |
| 4 | Branch closure before round advance | Every `branch_turn` message (`branch_depth = 1`) is followed in `(round, turn_index)` order by either (a) a `primary_turn` in the same round whose `turn_index` is strictly greater, or (b) the round's `coordination_turn` — never by another branch_turn in a different parent chain. | `branch_closure_before_round_advance` |
| 5 | No B→C trigger | For every `branch_turn` with non-empty `content.direct_requests[]`, those requests appear as `suggested_followups` annotations on the same `branch_turn` message and do NOT appear as later `branch_turn` messages with `parent_id = <this message id>` (i.e. branches never spawn branches). | `no_b_to_c` |
| 6 | No transcript reinjection by Coordinator | The only message mutations across the canonical_transcript are runtime-emitted annotations (`panel_contraction`, `dropped_deferred`, `schema_failure`); no Coordinator turn rewrites or reorders a prior message. Testable via a transcript-monotonicity property: every produced canonical_transcript, read by `(round, turn_index)`, is an append-only suffix of the runtime's per-turn journal. | `no_transcript_reinjection` |
| 7 | Deterministic over `provider_result` sequence | Two runs of the same `(config, fake_script, problem_statement)` **under the §9.4.1 harness pinning requirements** produce byte-identical `transcript_digest` values. Property: `digest(run1) == digest(run2)`. The qualifier is REQUIRED — without pinned `Message.id` allocator and clock, runtime-allocated fields legitimately differ across runs (rule N3). | `scheduler_deterministic` |
| 8 | Hard-cap supremacy | For every hard cap, a FakeProviderScript that drives the cap to breach (see §9.6) produces an `Artifact.outcome.kind = "termination"` with the matched `outcome.termination_artifact.reason`, even when the most recent `coordination_turn.verdict.next_action` is `continue` or `finalize`. | `hard_cap_supremacy` |
| 9 | canonical_transcript as sole source of truth | The §7.7 semantic check (digest equality, usage-sum parity) holds on every produced Artifact: `transcript_digest == jcs_sha256(canonical_transcript)` and `cumulative_usage == sum(canonical_transcript[].usage)`. | `canonical_transcript_sole_truth` |

**Property-test form (RECOMMENDED).** For invariants 1–8 the
property is `forall script in FakeProviderScript: P(run(config,
script))`. A conforming property suite generates a representative
family of scripts (e.g. via property-based-testing harnesses like
Hypothesis / fast-check / QuickCheck, or via a hand-curated
catalogue covering the §4.11 pseudocode branches) and asserts the
property on every produced Artifact. Generated scripts MUST
remain schema-valid against `fake_provider_script.schema.json`.

**Roundtrip property (invariant 9).** Every produced Artifact MUST
pass the §7.7 semantic check (Pass 6 ratified for fixtures; Pass 7
ratifies as a §9 verification recipe). The §9 verification suite
MUST reject any produced Artifact that fails the semantic check
— this is a test-side assertion, not a new runtime obligation
(rule N6). Implementations MAY wire the semantic check into the
runtime's persist path (§4.1 **persist** phase) as a defence-in-
depth check, but §9 does not mandate it; §7.3 / §7.4 own
persistence-path semantics.

**ADR-005 separation assertions.** Three property tests verify
the three-role separation (Selector / OrchestratorRuntime /
CoordinatorAgent) directly:

- `selector_fixed_no_provider_invocation` — for `strategy =
  fixed` runs, the FakeProvider observes zero `invoke` calls
  attributable to the selector phase (the script's entry-0 binds
  to the first agent invocation, not to selector). The FakeProvider's
  internal call counter MUST equal the count of dispatch events
  in §4.1 deliberation phase + finalize phase. Categorized as
  `scheduler_invariant`.
- `coordinator_advisory_only` — for a script that causes a
  hard-cap breach during a round whose coordinator emits
  `verdict.next_action = continue`, the runtime MUST terminate
  with the matched cap reason; the produced Artifact's
  `outcome.kind = "termination"` despite the verdict.
  Categorized as `scheduler_invariant`.
- `runtime_hardcap_authority` — symmetric form: for a script whose
  coordinator emits `verdict.next_action = finalize` after a cap
  has been breached, the runtime MUST terminate with the cap
  reason, NOT proceed to synthesis. The Coordinator opined;
  the runtime executed. Categorized as `scheduler_invariant`.

### 9.6 Budget-enforcement tests — §8.1

Each row of the §8.1 table has a FakeProviderScript that drives
the cap to breach (when the cap is termination-bearing) or to its
structural effect (when the cap is non-terminating). An MVP-
conformant test suite MUST cover every row.

| §8.1 cap | Test recipe (FakeProviderScript shape) | Expected outcome | `expected_assertions[].name` |
|----------|----------------------------------------|------------------|------------------------------|
| `budget.max_rounds` | Configure `max_rounds = 1`; script ≥1 successful round whose coordinator emits `verdict.next_action = continue`; runtime opens round 2 → cap fires. | `outcome.kind = "termination"`, `reason = "budget_exceeded"`. | `budget_max_rounds` |
| `budget.max_wallclock_seconds` | Configure `max_wallclock_seconds = N`; the §9.4.1 fixed-clock harness advances the simulated clock by `> N` seconds between the start anchor and one of the §4.7 hard-cap check points (pre-invocation / post-invocation / state transition). Wallclock is owned by the runtime via its clock source, NOT by the FakeProvider — `tool_events[].latency_ms` measures only the tool-handler interval (§6.4) and is the wrong proxy for invocation wallclock. | `outcome.kind = "termination"`, `reason = "timeout"`. | `budget_max_wallclock_seconds` |
| `budget.max_total_cost_usd` | Script populates `usage.cost_usd` such that cumulative cost exceeds the cap. | `outcome.kind = "termination"`, `reason = "budget_exceeded"`. | `budget_max_total_cost_usd` |
| `budget.max_total_tokens` | Script populates `usage.total_tokens` such that cumulative tokens exceed the cap. | `outcome.kind = "termination"`, `reason = "budget_exceeded"`. | `budget_max_total_tokens` |
| `budget.per_agent_token_budget[<id>]` | Configure a per-agent cap < per-session cap; script that agent's invocation with `usage.total_tokens` exceeding the per-agent cap. | `outcome.kind = "termination"`, `reason = "budget_exceeded"`. | `budget_per_agent_token` |
| `budget.selector_budget` (only when `selector.strategy = llm`) | Configure `strategy = llm` with a tight `selector_budget`; script the selector invocation to report `usage` exceeding it. (MVP `strategy = fixed` has no LLM call, so this test is gated on the v1+ LLM selector being available — the recipe is documented for forward consistency.) | `outcome.kind = "termination"`, `reason = "budget_exceeded"`. | `budget_selector` |
| `runtime.max_branch_depth` | Configure `max_branch_depth = 1` (the MVP default); script a primary_turn emitting two direct_requests. | First request dispatches in-line as a branch_turn (depth 1); second request defers (no termination); structurally the runtime appends one `branch_turn` per primary_turn and queues the remainder. | `budget_branch_depth_structural` |
| `runtime.max_deferred_queue_length` | Configure a small queue (e.g. 2); script a sequence of primary_turns that enqueue more deferred requests than the cap. | Overflow defers are dropped with `dropped_deferred` annotations on the originating primary_turn (§4.6); no termination. | `budget_deferred_queue_structural` |
| `runtime.max_deferred_drains_per_round` | Configure cap = 1; script a session that accumulates ≥2 defers before round N opens. | Exactly one drain per round open; remaining defers persist to subsequent rounds (or are dropped on overflow per the prior row). | `budget_deferred_drains_structural` |
| `runtime.per_agent_retry_budget` | Configure budget = 1; script a `malformed_response` followed by a second `malformed_response` for the same agent. | `apply_agent_failure_policy` triggers `schema_error` termination (with `on_agent_failure = terminate`) or `panel_contraction` (with `continue_without`). | `budget_retry_structural` |
| `runtime.max_tool_iterations` (adapter-internal) | The runtime hands `max_tool_iterations` to the adapter via the adapter-construction contract (§8.11). The FakeProvider directly emits `error.kind = "tool_failure"`, `retriable = false`, with `error.message` and `error.details` naming the cause (e.g. `details.reason = "max_tool_iterations"`). The number of `tool_events[]` entries is irrelevant: §6.4 counts loop *iterations* (each iteration may carry a batch of parallel `tool_events[]`), and the FakeProvider does not run the loop — it scripts the terminal failure shape directly. | FakeProvider surfaces `error.kind = "tool_failure"`, `retriable = false`; runtime maps to `provider_unrecoverable` via §6.6. | `budget_tool_iterations_structural` |

The MVP-MUST subset is rows 1–5 (termination-bearing) + row 10
(retry budget → §4.9 path). Rows 6–9 and 11 SHOULD be covered;
each row has at least one test in the conformance suite.

### 9.7 Failure-taxonomy tests — §6.6 / §8.3 / §4.9

Every value of the `error.kind` CLOSED enum (§6.6, 12 values)
MUST have a FakeProviderScript-based test exercising its §4.9
outcome. Tests MUST be exhaustive (rule N9). The §6.6 mapping
table is the canonical reference for the expected outcome.

| `error.kind` | Script recipe | Expected §4.9 outcome | `expected_assertions[].name` |
|--------------|---------------|------------------------|------------------------------|
| `timeout` | `result.error.kind = "timeout"`, `retriable = true`. Repeat for `per_agent_retry_budget + 1` consecutive entries. | After exhaustion: `apply_agent_failure_policy(timeout)` → `provider_unrecoverable` (or `timeout` if `max_wallclock_seconds` fires first). | `failure_timeout` |
| `network` | Same shape with `kind = "network"`. | After exhaustion: `provider_unrecoverable`. | `failure_network` |
| `rate_limit` | Same shape with `kind = "rate_limit"`. | After exhaustion: `provider_unrecoverable`. | `failure_rate_limit` |
| `quota_exhausted` | Single entry with `kind = "quota_exhausted"`, `retriable = false`. | Immediate: `provider_unrecoverable`. | `failure_quota_exhausted` |
| `auth_failure` | Single entry with `kind = "auth_failure"`, `retriable = false`. | Immediate: `provider_unrecoverable`. | `failure_auth_failure` |
| `model_unavailable` | Single entry with `kind = "model_unavailable"`, `retriable = false`. | Immediate: `provider_unrecoverable`. | `failure_model_unavailable` |
| `context_length_exceeded` | Single entry with `kind = "context_length_exceeded"`, `retriable = false`. | Immediate: `provider_unrecoverable`. | `failure_context_length_exceeded` |
| `content_filter` | Single entry with `kind = "content_filter"`, `retriable = false`. | Immediate: `provider_unrecoverable` (§8.3 treats as agent-failure refusal; §6.6 maps to `provider_unrecoverable`). | `failure_content_filter` |
| `invalid_request` | Single entry with `kind = "invalid_request"`, `retriable = false`. | Immediate: `provider_unrecoverable`. | `failure_invalid_request` |
| `malformed_response` | Entry with `structured_output = null` and `error.kind = "malformed_response"`, `retriable = true`. Repeat for `per_agent_retry_budget + 1` consecutive entries. | After exhaustion: `apply_agent_failure_policy(schema_error)` → `schema_error`. With `on_agent_failure = continue_without`: `panel_contraction`. | `failure_malformed_response` |
| `tool_failure` | Entry with `tool_events[]` carrying an `error` on one event; `result.error.kind = "tool_failure"`, `retriable = false`. | Immediate: `provider_unrecoverable` (per §6.6 default mapping; adapter MAY override `retriable`). | `failure_tool_failure` |
| `internal` | Single entry with `kind = "internal"`, `retriable = false`. | Immediate: `provider_unrecoverable`. | `failure_internal` |

**Coordinator-specific tests.** §4.9 prohibits Coordinator
contraction; on Coordinator failure, the runtime MUST terminate
regardless of `on_agent_failure`. The failure-taxonomy suite MUST
include at least one test per `error.kind` value targeted at the
Coordinator's invocation (the coordination_turn slot or the
synthesis invocation), verifying that the outcome is termination
(not contraction).

**Empty-panel termination.** When `on_agent_failure =
continue_without` and the script contracts every panel member, the
runtime MUST terminate with `provider_unrecoverable` (§4.9). A
suite SHOULD include one test exercising this path.

### 9.8 Replay tests — §7.5 / §7.6 / §7.7

The replay surface has three contracts; §9 ships at least one
property test per contract.

**`transcript_replay` (§7.5)** — UNCONDITIONAL byte-identity
(N3). Recipe: (a) run a `GoldenTestCase` end-to-end with
FakeProvider, capture the produced `Artifact`; (b) feed the
Artifact's `canonical_transcript` to `transcript_replay`; (c)
assert the JCS-canonical re-emission of the re-rendered
transcript is byte-identical to the JCS-canonical re-emission of
the original. The `transcript_digest` (§7.7) is the integrity
signal: `jcs_sha256(replay_output) == artifact.transcript_digest`.
Canonical name: `replay_transcript`.

**`execution_replay` (§7.6)** — CONDITIONAL byte-identity (N3,
under the ten pinning conditions). Recipe: (a) run a
`GoldenTestCase` with FakeProvider, capture the produced
`Artifact` and its `RunManifest`; (b) re-run with the *same*
`(runtime, adapter, provider, model, sampling, cache, tool_env,
wallclock, persona, transcript_prefix)` state — when the
adapter is FakeProvider the §9.4.1 harness pinning supplies the
wallclock and the message-id allocator (the latter is a
golden-test-only extension to the §7.6 set, see §9.4.1
rationale); (c) assert the second run's
`Artifact.transcript_digest` equals the first's. Because
FakeProvider has no LLM and no real provider behind it, the only
non-trivial pinning conditions in a FakeProvider-only golden test
are conditions 1–2 (runtime / adapter implementation versions),
8 (wallclock, supplied by §9.4.1), and 9 (resolved persona
material). The other six (provider, model, sampling, cache,
tool_env, transcript_prefix) are trivially constant or
script-determined. Canonical name: `replay_execution`.

**`pinning_violation` (§7.6)** — recipe: run a GoldenTestCase
once, then re-run with one pinning condition deliberately changed
(e.g. inject a different `fake_script`, which violates the
broader notion of "starting state"; or change
`AdapterFactory.version`, which violates condition 2). The
runtime MUST emit a `pinning_violation` diagnostic with the
correct `condition` enum value and MUST NOT produce a fresh
`Artifact`. Every condition in the §7.6 ten-element enum SHOULD
have a test; the MVP MUST subset is conditions 1–2 (runtime /
adapter implementation versions) because those are the most
common operator-driven failures. Canonical name:
`replay_pinning_violation`.

### 9.9 Schema-validation tests — §5.15 / §7.7 semantic

**Existing surface (Pass 4–6).**
`docs/schemas/v1.0.0/examples/validate.py` and
`validate_negative.py` already enforce:

- Every example fixture validates against its schema (positive
  set: 25/25 at Pass 6 sign-off; Pass 7 adds new fixtures —
  count updated in §9.15).
- Every intentionally-invalid instance rejects (negative set:
  33/33 at Pass 6; Pass 7 adds new negative cases).
- Every Artifact fixture passes the §7.7 semantic check
  (transcript_digest equality, cumulative_usage sum parity).

Pass 7 EXTENDS the harness with:

- `fake_provider_script.schema.json` positive validation of the
  Pass-7 example fixture and at least one negative case (e.g.
  missing `entries`, malformed `match.expected_output_schema`).
- `golden_test_case.schema.json` positive validation of the
  Pass-7 example fixture and at least one negative case (e.g.
  `case_id` outside the charset, or `expected_artifact` failing
  its §7.7 semantic check).
- A **roundtrip property** assertion: for every Artifact produced
  by the runtime (driven by FakeProvider on the example
  GoldenTestCase), the §7.9 verification suite MUST reject any
  produced Artifact that fails the §7.7 semantic check. This is
  a test-side assertion, not a runtime self-check (rule N6 — §9
  does not extend runtime obligations); §7.3 / §7.4 retain
  persistence-path ownership.

Canonical name for `expected_assertions[].name` in this category:
`semantic_digest` (transcript_digest equality) and
`semantic_usage_sum` (cumulative_usage parity).

### 9.10 Evaluation harness — §7.10 v1 metrics

The evaluation harness computes the §7.10 v1 metric set on a
single session or a batch and produces a structured report. The
harness is **[v1]**; an MVP runtime MAY ship it under
`observability_level = mvp` as a stub (no metrics computed) or
omit it entirely.

**Scope split.** §7.10 lists seven v1 metrics: five are
**deliberation-quality** metrics derivable from a persisted
Artifact alone, and two are **observability-event** metrics
requiring a live invocation-event stream (the `observability_event`
schema that §7.10 / §7.11 formally deferred to v1+). §9 owns the
recipe for the first five; the latter two are **re-deferred** to
v1+ inheriting the Pass 6 §7.11 deferral:

| §7.10 metric | §9 home | §7.10 status |
|---|---|---|
| `role_purity_score` | §9.10.1 (recipe) | covered |
| `disagreement_frequency` | §9.10.2 (recipe) | covered |
| `interaction_graph` | §9.10.3 (recipe) | covered |
| `delegation_frequency` | §9.10.4 (recipe) | covered |
| `time_to_finalize` | §9.10.5 (recipe) | covered |
| Provider-level retry count per agent (corrective + transport) | re-deferred to v1+ | requires `observability_event` stream |
| Failure count per agent by `error.kind` | re-deferred to v1+ | requires `observability_event` stream |

A v1-conformant harness MUST compute every "covered" metric
above. The two re-deferred metrics MAY be added once the
`observability_event` schema lands; §9 does not assign them a
recipe in MVP because the underlying data source does not exist
in the MVP MUST-set.

**Inputs.** A persisted `runs/<session_id>/artifact.json` (and
`manifest.json` for status / digest cross-check). No live event
stream is required (§7.10 defers `observability_event` to v1+).

**Outputs.** A structured report keyed by `session_id` carrying
one entry per metric below.

#### 9.10.1 `role_purity_score`

Per-agent score in `[0, 1]` measuring scope adherence.

```
role_purity_score(agent) = max(0,
    1 − scope_violations(agent) / max(1, total_turns(agent))
)
```

where:

- `total_turns(agent)` = count of `canonical_transcript[]` entries
  with `speaker = agent.id` and
  `type ∈ {primary_turn, branch_turn}`.
- `scope_violations(agent)` = count of detected violations.
  A violation is recorded when one of the following holds for
  the agent's turn:
  - (V1) The agent is a `domain_persona`, and the turn's
    `content.text` matches a pattern from
    `persona.forbidden_domains[]` per the harness's MVP
    rule-based detector (case-insensitive substring match on the
    `forbidden_domain` name; v1+ MAY swap in an LLM-based
    detector — see §9.11 postmortem `capability_gaps`).
  - (V2) The agent's persona has a `must_delegate[<forbidden_domain>
    → <target_persona>]` entry, the turn's `content.text` matches
    `<forbidden_domain>` via the same MVP rule-based detector used
    in V1 (case-insensitive substring match on the
    forbidden-domain name), AND the turn's
    `content.direct_requests[]` does not contain an entry whose
    `target` equals `<target_persona>`. The "domain the turn
    touches" predicate is therefore concrete: the V1 detector is
    reused. v1+ MAY swap in an LLM-based topic detector — the
    swap is shared with V1.
  - (V3) A `schema_failure` annotation is present on the turn
    (the agent failed to emit a valid direct_request — a
    structural scope failure).

**Edge cases.**

- `horizontal_persona` (no `forbidden_domains`,
  no `must_delegate`) → V1 / V2 are vacuously false; only V3
  applies. A horizontal_persona with zero `schema_failure`
  annotations scores `1.0`.
- `total_turns(agent) = 0` (agent contracted out before any
  turn) → score is `null` (not computed; the agent did not
  contribute).
- A turn with a panel_contraction annotation is NOT counted in
  `total_turns(agent)` because the agent did not produce it.

**Determinism qualifier (N3).** The MVP rule-based detector is
deterministic — identical detector inputs yield identical
violation counts. The optional v1+ LLM-based detector is NOT
deterministic without §7.6 pinning; a harness using it MUST flag
the score with an `estimator = "llm"` annotation and SHOULD NOT
include the score in golden-test `expected_artifact` comparisons.

#### 9.10.2 `disagreement_frequency`

Per-session non-negative real measuring how often the Coordinator
records unresolved disagreements.

```
disagreement_frequency = (
    sum over rounds R of |coordination_turn[R].verdict.unresolved_disagreements|
) / max(1, num_rounds)
```

where `num_rounds = max(canonical_transcript[].round)`. A
per-round series `[r → |unresolved_disagreements|]` SHOULD also be
emitted for trend analysis.

#### 9.10.3 `interaction_graph`

Directed graph with weighted edges:

```
edges = [
    {
        speaker: <agent_id>,
        target: <direct_request.target>,
        type: <direct_request.type>,
        count: <occurrences>
    }
    for every (speaker, target, type) tuple
    derived from canonical_transcript[].content.direct_requests[]
    over messages of type ∈ {primary_turn, branch_turn},
    aggregated and de-duplicated by (speaker, target, type)
]
```

Serialization: an edge list (JSON array of edge objects). A node
list `[<agent_id>]` SHOULD also be emitted, derived from
`config.agents[].id ∪ {config.coordinator.id}`. Self-loops
(speaker == target) are included if they appear in the data.

#### 9.10.4 `delegation_frequency`

Non-negative real in `[0, 1]` measuring how often inter-agent
requests carry the canonical `delegation` type.

```
delegation_frequency = (
    sum over edges E in interaction_graph of E.count where E.type = "delegation"
) / max(1, total_edge_count)
```

where `total_edge_count = sum over edges E of E.count`. Edge case:
total_edge_count = 0 → `delegation_frequency = 0.0`.

#### 9.10.5 `time_to_finalize`

Per-session non-negative integer in seconds:

```
time_to_finalize = floor(
    parse_iso8601(Artifact.ended_at) − parse_iso8601(Artifact.started_at)
)
```

Both timestamps are required by `artifact.schema.json` (§5.10).
v1+ framing per §7.10 is cross-session deliberation-quality
benchmarking (e.g. compare distributions across panel
configurations).

#### 9.10.6 Curated reference problem set

Pass 7 SHIPS NO normative benchmark. An implementation's
evaluation harness MAY use any curated problem set; a smoke-test
suite typically uses 3–5 problems covering: a healthy
synthesis path, a budget-exceeded termination, a malformed_response
recovery, an unrecoverable agent failure with
`on_agent_failure = continue_without`, and a request_user_input
verdict path. The choice is illustrative, not normative.

### 9.11 Postmortem (v1)

Per Pass 1 row #133 (MAY [v1]), an evaluation harness MAY
optionally produce a postmortem object answering structured
questions about a session. Pass 7 defers schematization; the
shape is documented here as v1 prose for forward consistency.

Suggested fields:

- `correct_agents_selected: boolean` — were the panel agents
  appropriate for the problem? Detector mechanism is
  implementation-defined; an LLM-based judge is typical.
- `scope_violations: [{agent, turn_message_id, evidence}]` — all
  `scope_violation` events recorded by §9.10.1's detector.
- `capability_gaps: [{missing_capability, reason, suggested_persona,
  evidence}]` — Pass 1 row #124 missing-capability detection.
  `missing_capability` names the capability the panel lacked;
  `reason` describes why the gap was identified (the verbatim
  Pass 1 #124 field); `suggested_persona` names a Persona that
  could fill the gap; `evidence` cites the canonical_transcript
  message ids that demonstrate the gap (e.g. recurring unresolved
  disagreements no agent had standing to address). The first
  three fields match Pass 1 #124 exactly; `evidence` is Pass 7's
  addition for postmortem actionability.
- `agent_overlap: [{topic, agents}]` — multiple agents
  redundantly addressed the same sub-problem. Inverse of
  capability_gaps.
- `best_contributors: [{agent, contribution_score}]` — ranked
  list. `contribution_score` is implementation-defined (a
  reasonable MVP is `total_turns(agent) × (1 −
  schema_failure_rate(agent))`).

A v1-conformant harness MAY emit any subset of the above; MVP
harnesses MAY skip the postmortem entirely. v1+ MAY schematize as
`postmortem.schema.json` once the field shapes stabilize.

### 9.12 Vocabulary introduced by §9 (status table)

| Item | Disposition | Where |
|------|-------------|-------|
| `fake_provider` (canonical adapter name; rule N4) | **Defined** in §2 amendment block. | §9.1 + §2 amendment |
| `FakeProviderScript` shape | **Defined** as new schema. | §9.2 + `fake_provider_script.schema.json` |
| `FakeProviderScript.on_exhaustion` CLOSED enum `{error, loop}` | **Defined**. | §9.2 + schema |
| `FakeProviderScript.entries[].match` CLOSED-field assertion shape | **Defined**. | §9.2 + schema |
| `GoldenTestCase` bundle | **Defined** as new schema. | §9.4 + `golden_test_case.schema.json` |
| `GoldenTestCase.expected_assertions[].category` CLOSED enum `{scheduler_invariant, budget_cap, failure_kind, replay, schema_semantic}` | **Defined**. | §9.4 + schema |
| GoldenTestCase harness pinning (deterministic message-id allocator + fixed-clock source) | **Defined** as test-harness obligation, distinct from §7.6 production pinning. | §9.4.1 |
| ADR-005 separation assertions (`selector_fixed_no_provider_invocation`, `coordinator_advisory_only`, `runtime_hardcap_authority`) | **Defined** as §9.5 property tests verifying ADR-005 role separation. | §9.5 |
| `evaluation_harness` | **Defined** in §2 amendment block; v1 owner. | §9.10 + §2 amendment |
| `property_test` | **Defined** in §2 amendment block; language-agnostic. | §9.5 + §2 amendment |
| `postmortem` | **Defined** as v1 prose recipe (no schema). | §9.11 + §2 amendment |
| `scope_violation` (per-turn event) | **Defined** in §2 amendment block. | §9.10.1 + §2 amendment |
| `role_purity_score` recipe | **Defined** (was §7.10 cross-ref; §9 owns the computation). | §9.10.1 |
| `disagreement_frequency` recipe | **Defined**. | §9.10.2 |
| `interaction_graph` serialization | **Defined** as edge list + node list. | §9.10.3 |
| `delegation_frequency` recipe | **Defined**. | §9.10.4 |
| `time_to_finalize` recipe | **Defined**. | §9.10.5 |
| `Message.provider_used` / `Message.model_used` | **Re-deferred** to v1 (inherits Pass 6 §8.4 / §8.10 rationale). MVP golden tests do not exercise `fallback_model`, so per-message attribution is not yet required. | §9.13 |
| `postmortem.schema.json` | **Formally deferred** to v1+ (Pass 1 row #133 is MAY [v1]). | §9.11 |
| `observability_event` live stream | **Re-deferred** to v1+ (inherits Pass 6 §7.11 deferral). MVP property tests do not require live event assertions; the persisted Artifact is sufficient. | §9.13 |
| Adapter audit (signed adapters, attestation) | **Re-deferred** to v1+ (inherits Pass 5 §6.15 / Pass 6 §8.8 deferral). Property tests do NOT verify adapter trustworthiness. | §9.13 |
| Curated reference problem set | **Illustrative**, not normative. | §9.10.6 |
| Test-harness directory layout | **RECOMMENDED**, not normative. | §9 preamble |

### 9.13 Schema pre-publication finalization notes

Pass 7 applies **no amendments** to Pass 4 / 5 / 6 schemas. Three
amendment candidates were considered (Steps 8, 9, and 12 of the
Pass-7 prompt); all were deferred or determined unnecessary:

- **`Message.provider_used` / `Message.model_used` (Pass 4
  `message.schema.json`)** — re-deferred to v1, inheriting Pass
  6 §8.4 / §8.10 rationale. The attribution gap blocks
  `fallback_model` golden tests, but `fallback_model` is itself
  deferred to v1; per-message provider/model attribution
  remains unnecessary for MVP test coverage.
- **`Artifact.postmortem` reference (Pass 4
  `artifact.schema.json`)** — deferred. §9.11 ships postmortem as
  v1 prose; no schema reference is needed in MVP.
- **§4.7 / §8.1 bullet-list tightening** — completed in the
  Pass 9 editorial pass: §4.7 now enumerates
  `per_agent_token_budget` alongside the other budget caps,
  cross-referencing §8.1 and §5.2; the two locations are
  consistent.

**Version bump decision.** N/A — no schemas amended.
`schema_version` for `fake_provider_script.schema.json` and
`golden_test_case.schema.json` is `1.0.0`, matching the rest of
the v1.0.0 surface. The two new schemas join the v1.0.0 set as
pre-publication additions; no version bump per the Pass 5 §6.16
/ Pass 6 §7.12 precedent.

### 9.14 Worked example

A canonical end-to-end worked example is the §5.13 two-round
session (panel `[logician, researcher, critic]`, one in-line fork
in round 1, one drained defer at round-2 open, synthesis at the
end). Its committed Artifact is
[`docs/schemas/v1.0.0/examples/worked_example_artifact.json`](schemas/v1.0.0/examples/worked_example_artifact.json),
mechanically validated by Pass 6 against `artifact.schema.json`
and the §7.7 semantic check.

A FakeProviderScript driving this session would carry 11 entries
in the following declared dispatch order (referencing the
§4.11 pseudocode):

1. `match.agent_id = "logician"`,
   `match.expected_output_schema = "turn_structured_output"`,
   `match.round = 1`, `match.turn_index = 1` — primary_turn
   emitting two direct_requests (researcher, critic).
2. `match.agent_id = "researcher"`, `expected = "turn_structured_output"`,
   `round = 1`, `turn_index = 2` — branch_turn under logician.
3. `match.agent_id = "researcher"`, `expected = "turn_structured_output"`,
   `round = 1`, `turn_index = 3` — primary_turn (no requests).
4. `match.agent_id = "critic"`, `expected = "turn_structured_output"`,
   `round = 1`, `turn_index = 4` — primary_turn.
5. `match.agent_id = "coordinator"`, `expected = "verdict"`,
   `round = 1`, `turn_index = 5` — coordination_turn with
   `next_action = continue`.
6. `match.agent_id = "critic"`, `expected = "turn_structured_output"`,
   `round = 2`, `turn_index = 1` — branch_turn from the drained
   defer (logician → critic from round 1).
7. `match.agent_id = "logician"`, `expected = "turn_structured_output"`,
   `round = 2`, `turn_index = 2` — primary_turn.
8. `match.agent_id = "researcher"`, `expected = "turn_structured_output"`,
   `round = 2`, `turn_index = 3` — primary_turn.
9. `match.agent_id = "critic"`, `expected = "turn_structured_output"`,
   `round = 2`, `turn_index = 4` — primary_turn.
10. `match.agent_id = "coordinator"`, `expected = "verdict"`,
    `round = 2`, `turn_index = 5` — coordination_turn with
    `next_action = finalize`.
11. `match.agent_id = "coordinator"`, `expected = "synthesis_content"`,
    `round = 2`, `turn_index = 6` — synthesis attempt (§4.8).

Wrapping the script in a `GoldenTestCase` with the §5.13 Config
and `expected_artifact = worked_example_artifact` yields a
regression test whose primary assertion is
`digest(produced) == <stored digest of worked_example_artifact>`.
Property assertions exercised by this case (referenced via
`expected_assertions[].name`):

- `declared_order` — round 1 panel order is
  `(logician, researcher, critic)`, matching
  `default_deliberation_panel`. Verifiable by filtering
  `canonical_transcript` for `type = primary_turn ∧ round = 1`
  and checking the `(turn_index)` ordering of `speaker` values.
- `coordinator_last` — round 1 coordinator turn_index = 5 >
  primary turn_indices 1, 3, 4; same for round 2 (turn_index =
  5).
- `branch_closure_before_round_advance` — msg-002 (branch_turn,
  parent msg-001) is followed by msg-003 (researcher primary,
  same round); no second branch_turn appears before the round's
  coordination_turn.
- `no_b_to_c` — msg-002 has empty `direct_requests` (its scope
  was a clean confirmation); had it carried requests, they
  would appear as `suggested_followups` annotations and NOT as
  later branch_turns.
- `runtime_hardcap_authority` (contrast case, not in this
  fixture) — a separate GoldenTestCase with `max_rounds = 1`
  and a round-1 verdict of `continue` would exercise the
  invariant by forcing termination on cap breach despite the
  verdict; left to the suite. The healthy worked example does
  NOT exercise `hard_cap_supremacy` because no cap is breached.
- `canonical_transcript_sole_truth` — semantic check passes:
  `digest(canonical_transcript) == Artifact.transcript_digest`;
  `sum(canonical_transcript[].usage)` ==
  `Artifact.cumulative_usage` (Pass 6 mechanically verified).

The §7.10 metrics computed on this Artifact:

- `role_purity_score`: all three panel agents are
  `domain_persona`-classified in this example (logician is a
  `horizontal_persona` per §2.3, researcher and critic are
  domain_personas). With no `schema_failure` annotations and no
  forbidden_domain hits in the worked Artifact, all three score
  `1.0`.
- `disagreement_frequency`: round 1 verdict carries
  `|unresolved_disagreements| = 1` ("causal direction"); round
  2 verdict carries `|unresolved_disagreements| = 0` (resolved
  in synthesis). Mean over 2 rounds = 0.5.
- `interaction_graph`: edges
  `[(logician, researcher, verification, 1),
    (logician, critic, critique, 1)]`. Two edges, both
  originating from logician.
- `delegation_frequency`: 0 / 2 = 0.0 (no edges have
  `type = delegation`).
- `time_to_finalize`: `Artifact.ended_at − Artifact.started_at`;
  computable from the stored fixture.

**Fixture status.** The §5.13 Artifact already validates; Pass 7
ships a smaller GoldenTestCase fixture (one-round, one-panel-agent,
synthesis path) to keep the schema-validation harness fast — see
§9.15 fixture inventory. The §5.13 ↔ FakeProviderScript
mapping above is documentation, not a committed fixture.

### 9.15 Mechanical validation (Pass 7 evidence)

`docs/schemas/v1.0.0/examples/validate.py` and
`validate_negative.py` are extended to cover the new schemas.
Positive cases added:

- `fake_provider_script.schema.json` ←
  `fake_provider_script_example.json` (3-entry script for the
  minimal 1-agent + coordinator session).
- `golden_test_case.schema.json` ←
  `golden_test_case_example.json` (the minimal session's bundle,
  with a digest computed via RFC-8785 JCS + SHA-256).

Negative cases added:

- `fake_provider_script_missing_entries.json` (no `entries`
  field) — MUST reject.
- `fake_provider_script_invalid_match_schema.json`
  (`match.expected_output_schema` outside the closed enum) — MUST
  reject.
- `golden_test_case_invalid_case_id.json` (`case_id` violates the
  charset pattern) — MUST reject.

Roundtrip property: the `golden_test_case_example.json`'s
`expected_artifact` MUST pass the §7.7 semantic check (digest
equality, usage-sum parity); enforced by
`validate_artifact_semantics` extension in `validate.py`.

Validation evidence files (committed alongside the schemas):

- `validation_positive.txt` — updated.
- `validation_negative.txt` — updated.

Final counts after Pass 7 validation run on 2026-05-25:

- **Positive: 28/28** (Pass 6 ran 25/25; Pass 7 added 2 wrapper
  cases + 1 GoldenTestCase semantic case = 28 total, 0 failed).
- **Negative: 36/36** (Pass 6 ran 33/33; Pass 7 added 3 negative
  cases = 36 total, 0 failed).

The new GoldenTestCase fixture's `expected_artifact` satisfies the
§7.7 semantic check end-to-end:
`transcript_digest = 66db8894ca3eee7335b4ffe87006957b972efc1de1b948171d15c8942ebbb654`
(computed via `rfc8785.dumps` + `hashlib.sha256` over the
canonical_transcript).

### 9.16 Coverage table — Pass-1 rows targeting §9

| Pass-1 row | Description | Disposition | §9 home |
|------------|-------------|-------------|---------|
| #6 | "Goal is NOT to simulate fake personalities" → FakeProvider determinism is testable | Absorbed | §9.1 (FakeProvider determinism) |
| #10 | Testing blocker — without FakeProvider/golden/scheduler not implementable | Absorbed | §9.1, §9.3, §9.5 |
| #59 (structural) | Scheduler property tests — no infinite loops, branch depth, budgets, schema-valid outputs | Absorbed (structural subset) | §9.5 (invariants 1, 4, 7, 8, 9), §9.6 (budget caps) |
| #59a–e (behavioral) | Behavioral rules — repetition / reference / confidence / fact-vs-speculation / cross-domain contamination | **Re-deferred** to §7.10 v1+ observability. These are content-quality assertions over `Message.content`, not scheduler invariants. §9.10.1 `role_purity_score` partially covers cross-domain contamination via V1; full behavioral coverage (repetition detection, reference checking, etc.) requires NLP heuristics out of MVP scope. | (re-deferred) |
| #87 | "System MUST prevent infinite loops, uncontrolled recursion, ping-pong" | Absorbed | §9.5 (invariant 4 branch-closure-before-round-advance + §9.6 max_branch_depth structural test) |
| #124 | Missing-capability detection v1 → postmortem artifact | Absorbed | §9.11 (`capability_gaps` field) |
| #131 | Role purity goal (cross-domain contamination detection) | Absorbed | §9.10.1 + §9.11 (`scope_violations`) |
| #132 | Framework may compute `role_purity_score` (v1 SHOULD) | Absorbed (recipe committed) | §9.10.1 |
| #133 | Postmortem (MAY [v1]) — correct agents? overlap? scope violations? capability missing? best contributors? | Absorbed (prose recipe; schema deferred to v1+) | §9.11 |
| #150 | Observability — provider/model usage / interaction graph / delegation frequency / branch depth | Absorbed (cross-ref §7.9 MVP set + §9.10 v1 recipes) | §9.10 (interaction_graph, delegation_frequency); §7.9 (provider/model usage, branch depth) — §9 ratifies the §7 split |

Summary: **9 §9-targeted Pass-1 rows handled**: 9 absorbed
(including #59 structural and #133 prose recipe for postmortem)
+ 1 explicit re-deferral (#59a-e behavioral subset) with
rationale + §7.10 v1 home for future absorption. 0 silent drops.

---

## 10. Competitive Positioning

**[Core MVP]** (positioning statement, defensible differentiators)
**[v1]** (full comparison table with citations)

### 10.1 Positioning statement

Symposium is an **opinionated protocol for structured, sequential,
adversarial multi-agent deliberation** (§1). It is **not** a generic
agent framework, **not** a multi-persona prompt, **not** an
arbitrary-topology orchestration engine, and **not** a host
environment (§3, ADR-001, ADR-005). The protocol's value is
structural: a declared-order panel emits one `primary_turn` per
member per round (R1); a separated `coordinator_agent` emits a
machine-readable `verdict` (ADR-002); a deterministic
`orchestrator_runtime` schedules, terminates, and persists
(ADR-005). The three roles are distinct; the control plane is
structured (ADR-003); the canonical_transcript is append-only and
artifact-first (§2.4, §7.3). Comparison to other multi-agent
frameworks in §10.3–§10.6 is descriptive of structural trade-offs,
not a quality claim.

### 10.2 Defensible differentiators (D1–D6)

Each differentiator below names a structural property of the
Symposium protocol and the ADR / refinement / closed-mechanism
that anchors it. No differentiator is a quality claim ("better",
"faster", "smarter", "more powerful" are forbidden). The form is:
property → anchor → contrast with the prevailing alternative.

**D1 — Opinionated deliberation protocol vs. generic agent
framework.** Symposium enforces one topology: a fixed, declared-
order `deliberation_panel` whose members each take one
`primary_turn` per round (R1, ADR-001), followed by a
`coordination_turn` from a single, structurally-separated
`coordinator_agent` (ADR-005). Branch depth is bounded
(`max_branch_depth` default 1, §4.5–§4.6). Competitors are
designed to express arbitrary multi-agent topologies; Symposium
expresses exactly one. The trade-off is intentional: the protocol
gives up topology flexibility to obtain testable scheduler
invariants (§9.5) and the `transcript_replay` byte-identity
guarantee of §7.5 (D3 contract 1).

**D2 — Structural role purity.** Persona scope is schematized into
two disjoint classes (`horizontal_persona`, `domain_persona`)
with distinct required-field sets (§5.3, A5, Pass 1 Q2). A
`domain_persona` declares `domain_scope`, `forbidden_domains`,
and `must_delegate` mappings; a `horizontal_persona` carries
`reasoning_scope` with no `forbidden_domains` (reasoning is
cross-domain by construction). The runtime enforces the
`must_delegate` mapping through the `direct_request` structured
field (§5.5, §4.5). The eval harness measures adherence as
`role_purity_score` (§9.10.1). Persona scope as a schema-level
concern, rather than a prompt-style hint, is structural.

**D3 — Artifact-first replayability with four distinct
determinism contracts.** Symposium's canonical output is a
persisted `Artifact` (§5.10, §7.1), digested with RFC 8785 JCS
canonicalization plus SHA-256 (§7.7, `transcript_digest`).
Four determinism contracts coexist and the spec keeps them
distinct (N3, N12):

- `transcript_replay` (§7.5) re-renders a stored
  `canonical_transcript` without invoking any provider. **Byte
  identity is unconditional** because no LLM call is involved.
- `execution_replay` (§7.6) re-runs the `orchestrator_runtime`
  against pinned inputs. **Byte identity is conditional** on the ten
  §7.6 pinning conditions (runtime, adapter, provider, model,
  sampling, cache, tool_env, wallclock, persona,
  transcript_prefix). Any unsatisfiable pinning raises
  `pinning_violation`.
- Golden-test byte identity (§9.3, §9.4.1) is conditional on
  the §9.4.1 harness pinning (FakeProvider + clock pinning +
  id-allocator pinning).
- `fake_provider` determinism (§9.1, §6.14) — is
  **unconditional** because the test adapter substitutes the
  LLM call.

D3 anchors in A2 (determinism qualifier from the joint review)
and N3 (the spec's normative determinism rule); the four-way
split is structurally enforced by §7.5 / §7.6 / §7.7 / §9.3 /
§9.4.1 mechanisms. Frameworks that conflate "replayability"
into a single concept end up either over-promising on one
contract or under-delivering on another; Symposium documents
all four with their respective conditions so an integrator
can pick the right one for the use case.

**D4 — Separated control plane.** The `selector`, the
`coordinator_agent`, and the `orchestrator_runtime` are three
distinct roles (ADR-005, R3): the selector chooses *who*
deliberates (pre-session; MVP `strategy=fixed`, no LLM call);
the coordinator_agent emits a `verdict` *recommendation*
(LLM-emitted, no executive authority); the orchestrator_runtime
*schedules and terminates* (deterministic code, sole party that
decides when a session stops). `verdict.next_action ∈
{continue, finalize, request_user_input,
request_external_research}` is a Coordinator *opinion* — never
a runtime action (ADR-002); `abort` is not a valid verdict
value (§2.5, §2.9). The §8.5 termination contract closes the
loop: the runtime's `terminate(reason)` decision is independent
of any verdict.

**D5 — Structured-only inter-agent control plane.** Agents
address other agents exclusively through the structured
`direct_request` field (§2.5, §5.5, ADR-003). Inline
`@AgentName` in agent prose is never parsed as a control
signal; it is at most a display convention (§2.5 Boundary).
This is the protocol's prompt-injection resistance posture:
adversarial content in `Message.content` cannot become a
routing decision because the routing surface is a separate,
schema-validated field. Competitors that route on inline `@`
mentions or on natural-language handoff cues are
prompt-injectable on that surface; Symposium is not.

**D6 — Closed termination-reason enum and operator-facing
failure taxonomy.** Anchored in ADR-002 (runtime termination
separated from verdict) and R2 (synthesis attempt with
termination-artifact fallback): the runtime's termination
reasons are a closed seven-value enum (§4.7, §8.5):
`budget_exceeded`, `timeout`, `schema_error`,
`provider_unrecoverable`, `user_cancel`, `user_input_required`,
`external_research_required`. Adapter errors are a closed
twelve-value enum (§6.6, `error.kind`). The agent-failure /
panel-mutation policy is a closed three-value enum (§4.9,
`on_agent_failure`). Every operator-facing outcome maps to one
of these closed enums; §8.3 surfaces the joint failure
taxonomy. Operators get a single, closed, mapped failure
surface. Frameworks that surface failures as opaque exceptions
or open strings leave the operator to invent their own taxonomy.

### 10.3 Comparison: AutoGen (Microsoft)

**What AutoGen is.** AutoGen is a framework for building AI
agents and applications with three components: AgentChat (a
programming framework for conversational multi-agent
applications), Core (an event-driven framework for scalable
multi-agent AI systems), and Extensions (interfaces to external
services). The framework supports deterministic and dynamic
agentic workflows, distributed agents for multi-language
applications, streaming, and Docker / gRPC-based distributed
agent runtimes. The legacy `GroupChat` / `GroupChatManager`
pattern (v0.2-era, still documented) lets a manager LLM
schedule turns among a set of conversable agents.

**Concept mapping to Symposium.** AutoGen's
`GroupChatManager` collapses scheduling, turn-routing, and
manager-LLM opining into one component. In Symposium these are
*three distinct roles*: the `orchestrator_runtime` (deterministic
code) schedules and terminates, the `coordinator_agent` (LLM)
emits a `verdict`, and the `selector` (deterministic in MVP)
chooses panel membership (ADR-005). AutoGen's Core actor-model
runtime is closer to Symposium's `orchestrator_runtime` than
AgentChat's `GroupChatManager` is; AutoGen exposes both layers
to the developer, while Symposium's `orchestrator_runtime` is
fixed by the protocol.

**Where Symposium differs.** AutoGen ships streaming,
distributed cross-host execution, and a richer set of topology
patterns; Symposium ships none of these (ADR-001 prohibits
parallel agent execution within a turn; ADR-004 makes MVP
batch-only). AutoGen does not schematize persona scope or
require structured-only inter-agent control; Symposium does
(D2, D5). The structural trade-off: AutoGen gives developers
topology flexibility; Symposium gives developers testable
scheduler invariants and a closed termination-reason surface.

**Citation.** [microsoft.github.io/autogen/stable](https://microsoft.github.io/autogen/stable/)
(verified 2026-05-25). Concept-mapping pages:
[Core overview](https://microsoft.github.io/autogen/stable/user-guide/core-user-guide/index.html),
[AgentChat group chat](https://microsoft.github.io/autogen/stable/user-guide/core-user-guide/design-patterns/group-chat.html).

### 10.4 Comparison: CrewAI

**What CrewAI is.** CrewAI is a Python framework for
orchestrating autonomous AI agents organized into Crews. A
Crew is a collaborative group of agents working together on a
set of Tasks. CrewAI supports two execution processes:
**sequential** (tasks execute one after another in a linear
flow) and **hierarchical** (a manager LLM oversees the crew,
delegates tasks, and validates outcomes). The framework
includes built-in guardrails, memory, knowledge, observability,
and a `crewai test` CLI subcommand.

**Concept mapping to Symposium.** CrewAI's sequential process
maps to a Symposium round-with-no-coordinator-turn: a fixed
order of contributions, no LLM moderator between contributions.
CrewAI's hierarchical process maps roughly to Symposium's
Coordinator + Selector + OrchestratorRuntime, *collapsed into a
single Manager LLM agent*. In CrewAI's hierarchical mode, the
Manager LLM decides task delegation, ordering, and
acceptance — i.e. it conflates the three roles Symposium
separates (ADR-002 + ADR-005). The Crew + Tasks
abstraction maps to Symposium's `Config.agents` + problem
statement; CrewAI's Task carries an `expected_output` field
similar to Symposium's `output_requirements` (Pass 1 row #24b).

**Where Symposium differs.** CrewAI's hierarchical Manager
combines runtime scheduling with LLM opining; Symposium
separates them (D4, ADR-002). CrewAI's `crewai test` and
`crewai replay -t <task_id>` provide testing and replay at the
Task granularity; Symposium's testing surface is the
`FakeProvider` + golden-test contract (§9.1–§9.4) at the
Message granularity, with the `transcript_digest` (§7.7) as
the integrity signal. CrewAI does not schematize persona
scope (D2) and does not enforce structured-only inter-agent
control (D5). The structural trade-off: CrewAI gives the
Manager LLM executive authority; Symposium reserves executive
authority for the deterministic runtime.

**Citation.** [docs.crewai.com](https://docs.crewai.com/) (verified
2026-05-25). Concept-mapping pages:
[Crews](https://docs.crewai.com/en/concepts/crews),
[Hierarchical Process](https://docs.crewai.com/en/learn/hierarchical-process),
[Sequential Process](https://docs.crewai.com/en/learn/sequential-process).

### 10.5 Comparison: LangGraph (LangChain)

**What LangGraph is.** LangGraph is a low-level orchestration
framework and runtime for building, managing, and deploying
long-running, stateful agents. Its central capabilities are
durable execution (agents persist through failures and resume),
streaming, human-in-the-loop via the `interrupt` primitive,
comprehensive memory (short-term working memory plus long-term
cross-session memory), and integration with LangSmith for
tracing. Multi-agent patterns are expressed as `StateGraph`
nodes with conditional edges; the supervisor / handoff pattern
(`langgraph-supervisor-py`, `langgraph-swarm-py`) is one
recommended topology among several.

**Concept mapping to Symposium.** A LangGraph `StateGraph` is
a different structural surface than Symposium's protocol: a
graph of nodes with conditional edges can express Symposium's
round-with-coordinator topology as a special case, and it can
also express arbitrary topologies that the Symposium protocol
does not admit (parallel agents per round per ADR-001, cyclic
handoffs without depth bound per §4.5, mid-graph topology
mutation per §4.9). LangGraph's `interrupt` /
`Command` primitives map to Symposium's
`verdict.next_action = request_user_input` —
*conceptually* — but the mechanism differs: LangGraph's
`interrupt` pauses execution in-place and resumes from the
interrupt point; Symposium's MVP terminates with
`reason = user_input_required` and the host re-invokes (ADR-004
makes interactive pause v1+).

**Where Symposium differs.** LangGraph is a graph
*orchestration* framework; Symposium is a deliberation
*protocol*. LangGraph ships durable execution and live
human-in-the-loop pauses; Symposium MVP ships neither (ADR-004
batch-only). LangGraph does not prescribe persona scope or
inter-agent control-plane shape; Symposium does (D2, D5).
LangGraph's replayability is built on stateful checkpoints;
Symposium's is built on append-only canonical_transcript +
JCS-canonical digest + the four-way determinism contract of
D3 / N12. The structural trade-off: LangGraph gives developers
arbitrary graph topology and stateful pause; Symposium gives
developers a fixed sequential-adversarial topology and a
closed determinism surface.

**Citation.** [langchain-ai/langgraph](https://github.com/langchain-ai/langgraph),
[docs.langchain.com/oss/python/langgraph/overview](https://docs.langchain.com/oss/python/langgraph/overview)
(verified 2026-05-25). Concept-mapping pages:
[Workflows & Agents](https://docs.langchain.com/oss/python/langgraph/workflows-agents),
[Use Functional API (interrupt)](https://docs.langchain.com/oss/python/langgraph/use-functional-api).

### 10.6 Comparison: OpenAI Agents SDK

**What the OpenAI Agents SDK is.** The OpenAI Agents SDK is a
production-ready framework built on a minimal set of
primitives: **Agents** (LLMs with instructions and tools),
**Handoffs** (agents delegating to other agents), **Guardrails**
(input / output validation), **Sessions** (persistent memory),
and **Tracing** (visualization and debugging). It expresses
multi-agent workflows by composing these primitives with
Python language features rather than specialized abstractions.

**Concept mapping to Symposium.** The Agents SDK's **Handoff**
is a natural-language delegation mechanism: an agent's
`instructions` describe when to hand off, and the runtime
routes to the named target. This corresponds *semantically* to
Symposium's `direct_request` field (§5.5, §2.5) — both
express "agent A wants agent B to take the next contribution".
The mechanisms differ structurally: the Agents SDK
infers the handoff from the agent's instructions and the
LLM's output; Symposium requires a schema-validated
`direct_request` object in `structured_output` (ADR-003).
**Guardrails** map to Symposium's schema validation of
`provider_result.structured_output` against
`expected_output_schema` (§6.5) and to the §8.7 input-validation
mitigations. **Sessions** map to Symposium's
`canonical_transcript` (§2.4) plus the `runs/<session_id>/`
persistence layout (§7.1). **Tracing** maps to
Symposium's MVP observability set (§7.9) and the v1
evaluation-harness recipes (§9.10); Symposium's MVP
observability is derived from the persisted Artifact and does
not require a live event bus (the `observability_event` stream
is formally deferred to v1+, §7.10 + §12).

**Where Symposium differs.** The Agents SDK's design surface
is small (five primitives composed via Python). Symposium's
design surface is fixed by the protocol (one round structure,
three separated roles per ADR-005, closed enums for every
operator-facing termination / error / agent-failure decision
per §4.7 / §6.6 / §5.2 / §8.2). The Agents SDK's handoff is a
natural-language signal that the runtime interprets from the
agent's output; Symposium's `direct_request` (ADR-003) is a
structured field with `target`, `type`, `content` validated
against `direct_request.schema.json` before routing (D5). The
structural trade-off: the Agents SDK gives developers a thin,
composable surface that admits many topologies; Symposium gives
developers a fixed surface with the testable scheduler
invariants of §4.10 and §9.5.

**Citation.** [openai.github.io/openai-agents-python](https://openai.github.io/openai-agents-python/)
(verified 2026-05-25). Concept-mapping pages:
[Agents overview (handoffs)](https://openai.github.io/openai-agents-python/agents/),
[Quickstart](https://openai.github.io/openai-agents-python/quickstart/).

### 10.7 Explicit non-claims

Symposium **does NOT** aim to do the following. These are
descriptive non-goals, not deficiencies; they are scoped-out
intentionally and re-scoping them would change the protocol
identity.

- **Symposium does NOT replace AutoGen / CrewAI / LangGraph /
  OpenAI Agents SDK.** Those frameworks solve generic
  agent-orchestration problems; Symposium solves a narrower
  problem (structured adversarial deliberation) on a restricted
  topology, and ships the four-way determinism contract of
  §10.2 D3 (N12). A team that needs arbitrary multi-agent
  topology, streaming, distributed execution, or live
  human-in-the-loop should pick one of the generic frameworks.
- **Symposium has NO streaming in MVP.** ADR-001 forbids
  parallel agent execution within a turn; ADR-004 makes MVP
  batch-only. The §6.1 adapter surface is synchronous;
  `invoke(request) -> ProviderResult` returns the complete
  result.
- **Symposium has NO distributed runtime in MVP.** ADR-001 and
  the §4 scheduler describe a single-process state machine.
  Multi-host runtimes are §13 Vision.
- **Symposium has NO HTTP / RPC service in MVP.** The §11
  invocation surface is the CLI and the library API. An
  HTTP / RPC service host pattern is §12 Roadmap.
- **Symposium has NO interactive / human-in-the-loop pause in
  MVP.** ADR-004. `verdict.next_action = request_user_input`
  triggers `terminate(reason = user_input_required)`; the host
  re-invokes with the additional information. Live pause and
  resume require the interactive execution mode that is
  formally deferred to v1+ (§12).
- **Symposium does NOT autonomously create new personas during
  a session.** Pass 1 row #122 (MUST NOT, §3 non-goal). Mid-
  session panel mutation is forbidden (§4.9). Personas are
  authored offline, reviewed, and shipped via configuration.
- **Symposium does NOT maximize consensus.** §3 non-goal.
  Convergence criteria are operational (R2, M5):
  no new open questions surface, no new failure modes appear,
  hard_caps not yet exhausted. The Coordinator carries
  `unresolved_disagreements` into synthesis as a first-class
  output field (§5.6).
- **Symposium does NOT provide a UI / visualization in MVP.**
  §3 non-goal. The HTML replay interface, TTS narration, and
  graph-based reasoning visualization are §12 Roadmap.
- **Symposium does NOT compete on latency or throughput.** A
  full session is, by construction, 5 panel turns + 1
  coordinator turn × N rounds + N branch turns + 1 synthesis.
  Per-session wall-clock budget defaults to 1800 s
  (`max_wallclock_seconds`, §4.7). Cost budget defaults to
  USD 5.00 (`max_total_cost_usd`, §4.7). The protocol assumes
  the problem warrants this cost.

### 10.8 Citation freeze dates

Vendor documentation changes. The URLs below were verified
against current vendor content on the freeze date.
Future passes that re-fetch these URLs update the freeze date;
if the content has diverged from the §10.3–§10.6 summaries,
the comparison sections are re-drafted. (§10 is descriptive
positioning; the verbs in this paragraph are editorial-process
guidance, not RFC 2119 normative claims.)

| Vendor | Primary `/docs` URL | Concept-mapping URLs | Freeze date |
|--------|---------------------|----------------------|-------------|
| AutoGen (Microsoft) | https://microsoft.github.io/autogen/stable/ | https://microsoft.github.io/autogen/stable/user-guide/core-user-guide/index.html ; https://microsoft.github.io/autogen/stable/user-guide/core-user-guide/design-patterns/group-chat.html | 2026-05-25 |
| CrewAI | https://docs.crewai.com/ | https://docs.crewai.com/en/concepts/crews ; https://docs.crewai.com/en/learn/hierarchical-process ; https://docs.crewai.com/en/learn/sequential-process | 2026-05-25 |
| LangGraph (LangChain) | https://docs.langchain.com/oss/python/langgraph/overview | https://docs.langchain.com/oss/python/langgraph/workflows-agents ; https://docs.langchain.com/oss/python/langgraph/use-functional-api | 2026-05-25 |
| OpenAI Agents SDK | https://openai.github.io/openai-agents-python/ | https://openai.github.io/openai-agents-python/agents/ ; https://openai.github.io/openai-agents-python/quickstart/ | 2026-05-25 |

### 10.9 Vocabulary introduced by §10 (status table)

| Item | Disposition | Where |
|------|-------------|-------|
| `positioning_differentiator` (D1–D6 anchor concept) | **Pass 9 decision — kept as prose-only label in §10** (no §2 promotion). §2 is the vocabulary for §3–§9 normative protocol; the D1–D6 anchors are positioning prose with no runtime semantics. | §10.2 |
| `citation_freeze_date` (vendor-comparison verification timestamp) | **Pass 9 decision — kept as prose-only label in §10.8** (no §2 promotion). The freeze-date table is a positioning-prose artifact; it does not refer to any runtime concept. The re-verification cadence is informal (revisited each editorial pass) and does not warrant glossary status. | §10.8 |

§10 introduces no new field names in any schema (rule N11). The
two surfaced labels are positioning-prose constructs, not
runtime concepts.

### 10.10 Coverage table — Pass-1 rows targeting §10

| Pass-1 row | Description | Disposition | §10 home |
|------------|-------------|-------------|----------|
| #1 | "framework" wording retired for Symposium itself | Absorbed | §10.1 (echoes §1 "opinionated protocol"); "framework" allowed only when describing competitors per Pass 1 Q7 resolution |
| #3 | "Unlike a multi-persona prompt" positioning rationale | Absorbed | §10.1 (positioning statement) |
| #7 | Goals: cognitive specialization, productive disagreement, structured adversarial reasoning, iterative refinement | Absorbed | §10.1 (positioning) + §10.2 D1 |
| #9 | Tension / contradiction / verification / skepticism / creativity rationale | Absorbed | §10.1 + §10.7 (no-consensus non-claim) |
| #138 | "Many narrow disciplined specialists" over "few broad generalists" | Absorbed | §10.2 D2 (structural role purity) |
| #159 | Repo positioning: opinionated, replayable, deterministic, adversarial-collaboration | Absorbed | §10.1 + §10.2 D3 + §10.2 D5 + §10.2 D6 |
| #169 | Position adjacent to AutoGen / CrewAI / LangGraph / OpenAI Agents SDK | Absorbed | §10.3 / §10.4 / §10.5 / §10.6 (four sections) |

**Coverage: 7 / 7 §10-targeted Pass-1 rows absorbed; 0 deferred.**

---

## 11. Integrations (CLI / Skills / IDE / Hosts)

**[Core MVP]** (CLI invocation contract, library API surface)
**[v1+]** (Symposium-as-Skill example, other host patterns)

### 11.1 Host vs runtime — boundary

Symposium is **not itself a host**. The protocol provides two
invocation surfaces consumed by host environments:

- **CLI** (`symposium run <problem_file>`, §11.2) — Core MVP
  (Pass 1 row #49).
- **Library API** (`run_session(config) -> Artifact`, §11.3) —
  Core MVP; the §4.11 pseudocode entry point ratified as a
  language-agnostic contract.

A **host** is the environment that invokes Symposium. Examples
include: a CLI shell, a Claude Code Skill (§11.4 example), an
IDE plugin (Roadmap, §12), an HTTP / RPC service (Roadmap, §12),
an agentic workflow tool that shells out to the Symposium CLI.

Responsibilities split as follows:

| Concern | Host responsibility | Runtime responsibility |
|---------|---------------------|------------------------|
| Discover the CLI / library | Yes | — |
| Present the problem to the user | Yes | — |
| Invoke Symposium | Yes | — |
| Render the produced Artifact | Yes | — |
| Schedule rounds and turns | — | Yes (§4) |
| Own the canonical_transcript | — | Yes (§2.4, §7.1) |
| Persist runs/<session_id>/ | — | Yes (§7) |
| Decide termination | — | Yes (§4.7, §8.5, ADR-002) |
| Compute replay (`transcript_replay`, `execution_replay`) | — | Yes (§7.5, §7.6) |

Host concerns that are explicitly out of Symposium's MVP scope
(§8.8) include: encryption at rest, sandboxed adapter execution,
FIPS / KMS integration, credential rotation, transport-layer
authentication. Pass 1 row #67 (`OPENAI_API_KEY`,
`ANTHROPIC_API_KEY` environment variables) is a host
convention, not a runtime contract; §6.8 names the credential-
handling surface the runtime requires.

### 11.2 CLI invocation contract

The MVP CLI surface, in full:

```text
symposium run <problem_file>
    [--config <path>]
    [--max-rounds <N>]
    [--provider <id>]

symposium replay <session_id>
```

**`symposium run <problem_file>`** — invoke the protocol on
the problem text at `<problem_file>`. The runtime constructs
a session per §4.1, runs to termination, and writes
`runs/<session_id>/{manifest.json, artifact.json,
config.json}` per §7.1. The session_id is allocated by the
runtime (§7.1 charset `^[A-Za-z0-9_-]{1,64}$`).

Required flags: none.

Optional flags:

- `--config <path>` — load the configuration from `<path>`
  instead of the implementation default (Pass 1 row #50).
- `--max-rounds <N>` — override `Config.runtime.budget.max_rounds`
  (§4.7 hard cap). Other hard caps may be overridable via
  similarly-named flags; the spec commits only to `--max-rounds`
  in MVP.
- `--provider <id>` — override the default provider for all
  agents lacking a per-agent override. The provider id resolves
  through the §6.11 adapter registry; an unknown id terminates
  the session with `reason = schema_error`.

**`symposium replay <session_id>`** — invoke `transcript_replay`
(§7.5) against the persisted `runs/<session_id>/artifact.json`.
This is a deterministic re-emission of the canonical_transcript
that incurs no provider cost (D3 contract 1; §7.5 unconditional
byte-identity guarantee). The CLI's minimum output format is
the JCS-canonical JSON re-emission of the canonical_transcript
array specified in §7.5 (same RFC 8785 canonicalization rule as
§7.7 `transcript_digest`); two conforming implementations
processing the same Artifact MUST produce byte-identical
canonical output. Markdown / human-readable renders are
RECOMMENDED additions (per §7.5) but are not subject to the
byte-identity contract and are not a substitute for the
canonical output.

**Exit codes.** The mapping of termination reasons to process
exit codes is **implementation-defined**. The spec's canonical
machine-readable termination signal is
`Artifact.outcome.kind` and `RunManifest.outcome_kind`
(§4.7, §7.2); a host that needs to branch on the reason
SHOULD consult those fields rather than rely on a specific
exit code. Implementations MAY choose to map `synthesis` →
`0` and any termination → non-zero; the spec does not commit
to a finer-grained mapping because cross-platform exit-code
surface conventions vary.

**Other subcommands.** Pass 1 row #51 lists candidate verbs
(`symposium inspect`, `symposium benchmark`); these are
implementation-defined and not part of the MVP MUST surface.
A `symposium eval` subcommand exposing the §9.10 evaluation
harness is consistent with the v1 timing of the harness
itself and is documented at v1.

### 11.3 Library API surface

The library API exposes two Core MVP entry points and one
documented v1 entry point, matching the §4.11 pseudocode and
the §7.5 / §7.6 replay contracts. The surface is
language-agnostic; concrete bindings (Python, TypeScript,
others) are implementation-defined.

**Core MVP entry points:**

```text
run_session(config: Config, *, harness_pinning: HarnessPinning | null = null)
    -> Artifact | TerminationArtifact

transcript_replay(artifact: Artifact)
    -> canonical_re_emission
```

**Documented v1 entry point (MVP MAY ship; not in MVP MUST-set
per §7.6):**

```text
execution_replay(artifact: Artifact, pinning: PinningConditions)
    -> Artifact | pinning_violation
```

- **`run_session(config)`** — primary entry point; ratifies
  §4.11. Returns an `Artifact` (§5.10) on synthesis or a
  `TerminationArtifact` (§5.8) on early termination — both are
  variants of the persisted run output and follow the same
  `artifact.schema.json` discrimination.
- **`harness_pinning`** (optional) — when present, supplies a
  fixed clock source and a fixed id allocator per §9.4.1. The
  pinning hook is REQUIRED for golden-test byte identity (D3
  contract 3; §9.4.1) and OPTIONAL for production runs. The
  vocabulary `harness_pinning` is defined in §2.11.
- **`transcript_replay(artifact)`** — exposes §7.5. No
  provider invocation; byte-identity guarantee is unconditional
  (D3 contract 1).
- **`execution_replay(artifact, pinning)`** — exposes §7.6.
  §7.6 explicitly states this is **not part of the MVP MUST-set
  as a runtime feature** — the MVP CLI MAY ship only
  `transcript_replay`. The contract is documented in MVP so a
  v1 implementation can ship it consistently; if shipped, byte
  identity is conditional on the ten §7.6
  `pinning_conditions` (D3 contract 2); unsatisfiable pinning
  yields a `pinning_violation` diagnostic.

The two Core MVP library entry points form the canonical
integration path for embedding Symposium into a larger program.
The CLI (§11.2) is a thin wrapper over `run_session` and
`transcript_replay`.

### 11.4 Host integration example — Symposium as a Claude Code Skill (v1+)

> **Framing note.** This subsection documents an *example* host
> integration. The Skill is **NOT part of the Symposium
> runtime** (Codex turn-2 P2). It is one host pattern among
> many. The Symposium runtime owns the CLI (§11.2) and library
> API (§11.3); the Skill is a wrapper authored downstream that
> shells out to the CLI.

A Claude Code Skill named `symposium` may be authored to wrap
the CLI. The Skill's responsibilities are entirely host-side:

- **Activation.** User-invoked only ("Start a symposium", "Run
  the thinking team", "Use Symposium on this problem"). The
  Skill SHOULD NOT autonomously invoke Symposium without the
  user's explicit request (Pass 1 row #56, downgraded to NOTE
  because this is a Claude Code skill convention, not a
  Symposium runtime invariant).
- **Problem extraction.** The Skill captures the current
  problem context from the Claude Code conversation and
  writes it to a temporary file.
- **CLI invocation.** The Skill shells out:
  `symposium run <temp_problem_file> --config <project_config>`.
- **Artifact rendering.** The Skill reads
  `runs/<session_id>/artifact.json` and renders the
  `synthesis_content` (or `termination_artifact`) back into
  the Claude Code conversation.

Pass 1 rows #54 (Skill MUST ONLY activate when explicitly
requested) and #56 (Skill MUST NOT autonomously invoke) are
downgraded MUST → NOTE: these are host-environment conventions
that bind the Skill's behavior, not invariants the Symposium
runtime can enforce. The Symposium runtime accepts any
invocation that conforms to §11.2 / §11.3.

The Skill is NOT a system component, NOT a scheduling layer
(scheduling is `orchestrator_runtime`-owned per ADR-005),
NOT a runtime extension. It is a thin host adapter.

### 11.5 Other host patterns (Roadmap)

Other host integration patterns are recognized but deferred to
§12 Roadmap; they are not part of the MVP MUST surface:

- **IDE plugin (VS Code, JetBrains)** — a plugin that exposes
  `symposium run` and `symposium replay` as IDE commands and
  renders the Artifact in an IDE-native panel. Roadmap.
- **HTTP / RPC service** — a long-running service that accepts
  remote invocations (REST, gRPC) and returns Artifacts.
  Roadmap; depends on the v1+ execution-mode relaxation
  (ADR-004) for genuinely useful streaming responses.
- **Standalone subprocess invocation by an agentic workflow
  tool** — supported today via the §11.2 CLI contract; no
  Roadmap entry needed because the integration is the CLI
  itself.
- **Notebook integration** — a Jupyter / Colab cell that calls
  `run_session(config)` from the library API; supported today
  via the §11.3 library API; no Roadmap entry needed.

### 11.6 Provider-adapter integration cross-reference

The §6 ProviderAdapter contract is the spec's plugin surface
for backend integration. §11 does not redefine it; it
cross-references for the host's benefit.

- **§6.1** defines the adapter surface
  (`invoke(request) -> ProviderResult`, `shutdown()` optional,
  transport-agnostic).
- **§6.11** defines adapter registration and discovery in
  MVP (an in-process registry table; entry-point / plugin-style
  auto-discovery is §12 Roadmap).
- **§6.12 and §6.13** give worked-example HTTP adapters
  (OpenAI-shaped, Anthropic-shaped). §6.14 gives the FakeProvider
  for tests.

The host's responsibility is to wire adapter configurations
into the CLI / library invocation. Concrete vendor identifiers
(e.g. `openai`, `anthropic`) belong in
`examples/configs/*.yaml` (rule N4); the spec body uses semantic
placeholders. The §6.11 registry resolves the configured
provider id to an `AdapterFactory` at session init.

### 11.7 Vocabulary introduced by §11 (status table)

| Item | Disposition | Where |
|------|-------------|-------|
| `host_integration_pattern` (CLI / Skill / IDE / HTTP / subprocess / notebook) | **Pass 9 decision — kept as prose-only label in §11** (no §2 promotion). The list of host patterns is open and host-owned; only the CLI and library API surfaces (§11.2, §11.3) are normative, and those are already defined in §11 prose. | §11.1, §11.4, §11.5 |
| `harness_pinning` (referenced as optional `run_session` parameter) | **Already defined** in §2.11 (Pass 7 amendment). §11.3 cross-references the §2.11 entry and surfaces it on the library API; no §2 change needed. | §11.3 → §2.11 |

§11 introduces no new field names in any schema (rule N11).

### 11.8 Coverage table — Pass-1 rows targeting §11

| Pass-1 row | Description | Disposition | §11 home |
|------------|-------------|-------------|----------|
| #16 | Compatibility list (OpenAI / Claude / Codex CLI / Claude Code / local / future) | Absorbed (cross-ref to §6, §11.4, §11.5) | §11.1 + §11.6 |
| #30 | Engineer persona — Codex CLI, Claude Code recommendations | Absorbed (host-integration examples, not MVP Engineer dependencies; Pass 1 Q6 resolution) | §11.4 example + §11.5 |
| #42 | Provider tooling — OpenAI / Anthropic / Codex CLI / Claude Code / local | Absorbed (adapter contract in §6, host examples in §11) | §11.6 cross-reference |
| #49 | CLI `symposium run problem.md` is Core MVP MUST | Absorbed | §11.2 |
| #50 | Advanced flags `--config`, `--max-rounds`, `--provider` | Absorbed | §11.2 |
| #51 | Future CLI verbs (`replay`, `inspect`, `benchmark`) | Split: `replay` Core MVP (§11.2 + §7.5); `inspect` / `benchmark` implementation-defined / §12 | §11.2 + §12 |
| #52 | "System includes a conceptual Symposium Skill" | Absorbed (re-framed as example v1+ host pattern, not a system component, per Codex turn-2 P2) | §11.4 |
| #53 | Skill invokes orchestrator / initializes discussion / schedules agents / manages reasoning | Absorbed (rewrite: Skill is a host wrapper that shells out to CLI; does NOT schedule — scheduling is `orchestrator_runtime` per ADR-005) | §11.4 |
| #54 | "Skill MUST ONLY activate when explicitly requested" | Absorbed (downgrade MUST → NOTE per Pass 1 disposition; host convention, not Symposium runtime invariant) | §11.4 |
| #55 | Trigger phrase examples ("Start a symposium" etc.) | Absorbed (illustrative; same downgrade rationale) | §11.4 |
| #56 | "System MUST NOT autonomously invoke Symposium" | Absorbed (downgrade MUST → NOTE; about host behavior) | §11.4 |
| #67 | Environment variables `OPENAI_API_KEY`, `ANTHROPIC_API_KEY` | Absorbed (host convention; runtime's credential-handling surface is §6.8) | §11.1 host-concern note |

**Coverage: 12 / 12 §11-targeted Pass-1 rows absorbed; 0 deferred.**

---

## 12. Roadmap

**[Roadmap]** (descriptive only; no normative language per N9)

### 12.1 Roadmap principles

All §12 items are **non-binding**. Inclusion in §12 does NOT
obligate a future release. Re-prioritization between MVP and
Roadmap is allowed at any pre-publication point per §5.1
pre-publication-finalization precedent; post-publication, §5.1
MINOR rules apply (a Roadmap item promoted to v1 generally
requires a coordinated schema amendment and a corresponding
MINOR version bump).

§12 carries no RFC 2119 normative weight (rule N9). It is the
canonical aggregation point for every deferral surfaced during
Passes 1–7; the rows that follow trace each deferral to its
Pass-1 row, its disposing chapter, and the rationale.
Paraphrases of prior chapters in this section use lowercase or
neutral verbs ("required by", "recommended", "optional") so that
no normative keyword in §12 can be misread as a §12 commitment.

### 12.2 Aggregation table

Every row below originates from a Pass 1–7 deferral. The
"Disposition source" column names the chapter (and the
Pass / decision-log entry) where the deferral was last
recorded. The "Target window" is descriptive only.

| Feature | Origin | Disposition source | Rationale | Target window |
|---------|--------|--------------------|-----------|---------------|
| `EnsembleMode` (parallel first-pass perspectives) | Codex turn-2 P1, Pass 1 row implied | Pass 1 review, retained at Pass 2 §2.9 (`parallel Round 1` deprecated) | Parallel first-pass perspectives lose adversarial dynamics; preserved as an ensemble variant *separate* from Round 1 sequential semantics (ADR-001) | v1+ |
| Interactive event-stream execution mode | ADR-004 | Pass 6 §8.4 / §8.5; Pass 7 §9 acknowledges | MVP is batch-only; live event-stream requires the deferred `observability_event` schema below | v1+ |
| Async job API | ADR-004 | Pass 6 §8.4 | Same root as interactive mode; queue / job semantics absent in MVP | v1+ |
| LLM-driven Selector (`strategy ∈ {rules, llm}`) | R3, Pass 1 rows #111, #112, #117, #118 | Pass 2 §2.2 (`selector`); Pass 4 §5.11 (Selector output stub) | MVP `strategy = fixed`; `rules` / `llm` strategies reserved | v1+ |
| HTML replay interface | Pass 1 rows #98, #101 | Pass 6 §7.5 (transcript_replay prose) | Higher-fidelity replays are §12; MVP ships JCS-canonical re-emission only | Roadmap |
| Text-to-speech narration (per-agent voice, playback speed) | Pass 1 row #99, #100 (Artifact `voice` field) | Pass 4 prose | TTS replay metadata is Roadmap; MVP `Artifact` has no `voice` field | Roadmap |
| Expandable-branch / timeline / speaker-filter / branch-highlight / reasoning-graph / synced-audio / markdown-rendering / export-to-video viz | Pass 1 row #101 | Pass 6 §7.10 | Visualization features; MVP observability set is metric-only | Roadmap |
| Plugin architecture beyond providers (personas, schedulers, replay systems, TTS engines, evaluation systems, visualization) | Pass 1 row #152 | Pass 5 §6.11; Pass 6 §7.10 | MVP ships ProviderAdapter only as the plugin contract; other categories deferred | Roadmap |
| Persona registry (community contributions, versioning, lifecycle, signing, marketplace) | Pass 1 rows #116, #125, #135, #136, #137, #155 | Pass 5 §6.15; Pass 6 §8.8; Pass 4 §5.3 (`persona_version` reserved) | Persona-registry concerns (lifecycle, signing, third-party trust) | Roadmap |
| Benchmarking suite / curated normative problem sets | Pass 1 rows #51, #70, #131, #132, #133 | Pass 7 §9.10.6 | Pass 7 ships no normative benchmark; curated problem sets are illustrative | Roadmap |
| Web UI / visualization (interactive timeline of canonical_transcript with branch / verdict overlays) | Pass 1 rows #95, #101, #109, #110 | Pass 6 §7.10 | UI surface; MVP has no UI | Roadmap |
| `on_budget_exceeded` extensions: `degrade`, `escalate` | Pass 1 row #149 | Pass 6 §8.2 | MVP enum closed at `{stop}`; dynamic mid-session reduction conflicts with §4.9 panel immutability; external escalation requires interactive mode | Roadmap |
| `summarize_context` recovery action | Pass 1 row #145 | Pass 6 §8.4 | MVP `context_packet` derivation (§4.3) does not summarize; an MVP-level summarize_context action would conflict with the §4.3 contract | v1 |
| `replace_agent` recovery action | Pass 1 row #145 | Pass 6 §8.4 | §4.9 panel immutability forbids mid-session replacement; v1 may relax under panel-mutation extension | v1 |
| `pause_session` recovery action | Pass 1 row #145 | Pass 6 §8.4 (ADR-004) | Live pause requires interactive execution mode | v1+ |
| `request_human_intervention` recovery action | Pass 1 row #145 | Pass 6 §8.4 (ADR-004) | Same root as `pause_session` | v1+ |
| `AgentConfig.fallback_model` | Pass 1 row #145 (originally classified as recommended for Core MVP; overridden in Pass 6) | Pass 6 §8.4 + §8.10; Pass 7 §9.12 / §9.13 | Codex Pass-6 turn-2 attribution gap: requires paired `Message.provider_used` / `Message.model_used` schema amendment. The two fields are deferred together so a v1 implementation does not surface mis-attributed cost / token accounting | v1 |
| `Message.provider_used` / `Message.model_used` | Pass 6 §8.10; Pass 7 §9.12 (re-deferred) | Pass 7 §9.13 | Paired with `fallback_model`; required for per-message attribution that v1 golden tests will need | v1 |
| `observability_event` live stream schema | Pass 6 §7.10 / §7.11 | Pass 7 §9.10 / §9.12 (re-deferred) | MVP required metric set (§7.9) is derivable from persisted Artifact; live stream needed for v1+ retry-count and failure-by-error-kind metrics (Pass 7 §9.10's two re-deferred metrics) | v1+ |
| Two re-deferred v1+ metrics (provider-level retry count per agent; failure count per agent by `error.kind`) | Pass 6 §7.10 | Pass 7 §9.10 (re-deferred) | Both metrics require `observability_event` live stream (above); cannot be derived from MVP Artifact | v1+ |
| `postmortem.schema.json` | Pass 1 row #133 (optional, v1) | Pass 7 §9.11 / §9.12 | Pass 7 ships postmortem as prose recipe; schematization deferred until field shapes stabilize | v1+ |
| Signed adapters / adapter attestation / third-party adapter verification | Pass 5 §6.15; Pass 6 §8.7, §8.8; Pass 7 §9.12 | Pass 7 §9.12 (re-deferred) | Adapter audit / trust verification is host / registry concern; MVP §8.7 treats adapters as trust-on-first-load | v1+ |
| Capability-based per-agent tool allowlists / deny-list scheme | Pass 6 §8.8 | Pass 6 §8.8 | Tool capability gating beyond per-agent `tools` array | v1+ |
| Encryption at rest / sandboxed adapter exec / FIPS / KMS / key rotation / secret-store integration | Pass 6 §8.8 | Pass 6 §8.8 | Host concerns, not Symposium runtime (rule N6); explicitly out of MVP scope | Out of scope (host-owned) |
| Transcript summarization / semantic compression / rolling context windows / retrieval-based reinjection / selective memory injection | Pass 1 row #147 (N1 violation downgraded) | Pass 2 §2.9 (deprecated v0 wording); Pass 4 §5.9 (`context_packet` may be full transcript) | v0 "must eventually" wording rewritten per N1; summarization / compression / rolling windows = v1; retrieval injection = Roadmap | v1 recommended / Roadmap optional split |
| Semantic similarity checks / deadlock detection (beyond hard-cap deadlock prevention) | Pass 1 rows #107, #108 | Pass 6 §7.10 (v1 disposition); §8.1 (hard caps cover deadlock prevention) | Hard-cap-based deadlock prevention is required by §8.1; semantic similarity is a v1 observability addition | v1 |
| Behavioral content-quality assertions over `Message.content` (avoid repetition, reference previous discussion, identify confidence levels, distinguish fact from speculation, no cross-domain contamination beyond V1 detection) | Pass 1 rows #59a–e | Pass 7 §9.16 coverage table (re-deferred to §7.10 v1+ observability) | These are content-quality assertions, not scheduler invariants. §9.10.1 `role_purity_score` covers cross-domain contamination via V1; full behavioral coverage (repetition detection, reference checking, confidence-level extraction) requires NLP heuristics outside MVP scope | v1+ |
| Dynamic participant introduction during session | Pass 1 rows #112, #120 | Pass 4 §5.11 (Selector v1+ stub); §4.9 immutability | Mid-session panel mutation forbidden (§4.9); v1+ panel-mutation extension required | v1+ |
| Meta personas (Persona Librarian, Role Boundary Enforcer, Agent Evaluator, Postmortem Reviewer) | Pass 1 row #116 | Pass 1 disposition | Ecosystem-governance personas, not deliberation participants | Roadmap / Vision split |
| Persona lifecycle states (experimental / stable / deprecated / archived) | Pass 1 row #155 | Pass 4 §5.3 (`persona_version` reserved) | Lifecycle metadata is registry-level | Roadmap |
| Missing-capability detection (structured `capability_gap` output) | Pass 1 row #124 | Pass 7 §9.11 (`capability_gaps` field in postmortem prose) | Postmortem-level v1 surface; full standalone artifact is Roadmap | v1 + Roadmap |
| Plugin-style adapter discovery (entry-point conventions, auto-registration via package metadata) | Pass 5 §6.11 | Pass 5 §6.11 | MVP ships in-process registry; plugin-style discovery is v1 extension | v1 |
| `max_tool_iterations` promotion to `Config.runtime` | Pass 5 §6.15 | Pass 5 §6.15 (already promoted to `Config.runtime` in Pass 6, but the promotion path itself is the deferral target for any future tool-loop semantics extensions) | MVP fixes 8 as adapter-internal default; runtime visibility added in Pass 6 | (delivered in Pass 6; no further work) |
| External-loop adapter pattern (re-opens `finish_reason = tool_call` as terminal) | Pass 5 §6.10 | Pass 5 §6.10 | MVP internal-loop topology forbids `tool_call` as terminal; v1+ external-loop adapter would re-use the schema slot | v1+ |
| Selector LLM output schema (`excluded_agents[]`, `missing_capabilities[]`, richer reasoning fields) | Pass 1 row #119 | Pass 4 §5.11 (v1+ stub) | MVP Selector strategy = `fixed`; LLM output schema is degenerate in MVP | v1+ |
| IDE plugin (VS Code, JetBrains) | §11.5 cross-ref | §11.5 | Host pattern, not runtime concern | Roadmap |
| HTTP / RPC service host pattern | §11.5 cross-ref | §11.5 | Host pattern; depends on ADR-004 v1+ relaxation for genuinely useful streaming | Roadmap |
| `symposium inspect` / `symposium benchmark` CLI subcommands | Pass 1 row #51 | §11.2 (implementation-defined) | Beyond the MVP required CLI surface; `benchmark` depends on benchmarking-suite Roadmap entry above | Roadmap |
| `symposium eval` CLI subcommand exposing §9.10 evaluation harness | §11.2 cross-ref | §11.2 + Pass 7 §9.10 | Pairs with evaluation harness v1 timing | v1 |
| Voting / weighted-confidence convergence mechanisms (alternative to single-coordinator semantic convergence) | Pass 1 row #63 split | Pass 1 disposition (Roadmap aspect of #63) | MVP convergence is a single coordinator emitting `verdict.next_action` (ADR-002, R2); aggregating votes or confidence weights across panel members is an alternative aggregation mechanism that would require a new verdict shape and a new aggregation policy | Roadmap |
| Automatic paper / source retrieval as a built-in Researcher tool (e.g. arXiv / web-search adapter bundled with the runtime) | Pass 1 row #63 split (researcher tool aspect) | Pass 1 disposition; §6.4 leaves tool registration to adapter configuration | The runtime accepts tool registrations through the §6.4 adapter contract today; a curated set of research adapters shipped with the runtime is a Roadmap deliverable, not a protocol concern | Roadmap |

### 12.3 Cross-reference to chapters owning each deferral

A reader looking for "where is feature X discussed?" follows the
catena: **Pass 1 classification row → ADR / refinement (if any)
→ chapter where deferral was last recorded → §12**.

Example for `fallback_model`:

```
Pass 1 row #145  →  Pass 6 §8.4 (recovery-strategy split)
                 →  Pass 6 §8.10 (vocabulary status table; deferred to v1)
                 →  Pass 7 §9.13 (re-deferred, paired with provider_used / model_used)
                 →  §12 (aggregation row "AgentConfig.fallback_model")
```

Example for HTML replay interface:

```
Pass 1 rows #98, #101  →  Pass 6 §7.5 prose ("higher-fidelity replays are §12")
                       →  §12 (aggregation row "HTML replay interface")
```

Each aggregation-table row is a leaf node of the catena. No
aggregation-table row terminates here without a prior chapter
that originally deferred it.

### 12.4 Vocabulary introduced by §12 (status table)

| Item | Disposition | Where |
|------|-------------|-------|
| `target_window` (a prose label; observed values in §12.2 include `v1`, `v1+`, `Roadmap`, `Out of scope (host-owned)`, plus composite values `v1 + Roadmap`, `Roadmap / Vision split`, `v1 recommended / Roadmap optional split`, and the historical marker `(delivered in Pass 6; no further work)`) | **Pass 9 decision — kept as prose-only label in §12** (no §2 promotion). The value vocabulary is open and descriptive, not a closed enum; the four scope tags ([Core MVP] / [v1] / [Roadmap] / [Vision]) are already defined in §1 banner. Promoting `target_window` to §2 would duplicate that. | §12.2 |

§12 introduces no new schema fields (rule N11). The aggregation
table is prose with embedded tables.

### 12.5 Coverage table — Pass-1 rows targeting §12

| Pass-1 row | Description | Disposition | §12 home |
|------------|-------------|-------------|----------|
| #4e | Plugin-first architecture for future extensibility (personas / schedulers / replay / TTS / eval / UI) | Absorbed — explicitly named in §12.2 "Plugin architecture beyond providers" row; v0 "plugin-first" framing downgraded per N1 | §12.2 (plugin architecture row) |
| #41 | Multi-provider YAML example with vendor literals | Absorbed via `examples/configs/*.yaml` framing in §5.2 / §6.12 / §6.13; plugin-architecture aspect routes here | §12.2 (plugin architecture row) |
| #51 | Future CLI verbs `replay` / `inspect` / `benchmark` | Split: `replay` ratified Core MVP (§11.2 + §7.5); `inspect` / `benchmark` → §12 | §12.2 (`symposium inspect` / `benchmark` row) |
| #63 | Future Features grab-bag (recursive sub-symposiums, voting, weighted confidence, paper retrieval, benchmark, memory, graph, viz, distributed, fine-tuning, RL, autonomous experimentation) | Split: voting / weighted-confidence → §12 (dedicated row); paper retrieval → §12 (dedicated row); benchmark → §12 (benchmarking-suite row); graph / UI / viz / memory → §12 (existing visualization / plugin / transcript-summarization rows); recursive sub-symposiums / autonomous experimentation / distributed → §13 (dedicated items); fine-tuning / RL loops → §13 (reinforcement-loop item explicitly names them) | §12.2 (multiple rows) + §13.1 (multiple items) |
| #70 | Phase-2 tool integrations / web search / Codex / structured verdicts | Structured verdicts are Core MVP (#37); the rest routes to §12 plugin architecture | §12.2 (plugin architecture row) |
| #71 | Phase-3 plugin ecosystem / UI / distributed execution / benchmarking | Absorbed (plugin / UI / benchmarking rows; distributed → §13) | §12.2 |
| #95 | HTML viz / graph viz / analytics / future distributed | Absorbed (HTML / viz rows; distributed → §13) | §12.2 + §13 |
| #97 | JSON / Markdown / HTML export | JSON canonical Core MVP; Markdown render optional Core MVP; HTML replay → §12 | §12.2 (HTML replay row) |
| #98 | HTML replay visualization | Absorbed | §12.2 (HTML replay row) |
| #99 | TTS playback / per-agent voice | Absorbed | §12.2 (TTS narration row) |
| #100 | Suggested artifact schema includes `voice` field | Absorbed (TTS replay metadata; not in MVP Artifact schema) | §12.2 (TTS narration row) |
| #101 | HTML viz goals (expandable branches / timeline / speaker filter / etc.) | Absorbed | §12.2 (viz row) |
| #107 | Anti-loop: max branch depth / max chain / duplicate question / repeated topic | Split: structural caps Core MVP (§4.7); semantic similarity → v1 (§7.10) / Roadmap (§12) | §12.2 (semantic similarity / deadlock detection row) |
| #108 | Optional semantic similarity / deadlock detection | Absorbed (v1 optional) | §12.2 |
| #110 | OSS community contribution model | Roadmap aspect lives here; Meta aspect → §14 (Pass 9) | §12.2 (plugin architecture / persona registry rows) |
| #111 | "Symposium is NOT based on a fixed panel" — dynamic persona ecosystem | Absorbed (LLM Selector + dynamic participation rows) | §12.2 (LLM Selector + dynamic participation rows) |
| #112 | Dynamic participant selection / domain-aware orchestration / evolving persona libraries | Split: dynamic selection → §12; strict role boundaries Core MVP (§5.3); evolving libraries → §12 | §12.2 (multiple rows) |
| #116 | Meta personas | Absorbed (Roadmap / Vision split) | §12.2 + §13 |
| #117 | Selector (v1+) | Absorbed (LLM Selector row) | §12.2 |
| #118 | Selector responsibilities (v1+) | Absorbed (LLM Selector row) | §12.2 |
| #119 | Selector output schema | Absorbed (Selector LLM output schema row) | §12.2 |
| #120 | Dynamic participation (v1+) | Absorbed | §12.2 (Dynamic participant introduction row) |
| #124 | Missing-capability detection | Split: postmortem prose recipe v1 (§9.11); standalone artifact → §12 | §12.2 (Missing-capability detection row) |
| #125 | Persona creation workflow (offline / review / spec / validation / library) | Absorbed (persona registry row); Meta aspect → §14 | §12.2 + §14 |
| #135 | Long-term ecosystem — curated persona ecosystem / extensible / community-driven / versioned / benchmarked / domain-aware | Split: Roadmap aspects → §12 (persona registry / benchmarking); aspirational framing → §13 | §12.2 + §13 |
| #136 | Community contribution model | Split: registry Roadmap → §12; repo conventions Meta → §14 | §12.2 + §14 |
| #137 | Persona registry formal system | Absorbed | §12.2 (persona registry row) |
| #145 | Recovery strategies (retry / fallback / summarize / truncate / replace / pause / human intervention) | Split per Pass 6 §8.4: MVP required policy mechanism + recommended retry; `fallback_model` v1 (paired); `summarize_context` / `replace_agent` v1; `pause_session` / `request_human_intervention` v1+ | §12.2 (multiple rows) |
| #147 | Transcript summarization (v0 "must eventually" wording — N1 violation) | Downgraded per N1; v1 recommended + Roadmap optional split | §12.2 (transcript summarization row) |
| #148 | Memory layers (Session / Project / Persona / Knowledge) | Session = Core MVP (canonical_transcript); Project / Persona / Knowledge → §12 | §12.2 (transcript summarization row covers the cross-session memory aspect; further split if Pass 9+ surfaces specific schema) |
| #149 | Cost mgmt / hard limits / adaptive reasoning depth | Caps Core MVP (§8.1); on_budget_exceeded enum closed at `{stop}` Core MVP (§8.2); adaptive reasoning depth → §12; `degrade` / `escalate` → §12 | §12.2 (on_budget_exceeded extensions + adaptive reasoning) |
| #150 | Observability (token / latency / participation / interaction graph / delegation / branch depth / role purity / disagreement frequency) | MVP required set: token / latency / participation / branch depth (§7.9); v1 recommended set: rest (§7.10 + §9.10 recipes); live event stream → §12 | §12.2 (observability_event live stream row) |
| #152 | Plugin architecture (providers, personas, schedulers, replay, TTS, eval, viz) | MVP: ProviderAdapter only (§6); other categories → §12 | §12.2 (plugin architecture row) |
| #153 | Human intervention (pause / inject / override / remove / force participate / terminate / summary) | MVP: `terminate(user_cancel)` only; rest → v1+ interactive mode | §12.2 (pause_session / request_human_intervention rows) |
| #155 | Persona lifecycle states | Absorbed | §12.2 (persona lifecycle row) |

**Coverage: 35 / 35 §12-targeted Pass-1 rows absorbed; 0 silently
dropped.** (Turn-3 correction: added missing #4e row for
"plugin-first extensibility" originally flagged by Pass 1
disposition as `§12 Roadmap`.) Several rows split between
§12 and §13 (#63, #95, #116, #135) or between §12 and §14
(#110, #125, #136); the split column in each row makes the
dual home explicit.

---

## 13. Vision / Long-term ideas

**[Vision]** (non-normative, non-binding per N1)

### 13.1 Vision items

The following items describe directions that would extend
Symposium beyond its protocol identity. They are descriptive
only — non-binding, no RFC 2119 normative weight (rules N1,
N9). Inclusion in §13 is not a commitment; promotion to
Roadmap (§12) requires a formal re-classification (§13.2).

- **Recursive sub-symposiums.** A session would, mid-
  deliberation, spawn a child session as a tool-call and
  consume the child's `Artifact` (or `TerminationArtifact`) as
  evidence. The child session would have its own
  canonical_transcript, its own panel, its own budget. The
  recursion depth would be bounded structurally
  (cf. `max_branch_depth` for forks). This would change the
  §4 scheduler shape and the §7 persistence layout; the
  recursive structure would also require a session-id
  parent-pointer field on the Artifact.

- **Reinforcement-loop agent improvement (including model
  fine-tuning and RL loops).** Postmortem outputs
  (§9.11 `capability_gaps`, `scope_violations`,
  `best_contributors`) would feed back into persona definitions
  across sessions, allowing personas to evolve their
  `behavioral_constraints` and `failure_modes` based on
  observed performance. The Pass 1 row #63 enumeration
  ("agent fine-tuning, RL loops, autonomous experimentation")
  is absorbed here: model-level fine-tuning of a persona's
  associated model, RL training over sequences of
  canonical_transcripts, and autonomous experimentation
  pipelines are all variants of the same reinforcement-loop
  framing. This implies cross-session memory and a
  persona-lifecycle workflow that does not exist in MVP
  (Pass 1 row #155 routes lifecycle metadata to §12; the
  reinforcement loop itself is the §13 aspiration).

- **Autonomous research workflows.** Multi-session pipelines
  would chain Symposium executions via
  `verdict.next_action = request_external_research`: one
  session's request becomes another session's problem
  statement, and the pipeline runs without human re-invocation.
  This requires the interactive / async execution modes
  formally deferred to v1+ (§12) plus a pipeline-orchestration
  layer outside the runtime's current scope.

- **Distributed orchestration (multi-host runtimes).** A
  single Symposium session would execute across multiple
  hosts, with the canonical_transcript replicated and the
  orchestrator_runtime sharded. ADR-001 forbids parallel
  agent execution within a turn; this Vision item would
  require relaxing the single-process state-machine
  assumption while preserving the §4.10 scheduler invariants.
  Compare AutoGen's gRPC-based distributed agent runtimes —
  Symposium would arrive at a similar capability via a
  different structural path.

- **Long-term memory across sessions.** Cross-session persona
  / knowledge accumulation would let a persona "remember"
  prior sessions' canonical_transcripts and incorporate that
  history into future context_packets. This requires a
  persona-memory store that does not exist in MVP, plus a
  privacy / retention model that the host cannot opt out of
  silently.

- **Graph-based reasoning visualization (interactive
  timeline).** An interactive timeline of the
  canonical_transcript would let a reviewer expand branches,
  filter by speaker, overlay verdicts, and replay forks
  visually. §12 already lists the HTML replay interface and
  the expandable-branch viz as Roadmap; the Vision item is
  the *interactive observatory* framing — a reasoning
  exploration tool, not just a replay viewer.

- **Cognitive orchestration runtime / "operating system for
  distributed cognition".** Pass 1 row #113 (cognitive OS,
  modular reasoning framework, adaptive expert council)
  describes a long-term framing in which Symposium becomes a
  substrate for arbitrary cognitive workflows beyond
  deliberation: composable reasoning building blocks, third-
  party persona ecosystems, cross-session reasoning state.
  This framing is **explicitly Vision** per N1; it is NOT a
  normative description of the MVP protocol. §10.1's
  positioning statement deliberately does not use this
  language because the Vision framing would invite confusion
  about what the protocol guarantees today.

- **Replayable AI debate as a public artifact format.** Pass 1
  rows #109, #157, #170: Symposium artifacts would become a
  public format for sharing structured machine reasoning —
  archived, viewed in a "reasoning observatory", used as
  educational material, exported for citation in publications.
  This implies a stable cross-implementation artifact format
  (achieved in part by §5.10 `Artifact` schema and §7.7
  `transcript_digest`) plus a community-curated archive
  that does not exist today.

### 13.2 Promotion mechanism (Vision → Roadmap)

A Vision item is promoted to §12 Roadmap when an
implementation ships a working prototype demonstrating
feasibility. Promotion happens through a future Pass-style
re-classification review:

- An implementor publishes the prototype with documentation
  describing the structural changes it implies for §4 / §5 /
  §6 / §7.
- A Pass-style review (analogous to Passes 1–10) assesses
  whether the prototype's structure is consistent with the
  protocol's ADRs.
- If consistent, the item moves from §13 to §12 with a Pass-N
  row recording the promotion.
- If inconsistent, the item remains in §13 with a note citing
  the ADR(s) the prototype would violate.

This mechanism is itself descriptive (non-binding, no RFC 2119
normative weight); it documents how Vision-to-Roadmap promotion
has worked historically (the Pass 0–10 plan) so that a future
maintainer inherits a recognizable process.

### 13.3 Vocabulary introduced by §13 (status table)

§13 introduces no new vocabulary. Every term used (recursive
sub-symposium, reinforcement loop, autonomous research workflow,
distributed orchestration, long-term memory, cognitive
orchestration runtime, reasoning observatory) is descriptive
prose. None of these terms appears in any schema (rule N11) and
none is normative (rule N1).

### 13.4 Coverage table — Pass-1 rows targeting §13

| Pass-1 row | Description | Disposition | §13 home |
|------------|-------------|-------------|----------|
| #63 (split aspect) | Recursive sub-symposiums / fine-tuning / RL loops / autonomous experimentation / distributed orchestration | Absorbed (multiple §13.1 items) | §13.1 |
| #72 | Phase-4 autonomous research workflows / recursive symposiums / long-term memory | Absorbed | §13.1 |
| #73 | Final Vision — reasoning framework / collaborative AI thinking / public OSS experimentation platform / structured machine debate | Absorbed (cognitive OS framing) | §13.1 (last item) |
| #95 (split aspect) | Future distributed orchestration | Absorbed (distributed orchestration item) | §13.1 |
| #109 | Long-term replay vision — reasoning observatory / educational viz / public sharing | Absorbed (replayable AI debate item) | §13.1 |
| #113 | Adaptive expert council / cognitive OS / modular reasoning framework | Absorbed verbatim with N1 compliance | §13.1 (cognitive OS item) |
| #116 (split aspect) | Meta personas (ecosystem governance) | Absorbed (reinforcement-loop item subsumes governance) | §13.1 |
| #134 | "Creates a self-improving ecosystem" | Absorbed (reinforcement-loop item) | §13.1 |
| #135 (split aspect) | Long-term ecosystem aspirational framing | Absorbed (cognitive OS item) | §13.1 |
| #139 | Final conceptual vision — cognitive orchestration engine / modular expert council / structured reasoning framework | Absorbed (cognitive OS item) | §13.1 |
| #157 | Final architectural vision — cognitive orchestration runtime / multi-agent reasoning OS / replayable deliberation framework | Absorbed (cognitive OS + replayable debate items) | §13.1 |
| #170 | Long-term OSS vision — ecosystem for structured machine reasoning / reusable deliberation runtime / modular cognitive orchestration | Absorbed (cognitive OS + replayable debate items) | §13.1 |

**Coverage: 12 / 12 §13-targeted Pass-1 rows absorbed; 0 silently
dropped.** Several rows (#63, #95, #116, #135) split between
§12 and §13 — the split is logged in both chapters' coverage
tables.

---

## 14. Repository Strategy

**[Meta]** (non-normative; out of RFC 2119 scope)

Repository layout, installation guidance, licensing rationale,
contribution model, and related repo-engineering material live
in [`docs/repository-strategy.md`](repository-strategy.md). That
companion file is not part of the normative specification, is
not subject to this document's versioning policy (§5.1), and a
conforming implementation can diverge from its conventions
without affecting protocol conformance.

Lowercase verbs in this section are deliberate: §14 is the
non-normative Meta chapter and does not use RFC 2119 keywords.

---

## Appendix A — Architectural Decision Records (ADRs)

The following ADRs are referenced throughout. They are reproduced for
quick reference and frozen as of the convergence point on 2026-05-23
(joint Claude+Codex review).

- **ADR-001** — Core protocol is sequential, conversational, reactive.
- **ADR-002** — Coordinator verdict is separated from runtime termination.
- **ADR-003** — Directed inter-agent communication is via structured output only (no inline `@` parsing).
- **ADR-004** — MVP execution mode is `batch`-only.
- **ADR-005** — Three-role separation: `Selector` / `CoordinatorAgent` / `OrchestratorRuntime`, with `deliberation_panel` distinct from `coordinator_agent`.

---

## Appendix B — Refinements (R-series)

- **R1** — A round terminates when every agent in `active_deliberation_panel`
  has had its `primary_turn`. Coordinator performs a `coordination_turn`
  after the round; forks do not increment the round counter.
- **R2** — Convergence detection: Coordinator emits
  `verdict.next_action = finalize` when applicable; OrchestratorRuntime
  hard caps may force termination independently. Runtime MUST attempt
  synthesis; if impossible, MUST persist a termination artifact explaining why.
- **R3** — MVP Selector default: `strategy = fixed` with
  `default_deliberation_panel = [logician, visionary, researcher, critic, engineer]`
  and `coordinator_agent = coordinator`. LLM-driven Selector is opt-in from v1.

---

## Appendix C — Joint-review anchor list (A-series)

The A-series numbers cited inline (e.g. "A2", "A5") refer to the
turn-1 "missed points" raised by Codex in the original joint
review. The five anchors are reproduced here for reference;
inline citations in §1–§13 use predominantly A2 and A5, while
A1, A3, and A4 surface as historical anchors that have since
been subsumed into the ADR-series.

- **A1** — RFC 2119 normative keywords + scope tags
  (`[Core MVP]` / `[v1]` / `[Roadmap]` / `[Vision]`). Anchors
  §1's normative-language convention and the rule that
  Roadmap / Vision content carries no MUST.
- **A2** — Determinism qualifier: the scheduler is
  deterministic; provider outputs are replayable (re-renderable
  from a persisted `canonical_transcript`) but not
  regeneratively reproducible unless provider / model / sampling
  / cache / tool environment are pinned. Anchors §2.7, §7.5,
  §7.6, §7.8, and rule N3.
- **A3** — Positioning: "opinionated deliberation protocol",
  not "agent framework". Anchors §1, §10.1, §10.2 D1, and
  Pass-1 Q7 / row #1.
- **A4** — Three-role separation of CoordinatorAgent (LLM
  semantic moderator) from OrchestratorRuntime (deterministic
  scheduler / terminator). Subsumed and superseded by ADR-005
  in turn 3; surviving citation lives in ADR-005 references.
- **A5** — Persona scope split: horizontal personas declare
  `reasoning_scope` and reason cross-domain;
  domain personas declare `domain_scope` + `forbidden_domains`
  + `must_delegate`. Anchors §2.3, §5.3, and the Persona schema's
  two required-field sets (Pass-1 Q2).

The companion **M-series** (M1..M5) — Claude's turn-2 missed
points from the same review — is referenced in passing as M2
(replay semantics: `transcript_replay` vs `execution_replay`),
M3 (inline `@AgentName` is prompt-injectable; structured
`direct_request` only), M4 (Selector is an LLM call with its
own budget; `strategy ∈ {fixed, rules, llm}`), and M5
(disagreement-vs-convergence operational closure criterion).
M1 (latency budget / execution modes) is anchored in ADR-004
and `budget.max_wallclock_seconds` (§5.2 / §4.7).

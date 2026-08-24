# Symposium 2.0 — rooms, presence, and static photographic agents

Status: zero-cost static presence, persistent room control, and the first
browser-native room execution slice. The protocol in `docs/specification.md`
remains frozen at v1.0.0.

## Product intent

Symposium 2.0 is a work environment that feels like a persistent group call.
The user enters a room, addresses a team, watches agents listen and respond,
invites a specialist when needed, and dismisses that guest after the briefing.
Sartori is the quiet control-plane coordinator: it listens for instructions to
create, archive, or switch rooms and to create, onboard, invite, or remove
agents.

The v1 structured deliberation is not discarded. It becomes one conversation
mode inside a room and remains the deterministic source of transcript,
termination, replay, metrics, and tamper evidence.

## Architectural boundary

Presentation must not mutate historical truth or require a metered visual
service.

```text
v1 deliberation + transcript                 Symposium 2.0 shell
┌──────────────────────────┐                 ┌─────────────────────────┐
│ scheduler                │   domain events │ room / membership       │
│ model providers          ├────────────────►│ presence projection     │
│ transcript + artifact    │                 │ local identity catalog  │
│ digest + replay          │                 │ meeting UI              │
└──────────────────────────┘                 └─────────────────────────┘
```

Avatar asset references, optional browser voice selections, and room presence
are runtime/product data. They are not added to the frozen v1 config, message,
or artifact schemas. A historical run therefore remains byte-identical whether
it is read as text or displayed with static portraits.

## Domain model

The first persistent control-plane model should use four aggregates:

- `Workspace`: owner, default room, execution policy, and budget.
- `Room`: name, purpose, lifecycle, conversation mode, and memory boundary.
- `Agent`: globally reusable persona, instruction set, capabilities, optional
  local portrait and lifecycle.
- `RoomMembership`: the agent's role and state in one room, onboarding record,
  permissions, join/leave timestamps, and room-scoped context.

Presence is event-driven. The initial state vocabulary is `offline`, `joining`,
`listening`, `thinking`, `speaking`, and `leaving`. A guest is not copied into a
room: a membership references the global agent and records the context disclosed
for that visit.

Required room commands are:

```text
create_room       archive_room       switch_room
create_agent      onboard_agent      invite_agent
set_listening     request_briefing   interrupt_agent
dismiss_agent
```

The canonical acceptance scenario is: invite the responsible agent for Zeus
Focus Talking into the current call, disclose the minimum project context, let
it brief the group, then dismiss it while preserving an auditable room event
trail.

## Zero-cost presentation contract

The visual layer has one active contract:

1. Resolve the persona to a packaged local portrait when one exists.
2. Otherwise render initials and a color derived deterministically from the
   persona ID. The same agent keeps the same visual identity across rooms.
3. Mark the current speaker with a strong border and full luminance.
4. Attenuate every other participant while that speaker has the floor.
5. Never animate, synthesize video, open a remote avatar session, or require an
   avatar API key.

Local neural speech synthesis is an optional convenience. Visual focus works
with voice disabled and therefore does not depend on audio availability.

### Adding a new agent

Agent creation never waits for portrait generation. Sartori proposes one of 50
packaged fictional identities that is not assigned to another agent, shows its
portrait and voice presentation, and allows rerolling before creation. The
selected `avatar_id` is persisted with the agent. Unknown legacy run personas
still have a deterministic HTML/CSS initials fallback with no network, account,
GPU, or payment requirement.

## Current vertical slice

Implemented:

- six original synthetic photographic identities for the built-in panel;
- fifty additional synthetic identities for new agents, with unused-only
  preview, reroll, and persisted assignment;
- a responsive meeting grid in the existing read-only viewer;
- participant state derived from persisted SSE events;
- static active-speaker emphasis plus attenuation of the other participants;
- no portrait motion, CSS animation, lip synchronization, or avatar video;
- Sartori as the product-facing name for protocol agent `coordinator`;
- explicit synthetic-identity disclosure and accessible image descriptions;
- deterministic local identity cards for unknown legacy/adaptive personas;
- strict allow-listing of portrait assets by the viewer HTTP server;
- manual-by-default replay plus word-aware 0.5×, 1×, and 2× automatic modes;
- complete-turn local neural narration that advances only after speech ends;
- pinned local Parler-TTS Italian synthesis with no external API key, plus
  persisted feminine/masculine voice metadata matching each avatar;
- an atomically persisted local workspace with rooms, reusable agents,
  memberships, onboarding context, and a contiguous room-event trail;
- a default Symposium room containing the built-in panel and Sartori;
- new rooms that start with Sartori silently listening;
- CLI and `sartori_*` MCP commands for create, switch, invite, and dismiss;
- a browser-native Sartori panel that follows the active room, creates and
  archives rooms and agents, and updates membership without exposing private
  agent instructions;
- a bounded deterministic Italian command input with no LLM or network call;
- a tested Zeus membership lifecycle through create, onboard, invite, switch,
  presence, and dismiss operations;
- a browser-native **Parla alla stanza** composer that starts execution without
  a second terminal command;
- an explicit execution activity card showing automatic-loop confirmation,
  current thinker, elapsed time, expected local-model latency, completion, and
  actionable failure state;
- background room-to-v1 binding using only speaking memberships, with observers
  and Sartori excluded from the panel while Sartori remains coordinator;
- private agent instructions and membership onboarding context disclosed only
  to that agent's persisted v1 persona configuration;
- `briefing_requested`, `briefing_completed`, and `briefing_failed` audit events
  linking each room conversation to its immutable run;
- a deterministic end-to-end Zeus guest briefing that produces a synthesis and
  remains replayable through the normal viewer;
- a generated, ad-hoc-signed macOS `Symposium.app` with a small native AppKit
  supervisor that starts the loopback Python backend and opens the browser
  without displaying Terminal;
- singleton launcher semantics: a second double-click reopens the existing
  workspace, while stale PID locks recover after a crash; and
- browser-native clean shutdown, rejected while a room execution is active so
  local LLM subprocesses are never knowingly orphaned;
- isolated Claude CLI room turns: compact deliberation-only system prompt, safe
  mode, empty built-in tool set, empty MCP registry, and no Claude-side session
  persistence while retaining the user's subscription authentication;
- Claude CLI structured-output compatibility for successful result envelopes
  whose internal output facility reports `stop_reason: tool_use`; and
- technical runtime terminations mapped to failed jobs and `briefing_failed`
  audit events with the original provider diagnostic.

Not implemented yet:

- conversational continuity and room memory across multiple v1 runs (each
  browser question currently creates one bounded immutable session);
- concurrent executions (the local viewer deliberately permits one active
  room run at a time for this slice);
- multi-user permissions and disclosure policies beyond room-scoped context;
- microphone input, speech recognition, or voice interruption transport; and
- additional model/provider adapters for expanding the packaged portrait pool.

The replay is the zero-cost presence harness. In the default manual mode it
presents one turn, keeps that speaker active, and waits for **Avanti**. If
**voce locale** is enabled, advancing remains locked until the whole turn has been
spoken. Automatic modes wait for the real speech completion or, without voice,
estimate a readable interval from the word count. Live runs are never delayed.

## Static-presence acceptance gate

| Measure | Acceptance target |
|---|---|
| Active speaker | unmistakable without reading the transcript |
| Other participants | visible and identifiable, but clearly attenuated |
| Motion | no portrait, tile, transcript, or connector animation |
| Network | no visual-service or image-generation request at runtime |
| New agent | immediately receives one unused packaged synthetic portrait |
| Identity stability | persisted avatar id keeps face and voice together |
| Accessibility | state remains available in text and ARIA labels |
| Failure behavior | missing portrait falls back locally without blocking |

Paid avatar providers and hosted talking-head experiments are outside the
active product scope. Reintroducing one would require an explicit product
decision, a separate opt-in design, and must never be the default path.

## Delivery sequence

1. ✅ Ship and validate zero-cost static presence plus user-controlled replay.
2. ✅ Persist workspace, room, agent, membership, and room-event records.
3. ✅ Expose Sartori control commands through CLI and MCP host integration.
4. ✅ Connect room conversations to execution, fulfill a guest briefing, and
   complete the Zeus invite/brief/dismiss scenario at the model layer.
5. ✅ Ship a terminal-free macOS launcher with UI-owned clean shutdown.
6. ✅ Add no-marginal-cost portrait onboarding with a 50-identity packaged pool.
7. ✅ Add optional local neural speech while preserving the static baseline.
8. Add permission scopes, audit, quotas for model execution, and recovery.

Every slice must retain the complete v1 test/replay suite. CI and local viewing
must never depend on a paid avatar service or live visual network.

### Local voice engine

The viewer does not use `speechSynthesis`. From the **voce locale** control it
can prepare an isolated Parler-TTS Mini Multilingual v1.1 runtime and download
the pinned model once (about 3.8 GB) into `.symposium/`. No API key or hosted
inference call is involved. Generated WAV clips are content-addressed and
cached under `.symposium/tts-cache/`.

The engine supports Italian and its trained speaker metadata includes Julia
and Richard. Every avatar definition binds one of these speakers plus a stable
description of age, pitch, tone, and pacing. The browser sends only the agent
id and text; the server resolves the trusted voice description from the
persisted avatar assignment. The active-speaker focus starts on the real audio
`play` event and ends with that clip, while thinking focus remains available
underneath for the next agent.

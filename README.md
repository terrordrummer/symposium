<p align="center">
  <img src="docs/assets/logo.png" alt="Symposium logo" width="180">
</p>

<h1 align="center">Symposium</h1>

<p align="center">
  <em>An opinionated protocol for structured, sequential, adversarial multi-agent deliberation.</em>
</p>

<p align="center">
  <a href="docs/specification.md"><img alt="Spec" src="https://img.shields.io/badge/spec-1.0-1a365d?style=flat-square"></a>
  <a href="docs/schemas/v1.0.0/"><img alt="Schemas" src="https://img.shields.io/badge/JSON%20Schema-v1.0.0-d4a017?style=flat-square"></a>
  <a href="https://github.com/terrordrummer/symposium/actions/workflows/validate.yml"><img alt="Validators" src="https://img.shields.io/github/actions/workflow/status/terrordrummer/symposium/validate.yml?branch=main&label=validators&style=flat-square"></a>
  <a href="LICENSE"><img alt="License" src="https://img.shields.io/badge/license-Apache%202.0-333333?style=flat-square"></a>
</p>

---

## What is this?

Symposium is a **specification**, not a library. It defines how a runtime
should orchestrate a small panel of LLM-backed agents through a
structured, turn-based deliberation that produces a single, replayable,
schema-validated artifact.

It is **not** a generic agent framework. It enforces exactly one
conversation topology — fixed panel, one primary turn per agent per
round, one structurally-separated coordinator, bounded forks — and
trades topology flexibility for **testable scheduler invariants** and
**byte-identical replay** of any past session.

Read the specification: **[`docs/specification.md`](docs/specification.md)**.

---

## Why one more protocol?

Most multi-agent stacks expose enough flexibility (group chat,
arbitrary handoffs, nested supervisors) that any two implementations
diverge on the parts that matter — when does the conversation stop, what
exactly is replayed, what fails the run, how is delegation routed. Each
implementation invents its own answers, and operators end up debugging
the framework instead of the agents.

Symposium goes the opposite way: **one opinionated topology, sharp
boundaries, closed enums.** What you get in exchange:

| | Symposium |
|---|---|
| **Topology** | Fixed `deliberation_panel`, one `primary_turn` per agent per round, single `coordination_turn` from a structurally-separated `coordinator_agent`. |
| **Inter-agent routing** | Schema-validated `direct_request` only. Inline `@AgentName` in prose is never routing — prompt-injection resistant by construction. |
| **Roles** | Three-way separation: `Selector` chooses *who*, `CoordinatorAgent` recommends *what next* (LLM, no executive power), `OrchestratorRuntime` schedules and terminates (deterministic code, sole party that decides when a session stops). |
| **Failure surface** | Closed 7-value termination-reason enum; closed 12-value adapter `error.kind` enum; closed 3-value `on_agent_failure` policy. |
| **Replayability** | Four distinct contracts documented separately: `transcript_replay` (unconditional byte identity), `execution_replay` (conditional on ten pinning conditions), golden-test byte identity, `fake_provider` determinism. No "it should be deterministic" hand-waving. |
| **Persistence** | Canonical `Artifact` (§5.10) with RFC-8785 JCS-canonicalized `transcript_digest` (SHA-256). Tamper-evident. |
| **Execution mode** | MVP is **batch-only** (ADR-004). Interactive / event-stream / async are explicitly v1+. |

Full discussion in §10 *Competitive Positioning* of the spec.

---

## What's in this repo

```
.
├── docs/
│   ├── specification.md          # The protocol (normative, ~6440 lines)
│   ├── repository-strategy.md    # Reference-impl conventions (non-normative)
│   └── schemas/v1.0.0/           # 16 JSON Schemas (Draft 2020-12)
│       └── examples/             # 28 positive + 36 negative fixtures + validators
├── .github/workflows/validate.yml
├── LICENSE                       # Apache 2.0
└── README.md
```

**What's normative**: `docs/specification.md` §1–§9 + the JSON Schemas
under `docs/schemas/v1.0.0/`. A conformant Symposium runtime satisfies
every MUST / MUST NOT there and validates against the schemas. Sections
§10–§13 are positioning, integration, roadmap, and vision (non-binding).
§14 is a thin pointer to the non-normative companion.

**What's not in this repo**: there is no reference implementation in
this repository yet. The spec is the deliverable; runtimes can be
written in any language. The repo-strategy companion sketches a
Python-flavoured layout for the eventual reference implementation
(`pip install symposium`).

---

## Conformance check

The schemas ship with two validators. Any contributor or implementor
can re-run them locally:

```bash
cd docs/schemas/v1.0.0/examples
pip install "jsonschema==4.26.0" "referencing>=0.35" "rfc8785>=0.1.4"
python3 validate.py            # 28/28
python3 validate_negative.py   # 36/36
```

CI runs both on every push and every pull request (see badge above).

---

## Reading order

If you only want the gist, the first 200 lines of the spec are enough:
§1 (conformance surface), §2 (vocabulary), §3 (overview + non-goals).

If you intend to implement: §1 → §2 → §4 (runtime + scheduler) →
§5 (schemas) → §6 (provider/tool adapter contract) → §7
(persistence + replay) → §8 (budget + failure + security) →
§9 (testing harness). §4.11 is the canonical pseudocode.

If you want to compare against existing frameworks: §10 covers
AutoGen, CrewAI, LangGraph, and OpenAI Agents SDK.

---

## Status

**v1.0 — specification frozen 2026-05-26.** Ratified by joint adversarial
review (10 passes, bilateral sign-off). The 16 JSON Schemas under
`docs/schemas/v1.0.0/` are pinned at this version. Forward-compatible
changes will publish under `docs/schemas/v1.1.0/` etc., per the
versioning policy in §5.1.

Issues, errata, and discussion: use the GitHub issue tracker.

## License

[Apache 2.0](LICENSE).

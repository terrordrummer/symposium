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
  <a href="symposium/"><img alt="Reference impl" src="https://img.shields.io/badge/reference%20impl-Python%203.11%2B-3776ab?style=flat-square"></a>
  <a href="https://github.com/terrordrummer/symposium/actions/workflows/validate.yml"><img alt="CI" src="https://img.shields.io/github/actions/workflow/status/terrordrummer/symposium/validate.yml?branch=main&label=ci&style=flat-square"></a>
  <a href="LICENSE"><img alt="License" src="https://img.shields.io/badge/license-Apache%202.0-333333?style=flat-square"></a>
</p>

---

## What is this?

Symposium is a **protocol specification** + a **reference Python
runtime** that orchestrates a small panel of LLM-backed agents through
a structured, turn-based deliberation, producing a single, replayable,
schema-validated artifact.

It is **not** a generic agent framework. It enforces exactly one
conversation topology — fixed panel, one primary turn per agent per
round, one structurally-separated coordinator, bounded forks — and
trades topology flexibility for **testable scheduler invariants** and
**byte-identical replay** of any past session.

Two things ship together in this repo:

1. **`docs/specification.md`** — the normative protocol. Implementable
   in any language. The spec is what conformance means.
2. **`symposium/`** — the reference Python runtime. Walking-skeleton
   scope today: full scheduler, persistence, replay, and a deterministic
   `FakeProvider` adapter. Real provider adapters (OpenAI-shaped,
   Anthropic-shaped) land in a follow-up milestone.

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

## Quick start

The reference runtime ships with a deterministic `FakeProvider`. You
can drive a complete two-round deliberation against scripted agent
responses end-to-end — no API keys, no network — and inspect the
persisted, byte-identical artifact:

```bash
# Install (editable, while the package is pre-PyPI)
git clone https://github.com/terrordrummer/symposium
cd symposium
pip install -e .

# Run a session
symposium run \
  --config examples/configs/walking-skeleton.yaml \
  --script examples/scripts/walking-skeleton.json \
  --output runs/ \
  examples/problem.md

# Replay (byte-identity check on the stored canonical_transcript)
symposium replay runs/demo-walking-skeleton-001

# Validate the artifact against the v1.0.0 JSON Schemas
symposium validate runs/demo-walking-skeleton-001/artifact.json
```

Or use it as a library:

```python
from symposium import Config, FakeProviderScript
from symposium.providers import FakeProvider
from symposium.scheduler import run_session

artifact = run_session(config, {"default": FakeProvider(script=script)},
                       runs_root="runs/")
print(artifact.transcript_digest)        # 64-hex JCS-SHA-256 digest
print(artifact.outcome.kind)             # "synthesis" or "termination"
```

---

## What's in this repo

```
.
├── docs/
│   ├── specification.md          # The protocol (normative, ~6440 lines)
│   ├── repository-strategy.md    # Reference-impl conventions (non-normative)
│   └── schemas/v1.0.0/           # 16 JSON Schemas (Draft 2020-12)
│       └── examples/             # 28 positive + 36 negative fixtures + validators
├── symposium/                    # Reference Python runtime
│   ├── models.py                 # Pydantic models mirroring the JSON Schemas
│   ├── providers/                # ProviderAdapter contract + FakeProvider
│   ├── scheduler/                # §4.11 pseudocode → executable loop
│   ├── storage/                  # Run directory layout + JCS digest
│   ├── replay/                   # transcript_replay (§7.5)
│   ├── personas/                 # MVP default panel (R3)
│   └── cli/                      # `symposium` command
├── examples/                     # Walking-skeleton config + script
├── tests/                        # pytest suite (FakeProvider determinism,
│                                 #   scheduler invariants, e2e schema
│                                 #   validation, replay byte-identity)
├── pyproject.toml
├── .github/workflows/validate.yml
├── LICENSE                       # Apache 2.0
└── README.md
```

**What's normative**: `docs/specification.md` §1–§9 + the JSON Schemas
under `docs/schemas/v1.0.0/`. A conformant Symposium runtime satisfies
every MUST / MUST NOT there and validates against the schemas. Sections
§10–§13 are positioning, integration, roadmap, and vision (non-binding).
§14 is a thin pointer to the non-normative companion.

**What's reference, not normative**: everything under `symposium/`,
`examples/`, and `tests/`. The Python package is one valid implementation
of the protocol; a different runtime in a different language is equally
valid as long as it conforms to the spec.

---

## Conformance check

Two validators ship with the schemas. Any contributor or implementor
can re-run them locally:

```bash
cd docs/schemas/v1.0.0/examples
pip install "jsonschema==4.26.0" "referencing>=0.35" "rfc8785>=0.1.4"
python3 validate.py            # 28/28
python3 validate_negative.py   # 36/36
```

The reference runtime's own test suite (pytest) cross-checks the
artifact it emits against those same schemas:

```bash
pip install -e ".[test]"
pytest -q
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

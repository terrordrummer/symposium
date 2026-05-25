# Symposium — Repository Strategy

> **Non-normative companion document.** This file describes the
> repository layout, install / contribute / release conventions,
> and licensing rationale for an open-source Symposium
> implementation. It is **not** part of the normative
> specification (`docs/specification.md`) and does **not** use
> RFC 2119 normative keywords; verbs in this file are
> descriptive ("the file ignores", "the repository can include",
> "contributors are encouraged"). A conforming runtime authored
> in a different language, repository layout, or organization is
> fully valid as long as it satisfies `docs/specification.md`.
> This file describes the reference Python implementation's
> conventions only.
>
> This file is revised independently of
> `docs/specification.md`'s versioning policy (§5.1 of the spec).
> Conformance-testing tools consult the specification, never
> this file.

---

## 1. Repository identity

- **Recommended name**: `symposium`
- **Recommended URL**: `github.com/<owner>/symposium`
  (the owner is intentionally a placeholder; pick a personal,
  organizational, or fork-specific owner). The normative spec
  uses the owner-agnostic form in §1; do not commit a specific
  owner into the spec body.
- **License**: Apache License 2.0 (rationale in §3 below).
- **Status label**: `Experimental / Early Architecture Phase`.
  The label is intended to communicate active exploration,
  evolving APIs, architectural experimentation, and openness to
  contributions. The label is informational; the protocol's
  conformance surface is defined by `docs/specification.md` and
  the JSON schemas under `docs/schemas/v1.0.0/`.

## 2. Repository layout

The reference Python implementation uses the following tree.
Implementations in other languages or organizations are free to
diverge; only the `docs/specification.md` and
`docs/schemas/v1.0.0/` paths carry normative weight (and only
because the schemas are the conformance surface).

```text
symposium/
├── README.md                  — concise project introduction (see §6)
├── LICENSE                    — Apache 2.0 text
├── .gitignore                 — see §7
├── pyproject.toml             — Python package metadata
├── CONTRIBUTING.md            — see §8
├── ROADMAP.md                 — see §9
├── docs/
│   ├── specification.md       — normative protocol spec
│   ├── repository-strategy.md — this file (non-normative)
│   └── schemas/
│       └── v1.0.0/
│           ├── *.schema.json  — JSON Schemas (§5 of the spec)
│           └── examples/      — fixtures + validate.py / validate_negative.py
├── symposium/                 — reference Python package
│   ├── orchestrator/          — orchestrator_runtime (ADR-005)
│   ├── personas/              — built-in persona configs
│   ├── providers/             — ProviderAdapter implementations (§6)
│   ├── replay/                — transcript_replay / execution_replay
│   ├── scheduler/             — round / branch / queue mechanics
│   ├── storage/               — run directory I/O
│   └── cli/                   — `symposium` CLI entrypoint
├── examples/
│   └── configs/               — example YAML configs (vendor literals live here)
└── tests/
```

The tree absorbs Pass-1 rows #48 (v0 `§Repo Structure`) and
#161 (v0 `§Initial Repo Structure`). Aspects rooted in the
normative spec — provider adapter contract (§6),
`orchestrator_runtime` (§4), replay surfaces (§7) — live in
modules whose names mirror the spec sections.

## 3. Licensing rationale

The reference implementation uses **Apache License 2.0**. The
selection is conventional rather than principled, anchored in:

- **Permissive**: downstream re-licensing, commercial and
  non-commercial reuse without contagion.
- **Enterprise-friendly**: most corporate legal review processes
  pre-approve Apache 2.0; this lowers the friction of adoption
  inside companies.
- **Patent grant**: Apache 2.0 includes an explicit patent
  license / termination clause that MIT/BSD lack. For a
  protocol with an extensibility surface (provider adapters,
  personas, host wrappers) this materially reduces patent risk
  for contributors and downstream users.
- **Ecosystem compatibility**: Apache 2.0 is one-way compatible
  with the GPLv3 family and is the prevailing license for
  comparable Python AI tooling.

Alternative licenses may be used by downstream forks or
ecosystem modules without affecting protocol conformance.

(Absorbs Pass-1 row #160.)

## 4. Installation

The reference implementation targets standard Python packaging.
Two installation paths are expected:

```bash
# Stable / release install (when published to PyPI):
pip install symposium

# Development install:
git clone github.com/<owner>/symposium
cd symposium
pip install -e .
```

Optional containerized install:

```bash
# Docker
docker pull <owner>/symposium:latest
docker run --rm -it <owner>/symposium symposium run problem.md

# Docker Compose (multi-provider local development)
docker compose up
```

The CLI invocation contract is normative and lives in §11.2 of
the specification; this file documents only the installation
surface.

(Absorbs Pass-1 rows #66, #68.)

## 5. Environment variables (host convention)

The reference CLI reads provider credentials from environment
variables by convention:

```bash
export OPENAI_API_KEY=...
export ANTHROPIC_API_KEY=...
# additional variables per ProviderAdapter implementation
```

These variables are **host conventions**, not part of the
protocol. The normative credential-handling surface lives in
`docs/specification.md` §6.8: provider adapters receive
credentials through an injected `ProviderCredentials` object;
the env-var lookup happens at the CLI / host layer and is
implementation-defined. Other hosts (IDE plugins, library
embedders) may obtain credentials differently while remaining
conformant.

(Absorbs Pass-1 row #67 — Meta aspect. The runtime-level
credential-handling contract lives in spec §6.8.)

## 6. README philosophy and tagline

The README is the project's front door. It should remain:

- **concise** — readable in 5 minutes;
- **accessible** — for newcomers who have never read the spec;
- **architectural** — explains the three roles (selector /
  coordinator_agent / orchestrator_runtime, ADR-005) and the
  one-topology positioning (D1);
- **vision-oriented** — frames the project's positioning
  alongside §10 of the spec.

The README should **not** contain:

- the full specification (it lives in `docs/specification.md`);
- excessive implementation detail (per-module docs live next to
  the modules);
- prompt dumps or model-specific tuning.

**Recommended README tagline:**

```text
Symposium — an opinionated protocol for structured, sequential,
adversarial multi-agent deliberation.
```

The v0 draft tagline used "modular framework for structured
multi-agent reasoning, adversarial collaboration, and replayable
AI deliberation" (Pass-1 row #163). Pass 1 rule N1 and
Pass-1 row #1 retired "framework" as a self-description for
Symposium; the rewrite above matches §1 / §10.1 of the spec.
"Replayable" is retained only when paired with the §7.5 / §7.6
qualifier (transcript replay is unconditional; execution replay
is conditional on pinning).

(Absorbs Pass-1 rows #162, #163.)

## 7. `.gitignore` baseline

The reference repository ignores standard Python build / test
artifacts plus the runtime-generated run directories
(`runs/`, `artifacts/`, `sessions/`, `logs/`):

```gitignore
# Python
__pycache__/
*.py[cod]
*.so

# Virtual environments
.venv/
venv/
env/

# Build artifacts
build/
dist/
*.egg-info/

# Test / type-checker caches
.pytest_cache/
.mypy_cache/
.coverage
htmlcov/

# Secrets
.env
.env.*

# Symposium runtime output
runs/
artifacts/
sessions/
logs/

# Editor / OS
.DS_Store
.vscode/
.idea/
```

Generated Symposium artifacts (`runs/<session_id>/...`,
§7.1 of the spec) are not committed by default: artifacts are
session-specific output, not source.

(Absorbs Pass-1 row #165.)

## 8. `CONTRIBUTING.md` sketch

`CONTRIBUTING.md` defines:

- **Coding standards** — language, formatter, linter, type
  checker conventions for the reference implementation.
- **Persona contribution guidelines** — how to propose a new
  built-in persona. Per Pass-1 row #125 and spec §3
  (no autonomous persona creation), new personas are
  introduced **offline / intentionally / through review /
  after postmortem analysis**. The recommended workflow:
  session analysis → capability gap identified → persona
  proposal → human review → persona spec → validation → added
  to library. The Roadmap aspect of the persona-registry
  surface lives in spec §12.
- **Plugin / adapter requirements** — what a downstream
  provider adapter, replay tool, or scheduler plugin needs to
  satisfy to be acceptable upstream. Reference: spec §6
  (ProviderAdapter contract) for adapters; spec §12 for the
  plugin-architecture Roadmap.
- **Review processes** — branch / PR conventions, required
  tests (the `docs/schemas/v1.0.0/examples/` validators are
  run on every PR), spec-vs-code coupling check.

(Absorbs Pass-1 rows #125 Meta aspect, #167.)

## 9. `ROADMAP.md` sketch

`ROADMAP.md` is a thin front-of-repo pointer that:

- summarises the current development phase (see §10 below);
- links to spec §12 for the **normative Roadmap** (target
  windows: `v1`, `v1+`, `Roadmap`, etc.);
- links to spec §13 for **Vision** items that are not yet
  promoted to Roadmap.

`ROADMAP.md` is descriptive prose; the authoritative aggregation
table is `docs/specification.md` §12.2. Examples of entries
that historically lived in the v0 ROADMAP sketch — core
orchestrator, persona registry, provider abstraction, HTML
replay viewer, TTS playback, plugin architecture, benchmarking
suite, IDE integration — are all individually scoped in spec
§12.2 with target windows and ADR / Pass-1 anchors.

(Absorbs Pass-1 row #167.)

## 10. Initial development priorities

The reference implementation's near-term work is biased toward
**publishing architecture without overbuilding implementation**
(Pass-1 row #168):

- **Phase 1 (current)** — publish architecture, specifications,
  repository structure, roadmap, and core philosophy. The work
  product to date — `docs/specification.md`, the v1.0.0 JSON
  schemas, this companion file — is the Phase 1 deliverable.
- **Implementation milestones (Phase 1 build-out)** — when
  building the reference runtime: transcript system,
  `orchestrator_runtime` loop, the MVP default panel
  (logician / visionary / researcher / critic / engineer), a
  minimal ProviderAdapter pair (OpenAI-shaped, Anthropic-shaped),
  and the CLI (§11.2 of the spec).
- **Beyond Phase 1** — all subsequent feature work is governed
  by spec §12 (Roadmap) and §13 (Vision), not by this file.
  See spec §12.1 for the Roadmap principles and §12.2 for the
  aggregation table.

The deliberate emphasis is to invite community feedback,
architectural refinement, and contributor discussion **before**
implementation hardens. Spec changes lag implementation
intentionally during Phase 1.

(Absorbs Pass-1 rows #69 and #168. Pass-1 row #59 (the v0
"agents avoid repetition" claim) covers in-session agent
behavior, not repo strategy; it is handled in spec §5.3 /
§9 and is not re-claimed here.)

## 11. Community contribution model (Meta aspect)

The reference repository invites third-party contributions in
several categories. The categories below cover only the
**repository-strategy / contribution-process** aspect; the
**Roadmap aspect** (whether a category is a v1 / Roadmap /
Vision target) lives in spec §12.

Categories the repository accepts upstream contributions for:

- persona contributions (built-in or downstream persona packs);
- provider adapters (new ProviderAdapter implementations per
  spec §6);
- replay systems and visualizers (HTML replay viewer, TTS
  playback, IDE integrations);
- orchestration strategies (selector strategies, scheduler
  plugins — gated by spec §12 plugin architecture);
- benchmarking tools and evaluation harnesses (spec §9.10,
  §12);
- domain-specific persona packs (scientific, software, legal,
  creative writing, philosophy — Pass-1 row #136).

The repository's `CONTRIBUTING.md` documents the process; the
**runtime contract** that contributions must satisfy is
governed by the spec.

(Absorbs Pass-1 rows #110 Meta aspect, #136 Meta aspect.
Roadmap aspects of these rows are absorbed in spec §12.)

## 12. Persona creation workflow (Meta aspect)

Per spec §3 (no autonomous persona creation) and Pass-1 row
#122 (the spec's normative invariant that the runtime never
autonomously creates personas during a session), persona
creation is an **offline, intentional, reviewed** process. The recommended workflow:

```text
Session analysis
    ↓
Capability gap identified  (see spec §7.10 / §9.11 — gap reports)
    ↓
Persona proposal           (community contribution model, §11 above)
    ↓
Human review
    ↓
Persona specification      (spec §5.3 Persona schema fields)
    ↓
Validation                 (docs/schemas/v1.0.0/examples/validate.py)
    ↓
Added to library
```

The **Roadmap aspect** — a future persona registry with
versioning, lifecycle states, and benchmarking — lives in spec
§12. The **runtime constraint** that the runtime never creates
personas autonomously lives in spec §3 (non-goal) and §3 / §4
(no live-creation invariant).

(Absorbs Pass-1 row #125 Meta aspect.)

## 13. Relation to `docs/specification.md`

This file is not part of the normative specification. The
practical consequences:

- **Versioning is independent.** Per spec §5.1 the schemas
  follow semver and the spec carries an explicit version
  policy. This file may be revised at any time without bumping
  the spec version.
- **Conformance ignores this file.** A conforming runtime
  consumes `docs/specification.md` (and the JSON schemas under
  `docs/schemas/v1.0.0/`); it does not need to read this file.
- **Cross-references go one way.** The spec's §14 contains a
  pointer to this file; this file cross-references the spec
  extensively. The spec body does not depend on any claim in
  this file.

If a future implementor produces a conforming Symposium runtime
in a different language, organization, or repository layout,
the only material they need is `docs/specification.md` and
`docs/schemas/v1.0.0/`. This file is reference-implementation
guidance, not protocol surface.

---

## Appendix — Pass-1 Meta-row coverage table

| Pass-1 row | v0 draft section | Aspect | Home in this file |
|------------|------------------|--------|-------------------|
| #4f | L23-L29 §Overview | Repo URL `github.com/<owner>/symposium` | §1 |
| #5 | L31-L36 §Overview | Install / configure / reproduce / contribute / extend | §4 + §8 + §11 |
| #17 | L138-L141 §1.Orchestrator | Python suggested implementation hint | §2 (reference-impl framing) |
| #48 | L550-L572 §Repo Structure | Initial repo tree | §2 |
| #64 | L725-L738 §OSS Goals | Project should be easy to install / contribute / modular / extensible / well-documented | §4 + §8 + §11 |
| #65 | L745-L749 §Installation | Target audience: researchers, developers, AI engineers, reasoning enthusiasts | Spec §1 reading-order pointer + this file's overall framing |
| #66 | L753-L761 §Installation | `pip install symposium` / `git clone … && pip install -e .` | §4 |
| #67 | L766-L768 §Installation | `OPENAI_API_KEY` / `ANTHROPIC_API_KEY` env vars (Meta aspect) | §5 (with cross-ref to spec §6.8 for the runtime contract) |
| #68 | L771-L773 §Installation | Optional Docker / Compose | §4 |
| #69 | L780-L786 §Dev Priorities | Phase-1 dev priorities (transcript / orchestrator loop / static agents / provider abstraction / CLI) | §10 |
| #110 | L1262-L1274 §OSS Community Vision | Repo encourages custom agents / viz plugins / provider adapters / TTS / orchestration strategies / UI themes / replay (Meta aspect) | §11 (Roadmap aspect in spec §12) |
| #125 | L1517-L1543 §Persona Creation Workflow | Personas created offline / intentionally / through review / after postmortem | §8 + §12 (Roadmap aspect in spec §12) |
| #136 | L1709-L1721 §Community Contribution Model | OSS supports third-party personas / orchestration / shared persona packs / domain templates (Meta aspect) | §11 (Roadmap aspect in spec §12) |
| #158 | L2047-L2057 §OSS Repo Strategy | Recommended repo name `symposium`; GitHub location | §1 |
| #160 | L2090-L2102 §Licensing | Apache 2.0 + rationale | §3 |
| #161 | L2106-L2131 §Initial Repo Structure | Detailed tree with README / LICENSE / .gitignore / pyproject / CONTRIBUTING / ROADMAP / docs/ / package modules / examples/ / tests/ | §2 |
| #162 | L2138-L2151 §README Philosophy | README concise / accessible / architectural / vision-oriented | §6 |
| #163 | L2156-L2163 §README Tagline | Rewrite "framework" → "opinionated protocol" per N1 | §6 |
| #164 | L2169-L2179 §Status Label | "Experimental / Early Architecture Phase" | §1 |
| #165 | L2184-L2219 §.gitignore | Standard Python gitignore + runs/ artifacts/ sessions/ logs/ | §7 |
| #166 | L2226-L2233 §Community Contribution Strategy | OSS encourages persona / adapter / replay / orchestration / benchmarking / viz contributions | §11 |
| #167 | L2241-L2270 §Additional Files | CONTRIBUTING.md + ROADMAP.md sketches | §8 + §9 |
| #168 | L2278-L2293 §Initial Dev Strategy | Phase-1 publish-without-overbuilding strategy | §10 |

**Coverage:** all 23 §14-routed Meta-tagged rows from the
original spec classification are absorbed above. Two additional
rows are dual-targeted (primary scope `Meta`, secondary target
non-Meta): #65 (§1+§14 reading-order audience framing — reflected
in this file's overall positioning and in spec §1's reading
order) and #160 (§1+§14 — Apache 2.0 pinned in spec §1, rationale
here in §3). All Meta material has a home.

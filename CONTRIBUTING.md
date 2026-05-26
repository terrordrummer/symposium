# Contributing to Symposium

Thanks for your interest in Symposium — the structured, sequential,
adversarial multi-agent deliberation protocol. This file covers the
reference Python implementation under `symposium/`. The protocol itself
is specified in [`docs/specification.md`](docs/specification.md) (frozen
at v1.0.0); the repository conventions live in
[`docs/repository-strategy.md`](docs/repository-strategy.md), which this
file operationalizes (§8).

## What's normative vs reference

- **Normative**: `docs/specification.md` §1–§9 and the JSON Schemas under
  `docs/schemas/v1.0.0/`. These are **frozen** at v1.0.0 — a contribution
  MUST NOT edit the spec body or the schemas. Forward-compatible changes
  publish under a new schema version (`docs/schemas/v1.1.0/` …) per §5.1.
- **Reference (open to change)**: everything under `symposium/`,
  `examples/`, and `tests/`. The Python package is *one* conformant
  implementation; a different runtime in another language is equally valid
  as long as it conforms to the spec and validates against the schemas.

## Coding standards

- **Language**: Python ≥ 3.11. Type hints throughout; `from __future__
  import annotations` at module top.
- **Models**: every persisted shape is a Pydantic model in
  `symposium/models.py` mirroring its `docs/schemas/v1.0.0/*.schema.json`
  counterpart field-for-field, `extra="forbid"`. If the model and the
  schema disagree, **the schema wins** — fix the model.
- **Determinism**: the orchestrator's digest-bearing logic
  (`transcript_digest`, §7.7) must stay reproducible. No clock / RNG / I/O
  in pure components (e.g. the `rules` selector). Non-deterministic
  sources (`Message.id`, timestamps) are pinned through
  `symposium.replay.pinned_runtime`.
- **No real network in tests**: provider adapters are exercised with the
  deterministic `FakeProvider` (and `respx` for the HTTP adapters). CI must
  never require `OPENAI_API_KEY` / `ANTHROPIC_API_KEY`.
- **Docstrings**: explain the spec section a module implements and record
  any "open clarification" where the spec is silent — mirror the existing
  modules (`scheduler/loop.py`, `replay/execution.py`, `selector/`).

## Persona contribution guidelines

New built-in personas are introduced **offline, intentionally, and through
review** — never autonomously at runtime (spec §3, Pass-1 row #125). The
recommended workflow:

```
session analysis → capability gap identified (selector missing_capabilities
/ postmortem capability_gaps) → persona proposal → human review →
persona spec (validates against persona.schema.json) → added to
symposium/personas/
```

A new persona MUST validate against `docs/schemas/v1.0.0/persona.schema.json`
and respect the horizontal/domain class constraints enforced in
`models.Persona`. The persona-registry surface (versioning, lifecycle,
signing, marketplace) is spec §12 Roadmap, not part of this repository.

## Plugin / adapter requirements

- **Provider adapters** implement the `symposium.providers.base.ProviderAdapter`
  contract (spec §6.1): `invoke(ProviderRequest) -> ProviderResult`,
  validating `structured_output` against the request's
  `expected_output_schema` (§6.5) and reporting failures via the closed
  `error.kind` enum (§6.6). Register a factory with `AdapterRegistry`
  (§6.11). See `providers/openai.py` / `providers/anthropic.py` as
  references.
- **Other plugin categories** (schedulers, replay tools, personas,
  visualization) are spec §12 Roadmap; the MVP plugin contract is the
  ProviderAdapter only.

## Branch / PR / review process

1. **Branch** off `main`. Use a descriptive prefix: `impl/…` for runtime
   features, `chore/…` for tooling/release, `fix/…` for bug fixes.
2. **Tests**: add tests under `tests/` for any behaviour change. Keep the
   existing suite green.
3. **Run the gates locally** before opening a PR:
   ```bash
   pip install -e ".[test]"
   pytest -q
   python3 docs/schemas/v1.0.0/examples/validate.py          # 28 positive
   python3 docs/schemas/v1.0.0/examples/validate_negative.py  # 36 negative
   ```
4. **Open a PR** against `main`. CI runs two required checks —
   `schemas (positive + negative)` and `python reference impl (pytest)` —
   and both MUST pass before merge.
5. **Spec ↔ code coupling**: if a change touches a behaviour the spec
   pins, cite the section (`§4.x`) in the PR description. Do not edit the
   frozen spec/schemas to fit the code.

## Reporting issues

Use the GitHub issue tracker for bugs, errata, and design discussion.
Spec ambiguities are best raised as issues citing the section, so the
disposition can be tracked against §12 Roadmap where appropriate.

## License

By contributing you agree your contributions are licensed under the
project's [Apache 2.0](LICENSE) license.

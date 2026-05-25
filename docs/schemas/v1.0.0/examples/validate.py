#!/usr/bin/env python3
"""Mechanical validation of Pass 4/5/6/7 schemas + examples.

Validates each example JSON file against its corresponding schema using
the JSON Schema Draft 2020-12 validator from the `jsonschema` library.

Cross-schema $ref resolution is handled via a Registry built from all
schema files in docs/schemas/v1.0.0/.

Pass 6 additions:
- run_manifest.schema.json validation
- budget_breach_artifact fixture
- Semantic validation: for each Artifact, verify (a) the
  canonical_transcript usage sums match Artifact.cumulative_usage,
  and (b) the JCS-canonical SHA-256 digest of the canonical_transcript
  matches Artifact.transcript_digest (§7.7).

Pass 7 additions:
- fake_provider_script.schema.json validation
- golden_test_case.schema.json validation
- Semantic validation extended to the GoldenTestCase's
  expected_artifact (§9.9 roundtrip property).
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

from jsonschema import Draft202012Validator
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012


_HERE = Path(__file__).resolve().parent
SCHEMA_DIR = _HERE.parent
EXAMPLE_DIR = _HERE

# (schema_filename, example_filename) pairs.
CASES = [
    ("persona.schema.json", "persona_horizontal.json"),
    ("persona.schema.json", "persona_domain.json"),
    ("direct_request.schema.json", "direct_request.json"),
    ("verdict.schema.json", "verdict_continue.json"),
    ("verdict.schema.json", "verdict_finalize.json"),
    ("verdict.schema.json", "verdict_request_user_input.json"),
    ("provider_result.schema.json", "provider_result.json"),
    ("provider_result.schema.json", "provider_result_openai_example.json"),
    ("provider_result.schema.json", "provider_result_anthropic_example.json"),
    ("provider_result.schema.json", "provider_result_fake_example.json"),
    ("provider_result.schema.json", "provider_result_malformed_example.json"),
    ("provider_result.schema.json", "provider_result_invalid_request_example.json"),
    ("provider_request.schema.json", "provider_request.json"),
    ("artifact.schema.json", "worked_example_artifact.json"),
    ("artifact.schema.json", "budget_breach_artifact.json"),
    ("run_manifest.schema.json", "run_manifest_complete.json"),
    ("run_manifest.schema.json", "run_manifest_terminated.json"),
    ("run_manifest.schema.json", "run_manifest_in_progress.json"),
    # Pass 7 additions:
    ("fake_provider_script.schema.json", "fake_provider_script_example.json"),
    ("golden_test_case.schema.json", "golden_test_case_example.json"),
]

# §6.6 prose declares the closed enum. This list mirrors it; the
# parity check below asserts the schema's enum matches it exactly.
# Prevents prose/schema drift (Codex turn-2 regression note).
ERROR_KIND_PROSE_ENUM = [
    "timeout",
    "network",
    "rate_limit",
    "quota_exhausted",
    "auth_failure",
    "model_unavailable",
    "context_length_exceeded",
    "content_filter",
    "invalid_request",
    "malformed_response",
    "tool_failure",
    "internal",
]

# Nested structured_output validation (§6.5): each tuple is
#   (provider_result_example_filename, target_schema_filename, json_pointer_into_target_schema_or_None)
# The structured_output is extracted from the file and validated against
# the target schema (or sub-schema at the pointer).
NESTED_STRUCTURED_OUTPUT_CASES = [
    # OpenAI example: panel/branch turn → turn_structured_output
    ("provider_result_openai_example.json", "turn_structured_output.schema.json", None),
    # Anthropic example: coordination_turn → verdict
    ("provider_result_anthropic_example.json", "verdict.schema.json", None),
    # FakeProvider example: panel turn → turn_structured_output
    ("provider_result_fake_example.json", "turn_structured_output.schema.json", None),
    # Original Pass-4 provider_result example: panel turn → turn_structured_output
    ("provider_result.json", "turn_structured_output.schema.json", None),
]


def load_registry() -> Registry:
    """Load every schema file into a Registry keyed by both $id and a
    relative URI matching how schemas use $ref (e.g. `message.schema.json`)."""
    resources = []
    for schema_path in sorted(SCHEMA_DIR.glob("*.schema.json")):
        with open(schema_path) as f:
            schema = json.load(f)
        resource = Resource(contents=schema, specification=DRAFT202012)
        resources.append((schema["$id"], resource))
        # Also register under the relative filename so refs like
        # `message.schema.json` resolve when looked up from a sibling.
        resources.append((schema_path.name, resource))
    return Registry().with_resources(resources)


def validate_case(registry: Registry, schema_filename: str, example_filename: str) -> tuple[bool, list[str]]:
    schema_path = SCHEMA_DIR / schema_filename
    example_path = EXAMPLE_DIR / example_filename
    with open(schema_path) as f:
        schema = json.load(f)
    with open(example_path) as f:
        example = json.load(f)
    validator = Draft202012Validator(schema, registry=registry)
    errors = sorted(validator.iter_errors(example), key=lambda e: e.path)
    if errors:
        msgs = []
        for err in errors:
            path = "/".join(str(p) for p in err.absolute_path) or "<root>"
            msgs.append(f"  - at {path}: {err.message}")
        return False, msgs
    return True, []


def validate_nested_structured_output(registry: Registry, example_filename: str, target_schema_filename: str) -> tuple[bool, list[str]]:
    """Validate the `structured_output` field of a ProviderResult example
    against the target turn-shape schema (§6.5)."""
    example_path = EXAMPLE_DIR / example_filename
    with open(example_path) as f:
        example = json.load(f)
    structured_output = example.get("structured_output")
    if structured_output is None:
        # malformed_response example deliberately has null structured_output
        return True, [f"  (skipped — structured_output is null, expected for {example_filename})"]
    target_schema_path = SCHEMA_DIR / target_schema_filename
    with open(target_schema_path) as f:
        target_schema = json.load(f)
    validator = Draft202012Validator(target_schema, registry=registry)
    errors = sorted(validator.iter_errors(structured_output), key=lambda e: e.path)
    if errors:
        msgs = []
        for err in errors:
            path = "/".join(str(p) for p in err.absolute_path) or "<root>"
            msgs.append(f"  - at {path}: {err.message}")
        return False, msgs
    return True, []


def jcs_canonical_bytes(obj) -> bytes:
    """RFC 8785 JCS canonical serialization. Uses the `rfc8785` Python
    package which implements the ECMA-262 number-to-string rules
    required by JCS §3.2.2.2 (e.g. 0.0 → "0", 1.0 → "1"). Pass 6
    turn-3 switched from a `json.dumps`-based approximation to true
    JCS after Codex flagged digest divergence."""
    import rfc8785
    return rfc8785.dumps(obj)


def validate_artifact_semantics(example_filename: str) -> tuple[bool, list[str]]:
    """For an Artifact fixture, verify (a) usage sums match and (b) the
    JCS-canonical SHA-256 digest of canonical_transcript matches
    Artifact.transcript_digest (§7.7).
    """
    example_path = EXAMPLE_DIR / example_filename
    with open(example_path) as f:
        artifact = json.load(f)
    msgs = []
    ok = True
    transcript = artifact.get("canonical_transcript") or []
    p, c, t, cost = 0, 0, 0, 0.0
    for m in transcript:
        u = m.get("usage", {})
        p += u.get("prompt_tokens", 0)
        c += u.get("completion_tokens", 0)
        t += u.get("total_tokens", 0)
        cost += u.get("cost_usd", 0)
    cost = round(cost, 4)
    cu = artifact.get("cumulative_usage", {})
    expected_p = cu.get("prompt_tokens")
    expected_c = cu.get("completion_tokens")
    expected_t = cu.get("total_tokens")
    expected_cost = round(cu.get("cost_usd", 0), 4)
    if (p, c, t, cost) != (expected_p, expected_c, expected_t, expected_cost):
        ok = False
        msgs.append(
            f"  - usage sum mismatch: sum=({p},{c},{t},{cost}) vs cumulative=({expected_p},{expected_c},{expected_t},{expected_cost})"
        )
    # If termination, the inline termination_artifact's cumulative_usage MUST also match.
    outcome = artifact.get("outcome", {})
    if outcome.get("kind") == "termination":
        ta = outcome.get("termination_artifact", {})
        ta_cu = ta.get("cumulative_usage", {})
        ta_tuple = (
            ta_cu.get("prompt_tokens"),
            ta_cu.get("completion_tokens"),
            ta_cu.get("total_tokens"),
            round(ta_cu.get("cost_usd", 0), 4),
        )
        if ta_tuple != (expected_p, expected_c, expected_t, expected_cost):
            ok = False
            msgs.append(
                f"  - termination_artifact.cumulative_usage mismatch: ta={ta_tuple} vs artifact={(expected_p, expected_c, expected_t, expected_cost)}"
            )
    # Digest check (RFC 8785 JCS + SHA-256, §7.7)
    canonical_bytes = jcs_canonical_bytes(transcript)
    digest = hashlib.sha256(canonical_bytes).hexdigest()
    stored = artifact.get("transcript_digest")
    if stored != digest:
        ok = False
        msgs.append(
            f"  - transcript_digest mismatch: stored={stored} computed={digest}"
        )
    if outcome.get("kind") == "termination":
        ta_digest = outcome.get("termination_artifact", {}).get("transcript_digest")
        if ta_digest != digest:
            ok = False
            msgs.append(
                f"  - termination_artifact.transcript_digest mismatch: stored={ta_digest} computed={digest}"
            )
    return ok, msgs


SEMANTIC_ARTIFACT_CASES = [
    "worked_example_artifact.json",
    "budget_breach_artifact.json",
]

# Pass 7: GoldenTestCase fixtures whose `expected_artifact` MUST pass
# the §7.7 semantic check (§9.9 roundtrip property).
SEMANTIC_GOLDEN_CASES = [
    "golden_test_case_example.json",
]


def validate_golden_case_semantics(example_filename: str) -> tuple[bool, list[str]]:
    """For a GoldenTestCase fixture, verify the embedded `expected_artifact`
    satisfies the §7.7 semantic check (digest equality + cumulative_usage parity).
    Reuses the artifact-level checker by extracting the sub-object.
    """
    example_path = EXAMPLE_DIR / example_filename
    with open(example_path) as f:
        case = json.load(f)
    artifact = case.get("expected_artifact")
    if artifact is None:
        return False, ["  - GoldenTestCase missing `expected_artifact`"]
    # Run the same checks the per-Artifact validator runs, but on the
    # embedded artifact directly.
    msgs = []
    ok = True
    transcript = artifact.get("canonical_transcript") or []
    p, c, t, cost = 0, 0, 0, 0.0
    for m in transcript:
        u = m.get("usage", {})
        p += u.get("prompt_tokens", 0)
        c += u.get("completion_tokens", 0)
        t += u.get("total_tokens", 0)
        cost += u.get("cost_usd", 0)
    cost = round(cost, 4)
    cu = artifact.get("cumulative_usage", {})
    expected_p = cu.get("prompt_tokens")
    expected_c = cu.get("completion_tokens")
    expected_t = cu.get("total_tokens")
    expected_cost = round(cu.get("cost_usd", 0), 4)
    if (p, c, t, cost) != (expected_p, expected_c, expected_t, expected_cost):
        ok = False
        msgs.append(
            f"  - expected_artifact usage sum mismatch: sum=({p},{c},{t},{cost}) vs cumulative=({expected_p},{expected_c},{expected_t},{expected_cost})"
        )
    canonical_bytes = jcs_canonical_bytes(transcript)
    digest = hashlib.sha256(canonical_bytes).hexdigest()
    stored = artifact.get("transcript_digest")
    if stored != digest:
        ok = False
        msgs.append(
            f"  - expected_artifact transcript_digest mismatch: stored={stored} computed={digest}"
        )
    return ok, msgs


def check_error_kind_enum_parity() -> tuple[bool, list[str]]:
    """§6.6 prose enumerates 12 error.kind values; the schema's CLOSED
    enum MUST match exactly. Prevents Codex turn-2 regression where the
    schema only had 11 values while prose said 12."""
    schema_path = SCHEMA_DIR / "provider_result.schema.json"
    with open(schema_path) as f:
        schema = json.load(f)
    schema_enum = schema["properties"]["error"]["oneOf"][1]["properties"]["kind"]["enum"]
    if list(schema_enum) != list(ERROR_KIND_PROSE_ENUM):
        msgs = [
            f"  - schema enum: {schema_enum}",
            f"  - prose enum:  {ERROR_KIND_PROSE_ENUM}",
            f"  - schema_only: {sorted(set(schema_enum) - set(ERROR_KIND_PROSE_ENUM))}",
            f"  - prose_only:  {sorted(set(ERROR_KIND_PROSE_ENUM) - set(schema_enum))}",
        ]
        return False, msgs
    return True, []


def main() -> int:
    registry = load_registry()
    failed = 0
    print("# Pass 4/5 mechanical schema validation\n")
    print("Validator: jsonschema (Python), Draft 2020-12.\n")
    print(f"Schema dir: {SCHEMA_DIR}")
    print(f"Example dir: {EXAMPLE_DIR}\n")
    for schema_filename, example_filename in CASES:
        ok, msgs = validate_case(registry, schema_filename, example_filename)
        status = "OK " if ok else "FAIL"
        print(f"[{status}] {schema_filename} <- {example_filename}")
        for msg in msgs:
            print(msg)
        if not ok:
            failed += 1
    print(f"\nWrapper cases: {len(CASES)}; failed: {failed}\n")

    print("## Nested structured_output validation (§6.5)\n")
    nested_failed = 0
    for example_filename, target_schema_filename, _pointer in NESTED_STRUCTURED_OUTPUT_CASES:
        ok, msgs = validate_nested_structured_output(registry, example_filename, target_schema_filename)
        status = "OK " if ok else "FAIL"
        print(f"[{status}] {example_filename}.structured_output <- {target_schema_filename}")
        for msg in msgs:
            print(msg)
        if not ok:
            nested_failed += 1
    print(f"\nNested cases: {len(NESTED_STRUCTURED_OUTPUT_CASES)}; failed: {nested_failed}")

    print("\n## error.kind prose/schema parity (§6.6)\n")
    ok, msgs = check_error_kind_enum_parity()
    status = "OK " if ok else "FAIL"
    print(f"[{status}] §6.6 prose enum matches provider_result.schema.json error.kind enum")
    for msg in msgs:
        print(msg)
    parity_failed = 0 if ok else 1

    print("\n## Artifact semantic validation (§7.7 + cumulative_usage parity)\n")
    semantic_failed = 0
    for fname in SEMANTIC_ARTIFACT_CASES:
        ok, msgs = validate_artifact_semantics(fname)
        status = "OK " if ok else "FAIL"
        print(f"[{status}] {fname}")
        for msg in msgs:
            print(msg)
        if not ok:
            semantic_failed += 1
    print(f"\nSemantic cases: {len(SEMANTIC_ARTIFACT_CASES)}; failed: {semantic_failed}")

    print("\n## GoldenTestCase semantic validation (Pass 7 §9.9 roundtrip)\n")
    golden_failed = 0
    for fname in SEMANTIC_GOLDEN_CASES:
        ok, msgs = validate_golden_case_semantics(fname)
        status = "OK " if ok else "FAIL"
        print(f"[{status}] {fname} (expected_artifact)")
        for msg in msgs:
            print(msg)
        if not ok:
            golden_failed += 1
    print(f"\nGoldenTestCase semantic cases: {len(SEMANTIC_GOLDEN_CASES)}; failed: {golden_failed}")

    total_failed = failed + nested_failed + parity_failed + semantic_failed + golden_failed
    total_cases = (
        len(CASES)
        + len(NESTED_STRUCTURED_OUTPUT_CASES)
        + 1
        + len(SEMANTIC_ARTIFACT_CASES)
        + len(SEMANTIC_GOLDEN_CASES)
    )
    print(f"\nTOTAL: {total_cases} cases; failed: {total_failed}")
    return 1 if total_failed else 0


if __name__ == "__main__":
    sys.exit(main())

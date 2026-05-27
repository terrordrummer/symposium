"""Structural validation of the published JSON Schemas in docs/schemas/.

Catches the class of bug Codex flagged in review T3: a `$ref` pointer
inside `termination_artifact.schema.json` resolved to a path that
doesn't exist in the target schema (the target had a `oneOf`, not
direct `properties`). A jsonschema-compliant validator silently
ignored the failure on artifacts that didn't carry the offending
field, so the bug only surfaced under structural inspection.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

SCHEMAS_DIR = Path(__file__).resolve().parents[1] / "docs" / "schemas" / "v1.0.0"


def test_schemas_dir_exists():
    """Sanity: the schemas dir must exist and be non-empty."""
    assert SCHEMAS_DIR.is_dir()
    files = list(SCHEMAS_DIR.glob("*.schema.json"))
    assert files, f"no schema files under {SCHEMAS_DIR}"


def test_every_schema_is_valid_json_schema_draft_2020_12():
    """Each .schema.json file MUST be a valid Draft 2020-12 schema.

    `jsonschema.Draft202012Validator.check_schema(schema)` raises on
    structural problems (unknown keywords for the draft, malformed
    `type` arrays, etc.) — not on `$ref` resolution, which is the
    bug below.
    """
    from jsonschema import Draft202012Validator

    for path in SCHEMAS_DIR.glob("*.schema.json"):
        schema = json.loads(path.read_text())
        Draft202012Validator.check_schema(schema)


def test_every_internal_ref_resolves():
    """Every `$ref` to a sibling schema or fragment MUST resolve.

    Codex review T3 caught a broken pointer in
    `termination_artifact.schema.json` (referenced
    `provider_result.schema.json#/properties/error/properties/kind`,
    but `error` is a `oneOf` with no direct `properties` — the actual
    enum lives at `oneOf/1/properties/kind`). The validator returns
    the resolved subschema, so any unresolved pointer raises.
    """
    from referencing import Registry, Resource
    from referencing.jsonschema import DRAFT202012

    # Load every schema into a registry so cross-file $refs resolve.
    registry = Registry()
    for path in SCHEMAS_DIR.glob("*.schema.json"):
        schema = json.loads(path.read_text())
        sid = schema.get("$id") or path.name
        registry = registry.with_resource(
            uri=sid, resource=Resource(contents=schema, specification=DRAFT202012)
        )
        # ALSO register under bare filename so relative `$ref` like
        # `provider_result.schema.json#/...` (no full $id prefix) work.
        registry = registry.with_resource(
            uri=path.name, resource=Resource(contents=schema, specification=DRAFT202012)
        )

    def _walk(node, base_uri, where):
        if isinstance(node, dict):
            if "$ref" in node and isinstance(node["$ref"], str):
                ref = node["$ref"]
                try:
                    resolver = registry.resolver(base_uri=base_uri)
                    resolved = resolver.lookup(ref)
                    # Sanity: must produce a non-None contents dict.
                    assert resolved.contents is not None, (
                        f"$ref {ref!r} at {where} resolved to None"
                    )
                except Exception as exc:
                    pytest.fail(
                        f"unresolvable $ref {ref!r} at {where} (base={base_uri}): "
                        f"{type(exc).__name__}: {exc}"
                    )
            for k, v in node.items():
                _walk(v, base_uri, f"{where}.{k}")
        elif isinstance(node, list):
            for i, item in enumerate(node):
                _walk(item, base_uri, f"{where}[{i}]")

    for path in SCHEMAS_DIR.glob("*.schema.json"):
        schema = json.loads(path.read_text())
        base = schema.get("$id") or path.name
        _walk(schema, base, where=path.name)

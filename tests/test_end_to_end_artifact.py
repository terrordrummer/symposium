"""End-to-end: run the walking-skeleton sample, persist, replay, schema-validate."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator
from referencing import Registry, Resource

from symposium.providers import FakeProvider
from symposium.replay import replay_transcript
from symposium.scheduler import run_session


def _load_registry(schemas_dir: Path) -> Registry:
    resources = []
    for schema_path in schemas_dir.glob("*.json"):
        data = json.loads(schema_path.read_text())
        if "$id" not in data:
            continue
        resources.append((data["$id"], Resource.from_contents(data)))
        resources.append((schema_path.name, Resource.from_contents(data)))
    return Registry().with_resources(resources)


def test_walking_skeleton_produces_valid_artifact(tmp_path, repo_root, example_config, example_script):
    fp = FakeProvider(script=example_script)
    artifact = run_session(example_config, {"default": fp}, runs_root=str(tmp_path))

    run_dir = tmp_path / example_config.session_id
    assert (run_dir / "artifact.json").exists()
    assert (run_dir / "manifest.json").exists()
    assert (run_dir / "config.json").exists()
    assert (run_dir / "transcript.jsonl").exists()

    schemas_dir = repo_root / "docs" / "schemas" / "v1.0.0"
    registry = _load_registry(schemas_dir)

    artifact_schema = json.loads((schemas_dir / "artifact.schema.json").read_text())
    validator = Draft202012Validator(artifact_schema, registry=registry)
    artifact_json = json.loads((run_dir / "artifact.json").read_text())
    errors = list(validator.iter_errors(artifact_json))
    assert not errors, "artifact failed JSON Schema validation: " + "\n".join(
        f"{list(e.absolute_path)}: {e.message}" for e in errors
    )

    manifest_schema = json.loads((schemas_dir / "run_manifest.schema.json").read_text())
    validator = Draft202012Validator(manifest_schema, registry=registry)
    manifest_json = json.loads((run_dir / "manifest.json").read_text())
    errors = list(validator.iter_errors(manifest_json))
    assert not errors, "manifest failed JSON Schema validation: " + "\n".join(
        f"{list(e.absolute_path)}: {e.message}" for e in errors
    )

    config_schema = json.loads((schemas_dir / "config.schema.json").read_text())
    validator = Draft202012Validator(config_schema, registry=registry)
    config_json = json.loads((run_dir / "config.json").read_text())
    errors = list(validator.iter_errors(config_json))
    assert not errors, "config failed JSON Schema validation: " + "\n".join(
        f"{list(e.absolute_path)}: {e.message}" for e in errors
    )

    # transcript_digest cross-equality (artifact ↔ manifest)
    assert artifact_json["transcript_digest"] == manifest_json["transcript_digest"]


def test_transcript_replay_byte_identity(tmp_path, example_config, example_script):
    """§7.5 — replay re-emits with the same digest (byte-identical)."""
    fp = FakeProvider(script=example_script)
    run_session(example_config, {"default": fp}, runs_root=str(tmp_path))

    run_dir = tmp_path / example_config.session_id
    result = replay_transcript(run_dir)
    assert result.digest_matches, (
        f"transcript_replay digest mismatch: stored={result.artifact.transcript_digest} "
        f"recomputed={result.recomputed_digest}"
    )
    # message count is preserved
    assert len(result.re_emitted_messages) == len(result.artifact.canonical_transcript)


def test_session_with_invalid_id_rejected(example_script):
    """§7.1 session_id charset is enforced (^[A-Za-z0-9_-]{1,64}$)."""
    from symposium.models import (
        AgentConfig,
        BudgetConfig,
        Config,
        SelectorConfig,
    )
    from symposium.personas import COORDINATOR, LOGICIAN
    from symposium.providers import FakeProvider
    from symposium.scheduler import run_session

    bad_config = Config(
        schema_version="1.0.0",
        session_id="has spaces and ! chars",  # invalid charset
        originator="u",
        problem_statement="p",
        selector=SelectorConfig(
            strategy="fixed",
            default_deliberation_panel=["logician"],
            coordinator_agent="coordinator",
        ),
        agents=[
            AgentConfig(id="logician", persona_ref=LOGICIAN, provider="fake", model="m"),
        ],
        coordinator=AgentConfig(id="coordinator", persona_ref=COORDINATOR, provider="fake", model="m"),
        budget=BudgetConfig(
            max_total_tokens=100, max_total_cost_usd=1.0, max_rounds=1, max_wallclock_seconds=10
        ),
    )
    with pytest.raises(ValueError):
        run_session(bad_config, {"default": FakeProvider(script=example_script)}, runs_root="/tmp/x")

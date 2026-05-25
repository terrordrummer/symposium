"""`symposium` CLI (§11.2).

MVP subcommands:

  symposium run    --config CONFIG.yaml  --script SCRIPT.json  [--output runs/]  [problem.md]
      Runs a session against a FakeProvider driven by the given script.
      The `problem.md` positional overrides `config.problem_statement`.

  symposium replay RUN_DIR
      Re-renders the stored canonical_transcript and verifies the digest.

  symposium validate ARTIFACT.json
      Validates an artifact against the v1.0.0 JSON Schemas.

Real provider adapters (OpenAI-shaped, Anthropic-shaped) are not part
of the walking skeleton and the CLI does not yet wire them in. They
land in a follow-up milestone.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Optional

import click
import yaml

from symposium.models import Config, FakeProviderScript
from symposium.providers import FakeProvider
from symposium.replay import replay_transcript
from symposium.scheduler import run_session


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
@click.version_option(package_name="symposium")
def main() -> None:
    """Reference runtime for the Symposium 1.0 protocol."""


@main.command("run")
@click.option(
    "--config", "config_path",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    required=True,
    help="Session config (YAML or JSON).",
)
@click.option(
    "--script", "script_path",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    required=True,
    help="FakeProvider script (JSON).",
)
@click.option(
    "--output", "runs_root",
    type=click.Path(file_okay=False, path_type=Path),
    default=Path("runs"),
    show_default=True,
    help="Root directory for persisted run.",
)
@click.argument(
    "problem_path",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    required=False,
)
def run_cmd(config_path: Path, script_path: Path, runs_root: Path, problem_path: Optional[Path]) -> None:
    """Run a Symposium session with a FakeProvider."""
    config = _load_config(config_path, problem_path)
    script = _load_script(script_path)

    fp = FakeProvider(script=script)
    providers = {"default": fp}

    artifact = run_session(config, providers, runs_root=str(runs_root))
    run_dir = runs_root / config.session_id
    click.echo(f"session_id={config.session_id}")
    click.echo(f"outcome.kind={artifact.outcome.kind}")
    click.echo(f"transcript_digest={artifact.transcript_digest}")
    click.echo(f"persisted_to={run_dir}/")


@main.command("replay")
@click.argument("run_dir", type=click.Path(exists=True, file_okay=False, path_type=Path))
def replay_cmd(run_dir: Path) -> None:
    """Re-render a stored canonical_transcript and verify its digest (§7.5)."""
    result = replay_transcript(run_dir)
    status = "match" if result.digest_matches else "MISMATCH"
    click.echo(f"messages={len(result.re_emitted_messages)}")
    click.echo(f"stored_digest={result.artifact.transcript_digest}")
    click.echo(f"recomputed_digest={result.recomputed_digest}")
    click.echo(f"digest={status}")
    if not result.digest_matches:
        sys.exit(2)


@main.command("validate")
@click.argument("artifact_path", type=click.Path(exists=True, dir_okay=False, path_type=Path))
def validate_cmd(artifact_path: Path) -> None:
    """Validate an artifact.json against the v1.0.0 Artifact schema."""
    from jsonschema import Draft202012Validator
    from referencing import Registry, Resource
    schemas_dir = (
        Path(__file__).resolve().parents[2] / "docs" / "schemas" / "v1.0.0"
    )
    registry = _load_registry(schemas_dir)
    artifact_schema = json.loads((schemas_dir / "artifact.schema.json").read_text())
    validator = Draft202012Validator(artifact_schema, registry=registry)
    data = json.loads(artifact_path.read_text())
    errors = list(validator.iter_errors(data))
    if errors:
        for err in errors:
            click.echo(f"ERROR: {err.message} at {list(err.absolute_path)}", err=True)
        sys.exit(1)
    click.echo("VALID")


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _load_config(path: Path, problem_path: Optional[Path]) -> Config:
    raw = _read_yaml_or_json(path)
    if problem_path is not None:
        raw["problem_statement"] = problem_path.read_text().strip()
    # Resolve inline persona references in `agents[].persona_ref` if they
    # are strings referring to built-in personas.
    from symposium.personas import persona_by_id

    def _resolve(ac: dict) -> dict:
        ref = ac.get("persona_ref")
        if isinstance(ref, str):
            try:
                ac["persona_ref"] = persona_by_id(ref).model_dump(exclude_none=True)
            except KeyError:
                pass
        return ac

    for ac in raw.get("agents", []):
        _resolve(ac)
    if isinstance(raw.get("coordinator"), dict):
        _resolve(raw["coordinator"])
    return Config.model_validate(raw)


def _load_script(path: Path) -> FakeProviderScript:
    return FakeProviderScript.model_validate(_read_yaml_or_json(path))


def _read_yaml_or_json(path: Path):
    text = path.read_text()
    if path.suffix.lower() in (".yaml", ".yml"):
        return yaml.safe_load(text)
    return json.loads(text)


def _load_registry(schemas_dir: Path):
    from referencing import Registry, Resource

    resources = []
    for schema_path in schemas_dir.glob("*.json"):
        data = json.loads(schema_path.read_text())
        if "$id" not in data:
            continue
        # Register under both absolute $id and bare filename so cross-file
        # $refs (which use bare filenames) resolve.
        resources.append((data["$id"], Resource.from_contents(data)))
        resources.append((schema_path.name, Resource.from_contents(data)))
    return Registry().with_resources(resources)


if __name__ == "__main__":
    main()

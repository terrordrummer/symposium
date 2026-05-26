"""`symposium` CLI (§11.2).

MVP subcommands:

  symposium run --config CONFIG.yaml [--script SCRIPT.json]
                [--selector-script SCRIPT.json] [--output runs/] [problem.md]
      Runs a session. The runtime resolves `provider` strings on every
      agent (and on the coordinator) through the adapter registry
      (§6.11). The built-in registry ships `openai`; the `fake` adapter
      is registered ad-hoc when `--script` is given. The §4.1 selector
      runs first: `fixed` / `rules` make no provider call; `llm` makes one
      bounded call driven by `--selector-script` (a separate FakeProvider).
      Every run writes `<run_dir>/selector_output.json` (§5.11).

  symposium replay RUN_DIR
      Re-renders the stored canonical_transcript and verifies the
      transcript_digest.

  symposium validate ARTIFACT.json
      Validates an artifact against the v1.0.0 JSON Schemas.

  symposium metrics RUN_DIR
      Computes the §7.9 MVP observability metric set offline from
      `<RUN_DIR>/artifact.json` and writes `<RUN_DIR>/metrics.json`.

  symposium execution-replay RUN_DIR [--script SCRIPT.json] [--output runs/]
      Re-runs the orchestrator against the original problem_statement /
      Config under the §7.6 ten pinning conditions (distinct from
      `replay`, which is the §7.5 unconditional transcript re-render). On
      an unsatisfiable pinning condition it aborts with exit code 3; on a
      digest mismatch after a successful replay, exit code 4.

Environment variables consumed by built-in adapters:

  OPENAI_API_KEY  — required when any agent declares `provider: openai`.
                    The OpenAIProvider fails fast at construction
                    (§6.8) if the variable is missing.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Optional

import click
import yaml

from symposium.models import Artifact, Config, FakeProviderScript
from symposium.observability import (
    MetricsConsistencyError,
    compute_metrics,
    write_metrics,
)
from symposium.providers import (
    FakeProvider,
    MissingCredentialsError,
    UnknownProviderError,
    default_registry,
    make_fake_factory,
)
from symposium.replay import PinningViolation, execution_replay, replay_transcript
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
    required=False,
    help="FakeProvider script (JSON). Required when any agent declares `provider: fake`.",
)
@click.option(
    "--selector-script", "selector_script_path",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    required=False,
    help=(
        "FakeProvider script (JSON) driving the §4.1 `llm` selector invocation "
        "(mirrors --script). Used only when selector.strategy = llm; ignored "
        "for fixed / rules."
    ),
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
def run_cmd(
    config_path: Path,
    script_path: Optional[Path],
    selector_script_path: Optional[Path],
    runs_root: Path,
    problem_path: Optional[Path],
) -> None:
    """Run a Symposium session against the registered providers."""
    config = _load_config(config_path, problem_path)

    registry = default_registry()
    if script_path is not None:
        fp = FakeProvider(script=_load_script(script_path))
        registry.register("fake", make_fake_factory(fp))

    try:
        providers = registry.build_session_providers(config)
    except UnknownProviderError as exc:
        click.echo(
            f"ERROR: {exc} — either register a factory or set provider correctly in the config.",
            err=True,
        )
        sys.exit(2)
    except MissingCredentialsError as exc:
        click.echo(f"ERROR: {exc}", err=True)
        sys.exit(2)

    # §4.1 `llm` selector: a distinct FakeProvider drives the single selector
    # invocation so it never consumes deliberation-script entries.
    selector_providers = None
    if config.selector.strategy == "llm" and selector_script_path is not None:
        sel_fp = FakeProvider(script=_load_script(selector_script_path))
        selector_providers = {"default": sel_fp}

    artifact = run_session(
        config,
        providers,
        runs_root=str(runs_root),
        selector_providers=selector_providers,
    )
    run_dir = runs_root / config.session_id
    click.echo(f"session_id={config.session_id}")
    click.echo(f"selector_strategy={config.selector.strategy}")
    sel_path = run_dir / "selector_output.json"
    if sel_path.exists():
        selection = json.loads(sel_path.read_text())
        click.echo(f"selected_agents={selection['selected_agents']}")
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


@main.command("metrics")
@click.argument("run_dir", type=click.Path(exists=True, file_okay=False, path_type=Path))
@click.option(
    "--output", "output_path",
    type=click.Path(dir_okay=False, path_type=Path),
    default=None,
    help="Write metrics.json here instead of <run_dir>/metrics.json.",
)
@click.option(
    "--quiet", is_flag=True,
    help="Suppress the human-readable summary on stdout.",
)
def metrics_cmd(run_dir: Path, output_path: Optional[Path], quiet: bool) -> None:
    """Compute §7.9 MVP observability metrics from a persisted run directory."""
    artifact_path = run_dir / "artifact.json"
    if not artifact_path.exists():
        click.echo(f"ERROR: {artifact_path} not found", err=True)
        sys.exit(1)
    try:
        raw = json.loads(artifact_path.read_text())
        artifact = Artifact.model_validate(raw)
    except Exception as exc:  # noqa: BLE001 — surface any parse/validation failure as exit 1
        click.echo(f"ERROR: failed to load artifact: {exc}", err=True)
        sys.exit(1)

    try:
        metrics = compute_metrics(artifact)
    except MetricsConsistencyError as exc:
        click.echo(f"ERROR: metrics-consistency invariant failed: {exc}", err=True)
        sys.exit(2)

    if output_path is None:
        out_path = write_metrics(run_dir, metrics)
    else:
        # Inline mirror of write_metrics with a caller-chosen destination.
        from symposium.observability.metrics import _atomic_write_text

        payload = metrics.model_dump(mode="json", exclude_none=False)
        text = json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
        _atomic_write_text(output_path, text)
        out_path = output_path

    if not quiet:
        _print_metrics_summary(metrics, out_path)


def _print_metrics_summary(metrics, out_path: Path) -> None:
    click.echo(f"session_id={metrics.session_id}")
    click.echo(f"transcript_digest={metrics.transcript_digest}")
    if metrics.outcome_kind == "termination":
        click.echo(f"outcome=termination ({metrics.termination_reason})")
    else:
        click.echo("outcome=synthesis")
    tc = metrics.tokens_cumulative
    click.echo(
        f"tokens={tc.total_tokens} "
        f"(prompt={tc.prompt_tokens}, completion={tc.completion_tokens})"
    )
    click.echo(f"cost_usd={metrics.cost_cumulative.cost_usd}")

    top3 = sorted(
        metrics.tokens_per_agent.items(),
        key=lambda kv: kv[1].total_tokens,
        reverse=True,
    )[:3]
    if top3:
        click.echo("top_agents_by_tokens:")
        for agent, tb in top3:
            click.echo(f"  {agent}: {tb.total_tokens}")

    click.echo(f"branch_depth_max={metrics.branch_depth_max}")
    click.echo(f"deferred_queue_length_max={metrics.deferred_queue_length_max}")
    click.echo(
        f"panel_contraction_total={sum(p.count for p in metrics.panel_contraction_count)}"
    )
    click.echo(f"usage_estimated={metrics.usage_estimated}")
    click.echo(f"persisted_to={out_path}")


@main.command("execution-replay")
@click.argument("run_dir", type=click.Path(exists=True, file_okay=False, path_type=Path))
@click.option(
    "--script", "script_path",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    required=False,
    help="FakeProvider script (JSON). Required when the persisted config uses `provider: fake`.",
)
@click.option(
    "--output", "fresh_runs_root",
    type=click.Path(file_okay=False, path_type=Path),
    default=None,
    help="Root for the fresh `<session_id>-replay/` run dir (default: the run's parent).",
)
@click.option("--quiet", is_flag=True, help="Suppress the human-readable summary on stdout.")
@click.option(
    "--assume-cache-cleared", is_flag=True,
    help="Assert the §7.6 condition #6 prompt-cache assumption for live providers.",
)
def execution_replay_cmd(
    run_dir: Path,
    script_path: Optional[Path],
    fresh_runs_root: Optional[Path],
    quiet: bool,
    assume_cache_cleared: bool,
) -> None:
    """Re-run a persisted session under the §7.6 pinning conditions.

    The library API also accepts `fixed_clock` and `persona_hashes`; both
    are caller-side knobs not exposed on the command line in M5. With no
    `fixed_clock`, message ids and timestamps are replayed from the
    recorded transcript (§7.6 #8 fixed clock source), so a deterministic
    FakeProvider run reproduces its `transcript_digest`. Exit codes: 0
    match, 3 pinning violation, 4 digest mismatch, 1 any other error.
    """
    config_path = run_dir / "config.json"
    if not config_path.exists():
        click.echo(f"ERROR: {config_path} not found", err=True)
        sys.exit(1)

    try:
        config = Config.model_validate(json.loads(config_path.read_text()))
    except Exception as exc:  # noqa: BLE001 — surface parse/validation failure as exit 1
        click.echo(f"ERROR: failed to load config: {exc}", err=True)
        sys.exit(1)

    registry = default_registry()
    if script_path is not None:
        fp = FakeProvider(script=_load_script(script_path))
        registry.register("fake", make_fake_factory(fp))
    try:
        providers = registry.build_session_providers(config)
    except (UnknownProviderError, MissingCredentialsError) as exc:
        click.echo(
            f"ERROR: {exc} — pass --script for a fake session, or register/credential the adapter.",
            err=True,
        )
        sys.exit(1)

    try:
        result = execution_replay(
            run_dir,
            providers=providers,
            fresh_runs_root=fresh_runs_root,
            assume_cache_cleared=assume_cache_cleared,
        )
    except PinningViolation as exc:
        click.echo(f"ERROR: pinning_violation [{exc.condition}]: {exc}", err=True)
        sys.exit(3)
    except Exception as exc:  # noqa: BLE001 — missing/invalid persisted state → exit 1
        click.echo(f"ERROR: execution_replay failed: {exc}", err=True)
        sys.exit(1)

    if not quiet:
        for w in result.warnings:
            click.echo(f"WARNING: {w}", err=True)
        click.echo(f"conditions_checked={','.join(result.conditions_checked)}")
        click.echo(f"conditions_assumed={','.join(result.conditions_assumed)}")
        click.echo(f"original_digest={result.original_digest}")
        click.echo(f"fresh_digest={result.fresh_digest}")
        click.echo(f"fresh_run_dir={result.fresh_run_dir}/")

    if not result.digest_matches:
        diverge = result.first_diverging_message_id or "stored digest differs from recomputed (no message-level divergence)"
        click.echo(f"digest=MISMATCH (first_divergence={diverge})")
        sys.exit(4)
    click.echo("digest=match")


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _load_config(path: Path, problem_path: Optional[Path]) -> Config:
    raw = _read_yaml_or_json(path)
    if problem_path is not None:
        raw["problem_statement"] = problem_path.read_text().strip()
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
        resources.append((data["$id"], Resource.from_contents(data)))
        resources.append((schema_path.name, Resource.from_contents(data)))
    return Registry().with_resources(resources)


if __name__ == "__main__":
    main()

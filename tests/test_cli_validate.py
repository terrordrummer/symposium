"""`symposium validate` schema resolution.

The loader prefers packaged schemas (`symposium/schemas/v1.0.0/`, present
on wheel installs) and falls back to the repo's `docs/schemas/v1.0.0`
(editable installs / source checkouts). When neither exists the command
exits with a one-line error instead of a FileNotFoundError traceback.
"""

from __future__ import annotations

import json

from click.testing import CliRunner

from symposium.cli.main import _find_schemas_dir, main
from symposium.providers import FakeProvider
from symposium.scheduler import run_session


def test_find_schemas_dir_resolves_in_source_checkout():
    schemas_dir = _find_schemas_dir()
    assert schemas_dir is not None
    assert (schemas_dir / "artifact.schema.json").exists()


def test_validate_command_accepts_a_real_artifact(tmp_path, example_config, example_script):
    fp = FakeProvider(script=example_script)
    run_session(example_config, {"default": fp}, runs_root=str(tmp_path))
    artifact_path = tmp_path / example_config.session_id / "artifact.json"

    result = CliRunner().invoke(main, ["validate", str(artifact_path)])
    assert result.exit_code == 0, result.output
    assert "VALID" in result.output


def test_validate_command_reports_missing_schemas_cleanly(tmp_path, monkeypatch):
    """Neither packaged schemas nor a repo checkout (a bare wheel install
    without the parallel packaging change) → clean error, exit 1."""
    import importlib

    cli_main = importlib.import_module("symposium.cli.main")
    monkeypatch.setattr(cli_main, "_find_schemas_dir", lambda: None)
    artifact_path = tmp_path / "artifact.json"
    artifact_path.write_text(json.dumps({}))

    result = CliRunner().invoke(main, ["validate", str(artifact_path)])
    assert result.exit_code == 1
    combined = result.output
    try:
        combined += result.stderr
    except ValueError:
        pass  # stderr merged into output on this click version
    assert "JSON Schemas not found" in combined
    assert "Traceback" not in combined

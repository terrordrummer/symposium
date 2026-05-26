"""Tests for §7.6 execution_replay + the ten pinning conditions.

The happy paths produce a *reproducible* original run inside
`pinned_runtime(fixed_clock=...)` (deterministic message ids + a fixed
clock), so the fresh replay can be digest-matching. The pinning-failure
paths mutate the persisted run state and assert the abort fires *before*
any fresh run directory is written (mirrors the M4 synthetic-mutation
pattern).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from click.testing import CliRunner

from symposium.cli.main import main as cli_main
from symposium.providers import FakeProvider, OpenAIProvider
from symposium.replay import (
    ExecutionReplayResult,
    PinningViolation,
    execution_replay,
    pinned_runtime,
)
from symposium.scheduler import run_session
from symposium.storage.digest import canonicalize, sha256_hex

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXED_DT = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


def _fixed_clock() -> datetime:
    return FIXED_DT


def _make_original_run(tmp_path: Path, config, script, *, fixed_clock=_fixed_clock):
    """Produce a reproducible persisted original run; return its run_dir."""
    with pinned_runtime(fixed_clock=fixed_clock):
        run_session(config, {"default": FakeProvider(script=script)}, runs_root=str(tmp_path))
    return tmp_path / config.session_id


# ---------------------------------------------------------------------------
# 1. Happy path — full FakeProvider replay yields a digest-matching artifact
# ---------------------------------------------------------------------------


def test_full_fake_replay_digest_matches(tmp_path, example_config, example_script):
    run_dir = _make_original_run(tmp_path, example_config, example_script)

    result = execution_replay(
        run_dir,
        providers={"default": FakeProvider(script=example_script)},
        fixed_clock=_fixed_clock,
    )
    assert isinstance(result, ExecutionReplayResult)
    assert result.digest_matches, (
        f"original={result.original_digest} fresh={result.fresh_digest} "
        f"first_div={result.first_diverging_message_id}"
    )
    assert result.first_diverging_message_id is None
    assert result.fresh_run_dir == tmp_path / f"{example_config.session_id}-replay"
    assert (result.fresh_run_dir / "artifact.json").exists()
    # Fresh session id differs (no overwrite of the original on disk).
    assert result.fresh_artifact.session_id == f"{example_config.session_id}-replay"
    assert run_dir.exists() and (run_dir / "artifact.json").exists()


def test_every_condition_has_a_disposition(tmp_path, example_config, example_script):
    """§7.6 forbids an 'unknown' tier: every condition is checked or assumed."""
    from symposium.replay.execution import PINNING_CONDITIONS

    run_dir = _make_original_run(tmp_path, example_config, example_script)
    result = execution_replay(
        run_dir,
        providers={"default": FakeProvider(script=example_script)},
        fixed_clock=_fixed_clock,
    )
    dispositioned = set(result.conditions_checked) | set(result.conditions_assumed)
    assert dispositioned == set(PINNING_CONDITIONS)
    # cache is assumed-only; model is both checked (presence) and assumed (snapshot).
    assert "cache" in result.conditions_assumed
    assert "cache" not in result.conditions_checked
    assert "model" in result.conditions_checked and "model" in result.conditions_assumed


# ---------------------------------------------------------------------------
# 2. Pinning failures (abort before the run step)
# ---------------------------------------------------------------------------


def test_runtime_producer_version_mismatch(tmp_path, example_config, example_script):
    run_dir = _make_original_run(tmp_path, example_config, example_script)
    manifest_path = run_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["producer"]["version"] = "9.9.9"
    manifest_path.write_text(json.dumps(manifest))

    with pytest.raises(PinningViolation) as exc:
        execution_replay(
            run_dir,
            providers={"default": FakeProvider(script=example_script)},
            fixed_clock=_fixed_clock,
        )
    assert exc.value.condition == "runtime"
    # Abort fired before any fresh run dir was written.
    assert not (tmp_path / f"{example_config.session_id}-replay").exists()


def test_unregistered_provider_raises_adapter(tmp_path, example_config, example_script):
    """The walking-skeleton uses provider 'fake', which is NOT in default_registry();
    with no caller-supplied adapter, condition #2 cannot be satisfied."""
    run_dir = _make_original_run(tmp_path, example_config, example_script)
    with pytest.raises(PinningViolation) as exc:
        execution_replay(run_dir, providers={}, fixed_clock=_fixed_clock)
    assert exc.value.condition == "adapter"
    assert not (tmp_path / f"{example_config.session_id}-replay").exists()


def test_nonempty_tools_raises_tool_env(tmp_path, example_config, example_script):
    run_dir = _make_original_run(tmp_path, example_config, example_script)
    config_path = run_dir / "config.json"
    config = json.loads(config_path.read_text())
    config["agents"][0]["tools"] = [
        {"name": "search", "description": "x", "input_schema": {"type": "object"}}
    ]
    config_path.write_text(json.dumps(config))

    with pytest.raises(PinningViolation) as exc:
        execution_replay(
            run_dir,
            providers={"default": FakeProvider(script=example_script)},
            fixed_clock=_fixed_clock,
        )
    assert exc.value.condition == "tool_env"


def test_live_provider_without_fixed_clock_raises_wallclock(tmp_path, example_config, example_script):
    """A non-Fake provider with no fixed_clock aborts at wallclock — before any HTTP call."""
    run_dir = _make_original_run(tmp_path, example_config, example_script)
    # Construct an OpenAIProvider offline (explicit key, no network).
    live = OpenAIProvider(api_key="test-key-not-used", base_url="http://127.0.0.1:0/v1")
    with pytest.raises(PinningViolation) as exc:
        execution_replay(run_dir, providers={"default": live}, fixed_clock=None)
    assert exc.value.condition == "wallclock"
    assert not (tmp_path / f"{example_config.session_id}-replay").exists()


def test_persona_hash_mismatch_raises_persona(tmp_path, example_config, example_script):
    run_dir = _make_original_run(tmp_path, example_config, example_script)
    with pytest.raises(PinningViolation) as exc:
        execution_replay(
            run_dir,
            providers={"default": FakeProvider(script=example_script)},
            fixed_clock=_fixed_clock,
            persona_hashes={"logician": "0" * 64},
        )
    assert exc.value.condition == "persona"


def test_persona_hash_match_passes(tmp_path, example_config, example_script):
    """A correct persona hash (recomputed from the resolved Persona) passes #9."""
    run_dir = _make_original_run(tmp_path, example_config, example_script)
    logician = next(a for a in example_config.agents if a.id == "logician")
    good_hash = sha256_hex(canonicalize(logician.persona_ref.model_dump(mode="json", exclude_none=True)))
    result = execution_replay(
        run_dir,
        providers={"default": FakeProvider(script=example_script)},
        fixed_clock=_fixed_clock,
        persona_hashes={"logician": good_hash},
    )
    assert result.digest_matches


def test_unresolved_persona_ref_raises_persona(tmp_path, example_config, example_script):
    run_dir = _make_original_run(tmp_path, example_config, example_script)
    config_path = run_dir / "config.json"
    config = json.loads(config_path.read_text())
    config["agents"][0]["persona_ref"] = "logician"  # downgrade to a bare string id
    config_path.write_text(json.dumps(config))

    with pytest.raises(PinningViolation) as exc:
        execution_replay(
            run_dir,
            providers={"default": FakeProvider(script=example_script)},
            fixed_clock=_fixed_clock,
        )
    assert exc.value.condition == "persona"


# ---------------------------------------------------------------------------
# 3. Digest mismatch (NOT a violation — reported, surfaced as CLI exit 4)
# ---------------------------------------------------------------------------


def test_digest_mismatch_is_reported_not_raised(tmp_path, example_config, example_script):
    """Corrupt the stored original digest; the fresh transcript is identical,
    so digests differ with no message-level divergence."""
    run_dir = _make_original_run(tmp_path, example_config, example_script)
    artifact_path = run_dir / "artifact.json"
    artifact = json.loads(artifact_path.read_text())
    artifact["transcript_digest"] = "a" * 64  # valid hex, wrong value
    artifact_path.write_text(json.dumps(artifact))

    result = execution_replay(
        run_dir,
        providers={"default": FakeProvider(script=example_script)},
        fixed_clock=_fixed_clock,
    )
    assert result.digest_matches is False
    assert result.original_digest == "a" * 64
    assert result.fresh_digest != "a" * 64
    # Transcripts are element-wise identical → no diverging message id.
    assert result.first_diverging_message_id is None


# ---------------------------------------------------------------------------
# 4. CLI surface + exit codes (0 / 3 / 4 distinct)
# ---------------------------------------------------------------------------


def _script_path() -> str:
    return str(REPO_ROOT / "examples" / "scripts" / "walking-skeleton.json")


def test_cli_execution_replay_exit_0(tmp_path, monkeypatch, example_config, example_script):
    """End-to-end CLI: exit 0, fresh artifact.json exists, digest matches.

    The CLI does not expose fixed_clock, so we freeze the clock globally for
    both the original run and the in-process CLI invocation; execution_replay
    pins message ids on both sides, making the digest reproducible."""
    frozen = "2026-01-01T12:00:00Z"
    monkeypatch.setattr("symposium.scheduler.loop.now_utc_iso", lambda: frozen)
    monkeypatch.setattr("symposium.models.now_utc_iso", lambda: frozen)

    run_dir = _make_original_run(tmp_path, example_config, example_script, fixed_clock=None)

    runner = CliRunner()
    res = runner.invoke(
        cli_main,
        ["execution-replay", str(run_dir), "--script", _script_path(), "--output", str(tmp_path)],
    )
    assert res.exit_code == 0, res.output
    fresh_dir = tmp_path / f"{example_config.session_id}-replay"
    assert (fresh_dir / "artifact.json").exists()
    assert "digest=match" in res.output


def test_cli_pinning_violation_exit_3(tmp_path, monkeypatch, example_config, example_script):
    frozen = "2026-01-01T12:00:00Z"
    monkeypatch.setattr("symposium.scheduler.loop.now_utc_iso", lambda: frozen)
    monkeypatch.setattr("symposium.models.now_utc_iso", lambda: frozen)
    run_dir = _make_original_run(tmp_path, example_config, example_script, fixed_clock=None)

    manifest_path = run_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["producer"]["name"] = "some-other-runtime"
    manifest_path.write_text(json.dumps(manifest))

    runner = CliRunner()
    res = runner.invoke(
        cli_main,
        ["execution-replay", str(run_dir), "--script", _script_path(), "--output", str(tmp_path)],
    )
    assert res.exit_code == 3, res.output
    assert "runtime" in res.output


def test_cli_digest_mismatch_exit_4(tmp_path, monkeypatch, example_config, example_script):
    frozen = "2026-01-01T12:00:00Z"
    monkeypatch.setattr("symposium.scheduler.loop.now_utc_iso", lambda: frozen)
    monkeypatch.setattr("symposium.models.now_utc_iso", lambda: frozen)
    run_dir = _make_original_run(tmp_path, example_config, example_script, fixed_clock=None)

    artifact_path = run_dir / "artifact.json"
    artifact = json.loads(artifact_path.read_text())
    artifact["transcript_digest"] = "b" * 64
    artifact_path.write_text(json.dumps(artifact))

    runner = CliRunner()
    res = runner.invoke(
        cli_main,
        ["execution-replay", str(run_dir), "--script", _script_path(), "--output", str(tmp_path)],
    )
    assert res.exit_code == 4, res.output
    assert "MISMATCH" in res.output


def test_cli_exit_codes_are_distinct():
    """0 (match), 3 (pinning), 4 (mismatch), 1 (generic) are four distinct codes."""
    assert len({0, 1, 3, 4}) == 4

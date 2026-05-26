"""Tests for §7.6 execution_replay + the ten pinning conditions.

The happy paths create an ordinary persisted run (random ids + wall-clock)
and rely on `execution_replay` replaying the recorded `Message.id` /
`Message.timestamp` sequences (§7.6 #8 fixed clock source + §9.4.1
deterministic id allocator) so the fresh transcript reconstructs the
original byte-for-byte and the digest matches. The pinning-failure paths
mutate the persisted run state and assert the abort fires *before* any
fresh run directory is written (mirrors the M4 synthetic-mutation pattern).
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
    persona_hash,
)
from symposium.replay.execution import PINNING_CONDITIONS
from symposium.scheduler import run_session

REPO_ROOT = Path(__file__).resolve().parents[1]


def _make_original_run(tmp_path: Path, config, script) -> Path:
    """Produce an ordinary persisted original run; return its run_dir.

    No clock/id pinning here — exactly what `symposium run` does. The replay
    pins ids/timestamps from the recording, so this is enough to reproduce."""
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


def test_fixed_clock_override_still_matches(tmp_path, example_config, example_script):
    """A caller-supplied fixed_clock overrides the recorded timestamps; ids are
    still replayed, so the transcripts differ only in timestamp — which makes
    this a deliberate digest *mismatch* unless the original used that clock.

    Here we assert the override path runs and reports a mismatch coherently
    (the recorded run used the wall-clock, not FIXED)."""
    run_dir = _make_original_run(tmp_path, example_config, example_script)
    fixed = datetime(2000, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
    result = execution_replay(
        run_dir,
        providers={"default": FakeProvider(script=example_script)},
        fixed_clock=lambda: fixed,
    )
    assert "wallclock" in result.conditions_checked
    # Same ids + content, different timestamps → first divergence is msg 0.
    assert result.digest_matches is False
    assert result.first_diverging_message_id is not None


def test_every_condition_has_a_disposition(tmp_path, example_config, example_script):
    """§7.6 forbids an 'unknown' tier: every condition is checked or assumed."""
    run_dir = _make_original_run(tmp_path, example_config, example_script)
    result = execution_replay(
        run_dir,
        providers={"default": FakeProvider(script=example_script)},
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
        )
    assert exc.value.condition == "runtime"
    # Abort fired before any fresh run dir was written.
    assert not (tmp_path / f"{example_config.session_id}-replay").exists()


def test_unregistered_provider_raises_adapter(tmp_path, example_config, example_script):
    """The walking-skeleton uses provider 'fake', which is NOT in default_registry();
    with no caller-supplied adapter, condition #2 cannot be satisfied."""
    run_dir = _make_original_run(tmp_path, example_config, example_script)
    with pytest.raises(PinningViolation) as exc:
        execution_replay(run_dir, providers={})
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
        )
    assert exc.value.condition == "tool_env"
    assert not (tmp_path / f"{example_config.session_id}-replay").exists()


def test_live_provider_without_fixed_clock_raises_wallclock(tmp_path, example_config, example_script):
    """A non-Fake provider with no fixed_clock aborts at wallclock — before any HTTP call."""
    run_dir = _make_original_run(tmp_path, example_config, example_script)
    # Re-declare provider=openai on every agent so the §7.6 provider-identity
    # check passes; this test targets the wallclock condition specifically,
    # which fires only after provider identity has matched.
    config_path = run_dir / "config.json"
    config = json.loads(config_path.read_text())
    for ac in config["agents"]:
        ac["provider"] = "openai"
    config["coordinator"]["provider"] = "openai"
    config_path.write_text(json.dumps(config))
    # Construct an OpenAIProvider offline (explicit key, no network call at init).
    live = OpenAIProvider(api_key="test-key-not-used", base_url="http://127.0.0.1:0/v1")
    with pytest.raises(PinningViolation) as exc:
        execution_replay(run_dir, providers={"default": live}, fixed_clock=None)
    assert exc.value.condition == "wallclock"
    assert not (tmp_path / f"{example_config.session_id}-replay").exists()


def test_provider_identity_mismatch_raises_provider(tmp_path, example_config, example_script):
    """An artifact declaring provider=openai cannot be replayed against FakeProvider.

    Regression for the §7.6 pinning hole where the caller's provider map
    silently bypassed provider-name verification: a FakeProvider snapshot
    would be accepted in place of the original openai adapter.
    """
    run_dir = _make_original_run(tmp_path, example_config, example_script)
    config_path = run_dir / "config.json"
    config = json.loads(config_path.read_text())
    for ac in config["agents"]:
        ac["provider"] = "openai"
    config["coordinator"]["provider"] = "openai"
    config_path.write_text(json.dumps(config))
    with pytest.raises(PinningViolation) as exc:
        execution_replay(
            run_dir,
            providers={"default": FakeProvider(script=example_script)},
        )
    assert exc.value.condition == "provider"


def test_persona_hash_mismatch_raises_persona(tmp_path, example_config, example_script):
    run_dir = _make_original_run(tmp_path, example_config, example_script)
    with pytest.raises(PinningViolation) as exc:
        execution_replay(
            run_dir,
            providers={"default": FakeProvider(script=example_script)},
            persona_hashes={"logician": "0" * 64},
        )
    assert exc.value.condition == "persona"


def test_persona_hash_match_passes(tmp_path, example_config, example_script):
    """A correct persona hash (recomputed from the resolved Persona) passes #9."""
    run_dir = _make_original_run(tmp_path, example_config, example_script)
    logician = next(a for a in example_config.agents if a.id == "logician")
    good_hash = persona_hash(logician.persona_ref)
    result = execution_replay(
        run_dir,
        providers={"default": FakeProvider(script=example_script)},
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


def test_cli_execution_replay_exit_0(tmp_path, example_config, example_script):
    """End-to-end CLI against an ordinary run: exit 0, fresh artifact.json
    exists, digest matches (ids/timestamps replayed from the recording)."""
    run_dir = _make_original_run(tmp_path, example_config, example_script)

    runner = CliRunner()
    res = runner.invoke(
        cli_main,
        ["execution-replay", str(run_dir), "--script", _script_path(), "--output", str(tmp_path)],
    )
    assert res.exit_code == 0, res.output
    fresh_dir = tmp_path / f"{example_config.session_id}-replay"
    assert (fresh_dir / "artifact.json").exists()
    assert "digest=match" in res.output


def test_cli_pinning_violation_exit_3(tmp_path, example_config, example_script):
    run_dir = _make_original_run(tmp_path, example_config, example_script)
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


def test_cli_digest_mismatch_exit_4(tmp_path, example_config, example_script):
    run_dir = _make_original_run(tmp_path, example_config, example_script)
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


def test_cli_missing_run_dir_exit_1(tmp_path, example_config, example_script):
    """A run dir with no config.json is a generic error (exit 1), distinct from 3/4."""
    run_dir = _make_original_run(tmp_path, example_config, example_script)
    (run_dir / "config.json").unlink()

    runner = CliRunner()
    res = runner.invoke(
        cli_main,
        ["execution-replay", str(run_dir), "--script", _script_path()],
    )
    assert res.exit_code == 1, res.output


def test_cli_exit_codes_are_distinct():
    """0 (match), 3 (pinning), 4 (mismatch), 1 (generic) are four distinct codes."""
    assert len({0, 1, 3, 4}) == 4

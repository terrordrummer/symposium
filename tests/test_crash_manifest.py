"""Crash containment + provider teardown.

An uncaught exception mid-run used to leave the manifest `in_progress`
forever, the journal fd open, and the lock released only by GC — despite
the manifest schema declaring a `crashed` status that nothing ever wrote.
These tests pin the crash handler in `run_session` and the best-effort
adapter `shutdown()` at teardown.
"""

from __future__ import annotations

import json

import pytest

from symposium.models import RunManifest
from symposium.providers import FakeProvider
from symposium.scheduler import run_session


class _ExplodingProvider:
    """Adapter that violates the §6.1 contract by raising from invoke()."""

    name = "fake"

    def invoke(self, request):
        raise RuntimeError("provider exploded mid-round")


def test_midrun_exception_marks_manifest_crashed(tmp_path, example_config):
    with pytest.raises(RuntimeError, match="exploded mid-round"):
        run_session(
            example_config, {"default": _ExplodingProvider()}, runs_root=str(tmp_path)
        )

    run_dir = tmp_path / example_config.session_id
    manifest_raw = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest_raw["status"] == "crashed"
    assert manifest_raw.get("updated_at")
    # Still model-valid (status=crashed carries no digest / outcome_kind).
    RunManifest.model_validate(manifest_raw)
    # Lock released; no artifact was fabricated for the aborted run.
    assert not (run_dir / ".lock").exists()
    assert not (run_dir / "artifact.json").exists()
    # The journal survives up to the crash point (problem_statement line).
    journal_lines = [
        line
        for line in (run_dir / "transcript.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert len(journal_lines) == 1
    assert json.loads(journal_lines[0])["type"] == "problem_statement"


def test_midrun_exception_without_writer_still_propagates(example_config):
    """No runs_root → no writer to tear down; the exception surfaces as-is."""
    with pytest.raises(RuntimeError, match="exploded mid-round"):
        run_session(example_config, {"default": _ExplodingProvider()})


# ---------------------------------------------------------------------------
# provider shutdown at teardown
# ---------------------------------------------------------------------------


class _ShutdownTrackingFake(FakeProvider):
    def __init__(self, script):
        super().__init__(script=script)
        self.shutdown_calls = 0

    def shutdown(self):
        self.shutdown_calls += 1


def test_provider_shutdown_called_on_clean_completion(example_config, example_script):
    fp = _ShutdownTrackingFake(example_script)
    art = run_session(example_config, {"default": fp})
    assert art is not None
    assert fp.shutdown_calls == 1


def test_provider_shutdown_called_even_on_crash(tmp_path, example_config):
    class _ExplodingWithShutdown(_ExplodingProvider):
        def __init__(self):
            self.shutdown_calls = 0

        def shutdown(self):
            self.shutdown_calls += 1

    provider = _ExplodingWithShutdown()
    with pytest.raises(RuntimeError):
        run_session(example_config, {"default": provider}, runs_root=str(tmp_path))
    assert provider.shutdown_calls == 1


def test_raising_shutdown_does_not_mask_the_session_outcome(
    example_config, example_script
):
    class _BadShutdown(FakeProvider):
        def shutdown(self):
            raise RuntimeError("shutdown failed")

    art = run_session(example_config, {"default": _BadShutdown(script=example_script)})
    assert art.outcome.kind in ("synthesis", "termination")

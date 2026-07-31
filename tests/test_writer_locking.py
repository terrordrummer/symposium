"""RunWriter lock lifecycle + journal truncation regressions.

Pins three storage-writer failure modes:

  * a second writer whose start() fails with RunDirectoryLocked must NOT
    release (unlink) the active writer's lockfile when it is deleted /
    GC'd — pre-fix, `_lock_path` was set before acquisition, so the failed
    writer's `__del__` deleted the ACTIVE writer's lock;
  * restarting a run in the same directory must truncate the journal, not
    concatenate two runs into one transcript.jsonl;
  * breaking a stale lock re-reads the lockfile immediately before the
    unlink, so a starter that lost the break race cannot unlink the
    winner's fresh lock (TOCTOU).
"""

from __future__ import annotations

import gc
import json
import os

import pytest

import symposium.storage.writer as writer_mod
from symposium.models import (
    Artifact,
    Message,
    TerminationArtifact,
    TerminationOutcome,
    Usage,
    now_utc_iso,
)
from symposium.storage import RunDirectory, RunWriter, compute_transcript_digest
from symposium.storage.writer import RunDirectoryLocked


# ---------------------------------------------------------------------------
# builders
# ---------------------------------------------------------------------------


def _zero_usage() -> Usage:
    return Usage(prompt_tokens=0, completion_tokens=0, total_tokens=0, cost_usd=0.0)


def _problem_msg(config, msg_id: str = "m0") -> Message:
    return Message(
        id=msg_id,
        speaker=config.originator,
        type="problem_statement",
        content=config.problem_statement,
        round=0,
        turn_index=0,
        branch_depth=0,
        timestamp=now_utc_iso(),
        usage=_zero_usage(),
    )


def _termination_artifact(config, transcript) -> Artifact:
    digest = compute_transcript_digest(transcript)
    term = TerminationArtifact(
        reason="user_cancel",
        final_round=0,
        cumulative_usage=_zero_usage(),
        transcript_digest=digest,
    )
    return Artifact(
        session_id=config.session_id,
        config=config,
        canonical_transcript=transcript,
        outcome=TerminationOutcome(termination_artifact=term),
        cumulative_usage=_zero_usage(),
        cumulative_unresolved=[],
        transcript_digest=digest,
        started_at=now_utc_iso(),
        ended_at=now_utc_iso(),
    )


# ---------------------------------------------------------------------------
# lock ownership
# ---------------------------------------------------------------------------


def test_failed_second_writer_does_not_release_active_lock(tmp_path, example_config):
    rd = RunDirectory.for_session(tmp_path, example_config.session_id)
    active = RunWriter(rd)
    active.start(example_config, now_utc_iso())
    lock = rd.base / ".lock"
    assert lock.exists()

    loser = RunWriter(rd)
    with pytest.raises(RunDirectoryLocked):
        loser.start(example_config, now_utc_iso())
    # Explicit delete + GC: the loser's __del__ must be a no-op on the lock.
    del loser
    gc.collect()
    assert lock.exists(), "failed writer's GC released the ACTIVE writer's lock"

    # The active writer still works end-to-end and releases on finalize.
    msg = _problem_msg(example_config)
    active.append_message(msg)
    active.finalize(_termination_artifact(example_config, [msg]))
    assert not lock.exists()


# ---------------------------------------------------------------------------
# journal truncation
# ---------------------------------------------------------------------------


def test_restart_truncates_journal_instead_of_concatenating(tmp_path, example_config):
    rd = RunDirectory.for_session(tmp_path, example_config.session_id)
    first = RunWriter(rd)
    first.start(example_config, now_utc_iso())
    msg1 = _problem_msg(example_config, msg_id="run1-msg")
    first.append_message(msg1)
    first.finalize(_termination_artifact(example_config, [msg1]))

    second = RunWriter(rd)
    second.start(example_config, now_utc_iso())
    msg2 = _problem_msg(example_config, msg_id="run2-msg")
    second.append_message(msg2)
    second.finalize(_termination_artifact(example_config, [msg2]))

    lines = [
        json.loads(line)
        for line in rd.journal_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert [entry["id"] for entry in lines] == ["run2-msg"], (
        "journal concatenated two runs instead of starting fresh"
    )


# ---------------------------------------------------------------------------
# stale-lock break
# ---------------------------------------------------------------------------


def test_stale_lock_is_broken_and_reacquired(tmp_path, example_config, monkeypatch):
    """A lockfile naming a confirmed-dead pid must not block the session_id."""
    rd = RunDirectory.for_session(tmp_path, example_config.session_id)
    rd.ensure()
    dead_pid = 999_999_991
    lock = rd.base / ".lock"
    lock.write_text(f"{dead_pid} 2026-01-01T00:00:00Z\n", encoding="utf-8")
    monkeypatch.setattr(writer_mod, "_pid_is_dead", lambda pid: pid == dead_pid)

    w = RunWriter(rd)
    w.start(example_config, now_utc_iso())
    assert lock.read_text(encoding="utf-8").split()[0] == str(os.getpid())
    msg = _problem_msg(example_config)
    w.append_message(msg)
    w.finalize(_termination_artifact(example_config, [msg]))
    assert not lock.exists()


def test_stale_lock_break_rechecks_content_before_unlink(
    tmp_path, example_config, monkeypatch
):
    """Two starters observe the same stale lock; the winner breaks it and
    creates a fresh one. The loser must re-read the lockfile immediately
    before its own unlink, see the swap, and back off — never unlinking the
    winner's fresh lock."""
    rd = RunDirectory.for_session(tmp_path, example_config.session_id)
    rd.ensure()
    lock = rd.base / ".lock"
    live_pid = os.getpid()
    # The winner's fresh lock is already in place (names a live pid).
    lock.write_text(f"{live_pid} 2026-01-01T00:00:00Z\n", encoding="utf-8")

    dead_pid = 999_999_991
    # First read simulates the loser's pre-swap staleness observation; every
    # later read sees the real (fresh) lock content.
    reads = iter([dead_pid])
    real_read = writer_mod._read_lock_pid

    def fake_read(path):
        try:
            return next(reads)
        except StopIteration:
            return real_read(path)

    monkeypatch.setattr(writer_mod, "_read_lock_pid", fake_read)
    monkeypatch.setattr(writer_mod, "_pid_is_dead", lambda pid: pid == dead_pid)

    loser = RunWriter(rd)
    with pytest.raises(RunDirectoryLocked):
        loser.start(example_config, now_utc_iso())
    assert lock.exists(), "loser unlinked the winner's fresh lock"
    assert lock.read_text(encoding="utf-8").split()[0] == str(live_pid)


# ---------------------------------------------------------------------------
# atomic-write permissions
# ---------------------------------------------------------------------------


def test_atomic_writes_use_umask_consistent_permissions(tmp_path, example_config):
    """mkstemp's 0600 must not survive os.replace: manifest/config/artifact
    should carry the same umask-derived mode as the journal."""
    rd = RunDirectory.for_session(tmp_path, example_config.session_id)
    w = RunWriter(rd)
    w.start(example_config, now_utc_iso())
    msg = _problem_msg(example_config)
    w.append_message(msg)
    w.finalize(_termination_artifact(example_config, [msg]))

    umask = os.umask(0)
    os.umask(umask)
    expected = 0o666 & ~umask
    journal_mode = (rd.base / "transcript.jsonl").stat().st_mode & 0o777
    assert journal_mode == expected
    for name in ("manifest.json", "config.json", "artifact.json"):
        mode = (rd.base / name).stat().st_mode & 0o777
        assert mode == expected, f"{name} mode {oct(mode)} != {oct(expected)}"

"""Run-directory discovery for the live viewer.

A run directory is any child of ``--runs-dir`` that contains a
``transcript.jsonl`` (or at least a ``config.json`` — a run that has
started but not yet appended its first message). "Newest" is by the
most-recently-modified ``transcript.jsonl`` so that auto-follow latches
onto the run currently being written, including one that is *created
after* the viewer starts (the skill launches ``watch`` first, then fires
the deliberation).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional


def _is_stale_lock_safe(lock_path: Path) -> bool:
    # Reuse the writer's PID-liveness check; treat any import/parse failure
    # as "not stale" (safe: we keep streaming rather than cut off early).
    try:
        from symposium.storage.writer import _is_stale_lock

        return _is_stale_lock(lock_path)
    except Exception:  # noqa: BLE001
        return False


def _is_active(run_dir: Path) -> bool:
    """Live iff the writer's lock is present AND names a running pid.

    Same staleness logic as the stream's status events (`server._status`),
    so a crashed run reads inactive in the list and the stream alike.
    """
    lock = run_dir / ".lock"
    if not lock.exists():
        return False
    return not _is_stale_lock_safe(lock)


@dataclass(frozen=True)
class RunInfo:
    name: str          # directory name (session_id)
    path: str          # absolute path to the run directory
    mtime: float       # newest of transcript/config mtime, for ordering
    active: bool       # .lock present and not stale (best-effort "live" hint)
    has_transcript: bool


def _run_mtime(run_dir: Path) -> float:
    candidates = [run_dir / "transcript.jsonl", run_dir / "config.json", run_dir]
    best = 0.0
    for c in candidates:
        try:
            best = max(best, c.stat().st_mtime)
        except OSError:
            continue
    return best


def _is_run_dir(p: Path) -> bool:
    if not p.is_dir():
        return False
    return (p / "transcript.jsonl").exists() or (p / "config.json").exists()


def list_runs(runs_root: Path) -> List[RunInfo]:
    """All run dirs under ``runs_root``, newest first."""
    runs_root = Path(runs_root)
    if not runs_root.is_dir():
        return []
    infos: List[RunInfo] = []
    for child in runs_root.iterdir():
        if not _is_run_dir(child):
            continue
        infos.append(
            RunInfo(
                name=child.name,
                path=str(child.resolve()),
                mtime=_run_mtime(child),
                active=_is_active(child),
                has_transcript=(child / "transcript.jsonl").exists(),
            )
        )
    infos.sort(key=lambda r: r.mtime, reverse=True)
    return infos


def newest_run(runs_root: Path) -> Optional[RunInfo]:
    """The most-recently-touched run dir, or None if none exist yet."""
    runs = list_runs(runs_root)
    return runs[0] if runs else None

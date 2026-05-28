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


@dataclass(frozen=True)
class RunInfo:
    name: str          # directory name (session_id)
    path: str          # absolute path to the run directory
    mtime: float       # newest of transcript/config mtime, for ordering
    active: bool       # .lock present (best-effort "live" hint)
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
                active=(child / ".lock").exists(),
                has_transcript=(child / "transcript.jsonl").exists(),
            )
        )
    infos.sort(key=lambda r: r.mtime, reverse=True)
    return infos


def newest_run(runs_root: Path) -> Optional[RunInfo]:
    """The most-recently-touched run dir, or None if none exist yet."""
    runs = list_runs(runs_root)
    return runs[0] if runs else None

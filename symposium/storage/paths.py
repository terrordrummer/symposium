"""Run directory layout (§7.1).

```
runs/<session_id>/
├── manifest.json          # RunManifest (read first)
├── config.json            # Config snapshot
├── artifact.json          # Artifact (authoritative output)
├── transcript.jsonl       # Append-only per-turn journal (in-progress / debug)
├── termination.json       # TerminationArtifact (terminate paths only)
└── selector_output.json   # SelectorOutput (§5.11; additive, not in the Artifact)
```

The session_id charset is constrained to `^[A-Za-z0-9_-]{1,64}$` so the
directory roundtrips on POSIX, Windows, and case-insensitive
filesystems.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def validate_session_id(session_id: str) -> str:
    if not _SESSION_ID_RE.match(session_id):
        raise ValueError(
            f"session_id {session_id!r} must match ^[A-Za-z0-9_-]{{1,64}}$ (§7.1)"
        )
    return session_id


@dataclass(frozen=True)
class RunDirectory:
    """Filesystem layout for a single session run."""

    base: Path

    @classmethod
    def for_session(cls, runs_root: Path, session_id: str) -> "RunDirectory":
        validate_session_id(session_id)
        return cls(base=runs_root / session_id)

    @property
    def manifest_path(self) -> Path:
        return self.base / "manifest.json"

    @property
    def config_path(self) -> Path:
        return self.base / "config.json"

    @property
    def artifact_path(self) -> Path:
        return self.base / "artifact.json"

    @property
    def journal_path(self) -> Path:
        return self.base / "transcript.jsonl"

    @property
    def termination_path(self) -> Path:
        return self.base / "termination.json"

    @property
    def selector_output_path(self) -> Path:
        return self.base / "selector_output.json"

    def ensure(self) -> None:
        self.base.mkdir(parents=True, exist_ok=True)

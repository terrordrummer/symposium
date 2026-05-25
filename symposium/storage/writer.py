"""Run writer: atomic Artifact / TerminationArtifact / RunManifest emit (§7.4)."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Optional

from symposium.models import (
    Artifact,
    Config,
    Message,
    RunManifest,
    TerminationArtifact,
)
from symposium.storage.digest import serialize_pretty
from symposium.storage.paths import RunDirectory

PRODUCER_NAME = "symposium-py"
PRODUCER_VERSION = "1.0.0"


class RunWriter:
    """Owns writes to a single `RunDirectory`.

    Behaviour:
      - `start(config)` ensures the directory, snapshots Config to
        `config.json`, opens an in-progress manifest, and starts the
        per-turn journal.
      - `append_message(msg)` appends one JSON line to the journal.
      - `finalize(...)` writes `artifact.json` (and `termination.json`
        on terminate paths) atomically, then rewrites the manifest with
        the final status.
    """

    def __init__(self, run_dir: RunDirectory) -> None:
        self.run_dir = run_dir
        self._journal_fp = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self, config: Config, started_at: str) -> None:
        self.run_dir.ensure()
        config_dump = config.model_dump(mode="json", exclude_none=True)
        _atomic_write_text(self.run_dir.config_path, serialize_pretty(config_dump))

        in_progress = RunManifest(
            session_id=config.session_id,
            status="in_progress",
            producer={"name": PRODUCER_NAME, "version": PRODUCER_VERSION},
            created_at=started_at,
            updated_at=started_at,
            artifact_path="artifact.json",
            config_path="config.json",
            journal_path="transcript.jsonl",
        )
        self._write_manifest(in_progress)
        # Open the journal in line-buffered append mode (crash-safe up to
        # the granularity of one line per turn).
        self._journal_fp = open(
            self.run_dir.journal_path, "a", encoding="utf-8", buffering=1
        )

    def append_message(self, msg: Message) -> None:
        if self._journal_fp is None:
            raise RuntimeError("RunWriter.start() must be called before append_message()")
        line = serialize_pretty(msg.model_dump(mode="json", exclude_none=True)).rstrip("\n")
        # Make each line a single, compact JSON object so the journal
        # remains line-delimited (`*.jsonl`).
        import json
        compact = json.dumps(msg.model_dump(mode="json", exclude_none=True), ensure_ascii=False)
        self._journal_fp.write(compact + "\n")

    def finalize(
        self,
        artifact: Artifact,
        termination: Optional[TerminationArtifact] = None,
        updated_at: Optional[str] = None,
    ) -> None:
        if self._journal_fp is not None:
            self._journal_fp.flush()
            self._journal_fp.close()
            self._journal_fp = None

        # Write artifact.json
        artifact_dump = artifact.model_dump(mode="json", exclude_none=True)
        _atomic_write_text(self.run_dir.artifact_path, serialize_pretty(artifact_dump))

        # Write termination.json on terminate paths
        if termination is not None:
            term_dump = termination.model_dump(mode="json", exclude_none=True)
            _atomic_write_text(self.run_dir.termination_path, serialize_pretty(term_dump))

        # Rewrite the manifest as complete / terminated
        outcome_kind = artifact.outcome.kind
        status = "complete" if outcome_kind == "synthesis" else "terminated"
        final_manifest = RunManifest(
            session_id=artifact.session_id,
            status=status,  # type: ignore[arg-type]
            producer={"name": PRODUCER_NAME, "version": PRODUCER_VERSION},
            created_at=artifact.started_at,
            updated_at=updated_at or artifact.ended_at,
            artifact_path="artifact.json",
            config_path="config.json",
            journal_path="transcript.jsonl",
            transcript_digest=artifact.transcript_digest,
            outcome_kind=outcome_kind,  # type: ignore[arg-type]
        )
        self._write_manifest(final_manifest)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _write_manifest(self, manifest: RunManifest) -> None:
        dump = manifest.model_dump(mode="json", exclude_none=True)
        _atomic_write_text(self.run_dir.manifest_path, serialize_pretty(dump))


def _atomic_write_text(path: Path, text: str) -> None:
    """Write text atomically by writing to a sibling tempfile then renaming.

    `os.replace` is atomic on POSIX and on Windows for same-volume
    renames; this is enough to satisfy §7.4 for the MVP.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(prefix=".tmp_", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise

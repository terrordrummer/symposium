"""Run writer: atomic Artifact / TerminationArtifact / RunManifest emit (§7.4)."""

from __future__ import annotations

import errno
import os
import tempfile
from pathlib import Path
from typing import Optional

from symposium.models import (
    Artifact,
    Config,
    Message,
    RunManifest,
    SelectorOutput,
    TerminationArtifact,
)
from symposium.storage.digest import serialize_pretty
from symposium.storage.paths import RunDirectory


_LOCKFILE_NAME = ".lock"


class RunDirectoryLocked(RuntimeError):
    """Raised when a second RunWriter tries to start() against an in-use run dir."""


def _is_stale_lock(lock_path: Path) -> bool:
    """Return True iff `lock_path` exists and names a non-running pid.

    Best-effort: reads the pid token from the lockfile (first whitespace-
    separated field) and signals 0 to test liveness. If we cannot determine
    aliveness (parse failure, ESRCH on a foreign pid we cannot signal), we
    treat the lock as live (safe-by-default — false positives on stale
    detection are worse than slow recovery).
    """
    try:
        with open(lock_path, "r", encoding="utf-8") as f:
            token = f.read().strip().split()
        if not token:
            return False
        pid = int(token[0])
    except (FileNotFoundError, ValueError, OSError):
        return False
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True  # confirmed dead
    except PermissionError:
        return False  # pid exists but in another user's session
    except OSError as exc:
        if getattr(exc, "errno", None) == errno.ESRCH:
            return True
        return False
    return False

PRODUCER_NAME = "symposium-py"
# The §7.6 condition-#1 "runtime" reproduction-surface identity, NOT the
# package version (`symposium.__version__`). It pins the digest-bearing
# runtime logic (canonicalization, id minting, packet derivation). M2–M6
# added features without changing that surface for an unchanged Config (the
# M6 `fixed` digest is byte-identical to M1), so it stays 1.0.0: runs
# recorded by any 1.x build remain mutually execution-replayable. Bump it
# only when the reproduction surface itself changes.
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
        self._lock_path: Optional[Path] = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self, config: Config, started_at: str) -> None:
        self.run_dir.ensure()
        # Acquire an exclusive lock on the run dir so two writers cannot
        # race on the same `session_id`. O_EXCL + O_CREAT is atomic on
        # POSIX and on Windows for the same volume; we hold the lock for
        # the lifetime of this writer. If a previous run crashed and left
        # a stale lockfile (pid no longer alive), we break it and proceed —
        # otherwise crashed runs would block the same session_id forever.
        self._lock_path = Path(self.run_dir.base) / _LOCKFILE_NAME
        try:
            self._acquire_lock(started_at)
        except RunDirectoryLocked:
            if _is_stale_lock(self._lock_path):
                try:
                    os.unlink(self._lock_path)
                except FileNotFoundError:
                    pass
                self._acquire_lock(started_at)
            else:
                raise

        try:
            config_dump = config.model_dump(mode="json", exclude_none=True)
            _atomic_write_text(self.run_dir.config_path, serialize_pretty(config_dump))
        except Exception:
            # Pre-journal failure: release the lock so the dir is not stuck.
            self._release_lock()
            raise

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

    def write_selector_output(self, selection: SelectorOutput) -> None:
        """Persist the §5.11 SelectorOutput to `<run_dir>/selector_output.json`.

        Additive sibling file: it is NOT part of the frozen Artifact /
        manifest schema and does not enter the `canonical_transcript` or
        the `transcript_digest`. Sorted-keys pretty JSON written through
        the same atomic temp-file → rename helper as the other artifacts
        (§7.4).
        """
        self.run_dir.ensure()
        dump = selection.model_dump(mode="json", exclude_none=True)
        _atomic_write_text(self.run_dir.selector_output_path, serialize_pretty(dump))

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

        # Release the exclusive lock now that the run has been fully persisted.
        self._release_lock()

    def _acquire_lock(self, started_at: str) -> None:
        try:
            fd = os.open(
                str(self._lock_path),
                os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                0o644,
            )
        except FileExistsError as exc:
            raise RunDirectoryLocked(
                f"run directory {self.run_dir.base!s} is already held by another writer "
                f"(lockfile present); refusing to start a second writer"
            ) from exc
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(f"{os.getpid()} {started_at}\n")

    def _release_lock(self) -> None:
        if self._lock_path is None:
            return
        try:
            os.unlink(self._lock_path)
        except FileNotFoundError:
            pass
        self._lock_path = None

    def __del__(self) -> None:  # best-effort lock release on GC
        try:
            self._release_lock()
        except Exception:  # noqa: BLE001
            pass

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _write_manifest(self, manifest: RunManifest) -> None:
        dump = manifest.model_dump(mode="json", exclude_none=True)
        _atomic_write_text(self.run_dir.manifest_path, serialize_pretty(dump))


def _atomic_write_text(path: Path, text: str) -> None:
    """Write text atomically by writing to a sibling tempfile then renaming.

    Order of operations: write → flush → fsync → rename → fsync(dir). The
    extra fsync of the parent directory makes the rename durable across a
    crash on POSIX file systems (without it, a post-crash recovery may see
    the file under its old name or not at all). `os.replace` is atomic on
    POSIX and on Windows for same-volume renames; the fsyncs make the
    durability promise survive power loss.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(prefix=".tmp_", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            try:
                os.fsync(f.fileno())
            except OSError:
                pass  # fsync not supported (e.g. some Windows / shared FS)
        os.replace(tmp_path, path)
        # Fsync the parent directory so the rename is durable.
        try:
            dir_fd = os.open(str(path.parent), os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except (OSError, AttributeError):
            pass  # not supported on Windows or some filesystems
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise

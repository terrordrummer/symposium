"""Shared atomic file replacement (write → fsync → rename → fsync dir).

One implementation for every on-disk artifact (run writer §7.4,
observability metrics, control-plane snapshots) so the durability
guarantees cannot drift between call sites.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path


def atomic_write_text(path: Path, text: str) -> None:
    """Write text atomically by writing to a sibling tempfile then renaming.

    Order of operations: write → flush → fsync → rename → fsync(dir). The
    extra fsync of the parent directory makes the rename durable across a
    crash on POSIX file systems (without it, a post-crash recovery may see
    the file under its old name or not at all). `os.replace` is atomic on
    POSIX and on Windows for same-volume renames; the fsyncs make the
    durability promise survive power loss.
    """
    path = Path(path)
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
        # mkstemp creates the tempfile 0600 and os.replace carries that mode
        # over. Keep it: run files hold full deliberation content, so they
        # stay private to the owning user.
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

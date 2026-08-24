"""Atomic local storage for the Symposium 2.x control plane."""

from __future__ import annotations

import errno
import json
import os
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterator, Optional, TypeVar

from symposium.control_plane.models import ControlPlaneState

_STATE_FILE = "control-plane.json"
_LOCK_FILE = ".control-plane.lock"
T = TypeVar("T")

# A lockfile that exists but cannot be parsed means the holder crashed
# between creating it (O_EXCL) and writing its PID. Such a lock must not
# wedge the workspace forever: once it is older than this grace period no
# live acquisition can still be filling it in, so it is treated as stale.
# The grace also covers a contender that raced past _stale_lock while the
# legitimate holder was mid-write.
_UNPARSEABLE_LOCK_STALE_SECONDS = 30.0


class ControlPlaneNotInitialized(RuntimeError):
    """Raised when no control-plane snapshot exists yet."""


class ControlPlaneBusy(RuntimeError):
    """Raised when another process is mutating the control plane."""


def _pid_is_dead(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    except OSError as exc:
        return getattr(exc, "errno", None) == errno.ESRCH
    return False


def _lock_age_seconds(path: Path) -> Optional[float]:
    try:
        return max(0.0, time.time() - path.stat().st_mtime)
    except OSError:
        return None


def _stale_lock(path: Path) -> bool:
    try:
        token = path.read_text(encoding="utf-8").strip().split()[0]
        pid = int(token)
    except (FileNotFoundError, IndexError, OSError, ValueError):
        # Unparseable lock (empty, truncated, or garbage): the holder died
        # between creating the file and writing its PID. Treat it as stale
        # only after the grace period so a mid-acquisition lock is never
        # broken — pre-fix, this state wedged the workspace permanently.
        age = _lock_age_seconds(path)
        return age is not None and age > _UNPARSEABLE_LOCK_STALE_SECONDS
    return pid > 0 and _pid_is_dead(pid)


class ControlPlaneStore:
    """One validated JSON snapshot, replaced atomically under a PID lock."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.path = self.root / _STATE_FILE
        self.lock_path = self.root / _LOCK_FILE

    @property
    def exists(self) -> bool:
        return self.path.is_file()

    def load(self) -> ControlPlaneState:
        if not self.exists:
            raise ControlPlaneNotInitialized(
                f"no workspace at {self.root}; run `symposium workspace init`"
            )
        return self._load_unlocked()

    def create(self, state: ControlPlaneState) -> ControlPlaneState:
        self.root.mkdir(parents=True, exist_ok=True)
        with self._locked():
            if self.exists:
                return self._load_unlocked()
            self._write_unlocked(state)
            return state

    def update(
        self, mutation: Callable[[ControlPlaneState], T]
    ) -> tuple[ControlPlaneState, T]:
        self.root.mkdir(parents=True, exist_ok=True)
        with self._locked():
            if not self.exists:
                raise ControlPlaneNotInitialized(
                    f"no workspace at {self.root}; run `symposium workspace init`"
                )
            state = self._load_unlocked()
            result = mutation(state)
            validated = ControlPlaneState.model_validate(state.model_dump(mode="json"))
            self._write_unlocked(validated)
            return validated, result

    def _load_unlocked(self) -> ControlPlaneState:
        try:
            return ControlPlaneState.model_validate_json(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ValueError(f"invalid control-plane state at {self.path}: {exc}") from exc

    def _write_unlocked(self, state: ControlPlaneState) -> None:
        payload = json.dumps(
            state.model_dump(mode="json"),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        ) + "\n"
        fd, temporary = tempfile.mkstemp(prefix=".control-plane-", dir=self.root)
        temp_path = Path(temporary)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_path, self.path)
        finally:
            try:
                temp_path.unlink()
            except FileNotFoundError:
                pass
        # Fsync the parent directory so the rename is durable across a
        # crash (same pattern as the §7.4 run writer). Best-effort: some
        # filesystems do not support directory fsync.
        try:
            dir_fd = os.open(str(self.root), os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except OSError:
            pass

    @contextmanager
    def _locked(self) -> Iterator[None]:
        fd: Optional[int] = None
        for _ in range(2):
            try:
                fd = os.open(self.lock_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                break
            except FileExistsError:
                if _stale_lock(self.lock_path):
                    try:
                        self.lock_path.unlink()
                    except FileNotFoundError:
                        pass
                    continue
                raise ControlPlaneBusy(f"workspace is busy: {self.lock_path}") from None
        if fd is None:
            raise ControlPlaneBusy(f"could not acquire workspace lock: {self.lock_path}")
        try:
            os.write(fd, f"{os.getpid()}\n".encode("ascii"))
            os.close(fd)
            fd = None
            yield
        finally:
            if fd is not None:
                os.close(fd)
            try:
                self.lock_path.unlink()
            except FileNotFoundError:
                pass

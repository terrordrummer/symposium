"""Incremental, truncation-safe reader for a line-delimited ``transcript.jsonl``.

The runtime appends one compact JSON object per message in line-buffered
append mode (``symposium/storage/writer.py``). A reader that opens the
file mid-write can therefore see a trailing line that has been partially
flushed; :class:`JournalTail` keeps a byte offset plus a pending-tail
buffer so each :meth:`drain` returns only the *complete* message dicts
appended since the previous call, holding any partial trailing line over
to the next drain.

This is the single source of truth for tailing the journal. The MCP
server's streaming path re-imports :class:`JournalTail` from here so the
two consumers cannot drift.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List


class JournalTail:
    """Stateful incremental reader for ``transcript.jsonl``.

    Usage::

        tail = JournalTail(path)
        new_msgs = tail.drain()   # every complete line so far
        ...                       # later, after more lines are appended
        new_msgs = tail.drain()   # only lines appended since last drain
    """

    def __init__(self, path: Path) -> None:
        self._path = Path(path)
        self._offset = 0
        self._pending = ""

    def drain(self) -> List[Dict[str, Any]]:
        """Return message dicts for every complete line appended since last call.

        A partial trailing line (append caught mid-flush) is buffered and
        re-evaluated on the next drain; malformed-but-complete lines are
        skipped defensively.
        """
        if not self._path.exists():
            return []
        with open(self._path, "r", encoding="utf-8") as fp:
            fp.seek(self._offset)
            chunk = fp.read()
            self._offset = fp.tell()
        if not chunk:
            return []
        self._pending += chunk
        lines = self._pending.split("\n")
        # The final element is the trailing partial line (or "" if the chunk
        # ended exactly on a newline); it survives to the next drain.
        self._pending = lines.pop()
        out: List[Dict[str, Any]] = []
        for line in lines:
            if not line.strip():
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                # A complete-but-corrupt line: skip it rather than abort the
                # stream. (Line-buffered atomic writes make this rare.)
                continue
        return out

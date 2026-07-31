"""Incremental, truncation-safe reader for a line-delimited ``transcript.jsonl``.

The runtime appends one compact JSON object per message in line-buffered
append mode (``symposium/storage/writer.py``). A reader that opens the
file mid-write can therefore see a trailing line that has been partially
flushed; :class:`JournalTail` keeps a byte offset plus a pending-tail
buffer so each :meth:`drain` returns only the *complete* message dicts
appended since the previous call, holding any partial trailing line over
to the next drain.

Two mid-write hazards are handled explicitly:

* **Truncation/rotation** — if the journal shrinks below the saved
  offset (log rotation, a re-created run dir), the tail restarts from
  the top of the file instead of seeking past EOF and going silent.
* **Split multi-byte characters** — reads are binary and decoded with
  an incremental UTF-8 decoder, so catching the writer mid-flush of a
  multi-byte character buffers the partial sequence for the next drain
  rather than raising ``UnicodeDecodeError``.

This is the single source of truth for tailing the journal. The MCP
server's streaming path re-imports :class:`JournalTail` from here so the
two consumers cannot drift.
"""

from __future__ import annotations

import codecs
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
        self._offset = 0  # byte offset of the next unread byte
        self._pending = ""
        self._decoder = self._new_decoder()

    @staticmethod
    def _new_decoder() -> codecs.IncrementalDecoder:
        # `errors="replace"` keeps a corrupt byte from killing the stream;
        # the JSON parse below skips the resulting garbage line anyway.
        return codecs.getincrementaldecoder("utf-8")("replace")

    def drain(self) -> List[Dict[str, Any]]:
        """Return message dicts for every complete line appended since last call.

        A partial trailing line (append caught mid-flush) is buffered and
        re-evaluated on the next drain; malformed-but-complete lines are
        skipped defensively. If the file shrank since the previous drain
        (truncation/rotation) the tail resets and re-reads from the top.
        """
        try:
            size = self._path.stat().st_size
        except OSError:
            return []
        if size < self._offset:
            # The journal shrank under us: seeking to the old offset would
            # land past EOF and read nothing forever (then resume mid-line
            # if the file regrows). Start over from the beginning.
            self._offset = 0
            self._pending = ""
            self._decoder = self._new_decoder()
        with open(self._path, "rb") as fp:
            fp.seek(self._offset)
            chunk = fp.read()
            self._offset = fp.tell()
        if not chunk:
            return []
        # Incremental decode: a multi-byte character split across drains is
        # held inside the decoder until its remaining bytes arrive.
        self._pending += self._decoder.decode(chunk)
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

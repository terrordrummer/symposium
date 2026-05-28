"""Read-only live viewer for Symposium deliberations.

A pure *consumer* of a run directory's persisted artifacts — exactly like
``get_run_summary`` / ``get_run_status``. It never touches the runtime,
the protocol, or the JSON Schemas, and it works equally on a live run
(tailing ``transcript.jsonl`` as the scheduler appends) or on a finished
run (replay). Nothing here can change a ``transcript_digest``.

Entry point: :func:`symposium.viewer.server.serve`, wired to the CLI as
``symposium watch``.
"""

from symposium.viewer.tail import JournalTail

__all__ = ["JournalTail"]

"""Live meeting workspace for Symposium deliberations.

The transcript side is a pure consumer of persisted v1 artifacts — exactly
like ``get_run_summary`` / ``get_run_status`` — and nothing here can change a
``transcript_digest``. A separate same-origin control API may mutate the local
2.x workspace, rooms, agents, and memberships or start a new immutable v1 run;
it never rewrites an existing run.

Entry point: :func:`symposium.viewer.server.serve`, wired to the CLI as
``symposium watch``.
"""

from symposium.viewer.tail import JournalTail

__all__ = ["JournalTail"]

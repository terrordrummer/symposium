"""Replay contracts (§7.5, §7.6).

Two distinct contracts share this namespace and NOTHING else (§7.8):

  * `transcript_replay` (§7.5) — unconditional byte-identical re-rendering
    of a stored `canonical_transcript`. No LLM call; determinism is free.
  * `execution_replay` (§7.6) — conditional re-execution of the
    orchestrator_runtime under the ten pinning conditions. Reproducible
    only when every non-deterministic source is pinned; aborts with a
    `PinningViolation` otherwise (silent best-effort replay is forbidden).

`pinned_runtime` is the deterministic-runtime context a library user wraps
around `run_session` to produce a *reproducible* original run.
"""

from symposium.replay.execution import (
    ExecutionReplayResult,
    PinningViolation,
    execution_replay,
    pinned_runtime,
)
from symposium.replay.transcript import (
    TranscriptReplayResult,
    replay_transcript,
)

__all__ = [
    "ExecutionReplayResult",
    "PinningViolation",
    "TranscriptReplayResult",
    "execution_replay",
    "pinned_runtime",
    "replay_transcript",
]

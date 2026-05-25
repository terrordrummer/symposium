"""Replay contracts (§7.5, §7.6).

Walking-skeleton ships `transcript_replay` only (§7.5 — unconditional
byte-identical re-rendering of a stored `canonical_transcript`).
`execution_replay` (§7.6, conditional on ten pinning conditions) is
deferred to the next milestone.
"""

from symposium.replay.transcript import (
    TranscriptReplayResult,
    replay_transcript,
)

__all__ = ["TranscriptReplayResult", "replay_transcript"]

"""transcript_replay (§7.5).

Re-render a stored `canonical_transcript` without invoking any provider.
Byte-identity is unconditional because no LLM call is involved.

The implementation loads `artifact.json` from a run directory, re-computes
the `transcript_digest` over the stored `canonical_transcript`, and
asserts it matches the persisted value. A consumer that re-canonicalizes
the same bytes obtains the same digest by construction (rfc8785 is
deterministic).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import List

from symposium.models import Artifact, Message
from symposium.storage.digest import compute_transcript_digest


@dataclass
class TranscriptReplayResult:
    artifact: Artifact
    re_emitted_messages: List[Message]
    recomputed_digest: str
    digest_matches: bool


def replay_transcript(run_dir: Path) -> TranscriptReplayResult:
    """Load `runs/<session_id>/artifact.json`, re-emit messages, verify digest.

    Returns a `TranscriptReplayResult` so the caller can inspect both
    the messages and the digest match. Raises `FileNotFoundError` if
    `artifact.json` is missing; the Artifact validation enforces the
    rest of the integrity surface (§5.10).
    """
    artifact_path = run_dir / "artifact.json"
    if not artifact_path.exists():
        raise FileNotFoundError(f"no artifact.json under {run_dir}")
    artifact = Artifact.model_validate(json.loads(artifact_path.read_text()))

    # Re-emit messages: with a deterministic load, this is just the list
    # as Pydantic parsed it. The "re-emit" semantic is: serialize back
    # using the same field-exclude rules as the writer, then re-parse.
    re_emitted: List[Message] = [
        Message.model_validate(m.model_dump(mode="json", exclude_none=True))
        for m in artifact.canonical_transcript
    ]

    recomputed = compute_transcript_digest(re_emitted)
    return TranscriptReplayResult(
        artifact=artifact,
        re_emitted_messages=re_emitted,
        recomputed_digest=recomputed,
        digest_matches=(recomputed == artifact.transcript_digest),
    )

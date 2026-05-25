"""RFC-8785 JCS canonicalization + SHA-256 over the canonical_transcript (§7.7).

The digest is the integrity signal for a stored Artifact. Computation:

  1. Canonicalize the `canonical_transcript` array with RFC 8785 JCS.
  2. SHA-256 the canonical byte string.
  3. Encode as lowercase hex (64 chars).

Used by `Artifact.transcript_digest`,
`TerminationArtifact.transcript_digest`, and
`RunManifest.transcript_digest`; all three MUST be equal when present.
"""

from __future__ import annotations

import hashlib
import json
from typing import Iterable, List

import rfc8785

from symposium.models import Message


def _message_to_jsonable(msg: Message) -> dict:
    # Round-trip through JSON to drop Pydantic-specific containers and
    # to apply field aliasing / exclusion exactly the way the persisted
    # artifact will serialize. `mode="json"` ensures datetime / enum
    # values become their JSON-form strings.
    return msg.model_dump(mode="json", exclude_none=True)


def compute_transcript_digest(canonical_transcript: Iterable[Message]) -> str:
    """Compute the RFC-8785 JCS-canonical SHA-256 over the transcript.

    Returns the lowercase hex digest (64 chars).
    """
    serializable: List[dict] = [_message_to_jsonable(m) for m in canonical_transcript]
    canonical_bytes = rfc8785.dumps(serializable)
    return hashlib.sha256(canonical_bytes).hexdigest()


def canonicalize(obj) -> bytes:
    """Public convenience: JCS-canonicalize an arbitrary JSON-able object."""
    return rfc8785.dumps(obj)


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def serialize_pretty(obj) -> str:
    """Pretty-print JSON for human-readable on-disk artifacts.

    The digest is computed over the JCS-canonical form, NOT this pretty
    form — readers re-canonicalize before recomputing the digest, so the
    on-disk pretty layout is purely cosmetic.
    """
    return json.dumps(obj, indent=2, sort_keys=True, ensure_ascii=False) + "\n"

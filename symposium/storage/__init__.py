"""Persistence layer (§7.1, §7.2, §7.4, §7.7).

Owns the on-disk layout, RFC-8785 JCS-canonicalized `transcript_digest`
computation, atomic Artifact / TerminationArtifact / RunManifest writes,
and the per-turn append-only journal that supports crash recovery.
"""

from symposium.storage.digest import compute_transcript_digest
from symposium.storage.paths import RunDirectory
from symposium.storage.writer import RunWriter

__all__ = ["RunDirectory", "RunWriter", "compute_transcript_digest"]

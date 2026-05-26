"""execution_replay (§7.6) — conditional re-execution under the ten pinning conditions.

Where `transcript_replay` (§7.5) re-renders a stored
`canonical_transcript` *unconditionally* (no LLM call is involved, so
byte-identity is free), `execution_replay` re-runs the
`orchestrator_runtime` against the original `problem_statement` /
`Config` to regenerate a fresh `canonical_transcript`. Identical output
requires every non-deterministic source to be pinned (§2.7 N3; §7.8:
*replayable ≠ reproducible*).

This module implements the §7.6 contract:

  1. Load the persisted `RunManifest` + `Config` + `Artifact` from a run
     directory.
  2. Check, in `PINNING_CONDITIONS` order, every condition that is
     *decidable offline* from the persisted run state plus the supplied
     providers / host environment. The disposition of each condition is
     one of:
       * **checked**  — decided here and recorded in `conditions_checked`.
       * **assumed**  — uncheckable offline; recorded in
         `conditions_assumed` with documentation (cache; the model
         provider-side snapshot).
       * **abort**    — a checkable condition that is unsatisfiable
         raises `PinningViolation` *before* the runtime executes.
     §7.6 forbids silent best-effort replay: a condition that is neither
     checked nor assumed is a bug, not a tolerated "unknown" tier.
  3. Only after every condition passes, re-run `run_session(...)` under a
     deterministic-runtime context (see `pinned_runtime`) into a *fresh*
     run directory (`<session_id>-replay/`), and compare the fresh
     `transcript_digest` against the original. The digest match/mismatch
     is reported, never raised — the caller decides how to surface it.

Open clarifications (no spec edits — see Hard-rule §3 of the milestone):

  * **Message-id minting.** The reference scheduler mints message ids
    with `uuid.uuid4()` (`symposium.scheduler.loop._new_id`). That is a
    non-deterministic source the `transcript_digest` depends on, yet
    §7.6's ten-condition enum does *not* name id generation. We treat it
    as part of condition #1 ("Runtime-level logic ... is part of the
    reproduction surface") and pin it deterministically inside
    `pinned_runtime`. A run that was *not* produced under the same
    deterministic id regime is simply not reproducible: its replay
    yields `digest_matches=False` rather than a spurious match. This is
    the §7.8 "replayable ≠ reproducible" statement made operational.
  * **"adapter-internal version" (condition #2).** The MVP registry
    (§6.11) keys on `provider_id` only; adapters carry no separately
    versioned identity. We decide #2 / #3 on registry resolvability (or
    a caller-supplied provider map) and assume adapter-internal version
    parity under the same `producer.version` (condition #1).
  * **"provider-side snapshot" (condition #4).** Not decidable offline.
    We check the `model` identifier for presence and record the
    snapshot half as an assumption (`conditions_assumed += ["model"]`):
    assumed under FakeProvider (no model snapshot exists), uncheckable
    under live providers.
"""

from __future__ import annotations

import itertools
import json
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, Iterator, List, Mapping, Optional

from symposium.models import Artifact, Config, Persona, RunManifest
from symposium.providers.base import ProviderAdapter
from symposium.providers.fake import FakeProvider
from symposium.providers.registry import default_registry
from symposium.scheduler import run_session
from symposium.storage.digest import canonicalize, sha256_hex
from symposium.storage.paths import validate_session_id
from symposium.storage.writer import PRODUCER_NAME, PRODUCER_VERSION

# Closed §7.6 enum, in the order conditions are evaluated.
PINNING_CONDITIONS = (
    "runtime",
    "adapter",
    "provider",
    "model",
    "sampling",
    "cache",
    "tool_env",
    "wallclock",
    "persona",
    "transcript_prefix",
)

# Fresh-session-id suffix (§3 Hard-rule: never reuse the original id, or
# the replay would overwrite the original on disk — a data-loss footgun).
REPLAY_SUFFIX = "-replay"


class PinningViolation(ValueError):
    """Aborts execution_replay when a §7.6 condition cannot be satisfied.

    Carries `condition` — one of the closed `PINNING_CONDITIONS` values —
    so a caller (the CLI maps it to exit code 3) can branch on the exact
    pin that failed without string-matching the message.
    """

    def __init__(self, condition: str, message: str) -> None:
        super().__init__(f"[{condition}] {message}")
        self.condition = condition


@dataclass
class ExecutionReplayResult:
    """Outcome of a successful (non-aborted) execution_replay.

    A `PinningViolation` is raised instead of returning when any
    condition is unsatisfiable; a digest *mismatch* is NOT a violation —
    it is reported here with `digest_matches=False` so the caller decides
    how to surface it (the CLI maps it to exit code 4).
    """

    fresh_artifact: Artifact
    fresh_run_dir: Path
    original_digest: str
    fresh_digest: str
    digest_matches: bool
    conditions_checked: List[str]
    conditions_assumed: List[str]
    first_diverging_message_id: Optional[str] = None
    warnings: List[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Deterministic-runtime context (condition #1 + condition #8 pinning)
# ---------------------------------------------------------------------------


@contextmanager
def pinned_runtime(
    fixed_clock: Optional[Callable[[], datetime]] = None,
) -> Iterator[None]:
    """Pin the runtime's non-deterministic sources for the duration of a run.

    Patches, on the scheduler module that actually calls them:

      * `_new_id` — message-id minting (normally `uuid.uuid4().hex`) is
        replaced with a per-context counter (`m000000`, `m000001`, ...).
        Two runs that drive the scheduler through the same deterministic
        control flow (e.g. the same `FakeProvider` script) mint identical
        id sequences, so the `transcript_digest` becomes reproducible.
      * `now_utc_iso` — only when `fixed_clock` is supplied: every message
        `timestamp` (§7.6 condition #8 "wall-clock seed") is sourced from
        `fixed_clock()` instead of the host clock. Patched on both
        `symposium.scheduler.loop` and `symposium.models` so no call site
        escapes the pin.

    Library users who want a *reproducible* original run (one whose
    `execution_replay` can produce a digest-matching fresh artifact)
    should produce it inside this context with the same `fixed_clock`.
    """
    import symposium.models as _models
    from symposium.scheduler import loop as _loop

    saved_new_id = _loop._new_id
    saved_loop_now = _loop.now_utc_iso
    saved_models_now = _models.now_utc_iso

    counter = itertools.count()

    def _det_id() -> str:
        return f"m{next(counter):06d}"

    _loop._new_id = _det_id  # type: ignore[assignment]

    if fixed_clock is not None:

        def _det_now() -> str:
            # Mirror models.now_utc_iso's second-resolution UTC format.
            return fixed_clock().strftime("%Y-%m-%dT%H:%M:%SZ")

        _loop.now_utc_iso = _det_now  # type: ignore[assignment]
        _models.now_utc_iso = _det_now  # type: ignore[assignment]

    try:
        yield
    finally:
        _loop._new_id = saved_new_id  # type: ignore[assignment]
        _loop.now_utc_iso = saved_loop_now  # type: ignore[assignment]
        _models.now_utc_iso = saved_models_now  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def execution_replay(
    run_dir: Path,
    *,
    providers: Mapping[str, ProviderAdapter],
    fixed_clock: Optional[Callable[[], datetime]] = None,
    persona_hashes: Optional[Dict[str, str]] = None,
    fresh_runs_root: Optional[Path] = None,
    assume_cache_cleared: bool = False,
) -> ExecutionReplayResult:
    """Re-run the orchestrator under the §7.6 pinning conditions.

    Args:
        run_dir: a persisted `runs/<session_id>/` directory holding
            `manifest.json`, `config.json`, and `artifact.json`.
        providers: the `agent_id -> ProviderAdapter` map (with an
            optional `"default"` fallback) that `run_session` consumes.
            The caller owns adapter state — pass a *fresh* `FakeProvider`
            so its `invocation_count` starts at zero.
        fixed_clock: pins message timestamps (§7.6 condition #8). Required
            for a live-provider replay; optional (system clock + warning)
            under an all-FakeProvider replay.
        persona_hashes: optional `agent_id -> sha256(JCS(Persona))` map
            captured at original run time (§7.6 condition #9).
        fresh_runs_root: where to persist the fresh run; defaults to the
            parent of `run_dir` (a sibling `<session_id>-replay/`).
        assume_cache_cleared: documents the §7.6 condition #6 assumption
            for live providers (suppresses the cache warning).

    Returns:
        `ExecutionReplayResult` on a completed replay (digest match OR
        mismatch).

    Raises:
        PinningViolation: on the first unsatisfiable condition, before any
            fresh run directory is written.
        FileNotFoundError / ValueError: missing or schema-invalid
            persisted state (surfaced by the CLI as a generic error).
    """
    run_dir = Path(run_dir)
    manifest, config, original_artifact = _load_run(run_dir)

    conditions_checked: List[str] = []
    conditions_assumed: List[str] = []
    warnings: List[str] = []

    agents_all = list(config.agents) + [config.coordinator]
    provider_instances = list(providers.values())
    all_fake = bool(provider_instances) and all(
        isinstance(p, FakeProvider) for p in provider_instances
    )

    # --- #1 runtime ---------------------------------------------------------
    if manifest.producer.name != PRODUCER_NAME or manifest.producer.version != PRODUCER_VERSION:
        raise PinningViolation(
            "runtime",
            f"producer {manifest.producer.name}@{manifest.producer.version} does not match "
            f"this runtime {PRODUCER_NAME}@{PRODUCER_VERSION}; runtime-level logic "
            "(canonicalization, id minting, packet derivation) is part of the reproduction surface",
        )
    conditions_checked.append("runtime")

    # --- #2 adapter / #3 provider ------------------------------------------
    # The MVP registry keys on provider_id (§6.11); adapter-internal version
    # is assumed parity under the same producer.version (condition #1). #2 and
    # #3 are co-decided: provider_id IS the registry key.
    agent_ids = [ac.id for ac in agents_all]
    provider_strings = sorted({ac.provider for ac in agents_all})
    caller_covers_all = bool(providers) and all(
        (aid in providers) or ("default" in providers) for aid in agent_ids
    )
    if not caller_covers_all:
        registry = default_registry()
        for pid in provider_strings:
            if not registry.has(pid):
                raise PinningViolation(
                    "adapter",
                    f"provider {pid!r} is not registered in default_registry() and no "
                    "adapter for it was supplied in the providers map; cannot guarantee "
                    "the same AdapterFactory registration (§6.11)",
                )
    conditions_checked.append("adapter")
    conditions_checked.append("provider")

    # --- #4 model -----------------------------------------------------------
    # Presence is checkable offline; the "identical provider-side snapshot"
    # half is not (assumed under FakeProvider, uncheckable under live).
    for ac in agents_all:
        if not ac.model:
            raise PinningViolation(
                "model", f"agent {ac.id!r} has an empty model identifier"
            )
    conditions_checked.append("model")
    conditions_assumed.append("model")  # provider-side snapshot half

    # --- #5 sampling --------------------------------------------------------
    # The MVP AgentConfig carries no sampling bag; the reference
    # build_provider_request emits sampling=None (§6.2's recommended names are
    # adapter-interpreted). The pinning surface is empty, and we re-use the
    # persisted Config verbatim, so this is vacuously satisfied. Open
    # clarification: a future Config-level sampling bag must be compared
    # byte-for-byte here.
    conditions_checked.append("sampling")

    # --- #6 cache -----------------------------------------------------------
    # Prompt-cache state is uncheckable offline. FakeProvider has no cache;
    # for live providers the caller asserts it via assume_cache_cleared.
    if not all_fake and not assume_cache_cleared:
        warnings.append(
            "cache: prompt-cache state is uncheckable offline (§7.6 #6); pass "
            "assume_cache_cleared=True to assert the cache was cleared/pre-warmed identically"
        )
    conditions_assumed.append("cache")

    # --- #7 tool_env --------------------------------------------------------
    # MVP panel + coordinator run tool-free. tool-handler-binding parity is a
    # v1+ surface; abort on presence rather than fabricate a hashing scheme.
    for ac in agents_all:
        if ac.tools:
            raise PinningViolation(
                "tool_env",
                f"agent {ac.id!r} declares tools; tool-handler-binding parity (§6.4) is not "
                "part of the M5 execution-replay surface (deferred to a v1+ tool-env-pinning module)",
            )
    conditions_checked.append("tool_env")

    # --- #8 wallclock -------------------------------------------------------
    if fixed_clock is not None:
        conditions_checked.append("wallclock")
    elif all_fake:
        warnings.append(
            "wallclock: no fixed_clock supplied; the replay uses the system clock, so message "
            "timestamps (and therefore the transcript_digest) may diverge from the original"
        )
        conditions_checked.append("wallclock")
    else:
        raise PinningViolation(
            "wallclock",
            "live-provider replay requires a fixed_clock to pin message timestamps and retry "
            "jitter (§7.6 #8); none was supplied",
        )

    # --- #9 persona ---------------------------------------------------------
    if persona_hashes is not None:
        for aid, expected in persona_hashes.items():
            ac = next((a for a in agents_all if a.id == aid), None)
            if ac is None:
                raise PinningViolation(
                    "persona", f"persona_hashes references unknown agent {aid!r}"
                )
            if not isinstance(ac.persona_ref, Persona):
                raise PinningViolation(
                    "persona",
                    f"agent {aid!r} persona_ref is an unresolved registry id; cannot hash it",
                )
            actual = _persona_hash(ac.persona_ref)
            if actual != expected:
                raise PinningViolation(
                    "persona",
                    f"resolved persona for agent {aid!r} hashes to {actual}, expected {expected} "
                    "(§7.6 #9: a mutated persona registry breaks replay at an unchanged persona_ref)",
                )
    else:
        for ac in agents_all:
            if not isinstance(ac.persona_ref, Persona):
                raise PinningViolation(
                    "persona",
                    f"agent {ac.id!r} persona_ref is an unresolved registry id {ac.persona_ref!r}; "
                    "M5 requires resolved inline personas, or supply persona_hashes captured at run time",
                )
    conditions_checked.append("persona")

    # --- #10 transcript_prefix ---------------------------------------------
    # M5 implements full replay only; the seed transcript is the empty list,
    # so the prefix condition is vacuously satisfied. Partial replay is a v1+
    # extension and out of scope.
    conditions_checked.append("transcript_prefix")

    # --- All conditions passed: re-run into a fresh directory --------------
    if fresh_runs_root is None:
        fresh_runs_root = run_dir.parent
    fresh_runs_root = Path(fresh_runs_root)

    fresh_session_id = f"{config.session_id}{REPLAY_SUFFIX}"
    validate_session_id(fresh_session_id)  # ValueError → generic error path
    fresh_config = config.model_copy(update={"session_id": fresh_session_id})

    with pinned_runtime(fixed_clock):
        fresh_artifact = run_session(
            fresh_config, dict(providers), runs_root=str(fresh_runs_root)
        )
    fresh_run_dir = fresh_runs_root / fresh_session_id

    original_digest = original_artifact.transcript_digest
    fresh_digest = fresh_artifact.transcript_digest
    digest_matches = original_digest == fresh_digest
    first_div = (
        None
        if digest_matches
        else _first_divergence(
            original_artifact.canonical_transcript, fresh_artifact.canonical_transcript
        )
    )

    return ExecutionReplayResult(
        fresh_artifact=fresh_artifact,
        fresh_run_dir=fresh_run_dir,
        original_digest=original_digest,
        fresh_digest=fresh_digest,
        digest_matches=digest_matches,
        conditions_checked=conditions_checked,
        conditions_assumed=conditions_assumed,
        first_diverging_message_id=first_div,
        warnings=warnings,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _load_run(run_dir: Path) -> tuple[RunManifest, Config, Artifact]:
    """Load + validate the three persisted documents a replay re-executes from."""
    manifest_path = run_dir / "manifest.json"
    config_path = run_dir / "config.json"
    artifact_path = run_dir / "artifact.json"
    for p in (manifest_path, config_path, artifact_path):
        if not p.exists():
            raise FileNotFoundError(f"{p} not found (cannot execution_replay {run_dir})")
    manifest = RunManifest.model_validate(json.loads(manifest_path.read_text()))
    config = Config.model_validate(json.loads(config_path.read_text()))
    artifact = Artifact.model_validate(json.loads(artifact_path.read_text()))
    return manifest, config, artifact


def _persona_hash(persona: Persona) -> str:
    """SHA-256 over the RFC-8785 JCS canonicalization of a resolved Persona."""
    return sha256_hex(canonicalize(persona.model_dump(mode="json", exclude_none=True)))


def _first_divergence(original: List, fresh: List) -> Optional[str]:
    """First diverging message id, or a length-mismatch marker, or None.

    `None` means the canonical_transcripts are element-wise identical even
    though the digests differ — e.g. the original's stored `transcript_digest`
    field was corrupted (§8.7). The caller surfaces that distinctly.
    """
    if len(original) != len(fresh):
        return "len(transcript) mismatch"
    for orig_msg, fresh_msg in zip(original, fresh):
        if orig_msg.model_dump(mode="json", exclude_none=True) != fresh_msg.model_dump(
            mode="json", exclude_none=True
        ):
            return orig_msg.id
    return None

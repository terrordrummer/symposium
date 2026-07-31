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
  3. Only after every condition passes, re-run `run_session(...)` with the
     runtime's two non-deterministic-but-digest-bearing fields pinned (see
     "Message-id and timestamp pinning" below) into a *fresh* run directory
     (`<session_id>-replay/`), and compare the fresh `transcript_digest`
     against the original. The digest match/mismatch is reported, never
     raised — the caller decides how to surface it.

Message-id and timestamp pinning (open clarification — no spec edits; see
Hard-rule §3 of the milestone):

  Two `Message` fields feed `transcript_digest` (§7.7) but are NOT supplied
  by the provider: `Message.id` (the reference scheduler mints it with
  `uuid.uuid4()` via `symposium.scheduler.loop._new_id`) and
  `Message.timestamp` (wall-clock via `now_utc_iso`). §7.6's ten-condition
  enum names only the wall-clock seed (#8), not id minting — §9.4.1 calls
  the deterministic-id allocator a *golden-test* addition and explicitly
  notes that the §7.6 digest comparison is "broken" when two runs each mint
  ids their own way. To make the §7.6 comparison *meaningful* without a
  spec edit, M5 pins BOTH fields to the values recorded in the original
  Artifact: the fresh run dispatches in the same deterministic order (same
  Config + same deterministic provider), so feeding back the recorded id
  sequence (a deterministic allocator per §9.4.1's "byte-identical ids for
  byte-identical dispatch sequences") and the recorded timestamp sequence
  (the §7.6 #8 "fixed clock source for replay") reconstructs each message
  byte-for-byte iff the re-execution genuinely reproduces it. A re-execution
  that diverges (different content, count, or routing) desyncs from the
  recorded sequences and yields `digest_matches=False` — never a spurious
  match. This is §7.8's "replayable ≠ reproducible" made operational, and
  it lets `execution_replay` work on an ordinary persisted run (random ids +
  wall-clock), not only on runs recorded under a pinned harness. A caller may
  override the timestamp source with `fixed_clock`.

Further open clarifications:

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

import hashlib
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

# Second-resolution UTC format, mirroring symposium.models.now_utc_iso.
_ISO_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


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
    *,
    id_source: Optional[Callable[[], str]] = None,
    clock: Optional[Callable[[], str]] = None,
) -> Iterator[None]:
    """Pin the runtime's two non-deterministic, digest-bearing sources.

    Patches, on the scheduler module that actually calls them:

      * `_new_id` — message-id minting (normally `uuid.uuid4().hex`) is
        replaced by `id_source`. When `id_source` is None it defaults to a
        sequential `msg-NNN` allocator (the §9.4.1 RECOMMENDED canonical
        scheme), so a standalone "record a reproducible run" use case
        works without arguments. `execution_replay` instead feeds the
        recorded id sequence (see module docstring).
      * `now_utc_iso` — only when `clock` is supplied: every message
        `timestamp` (§7.6 condition #8 "wall-clock seed") is sourced from
        `clock()` (an ISO-8601 string). Patched on both
        `symposium.scheduler.loop` and `symposium.models` so no call site
        escapes the pin (per §7.6 #8's "any wall-clock-reading function").

    Both `id_source` and `clock` are plain zero-arg callables returning
    strings; the runtime calls them once per appended message in dispatch
    order (`now_utc_iso` is additionally called once for `Artifact.ended_at`,
    which does not enter the digest).
    """
    import symposium.models as _models
    from symposium.scheduler import loop as _loop

    if id_source is None:
        id_source = _sequential_id_source()

    saved_new_id = _loop._new_id
    saved_loop_now = _loop.now_utc_iso
    saved_models_now = _models.now_utc_iso

    _loop._new_id = id_source  # type: ignore[assignment]
    if clock is not None:
        _loop.now_utc_iso = clock  # type: ignore[assignment]
        _models.now_utc_iso = clock  # type: ignore[assignment]
    try:
        yield
    finally:
        _loop._new_id = saved_new_id  # type: ignore[assignment]
        _loop.now_utc_iso = saved_loop_now  # type: ignore[assignment]
        _models.now_utc_iso = saved_models_now  # type: ignore[assignment]


def _sequential_id_source(prefix: str = "msg-", width: int = 3) -> Callable[[], str]:
    """The §9.4.1 RECOMMENDED canonical id scheme: `msg-000`, `msg-001`, …"""
    counter = itertools.count()
    return lambda: f"{prefix}{next(counter):0{width}d}"


def _recorded_id_source(artifact: Artifact) -> Callable[[], str]:
    """Replay the original's exact id sequence in dispatch order.

    Falls back to a distinct `replay-extra-NNN` id once the recording is
    exhausted (only reached if the re-execution allocates *more* messages
    than the original — a genuine divergence that will surface as a digest
    mismatch, never a spurious match)."""
    it = iter([m.id for m in artifact.canonical_transcript])
    overflow = itertools.count()

    def _next() -> str:
        try:
            return next(it)
        except StopIteration:
            return f"replay-extra-{next(overflow):06d}"

    return _next


def _recorded_clock_source(artifact: Artifact) -> Callable[[], str]:
    """Replay the original's exact timestamp sequence (§7.6 #8 fixed clock).

    Clamps to the last recorded timestamp once exhausted — the runtime calls
    the clock once more for `Artifact.ended_at` after the final message, and
    that value does not enter the `transcript_digest`."""
    stamps = [m.timestamp for m in artifact.canonical_transcript]
    it = iter(stamps)
    state = {"last": stamps[-1]}

    def _next() -> str:
        try:
            state["last"] = next(it)
        except StopIteration:
            pass
        return state["last"]

    return _next


def _fixed_clock_source(fixed_clock: Callable[[], datetime]) -> Callable[[], str]:
    """Adapt a caller's `() -> datetime` clock to the ISO-string the runtime emits."""

    def _next() -> str:
        v = fixed_clock()
        return v if isinstance(v, str) else v.strftime(_ISO_FORMAT)

    return _next


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
        fixed_clock: OPTIONAL override for the message-timestamp source
            (§7.6 condition #8). When omitted, timestamps are replayed
            from the original Artifact; when supplied, `fixed_clock()` is
            used instead. Required for a live-provider replay (an
            all-FakeProvider replay may omit it).
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

    # Per-agent provider IDENTITY check: when the caller supplies adapters
    # directly, each adapter's `name` MUST equal the AgentConfig.provider.
    # Without this guard, an artifact produced by `openai` could be silently
    # "replayed" against a FakeProvider — a §7.6 pinning hole that defeats
    # the audit/reproducibility intent of the replay surface.
    #
    # Exception: an all-FakeProvider replay against an artifact that was
    # itself produced under all-fake providers is the canonical golden-test
    # path and is allowed even when adapter.name="fake" diverges from a
    # vendor-shaped `provider` string — the adapter is acting as a captured
    # snapshot of vendor responses, not as the vendor.
    artifact_all_fake = all(ac.provider == "fake" for ac in agents_all)
    if providers and not (all_fake and artifact_all_fake):
        for ac in agents_all:
            adapter = providers.get(ac.id) or providers.get("default")
            if adapter is None:
                continue  # registry-resolution branch (above) covers this
            adapter_name = getattr(adapter, "name", None)
            if adapter_name != ac.provider:
                raise PinningViolation(
                    "provider",
                    f"agent {ac.id!r} declares provider={ac.provider!r} but the "
                    f"supplied adapter reports name={adapter_name!r}; replay would "
                    "execute against a different provider than the original run "
                    "(§7.6 condition #3 — provider identity must match)",
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
    # Decide the message-timestamp source. fixed_clock overrides; otherwise we
    # replay the recorded timestamps (a §7.6 #8 "fixed clock source for
    # replay"). Either way the condition is only PARTIALLY pinned: the
    # runtime's cap / soft-deadline decisions read the live `time.monotonic`,
    # which no clock source reaches — so the budget-decision half is recorded
    # as an assumption with a warning (a wallclock-terminated original cannot
    # be replay-matched). A live provider with no fixed_clock cannot be
    # pinned at all (its fresh content would not match the recording anyway)
    # → abort.
    if fixed_clock is not None:
        clock_source: Callable[[], str] = _fixed_clock_source(fixed_clock)
        conditions_checked.append("wallclock")  # message-timestamp half pinned
    elif all_fake:
        clock_source = _recorded_clock_source(original_artifact)
        warnings.append(
            "wallclock: no explicit fixed_clock; message timestamps are replayed from the "
            "recorded transcript (§7.6 #8 fixed clock source). Pass fixed_clock to override"
        )
    else:
        raise PinningViolation(
            "wallclock",
            "live-provider replay requires a fixed_clock to pin message timestamps and retry "
            "jitter (§7.6 #8); none was supplied",
        )
    warnings.append(
        "wallclock: hard-cap and soft-deadline decisions read the live monotonic clock "
        "and are NOT pinned (§7.6 #8, budget-decision half); a run terminated by the "
        "wall-clock cap cannot be replay-matched"
    )
    conditions_assumed.append("wallclock")  # budget-decision half

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
            actual = persona_hash(ac.persona_ref)
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

    # Clamp so the derived id stays within the §7.1 64-char session_id limit
    # even when the original id already uses most of it (a 57+-char original
    # would otherwise push "<sid>-replay" past the limit and raise a raw
    # ValueError after all conditions passed). A plain prefix truncation
    # would map two long ids sharing a prefix onto the same replay dir, so
    # the clamped form embeds a digest of the full id to keep it unique.
    max_stem = 64 - len(REPLAY_SUFFIX)
    stem = config.session_id
    if len(stem) > max_stem:
        digest = hashlib.sha256(stem.encode("utf-8")).hexdigest()[:8]
        stem = f"{stem[: max_stem - 9]}-{digest}"
    fresh_session_id = f"{stem}{REPLAY_SUFFIX}"
    validate_session_id(fresh_session_id)  # ValueError → generic error path
    fresh_config = config.model_copy(update={"session_id": fresh_session_id})

    id_source = _recorded_id_source(original_artifact)
    with pinned_runtime(id_source=id_source, clock=clock_source):
        fresh_artifact = run_session(
            fresh_config,
            dict(providers),
            runs_root=str(fresh_runs_root),
            # §7.6: keep the retry-backoff RNG seeded from the ORIGINAL
            # session_id, not from the `-replay`-suffixed fresh id. Otherwise
            # the original and the replay sleep different amounts, which can
            # flip a wallclock-cap decision and diverge the digest.
            rng_seed=config.session_id,
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


def persona_hash(persona: Persona) -> str:
    """SHA-256 over the RFC-8785 JCS canonicalization of a resolved Persona.

    Public so callers can compute the `persona_hashes` map they pass back to
    `execution_replay` (§7.6 condition #9) the same way the check does."""
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

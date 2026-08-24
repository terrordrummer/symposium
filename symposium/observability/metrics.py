"""§7.9 MVP observability metric set — offline computation from a persisted Artifact.

The metric computer is a pure function over an `Artifact`: no clock, no
filesystem, no RNG, no live event stream. The §7.10 v1+ SHOULD-set
(`role_purity_score`, `disagreement_frequency`, `interaction_graph`,
per-invocation retry counts, the live `observability_event` stream) is
formally deferred and out of scope here.

Each metric reproduces a row of the §7.9 MUST-table. Where a row's
"Data source" column is a closed structural derivation (e.g. per-agent
token sums), the implementation is direct. The only row whose §7.9
prose covers multiple defensible interpretations is the
**deferred-queue length max** (§4.6): the open clarification and the
chosen heuristic are documented inline next to that derivation.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

from symposium.models import Artifact, TerminationOutcome, TerminationReason
from symposium.storage.atomic import atomic_write_text
from symposium.storage.digest import serialize_pretty


# ---------------------------------------------------------------------------
# Public errors
# ---------------------------------------------------------------------------


class MetricsConsistencyError(ValueError):
    """Raised when the per-agent rollup disagrees with the Artifact's
    authoritative `cumulative_usage`, or when a panel_contraction
    references an agent not in the session config, or when a
    `schema_failure` annotation appears on a non-primary/branch turn.
    """


# ---------------------------------------------------------------------------
# Output model
# ---------------------------------------------------------------------------


def _strict() -> ConfigDict:
    return ConfigDict(extra="forbid")


class TokenBreakdown(BaseModel):
    model_config = _strict()
    prompt_tokens: int = Field(ge=0)
    completion_tokens: int = Field(ge=0)
    total_tokens: int = Field(ge=0)


class CostBreakdown(BaseModel):
    model_config = _strict()
    cost_usd: float = Field(ge=0.0)


class LatencySample(BaseModel):
    model_config = _strict()
    message_id: str = Field(min_length=1)
    speaker: str = Field(min_length=1)
    latency_seconds: float = Field(ge=0.0)


class ParticipationCount(BaseModel):
    model_config = _strict()
    round: int = Field(ge=0)
    speaker: str = Field(min_length=1)
    count: int = Field(ge=1)


class PanelContractionCount(BaseModel):
    model_config = _strict()
    agent_id: str = Field(min_length=1)
    reason: Literal["provider_unrecoverable", "schema_error"]
    count: int = Field(ge=1)


class ObservabilityMetrics(BaseModel):
    model_config = _strict()

    session_id: str = Field(min_length=1)
    schema_version: Literal["1.0.0"] = "1.0.0"
    transcript_digest: str = Field(pattern=r"^[0-9a-f]{64}$")

    tokens_per_agent: Dict[str, TokenBreakdown]
    tokens_cumulative: TokenBreakdown
    tokens_per_provider_model: Dict[str, TokenBreakdown]
    cost_per_agent: Dict[str, CostBreakdown]
    cost_cumulative: CostBreakdown
    cost_per_provider_model: Dict[str, CostBreakdown]
    latency_per_invocation: List[LatencySample]
    participation_per_round: List[ParticipationCount]
    branch_depth_max: int = Field(ge=0)
    deferred_queue_length_max: int = Field(ge=0)
    schema_failure_count_per_agent: Dict[str, int]
    panel_contraction_count: List[PanelContractionCount]
    outcome_kind: Literal["synthesis", "termination"]
    termination_reason: Optional[TerminationReason] = None
    usage_estimated: bool


# ---------------------------------------------------------------------------
# Computation
# ---------------------------------------------------------------------------


_ROUND_COST_DECIMALS = 6

# Absolute tolerance for the cumulative-cost parity check: one unit in the
# last reported decimal. Exact equality of rounded floats would reject a
# legitimate artifact whose raw sum sits on a rounding boundary.
_COST_PARITY_TOLERANCE = 1e-6


def _round_cost(x: float) -> float:
    return round(x, _ROUND_COST_DECIMALS)


def _parse_ts(ts: str) -> float:
    """Parse an ISO-8601 timestamp (with trailing Z) to POSIX seconds.

    The §7.9 latency derivation only uses differences between adjacent
    timestamps, so the absolute epoch choice is irrelevant — but we use
    `datetime.fromisoformat` after normalizing the `Z` so the parsing
    matches the persisted §5.4 timestamp format.
    """
    if ts.endswith("Z"):
        ts = ts[:-1] + "+00:00"
    return datetime.fromisoformat(ts).timestamp()


def compute_metrics(artifact: Artifact) -> ObservabilityMetrics:
    """Compute the §7.9 MVP observability set from a persisted Artifact.

    Pure function: no I/O, no clock, no RNG. Two calls on the same input
    produce equal output. The Artifact is treated as read-only.

    Cumulative-usage parity is the LAST check inside this function so a
    debugger can inspect the partial rollups when the invariant fires.
    """
    transcript = artifact.canonical_transcript

    # ---- Agent → (provider, model) lookup (joined per §7.9 row #3) -------
    agent_provider_model: Dict[str, str] = {}
    valid_agent_ids = set()
    for ac in artifact.config.agents:
        agent_provider_model[ac.id] = f"{ac.provider}/{ac.model}"
        valid_agent_ids.add(ac.id)
    coord = artifact.config.coordinator
    agent_provider_model[coord.id] = f"{coord.provider}/{coord.model}"
    valid_agent_ids.add(coord.id)

    # ---- Per-agent / per-(provider, model) accumulators ------------------
    tok_agent: Dict[str, Dict[str, int]] = defaultdict(
        lambda: {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    )
    cost_agent: Dict[str, float] = defaultdict(float)
    tok_pm: Dict[str, Dict[str, int]] = defaultdict(
        lambda: {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    )
    cost_pm: Dict[str, float] = defaultdict(float)

    # ---- Cumulative tracker (the cross-check, not a substitute) ----------
    cum = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    cum_cost = 0.0
    usage_estimated = False

    # ---- Latency, participation, branch depth, schema-failure ------------
    latency: List[LatencySample] = []
    prev_ts_seconds: Optional[float] = None
    participation: Dict[int, Dict[str, int]] = defaultdict(lambda: defaultdict(int))
    branch_depth_max = 0
    schema_failure_per_agent: Dict[str, int] = defaultdict(int)

    # ---- Panel contractions ---------------------------------------------
    contraction_counts: Dict[tuple, int] = defaultdict(int)

    # ---- Deferred-queue derivation (see _derive_deferred_queue_max) ------
    # Tracking dictionaries populated during the single pass and consumed
    # by the helper below. We capture per-round structural info:
    #   - direct_requests count per primary_turn id (used as "N")
    #   - dropped_deferred count per primary_turn id (used as "D")
    #   - same-round branch_turns per parent primary_turn id (used as "M")
    #   - cross-round drain branch_turns by round of drain
    #   - per-round ordered primary_turns and end-of-round boundary
    primary_meta: Dict[str, Dict[str, int]] = {}
    same_round_branches: Dict[str, int] = defaultdict(int)
    cross_round_drains_by_round: Dict[int, int] = defaultdict(int)
    primary_round_by_id: Dict[str, int] = {}
    rounds_seen: List[int] = []
    primaries_per_round: Dict[int, List[str]] = defaultdict(list)

    # ---- Single pass over the transcript ---------------------------------
    for msg in transcript:
        u = msg.usage
        if u.estimated:
            usage_estimated = True

        # Cumulative tracker (every message, including problem_statement)
        cum["prompt_tokens"] += u.prompt_tokens
        cum["completion_tokens"] += u.completion_tokens
        cum["total_tokens"] += u.total_tokens
        cum_cost += u.cost_usd

        # The `user:*` originator and the `runtime` panel_contraction
        # speaker are not agents — skip the per-agent / per-(provider,
        # model) rollup for them so the join in §7.9 row #3 stays clean.
        is_panel_or_coord = msg.speaker in valid_agent_ids
        if is_panel_or_coord:
            t = tok_agent[msg.speaker]
            t["prompt_tokens"] += u.prompt_tokens
            t["completion_tokens"] += u.completion_tokens
            t["total_tokens"] += u.total_tokens
            cost_agent[msg.speaker] += u.cost_usd

            pm_key = agent_provider_model[msg.speaker]
            tp = tok_pm[pm_key]
            tp["prompt_tokens"] += u.prompt_tokens
            tp["completion_tokens"] += u.completion_tokens
            tp["total_tokens"] += u.total_tokens
            cost_pm[pm_key] += u.cost_usd

        # Latency: timestamp[i] − timestamp[i-1]. §7.9 best-effort prose.
        ts_secs = _parse_ts(msg.timestamp)
        if prev_ts_seconds is not None:
            latency.append(
                LatencySample(
                    message_id=msg.id,
                    speaker=msg.speaker,
                    latency_seconds=max(0.0, ts_secs - prev_ts_seconds),
                )
            )
        prev_ts_seconds = ts_secs

        # Participation: only primary_turn + branch_turn count (§7.9 row #8)
        if msg.type in ("primary_turn", "branch_turn"):
            participation[msg.round][msg.speaker] += 1

        # Branch depth max
        if msg.branch_depth > branch_depth_max:
            branch_depth_max = msg.branch_depth

        # Schema-failure annotation count (§7.9 row #11): only allowed on
        # primary/branch turns per §5.4; raise if it appears elsewhere.
        if msg.schema_failure:
            if msg.type not in ("primary_turn", "branch_turn"):
                raise MetricsConsistencyError(
                    f"schema_failure annotation on disallowed message type "
                    f"{msg.type!r} (§5.4 permits only primary_turn / branch_turn); "
                    f"message_id={msg.id}"
                )
            schema_failure_per_agent[msg.speaker] += len(msg.schema_failure)

        # Panel-contraction grouping (§7.9 row #12)
        if msg.type == "panel_contraction":
            c = msg.content
            agent_id = c["agent_id"] if isinstance(c, dict) else c.agent_id
            reason = c["reason"] if isinstance(c, dict) else c.reason
            if agent_id not in valid_agent_ids:
                raise MetricsConsistencyError(
                    f"panel_contraction references unknown agent_id={agent_id!r} "
                    f"(not in Artifact.config.agents[] ∪ {{coordinator}}); "
                    f"message_id={msg.id}"
                )
            contraction_counts[(agent_id, reason)] += 1

        # Deferred-queue derivation inputs (see helper for the heuristic)
        if msg.type == "primary_turn":
            content = msg.content
            if isinstance(content, dict):
                direct_requests = content.get("direct_requests") or []
            else:
                direct_requests = content.direct_requests or []
            dropped = msg.dropped_deferred or []
            primary_meta[msg.id] = {
                "N": len(direct_requests),
                "D": len(dropped),
            }
            primary_round_by_id[msg.id] = msg.round
            primaries_per_round[msg.round].append(msg.id)
            if msg.round not in rounds_seen:
                rounds_seen.append(msg.round)
        elif msg.type == "branch_turn":
            parent_id = msg.parent_id
            if parent_id and parent_id in primary_round_by_id:
                parent_round = primary_round_by_id[parent_id]
                if parent_round == msg.round:
                    same_round_branches[parent_id] += 1
                else:
                    cross_round_drains_by_round[msg.round] += 1
            # If parent_id is missing or unknown, we cannot classify
            # this branch_turn structurally — fall through.

    # ---- Deferred-queue length max --------------------------------------
    deferred_queue_length_max = _derive_deferred_queue_max(
        rounds_seen=rounds_seen,
        primaries_per_round=primaries_per_round,
        primary_meta=primary_meta,
        same_round_branches=same_round_branches,
        cross_round_drains_by_round=cross_round_drains_by_round,
        max_drains_per_round=artifact.config.runtime.max_deferred_drains_per_round,
    )

    # ---- Build typed output models ---------------------------------------
    tokens_per_agent = {
        a: TokenBreakdown(**v) for a, v in sorted(tok_agent.items())
    }
    tokens_per_provider_model = {
        k: TokenBreakdown(**v) for k, v in sorted(tok_pm.items())
    }
    cost_per_agent = {
        a: CostBreakdown(cost_usd=_round_cost(c))
        for a, c in sorted(cost_agent.items())
    }
    cost_per_provider_model = {
        k: CostBreakdown(cost_usd=_round_cost(c))
        for k, c in sorted(cost_pm.items())
    }

    tokens_cumulative = TokenBreakdown(**cum)
    cost_cumulative = CostBreakdown(cost_usd=_round_cost(cum_cost))

    participation_per_round: List[ParticipationCount] = []
    for r in sorted(participation.keys()):
        for sp in sorted(participation[r].keys()):
            participation_per_round.append(
                ParticipationCount(round=r, speaker=sp, count=participation[r][sp])
            )

    panel_contraction_count = [
        PanelContractionCount(agent_id=a, reason=r, count=c)
        for (a, r), c in sorted(contraction_counts.items())
    ]

    # Outcome + termination reason
    outcome = artifact.outcome
    outcome_kind = outcome.kind
    termination_reason: Optional[TerminationReason] = None
    if isinstance(outcome, TerminationOutcome):
        termination_reason = outcome.termination_artifact.reason

    metrics = ObservabilityMetrics(
        session_id=artifact.session_id,
        transcript_digest=artifact.transcript_digest,
        tokens_per_agent=tokens_per_agent,
        tokens_cumulative=tokens_cumulative,
        tokens_per_provider_model=tokens_per_provider_model,
        cost_per_agent=cost_per_agent,
        cost_cumulative=cost_cumulative,
        cost_per_provider_model=cost_per_provider_model,
        latency_per_invocation=latency,
        participation_per_round=participation_per_round,
        branch_depth_max=branch_depth_max,
        deferred_queue_length_max=deferred_queue_length_max,
        schema_failure_count_per_agent=dict(sorted(schema_failure_per_agent.items())),
        panel_contraction_count=panel_contraction_count,
        outcome_kind=outcome_kind,
        termination_reason=termination_reason,
        usage_estimated=usage_estimated,
    )

    # ---- Last: cumulative-usage parity invariant (§7.9 cross-check) -----
    # Token counts are integers and must match exactly; the cost is a float
    # sum, so it gets an absolute tolerance rather than rounded equality.
    auth = artifact.cumulative_usage
    if (
        cum["prompt_tokens"] != auth.prompt_tokens
        or cum["completion_tokens"] != auth.completion_tokens
        or cum["total_tokens"] != auth.total_tokens
        or abs(cum_cost - auth.cost_usd) > _COST_PARITY_TOLERANCE
    ):
        raise MetricsConsistencyError(
            "cumulative_usage rollup disagrees with Artifact.cumulative_usage: "
            f"computed={{prompt={cum['prompt_tokens']}, completion={cum['completion_tokens']}, "
            f"total={cum['total_tokens']}, cost_usd={_round_cost(cum_cost)}}} vs "
            f"artifact={{prompt={auth.prompt_tokens}, completion={auth.completion_tokens}, "
            f"total={auth.total_tokens}, cost_usd={_round_cost(auth.cost_usd)}}}"
        )

    return metrics


def _derive_deferred_queue_max(
    *,
    rounds_seen: List[int],
    primaries_per_round: Dict[int, List[str]],
    primary_meta: Dict[str, Dict[str, int]],
    same_round_branches: Dict[str, int],
    cross_round_drains_by_round: Dict[int, int],
    max_drains_per_round: int = 1,
) -> int:
    """Heuristic derivation of `deferred_queue_length_max` from a transcript.

    Open clarification (§7.9): the prose lists three input sources —
    `dropped_deferred` annotations, per-round drain counts from
    `coordination_turn.content`, and per-round dispatch order from
    `(round, turn_index, parent_id)` — but does not pin the queue
    accounting precisely. The interpretation below is the most
    structurally defensible one and consumes only data that survives
    canonicalization in the persisted Artifact.

    Per §4.6, a `primary_turn`'s `direct_requests` either:
      (a) dispatch immediately in the same round as a `branch_turn`
          whose `parent_id` points to the originating primary_turn, or
      (b) enqueue onto the FIFO `deferred_request` queue, which the
          runtime drains at most once per round-open as a `branch_turn`
          in a later round whose `parent_id` references an earlier-round
          primary_turn, or
      (c) drop into a `dropped_deferred` annotation on the originating
          primary_turn when the queue overflow cap (§4.6) fires.

    For each `primary_turn` M:
      - N(M) = `len(M.content.direct_requests)`
      - M(M) = number of same-round `branch_turn` messages with
        `parent_id = M.id`
      - D(M) = `len(M.dropped_deferred)`
      - enqueued(M) = max(N − M − D, 0)

    Round-by-round simulation (in round order, with primary_turns within
    a round processed in transcript order — §4.10 invariant 7):
      - at round-open R > first round: drain `min(1, queue)` items
        (the spec default is one drain per round-open per §4.6, and the
        observed cross-round drains in the transcript ratify this)
      - for each primary_turn M in R: queue += enqueued(M)
      - track `queue_length_max` after each enqueue and each drain step

    The cross-round drain count is a *check*: if the simulation says
    "drain 1" but no cross-round branch appears in round R for queue>0,
    the derivation still proceeds (the drain may have been suppressed by
    `max_deferred_drains_per_round=0` or an empty-queue race); we never
    over-drain. If extra cross-round drains appear, we honour them up
    to the queue contents.

    The §7.9 row says **max** queue length, so the peak across all
    observation points wins. Returns 0 when no deferred behavior is
    structurally observable.
    """
    queue = 0
    queue_max = 0
    sorted_rounds = sorted(rounds_seen)
    for idx, r in enumerate(sorted_rounds):
        # Drain at round open (skip the first deliberation round — the
        # queue is empty before any primary_turn fires). Observed cross-
        # round drains are transcript evidence and win outright: honour
        # them up to the queue contents, even past the configured cap.
        # Only when the round shows no drains do we fall back to
        # simulating the runtime cap (`max_deferred_drains_per_round`,
        # no drain at all when it is 0). Never drain below empty.
        if idx > 0 and queue > 0:
            drains_observed = cross_round_drains_by_round.get(r, 0)
            if drains_observed:
                drains = min(queue, drains_observed)
            else:
                drains = min(queue, max_drains_per_round)
            queue -= drains
        for primary_id in primaries_per_round[r]:
            meta = primary_meta[primary_id]
            enqueued = max(meta["N"] - same_round_branches.get(primary_id, 0) - meta["D"], 0)
            queue += enqueued
            if queue > queue_max:
                queue_max = queue
    return queue_max


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def write_metrics(run_dir: Path, metrics: ObservabilityMetrics) -> Path:
    """Atomically write `<run_dir>/metrics.json`.

    Same temp-file → fsync → rename pattern as §7.4 for the Artifact
    (shared `storage.atomic.atomic_write_text`). The on-disk form is the
    sorted-keys pretty JSON used by every other artifact; there is no
    v1.0.0 schema for the observability output (§7.10 defers schema
    publication to v1+).
    """
    run_dir = Path(run_dir)
    out_path = run_dir / "metrics.json"
    payload = metrics.model_dump(mode="json", exclude_none=False)
    atomic_write_text(out_path, serialize_pretty(payload))
    return out_path

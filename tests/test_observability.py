"""Tests for §7.9 MVP observability metrics (`symposium.observability`)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from symposium.cli.main import main as cli_main
from symposium.models import Artifact
from symposium.observability import (
    MetricsConsistencyError,
    ObservabilityMetrics,
    compute_metrics,
    write_metrics,
)
from symposium.observability.metrics import _derive_deferred_queue_max
from symposium.providers import FakeProvider
from symposium.scheduler import run_session

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = REPO_ROOT / "docs" / "schemas" / "v1.0.0" / "examples"
WORKED_PATH = EXAMPLES / "worked_example_artifact.json"
BUDGET_BREACH_PATH = EXAMPLES / "budget_breach_artifact.json"


def _load(path: Path) -> Artifact:
    return Artifact.model_validate(json.loads(path.read_text()))


# ---------------------------------------------------------------------------
# 1. Happy path — worked example
# ---------------------------------------------------------------------------


def test_worked_example_happy_path():
    artifact = _load(WORKED_PATH)
    metrics = compute_metrics(artifact)

    assert metrics.session_id == "demo-2026-05-25-001"
    assert metrics.outcome_kind == "synthesis"
    assert metrics.termination_reason is None
    assert metrics.usage_estimated is False

    # Cumulative rollup matches the authoritative field exactly.
    assert metrics.tokens_cumulative.prompt_tokens == artifact.cumulative_usage.prompt_tokens
    assert metrics.tokens_cumulative.completion_tokens == artifact.cumulative_usage.completion_tokens
    assert metrics.tokens_cumulative.total_tokens == artifact.cumulative_usage.total_tokens
    assert metrics.cost_cumulative.cost_usd == pytest.approx(
        artifact.cumulative_usage.cost_usd, abs=1e-6
    )

    # Every panel + coordinator agent has a row in tokens_per_agent.
    expected_agents = {a.id for a in artifact.config.agents} | {artifact.config.coordinator.id}
    assert set(metrics.tokens_per_agent.keys()) == expected_agents
    assert set(metrics.cost_per_agent.keys()) == expected_agents

    # (provider, model) rollup: two distinct providers in worked example.
    assert set(metrics.tokens_per_provider_model.keys()) == {
        "provider_a/reasoning_model",
        "provider_b/reasoning_model",
    }

    # Latency: one fewer sample than total messages.
    assert len(metrics.latency_per_invocation) == len(artifact.canonical_transcript) - 1

    assert metrics.branch_depth_max == 1
    # Worked example: msg-006 (round 2 critic branch) is a queue drain
    # of msg-001's deferred direct_request — peak queue size = 1.
    assert metrics.deferred_queue_length_max == 1
    assert metrics.panel_contraction_count == []
    assert metrics.schema_failure_count_per_agent == {}


# ---------------------------------------------------------------------------
# 2. Termination path — budget breach
# ---------------------------------------------------------------------------


def test_budget_breach_termination_path():
    artifact = _load(BUDGET_BREACH_PATH)
    metrics = compute_metrics(artifact)

    assert metrics.outcome_kind == "termination"
    assert metrics.termination_reason == "budget_exceeded"

    # Cumulative parity still holds on termination paths.
    assert metrics.tokens_cumulative.total_tokens == artifact.cumulative_usage.total_tokens
    assert metrics.cost_cumulative.cost_usd == pytest.approx(
        artifact.cumulative_usage.cost_usd, abs=1e-6
    )

    # No branches in the budget-breach example.
    assert metrics.branch_depth_max == 0
    assert metrics.deferred_queue_length_max == 0


# ---------------------------------------------------------------------------
# 3. Cumulative-usage consistency invariant
# ---------------------------------------------------------------------------


def test_cumulative_usage_consistency_invariant_fires():
    """A deliberately-corrupted cumulative_usage must raise."""
    raw = json.loads(WORKED_PATH.read_text())
    raw["cumulative_usage"]["total_tokens"] += 1  # off by 1
    artifact = Artifact.model_validate(raw)

    with pytest.raises(MetricsConsistencyError) as excinfo:
        compute_metrics(artifact)
    assert "cumulative_usage" in str(excinfo.value)


def test_cumulative_cost_rounding_boundary_tolerated():
    """A sub-tolerance cost delta (a raw sum sitting on a rounding boundary)
    must NOT trip the parity invariant — the check uses abs(diff) <= 1e-6,
    not exact equality of rounded floats."""
    raw = json.loads(WORKED_PATH.read_text())
    raw["cumulative_usage"]["cost_usd"] += 4e-7  # within tolerance
    artifact = Artifact.model_validate(raw)
    metrics = compute_metrics(artifact)  # no MetricsConsistencyError
    assert metrics.cost_cumulative.cost_usd == pytest.approx(
        artifact.cumulative_usage.cost_usd, abs=1e-6
    )


def test_cumulative_cost_divergence_beyond_tolerance_raises():
    raw = json.loads(WORKED_PATH.read_text())
    raw["cumulative_usage"]["cost_usd"] += 1e-4  # well past tolerance
    artifact = Artifact.model_validate(raw)
    with pytest.raises(MetricsConsistencyError) as excinfo:
        compute_metrics(artifact)
    assert "cumulative_usage" in str(excinfo.value)


# ---------------------------------------------------------------------------
# 4. Per-(provider, model) aggregation
# ---------------------------------------------------------------------------


def test_per_provider_model_aggregation():
    artifact = _load(WORKED_PATH)
    metrics = compute_metrics(artifact)

    # provider_a holds logician + critic; provider_b holds researcher + coordinator.
    a_tokens = metrics.tokens_per_provider_model["provider_a/reasoning_model"].total_tokens
    b_tokens = metrics.tokens_per_provider_model["provider_b/reasoning_model"].total_tokens
    expected_a = (
        metrics.tokens_per_agent["logician"].total_tokens
        + metrics.tokens_per_agent["critic"].total_tokens
    )
    expected_b = (
        metrics.tokens_per_agent["researcher"].total_tokens
        + metrics.tokens_per_agent["coordinator"].total_tokens
    )
    assert a_tokens == expected_a
    assert b_tokens == expected_b

    # Sum of per-(provider, model) cost equals cumulative cost.
    assert sum(
        c.cost_usd for c in metrics.cost_per_provider_model.values()
    ) == pytest.approx(metrics.cost_cumulative.cost_usd, abs=1e-6)


# ---------------------------------------------------------------------------
# 5. Latency derivation
# ---------------------------------------------------------------------------


def test_latency_derivation_is_timestamp_diff():
    """Per §7.9: latency = timestamp[i] − timestamp[i-1]."""
    raw = json.loads(WORKED_PATH.read_text())
    artifact = Artifact.model_validate(raw)
    metrics = compute_metrics(artifact)

    transcript = artifact.canonical_transcript
    # msg-001 (10:00:05) − msg-000 (10:00:00) = 5s.
    first = metrics.latency_per_invocation[0]
    assert first.message_id == "msg-001"
    assert first.speaker == "logician"
    assert first.latency_seconds == pytest.approx(5.0, abs=1e-6)

    # msg-005 (10:00:31) − msg-004 (10:00:25) = 6s.
    coord_idx = next(
        i for i, s in enumerate(metrics.latency_per_invocation) if s.message_id == "msg-005"
    )
    assert metrics.latency_per_invocation[coord_idx].latency_seconds == pytest.approx(6.0, abs=1e-6)

    # One fewer sample than messages.
    assert len(metrics.latency_per_invocation) == len(transcript) - 1


# ---------------------------------------------------------------------------
# 6. usage_estimated propagation
# ---------------------------------------------------------------------------


def test_usage_estimated_propagates_session_level():
    """Any single estimated usage flips the session-level flag."""
    raw = json.loads(WORKED_PATH.read_text())
    raw["canonical_transcript"][3]["usage"]["estimated"] = True
    artifact = Artifact.model_validate(raw)
    metrics = compute_metrics(artifact)
    assert metrics.usage_estimated is True

    # Baseline: worked example carries no estimated=true.
    baseline = compute_metrics(_load(WORKED_PATH))
    assert baseline.usage_estimated is False


# ---------------------------------------------------------------------------
# 7. Panel-contraction grouping
# ---------------------------------------------------------------------------


def test_panel_contraction_grouping():
    raw = json.loads(WORKED_PATH.read_text())
    # Inject two contractions on the same (agent_id, reason) and one different.
    raw["canonical_transcript"].extend(
        [
            {
                "id": "msg-pc-1",
                "speaker": "runtime",
                "type": "panel_contraction",
                "content": {"agent_id": "critic", "reason": "provider_unrecoverable"},
                "parent_id": None,
                "round": 2,
                "turn_index": 7,
                "branch_depth": 0,
                "timestamp": "2026-05-25T10:01:20Z",
                "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "cost_usd": 0.0},
            },
            {
                "id": "msg-pc-2",
                "speaker": "runtime",
                "type": "panel_contraction",
                "content": {"agent_id": "critic", "reason": "provider_unrecoverable"},
                "parent_id": None,
                "round": 2,
                "turn_index": 8,
                "branch_depth": 0,
                "timestamp": "2026-05-25T10:01:21Z",
                "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "cost_usd": 0.0},
            },
            {
                "id": "msg-pc-3",
                "speaker": "runtime",
                "type": "panel_contraction",
                "content": {"agent_id": "researcher", "reason": "schema_error"},
                "parent_id": None,
                "round": 2,
                "turn_index": 9,
                "branch_depth": 0,
                "timestamp": "2026-05-25T10:01:22Z",
                "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "cost_usd": 0.0},
            },
        ]
    )
    artifact = Artifact.model_validate(raw)
    metrics = compute_metrics(artifact)

    counts_by_key = {
        (p.agent_id, p.reason): p.count for p in metrics.panel_contraction_count
    }
    assert counts_by_key == {
        ("critic", "provider_unrecoverable"): 2,
        ("researcher", "schema_error"): 1,
    }


def test_panel_contraction_unknown_agent_raises():
    raw = json.loads(WORKED_PATH.read_text())
    raw["canonical_transcript"].append(
        {
            "id": "msg-pc-bad",
            "speaker": "runtime",
            "type": "panel_contraction",
            "content": {"agent_id": "ghost_agent", "reason": "provider_unrecoverable"},
            "parent_id": None,
            "round": 2,
            "turn_index": 7,
            "branch_depth": 0,
            "timestamp": "2026-05-25T10:01:20Z",
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "cost_usd": 0.0},
        }
    )
    artifact = Artifact.model_validate(raw)
    with pytest.raises(MetricsConsistencyError) as excinfo:
        compute_metrics(artifact)
    assert "ghost_agent" in str(excinfo.value)


# ---------------------------------------------------------------------------
# 8. Deferred-queue derivation
# ---------------------------------------------------------------------------


def test_deferred_queue_length_max_worked_example():
    """The worked example carries exactly one queue-drain pattern:

    msg-001 (round 1, logician) emits two direct_requests; msg-002 (round
    1, researcher branch) drains one in-round, the other defers. msg-006
    (round 2, critic branch with parent_id=msg-001) is the cross-round
    drain. Peak queue size at end of round 1 = 1.
    """
    metrics = compute_metrics(_load(WORKED_PATH))
    assert metrics.deferred_queue_length_max == 1


def test_deferred_queue_length_max_with_dropped_annotations():
    """`dropped_deferred` annotations count as enqueue events too.

    Heuristic input: a primary_turn with N direct_requests, M same-round
    matched branches, D dropped_deferred entries contributes
    `max(N - M - D, 0)` to the queue at that turn. Here N=4, M=0, D=2 →
    enqueue 2; queue peaks at 2.
    """
    raw = json.loads(WORKED_PATH.read_text())
    # Append a primary_turn in round 3 with 4 direct_requests + 2 dropped.
    raw["canonical_transcript"].append(
        {
            "id": "msg-stress",
            "speaker": "logician",
            "type": "primary_turn",
            "content": {
                "text": "stress turn",
                "direct_requests": [
                    {"target": "researcher", "type": "question", "content": "a"},
                    {"target": "researcher", "type": "question", "content": "b"},
                    {"target": "critic", "type": "critique", "content": "c"},
                    {"target": "critic", "type": "critique", "content": "d"},
                ],
            },
            "dropped_deferred": [
                {"target": "researcher", "type": "question", "content": "x"},
                {"target": "critic", "type": "critique", "content": "y"},
            ],
            "parent_id": None,
            "round": 3,
            "turn_index": 1,
            "branch_depth": 0,
            "timestamp": "2026-05-25T10:02:00Z",
            "usage": {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150, "cost_usd": 0.0015},
        }
    )
    # Update cumulative_usage to keep parity.
    raw["cumulative_usage"]["prompt_tokens"] += 100
    raw["cumulative_usage"]["completion_tokens"] += 50
    raw["cumulative_usage"]["total_tokens"] += 150
    raw["cumulative_usage"]["cost_usd"] = round(raw["cumulative_usage"]["cost_usd"] + 0.0015, 6)

    artifact = Artifact.model_validate(raw)
    metrics = compute_metrics(artifact)
    # Round 3 open drains 1 (was 1 from round 1, but msg-006 already drained
    # it at round-2 open → queue = 0 entering round 3). Then enqueue 2 at
    # msg-stress → queue peaks at 2.
    assert metrics.deferred_queue_length_max == 2


def test_deferred_drain_observed_beyond_cap_honored():
    """Observed cross-round drains are transcript evidence: honour them up
    to the queue contents even past `max_deferred_drains_per_round`."""
    # round 1: p1 enqueues 3; round 2: TWO observed drains under a cap of 1,
    # then p2 enqueues 1; round 3: no observed drains → cap fallback (1),
    # then p3 enqueues 3.
    queue_max = _derive_deferred_queue_max(
        rounds_seen=[1, 2, 3],
        primaries_per_round={1: ["p1"], 2: ["p2"], 3: ["p3"]},
        primary_meta={
            "p1": {"N": 3, "D": 0},
            "p2": {"N": 1, "D": 0},
            "p3": {"N": 3, "D": 0},
        },
        same_round_branches={},
        cross_round_drains_by_round={2: 2},
        max_drains_per_round=1,
    )
    # queue: 3 → drain 2 → 1 → +1 = 2 → drain 1 → 1 → +3 = 4 (the peak).
    # Capping the observed drains at 1 would have peaked at 5 instead.
    assert queue_max == 4


def test_deferred_drain_observed_capped_at_queue_contents():
    """Never drain below empty, however many drains the transcript shows."""
    queue_max = _derive_deferred_queue_max(
        rounds_seen=[1, 2],
        primaries_per_round={1: ["p1"], 2: ["p2"]},
        primary_meta={"p1": {"N": 2, "D": 0}, "p2": {"N": 1, "D": 0}},
        same_round_branches={},
        cross_round_drains_by_round={2: 5},  # more than the queue holds
        max_drains_per_round=1,
    )
    # queue: 2 → drain min(2, 5) = 2 → 0 → +1 = 1; peak stays 2.
    assert queue_max == 2


# ---------------------------------------------------------------------------
# 9. Schema-failure count
# ---------------------------------------------------------------------------


def test_schema_failure_count_per_agent():
    raw = json.loads(WORKED_PATH.read_text())
    # Annotate two different primary_turns on two different agents.
    # msg-001 is logician, msg-003 is researcher.
    raw["canonical_transcript"][1]["schema_failure"] = [
        {
            "offending_request": {"target": "researcher", "type": "question", "content": "?"},
            "reason": "missing required field",
        },
        {
            "offending_request": {"target": "critic", "type": "critique", "content": "?"},
            "reason": "extra property",
        },
    ]
    raw["canonical_transcript"][3]["schema_failure"] = [
        {
            "offending_request": {"target": "logician", "type": "question", "content": "?"},
            "reason": "type mismatch",
        },
    ]
    artifact = Artifact.model_validate(raw)
    metrics = compute_metrics(artifact)
    assert metrics.schema_failure_count_per_agent == {
        "logician": 2,
        "researcher": 1,
    }


# ---------------------------------------------------------------------------
# 10. CLI end-to-end (FakeProvider walking-skeleton + `symposium metrics`)
# ---------------------------------------------------------------------------


def test_cli_metrics_end_to_end(tmp_path, example_config, example_script):
    """Run the walking skeleton, then shell into `symposium metrics`."""
    fp = FakeProvider(script=example_script)
    artifact = run_session(example_config, {"default": fp}, runs_root=str(tmp_path))
    run_dir = tmp_path / example_config.session_id

    runner = CliRunner()
    result = runner.invoke(cli_main, ["metrics", str(run_dir)])
    assert result.exit_code == 0, result.output

    metrics_path = run_dir / "metrics.json"
    assert metrics_path.exists()
    data = json.loads(metrics_path.read_text())
    # Validates against the pydantic model.
    ObservabilityMetrics.model_validate(data)
    # Stdout summary is non-empty and references the session id.
    assert artifact.session_id in result.output
    assert "transcript_digest=" in result.output
    assert "tokens=" in result.output


def test_cli_metrics_consistency_failure_exit_code(tmp_path):
    """A corrupted artifact triggers exit code 2 with a named invariant."""
    raw = json.loads(WORKED_PATH.read_text())
    raw["cumulative_usage"]["total_tokens"] += 999
    run_dir = tmp_path / "corrupted"
    run_dir.mkdir()
    (run_dir / "artifact.json").write_text(json.dumps(raw))

    runner = CliRunner()
    result = runner.invoke(cli_main, ["metrics", str(run_dir)])
    assert result.exit_code == 2
    assert "cumulative_usage" in result.output


def test_write_metrics_atomic_pretty_format(tmp_path):
    """write_metrics emits pretty-printed sorted-keys JSON."""
    artifact = _load(WORKED_PATH)
    metrics = compute_metrics(artifact)
    out = write_metrics(tmp_path, metrics)
    assert out == tmp_path / "metrics.json"
    text = out.read_text()
    # Pretty form: top-level fields are sorted alphabetically.
    keys_seen = [line.lstrip().split('"')[1] for line in text.splitlines() if line.startswith("  \"")]
    assert keys_seen == sorted(keys_seen)


def test_compute_metrics_is_pure():
    """Two calls on the same Artifact produce equal outputs."""
    artifact = _load(WORKED_PATH)
    m1 = compute_metrics(artifact)
    m2 = compute_metrics(artifact)
    assert m1.model_dump() == m2.model_dump()

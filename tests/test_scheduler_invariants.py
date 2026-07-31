"""Subset of §4.10 scheduler invariants verified end-to-end."""

from __future__ import annotations

from symposium.providers import FakeProvider
from symposium.scheduler import run_session


def test_round_terminates_when_panel_complete_then_coordinator(example_config, example_script):
    """§4.10 inv 1: A round closes when every panel member has had its
    primary_turn and the coordinator has emitted its coordination_turn."""
    fp = FakeProvider(script=example_script)
    art = run_session(example_config, {"default": fp})
    panel = example_config.selector.default_deliberation_panel
    coord = example_config.selector.coordinator_agent

    rounds_seen = sorted({m.round for m in art.canonical_transcript if m.round > 0})
    for r in rounds_seen:
        msgs_in_round = [m for m in art.canonical_transcript if m.round == r]
        primary_speakers = [m.speaker for m in msgs_in_round if m.type == "primary_turn"]
        assert primary_speakers == panel, (
            f"round {r}: primary_turn speakers {primary_speakers!r} != panel {panel!r}"
        )
        coord_msgs = [m for m in msgs_in_round if m.type == "coordination_turn"]
        assert len(coord_msgs) == 1
        assert coord_msgs[0].speaker == coord


def test_turn_index_monotonic_per_round(example_config, example_script):
    """§4.10 inv 7: within a round, `turn_index` is strictly increasing."""
    fp = FakeProvider(script=example_script)
    art = run_session(example_config, {"default": fp})
    by_round = {}
    for m in art.canonical_transcript:
        if m.round == 0:
            continue
        by_round.setdefault(m.round, []).append(m.turn_index)
    for r, idxs in by_round.items():
        assert all(a < b for a, b in zip(idxs, idxs[1:])), (
            f"round {r}: turn_index not strictly increasing: {idxs}"
        )


def test_problem_statement_is_first_and_round_zero(example_config, example_script):
    """§5.10 constraint: canonical_transcript[0] is the problem_statement at (round=0, turn_index=0)."""
    fp = FakeProvider(script=example_script)
    art = run_session(example_config, {"default": fp})
    first = art.canonical_transcript[0]
    assert first.type == "problem_statement"
    assert first.round == 0 and first.turn_index == 0
    assert first.content == example_config.problem_statement


def test_synthesis_appears_only_when_outcome_synthesis(example_config, example_script):
    """When the session ends with outcome.kind=synthesis there is exactly one
    `synthesis` message in the transcript, and it is the last one."""
    fp = FakeProvider(script=example_script)
    art = run_session(example_config, {"default": fp})
    if art.outcome.kind == "synthesis":
        synth = [m for m in art.canonical_transcript if m.type == "synthesis"]
        assert len(synth) == 1
        assert synth[0] is art.canonical_transcript[-1]
        assert art.outcome.synthesis_message_id == synth[0].id

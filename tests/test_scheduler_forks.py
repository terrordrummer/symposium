"""§4.5 / §4.6 / §4.9 fork-scheduler tests.

The repo's bundled walking-skeleton fixture has no forks, so the prior
invariant test suite never exercised the §4.5 dispatch path. These tests
build minimal panels whose `primary_turn`s emit `direct_requests`, and
assert:

  * Branch ordering — the `primary_turn` is appended to the
    `canonical_transcript` BEFORE the `branch_turn` it parents (no parent
    pointer ever points forward in the journal).
  * Failure policy propagation — when a `branch_turn` provider fails with
    `on_agent_failure="terminate"`, the session terminates instead of
    silently demoting the failure to a `panel_contraction`.
  * Deferred drain semantics — overflow `direct_requests` enqueue and
    drain as `branch_turn`s at later round-opens.
"""

from __future__ import annotations

from typing import Any, Dict, List

from symposium.models import (
    AgentConfig,
    BudgetConfig,
    Config,
    FakeProviderEntry,
    FakeProviderMatch,
    FakeProviderScript,
    Persona,
    ProviderError,
    ProviderRawMessage,
    ProviderResult,
    RuntimeConfig,
    SelectorConfig,
    Usage,
)
from symposium.providers import FakeProvider
from symposium.scheduler import run_session


# ---------------------------------------------------------------------------
# Helpers — build minimal personas / configs / scripts inline
# ---------------------------------------------------------------------------


def _persona(persona_id: str) -> Persona:
    return Persona(
        persona_class="horizontal",
        id=persona_id,
        reasoning_scope="test",
        reasoning_style="test",
        behavioral_constraints=["x"],
        failure_modes=["y"],
    )


def _config(*, agent_ids: List[str], coordinator_id: str = "coord", session_id: str) -> Config:
    agents = [
        AgentConfig(
            id=aid,
            persona_ref=_persona(aid),
            provider="fake",
            model="fake-1",
        )
        for aid in agent_ids
    ]
    coord = AgentConfig(
        id=coordinator_id,
        persona_ref=_persona(coordinator_id),
        provider="fake",
        model="fake-1",
    )
    return Config(
        schema_version="1.0.0",
        session_id=session_id,
        originator="test-runner",
        problem_statement="P",
        selector=SelectorConfig(
            strategy="fixed",
            default_deliberation_panel=list(agent_ids),
            coordinator_agent=coordinator_id,
        ),
        agents=agents,
        coordinator=coord,
        budget=BudgetConfig(
            max_total_tokens=100000,
            max_total_cost_usd=10.0,
            max_rounds=4,
            max_wallclock_seconds=120,
        ),
        runtime=RuntimeConfig(),
    )


def _usage() -> Usage:
    return Usage(prompt_tokens=10, completion_tokens=10, total_tokens=20, cost_usd=0.001)


def _ok(structured: Dict[str, Any]) -> ProviderResult:
    return ProviderResult(
        messages=[ProviderRawMessage(role="assistant", content="x")],
        tool_events=[],
        usage=_usage(),
        finish_reason="stop",
        structured_output=structured,
        raw=None,
        error=None,
    )


def _err_unrecoverable() -> ProviderResult:
    return ProviderResult(
        messages=[ProviderRawMessage(role="assistant", content="")],
        tool_events=[],
        usage=_usage(),
        finish_reason="error",
        structured_output=None,
        raw=None,
        error=ProviderError(
            kind="auth_failure",
            message="forced auth failure for test",
            retriable=False,
        ),
    )


def _entry(*, agent: str, schema: str, result: ProviderResult, round_: int = None) -> FakeProviderEntry:
    match = FakeProviderMatch(agent_id=agent, expected_output_schema=schema, round=round_)
    return FakeProviderEntry(match=match, result=result)


# ---------------------------------------------------------------------------
# Branch ordering — primary appears BEFORE branch in the canonical_transcript
# ---------------------------------------------------------------------------


def test_branch_turn_appears_after_its_parent_primary_turn():
    """Regression for the §5.4 / §4.5 branch-before-parent ordering bug.

    The primary_turn emits a direct_request; the runtime dispatches the
    branch inline. The journal MUST list the primary_turn before the
    branch_turn it parents — otherwise `parent_id` points forward in the
    canonical_transcript and replays-and-audits cannot use the journal as
    an append-only execution log.
    """
    config = _config(agent_ids=["alpha", "beta"], session_id="fork-ordering")
    script = FakeScriptBuilder()
    script.add(_entry(
        agent="alpha", schema="turn_structured_output", round_=1,
        result=_ok({
            "text": "alpha thoughts",
            "direct_requests": [{"target": "beta", "type": "question", "content": "verify?"}],
        }),
    ))
    script.add(_entry(
        agent="beta", schema="turn_structured_output", round_=1,
        result=_ok({"text": "beta — also as branch responder"}),
    ))
    # beta's primary still runs in round 1 (panel iteration); add a second
    # entry to satisfy the panel pass after the branch fires.
    script.add(_entry(
        agent="beta", schema="turn_structured_output", round_=1,
        result=_ok({"text": "beta primary"}),
    ))
    script.add(_entry(
        agent="coord", schema="verdict", round_=1,
        result=_ok({
            "next_action": "finalize",
            "rationale": "done",
            "confidence": 0.9,
            "focus": "f",
            "next_agents": [],
            "resolved_disagreements": [],
            "unresolved_disagreements": [],
        }),
    ))
    script.add(_entry(
        agent="coord", schema="synthesis_content",
        result=_ok({
            "integrated_answer": "ok",
            "resolved_disagreements": [],
            "unresolved_disagreements": [],
        }),
    ))

    art = run_session(config, {"default": FakeProvider(script=script.build())})

    transcript = art.canonical_transcript
    by_id = {m.id: m for m in transcript}

    branches = [m for m in transcript if m.type == "branch_turn"]
    assert branches, "expected at least one branch_turn in the transcript"

    for branch in branches:
        # The parent must exist AND must precede the branch in the journal.
        parent = by_id.get(branch.parent_id)
        assert parent is not None, (
            f"branch_turn {branch.id} references unknown parent {branch.parent_id}"
        )
        parent_idx = transcript.index(parent)
        branch_idx = transcript.index(branch)
        assert parent_idx < branch_idx, (
            f"branch_turn at index {branch_idx} appears BEFORE its parent_turn at "
            f"index {parent_idx} — execution-order invariant violated"
        )
        assert parent.type == "primary_turn"
        assert branch.parent_id == parent.id


# ---------------------------------------------------------------------------
# Failure policy — branch failure under terminate policy must terminate
# ---------------------------------------------------------------------------


def test_branch_target_failure_with_terminate_policy_propagates():
    """Regression for §4.9: `_dispatch_branch` previously demoted any branch
    target failure to a `panel_contraction`, silently overriding
    `on_agent_failure="terminate"`. The fix returns the termination
    reason to the loop, which terminates the session."""
    config = _config(agent_ids=["alpha", "beta"], session_id="fork-fail-terminate")
    # Default RuntimeConfig.on_agent_failure="terminate" — confirm.
    assert config.runtime.on_agent_failure == "terminate"

    script = FakeScriptBuilder()
    script.add(_entry(
        agent="alpha", schema="turn_structured_output", round_=1,
        result=_ok({
            "text": "alpha",
            "direct_requests": [{"target": "beta", "type": "question", "content": "verify?"}],
        }),
    ))
    # beta's branch invocation: hard failure → forces terminate per policy.
    script.add(_entry(
        agent="beta", schema="turn_structured_output", round_=1,
        result=_err_unrecoverable(),
    ))

    art = run_session(config, {"default": FakeProvider(script=script.build())})

    # Session should have terminated (no synthesis).
    assert art.outcome.kind == "termination", (
        f"expected termination outcome, got {art.outcome.kind!r}"
    )
    # The transcript should NOT contain a panel_contraction message for the
    # branch failure (that was the old buggy behavior).
    contractions = [m for m in art.canonical_transcript if m.type == "panel_contraction"]
    assert contractions == [], (
        "branch-target failure was silently demoted to panel_contraction "
        f"despite on_agent_failure=terminate (saw {len(contractions)} contraction msgs)"
    )


# ---------------------------------------------------------------------------
# Deferred drain — overflow direct_requests enqueue and drain at round-open
# ---------------------------------------------------------------------------


def test_deferred_drain_dispatches_queued_direct_request_at_next_round_open():
    """A `primary_turn` that emits TWO `direct_requests` to two valid targets:
    the first dispatches inline (one branch per round per §4.5); the second
    enqueues. At the next round-open the queue drains and emits a branch_turn.
    """
    config = _config(
        agent_ids=["alpha", "beta", "gamma"], session_id="fork-deferred-drain"
    )

    script = FakeScriptBuilder()

    # --- Round 1 -------------------------------------------------------------
    # alpha emits two direct_requests; the second goes into the deferred queue.
    script.add(_entry(
        agent="alpha", schema="turn_structured_output", round_=1,
        result=_ok({
            "text": "alpha r1",
            "direct_requests": [
                {"target": "beta", "type": "question", "content": "q1"},
                {"target": "gamma", "type": "question", "content": "q2"},
            ],
        }),
    ))
    # beta's branch_turn (dispatched inline by alpha's first DR).
    script.add(_entry(
        agent="beta", schema="turn_structured_output", round_=1,
        result=_ok({"text": "beta branch r1"}),
    ))
    # beta's own primary_turn (panel iteration continues after alpha's primary).
    script.add(_entry(
        agent="beta", schema="turn_structured_output", round_=1,
        result=_ok({"text": "beta primary r1"}),
    ))
    # gamma's primary_turn in round 1 (still happens; gamma DR is deferred).
    script.add(_entry(
        agent="gamma", schema="turn_structured_output", round_=1,
        result=_ok({"text": "gamma primary r1"}),
    ))
    script.add(_entry(
        agent="coord", schema="verdict", round_=1,
        result=_ok({
            "next_action": "continue",
            "rationale": "r1 done",
            "confidence": 0.5,
            "focus": "f",
            "next_agents": [],
            "resolved_disagreements": [],
            "unresolved_disagreements": [],
        }),
    ))

    # --- Round 2 -------------------------------------------------------------
    # At round-open, the deferred queue drains: gamma branch_turn fires.
    script.add(_entry(
        agent="gamma", schema="turn_structured_output", round_=2,
        result=_ok({"text": "gamma branch (drained)"}),
    ))
    script.add(_entry(
        agent="alpha", schema="turn_structured_output", round_=2,
        result=_ok({"text": "alpha r2"}),
    ))
    script.add(_entry(
        agent="beta", schema="turn_structured_output", round_=2,
        result=_ok({"text": "beta r2"}),
    ))
    script.add(_entry(
        agent="gamma", schema="turn_structured_output", round_=2,
        result=_ok({"text": "gamma primary r2"}),
    ))
    script.add(_entry(
        agent="coord", schema="verdict", round_=2,
        result=_ok({
            "next_action": "finalize",
            "rationale": "done",
            "confidence": 0.9,
            "focus": "f",
            "next_agents": [],
            "resolved_disagreements": [],
            "unresolved_disagreements": [],
        }),
    ))
    script.add(_entry(
        agent="coord", schema="synthesis_content",
        result=_ok({
            "integrated_answer": "ok",
            "resolved_disagreements": [],
            "unresolved_disagreements": [],
        }),
    ))

    art = run_session(config, {"default": FakeProvider(script=script.build())})

    # We expect exactly two branch_turn messages: one in round 1 (inline),
    # one in round 2 (drained).
    branches_r1 = [m for m in art.canonical_transcript if m.type == "branch_turn" and m.round == 1]
    branches_r2 = [m for m in art.canonical_transcript if m.type == "branch_turn" and m.round == 2]
    assert len(branches_r1) == 1, f"expected 1 inline branch in round 1, got {len(branches_r1)}"
    assert len(branches_r2) == 1, f"expected 1 drained branch in round 2, got {len(branches_r2)}"
    # Both branches' parent_id must point to alpha's round-1 primary_turn.
    alpha_r1 = next(
        m for m in art.canonical_transcript
        if m.type == "primary_turn" and m.speaker == "alpha" and m.round == 1
    )
    assert branches_r1[0].parent_id == alpha_r1.id
    assert branches_r2[0].parent_id == alpha_r1.id


# ---------------------------------------------------------------------------
# Builder helper
# ---------------------------------------------------------------------------


class FakeScriptBuilder:
    def __init__(self) -> None:
        self._entries: List[FakeProviderEntry] = []

    def add(self, entry: FakeProviderEntry) -> "FakeScriptBuilder":
        self._entries.append(entry)
        return self

    def build(self) -> FakeProviderScript:
        return FakeProviderScript(
            schema_version="1.0.0",
            on_exhaustion="error",
            entries=self._entries,
        )

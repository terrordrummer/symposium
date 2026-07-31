"""§4.9 `continue_without` panel-contraction edge cases.

  * an inline-branch failure that contracts an agent mid-round must also
    cancel that agent's primary turn for the round (the panel iteration
    runs over a snapshot of `active_panel`);
  * a drain-time branch failure that contracts the LAST panel member must
    terminate the run cleanly (pre-fix: the next ContextPacket was derived
    over an empty `panel_disclosure` — an uncaught ValidationError with no
    artifact);
  * a deferred direct_request that became unroutable by drain time is
    recorded on its originating message instead of vanishing silently.
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
# builders
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


def _config(*, agent_ids: List[str], session_id: str, max_rounds: int = 4) -> Config:
    agents = [
        AgentConfig(id=aid, persona_ref=_persona(aid), provider="fake", model="fake-1")
        for aid in agent_ids
    ]
    coord = AgentConfig(
        id="coord", persona_ref=_persona("coord"), provider="fake", model="fake-1"
    )
    return Config(
        schema_version="1.0.0",
        session_id=session_id,
        originator="test-runner",
        problem_statement="P",
        selector=SelectorConfig(
            strategy="fixed",
            default_deliberation_panel=list(agent_ids),
            coordinator_agent="coord",
        ),
        agents=agents,
        coordinator=coord,
        budget=BudgetConfig(
            max_total_tokens=100000,
            max_total_cost_usd=10.0,
            max_rounds=max_rounds,
            max_wallclock_seconds=3600,
        ),
        runtime=RuntimeConfig(on_agent_failure="continue_without"),
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
            kind="auth_failure", message="forced failure for test", retriable=False
        ),
    )


def _entry(*, agent: str, schema: str, result: ProviderResult) -> FakeProviderEntry:
    return FakeProviderEntry(
        match=FakeProviderMatch(agent_id=agent, expected_output_schema=schema),
        result=result,
    )


def _verdict(next_action: str) -> Dict[str, Any]:
    return {
        "next_action": next_action,
        "rationale": "r",
        "confidence": 0.5,
        "focus": "f",
        "next_agents": [],
        "resolved_disagreements": [],
        "unresolved_disagreements": [],
    }


def _script(entries: List[FakeProviderEntry]) -> FakeProviderScript:
    return FakeProviderScript(
        schema_version="1.0.0", on_exhaustion="error", entries=entries
    )


# ---------------------------------------------------------------------------
# mid-round contraction cancels the primary turn
# ---------------------------------------------------------------------------


def test_agent_contracted_via_inline_branch_takes_no_primary_turn_same_round():
    """alpha's inline direct_request to gamma fails non-retriably → gamma is
    contracted mid-round. The panel iteration snapshot was taken before the
    contraction, but gamma must NOT still get its primary turn."""
    config = _config(agent_ids=["alpha", "beta", "gamma"], session_id="cw-midround")
    script = _script(
        [
            _entry(
                agent="alpha",
                schema="turn_structured_output",
                result=_ok(
                    {
                        "text": "alpha r1",
                        "direct_requests": [
                            {"target": "gamma", "type": "question", "content": "q"}
                        ],
                    }
                ),
            ),
            # gamma's inline branch: hard failure → contraction (continue_without).
            _entry(
                agent="gamma",
                schema="turn_structured_output",
                result=_err_unrecoverable(),
            ),
            _entry(
                agent="beta",
                schema="turn_structured_output",
                result=_ok({"text": "beta r1"}),
            ),
            # No gamma primary entry: it must be skipped.
            _entry(agent="coord", schema="verdict", result=_ok(_verdict("finalize"))),
            _entry(
                agent="coord",
                schema="synthesis_content",
                result=_ok(
                    {
                        "integrated_answer": "done",
                        "resolved_disagreements": [],
                        "unresolved_disagreements": [],
                    }
                ),
            ),
        ]
    )

    art = run_session(config, {"default": FakeProvider(script=script)})

    assert art.outcome.kind == "synthesis"
    primaries_r1 = [
        m.speaker
        for m in art.canonical_transcript
        if m.type == "primary_turn" and m.round == 1
    ]
    assert primaries_r1 == ["alpha", "beta"], (
        f"contracted agent still took a primary turn: {primaries_r1}"
    )
    contractions = [
        m for m in art.canonical_transcript if m.type == "panel_contraction"
    ]
    assert len(contractions) == 1
    assert contractions[0].content["agent_id"] == "gamma"


# ---------------------------------------------------------------------------
# branch contraction of the last panel member terminates cleanly
# ---------------------------------------------------------------------------


def test_branch_contraction_emptying_panel_terminates_with_artifact(tmp_path):
    """Three rounds of contractions: beta falls in round 1, alpha in round 2,
    and in round 3 a drained branch contracts gamma — the LAST member. The
    run must terminate `provider_unrecoverable` with a persisted artifact
    (pre-fix: uncaught ValidationError deriving a packet over an empty
    panel, no artifact)."""
    config = _config(agent_ids=["alpha", "beta", "gamma"], session_id="cw-empty-panel")
    script = _script(
        [
            # --- round 1 --------------------------------------------------
            _entry(
                agent="alpha",
                schema="turn_structured_output",
                result=_ok(
                    {
                        "text": "alpha r1",
                        "direct_requests": [
                            {"target": "beta", "type": "question", "content": "inline"},
                            {"target": "gamma", "type": "question", "content": "d1"},
                            {"target": "gamma", "type": "question", "content": "d2"},
                        ],
                    }
                ),
            ),
            _entry(
                agent="beta",
                schema="turn_structured_output",
                result=_ok({"text": "beta branch r1"}),
            ),
            _entry(
                agent="beta",
                schema="turn_structured_output",
                result=_err_unrecoverable(),  # beta contracted
            ),
            _entry(
                agent="gamma",
                schema="turn_structured_output",
                result=_ok({"text": "gamma r1"}),
            ),
            _entry(agent="coord", schema="verdict", result=_ok(_verdict("continue"))),
            # --- round 2 (drain d1 → gamma branch) ------------------------
            _entry(
                agent="gamma",
                schema="turn_structured_output",
                result=_ok({"text": "gamma branch r2 (drained d1)"}),
            ),
            _entry(
                agent="alpha",
                schema="turn_structured_output",
                result=_err_unrecoverable(),  # alpha contracted
            ),
            _entry(
                agent="gamma",
                schema="turn_structured_output",
                result=_ok({"text": "gamma r2"}),
            ),
            _entry(agent="coord", schema="verdict", result=_ok(_verdict("continue"))),
            # --- round 3 (drain d2 → gamma branch fails, panel empties) ---
            _entry(
                agent="gamma",
                schema="turn_structured_output",
                result=_err_unrecoverable(),  # gamma contracted → panel empty
            ),
        ]
    )

    art = run_session(
        config, {"default": FakeProvider(script=script)}, runs_root=str(tmp_path)
    )

    assert art.outcome.kind == "termination"
    term = art.outcome.termination_artifact
    assert term.reason == "provider_unrecoverable"
    contractions = [
        m.content["agent_id"]
        for m in art.canonical_transcript
        if m.type == "panel_contraction"
    ]
    assert contractions == ["beta", "alpha", "gamma"]
    # The run persisted normally despite the empty panel.
    run_dir = tmp_path / config.session_id
    assert (run_dir / "artifact.json").exists()
    assert not (run_dir / ".lock").exists()


# ---------------------------------------------------------------------------
# drain-time drops leave a transcript record
# ---------------------------------------------------------------------------


def test_unroutable_deferred_request_is_recorded_at_drain_time():
    """gamma is contracted in round 1 AFTER alpha deferred a request to it;
    at the round-2 drain the request is unroutable and must be recorded on
    alpha's originating primary_turn (pre-fix: popped silently)."""
    config = _config(agent_ids=["alpha", "beta", "gamma"], session_id="cw-drain-drop")
    script = _script(
        [
            # --- round 1 --------------------------------------------------
            _entry(
                agent="alpha",
                schema="turn_structured_output",
                result=_ok(
                    {
                        "text": "alpha r1",
                        "direct_requests": [
                            {"target": "beta", "type": "question", "content": "inline"},
                            {"target": "gamma", "type": "question", "content": "deferred"},
                        ],
                    }
                ),
            ),
            _entry(
                agent="beta",
                schema="turn_structured_output",
                result=_ok({"text": "beta branch r1"}),
            ),
            _entry(
                agent="beta",
                schema="turn_structured_output",
                result=_ok({"text": "beta r1"}),
            ),
            _entry(
                agent="gamma",
                schema="turn_structured_output",
                result=_err_unrecoverable(),  # gamma contracted
            ),
            _entry(agent="coord", schema="verdict", result=_ok(_verdict("continue"))),
            # --- round 2 (drain finds the request unroutable — no call) ---
            _entry(
                agent="alpha",
                schema="turn_structured_output",
                result=_ok({"text": "alpha r2"}),
            ),
            _entry(
                agent="beta",
                schema="turn_structured_output",
                result=_ok({"text": "beta r2"}),
            ),
            _entry(agent="coord", schema="verdict", result=_ok(_verdict("finalize"))),
            _entry(
                agent="coord",
                schema="synthesis_content",
                result=_ok(
                    {
                        "integrated_answer": "done",
                        "resolved_disagreements": [],
                        "unresolved_disagreements": [],
                    }
                ),
            ),
        ]
    )

    art = run_session(config, {"default": FakeProvider(script=script)})

    assert art.outcome.kind == "synthesis"
    # No branch was dispatched in round 2 — the drained request was dropped.
    assert not any(
        m.type == "branch_turn" and m.round == 2 for m in art.canonical_transcript
    )
    alpha_r1 = next(
        m
        for m in art.canonical_transcript
        if m.type == "primary_turn" and m.speaker == "alpha" and m.round == 1
    )
    assert alpha_r1.schema_failure, "drain-time drop left no transcript record"
    reasons = [rec.reason for rec in alpha_r1.schema_failure]
    assert any("drain" in r for r in reasons), reasons
    dropped_targets = [
        rec.offending_request.get("target") for rec in alpha_r1.schema_failure
    ]
    assert "gamma" in dropped_targets

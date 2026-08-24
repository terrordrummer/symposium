"""Runtime setup guards + non-determinism injection.

  * provider-map coverage is validated UP FRONT (pre-fix: a missing adapter
    surfaced mid-run as a bare KeyError("default") that crash-marked the
    manifest);
  * escalating an uncontractable coordinator — or a panel contracted to
    empty — carries the ACTUAL failure cause (schema_error), not the
    hardcoded provider_unrecoverable;
  * `run_session(id_source=/clock_source=/started_at=)` pins every
    digest-bearing non-deterministic source PER-SESSION, with no global
    patching (the legacy `pinned_runtime` contract, made thread-safe).
"""

from __future__ import annotations

from typing import Any, Dict, List

import pytest

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
from symposium.scheduler.loop import Session


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


def _config(
    *,
    agent_ids: List[str],
    session_id: str = "guards-001",
    on_agent_failure: str = "continue_without",
) -> Config:
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
            max_rounds=4,
            max_wallclock_seconds=3600,
        ),
        runtime=RuntimeConfig(on_agent_failure=on_agent_failure),
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


def _err(kind: str) -> ProviderResult:
    return ProviderResult(
        messages=[ProviderRawMessage(role="assistant", content="")],
        tool_events=[],
        usage=_usage(),
        finish_reason="error",
        structured_output=None,
        raw=None,
        error=ProviderError(
            kind=kind, message="forced failure for test", retriable=False
        ),
    )


def _synthesis(answer: str) -> Dict[str, Any]:
    return {
        "integrated_answer": answer,
        "resolved_disagreements": [],
        "unresolved_disagreements": [],
        "confidence": 0.9,
    }


def _turn(text: str) -> Dict[str, Any]:
    return {"text": text, "direct_requests": None}


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
    return FakeProviderScript(entries=entries)


# ---------------------------------------------------------------------------
# provider-map coverage
# ---------------------------------------------------------------------------


def test_missing_adapter_and_default_fails_fast():
    config = _config(agent_ids=["a"], session_id="coverage-missing")
    with pytest.raises(ValueError, match=r"no provider adapter for agent id\(s\)"):
        run_session(config, {})


def test_partial_coverage_lists_only_uncovered_agents():
    config = _config(agent_ids=["a", "b"], session_id="coverage-partial")

    class _NamedAdapter:
        name = "named"

        def invoke(self, request):  # pragma: no cover — never reached
            raise AssertionError("must not be invoked")

    with pytest.raises(ValueError) as excinfo:
        run_session(config, {"b": _NamedAdapter()})
    assert "'a'" in str(excinfo.value)
    assert "'coord'" in str(excinfo.value)


def test_per_agent_keys_without_default_are_sufficient():
    """Explicit per-agent coverage (panel + coordinator) needs no fallback."""
    config = _config(agent_ids=["a"], session_id="coverage-explicit")
    script_a = _script([
        FakeProviderEntry(
            match=FakeProviderMatch(agent_id="a", expected_output_schema="turn_structured_output"),
            result=_ok(_turn("t1")),
        ),
    ])
    script_coord = _script([
        FakeProviderEntry(
            match=FakeProviderMatch(agent_id="coord", expected_output_schema="verdict"),
            result=_ok(_verdict("finalize")),
        ),
        FakeProviderEntry(
            match=FakeProviderMatch(agent_id="coord", expected_output_schema="synthesis_content"),
            result=_ok(_synthesis("done")),
        ),
    ])
    providers = {
        "a": FakeProvider(script=script_a),
        "coord": FakeProvider(script=script_coord),
    }
    artifact = run_session(config, providers)
    assert artifact.outcome.kind == "synthesis"


# ---------------------------------------------------------------------------
# escalation reasons carry the actual cause
# ---------------------------------------------------------------------------


def test_coordinator_schema_failure_terminates_as_schema_error(tmp_path):
    """§4.9 continue_without: the coordinator cannot be contracted; the
    escalation must report `schema_error` (an invalid_request classification)
    instead of the pre-fix hardcoded provider_unrecoverable."""
    config = _config(agent_ids=["a"], session_id="escalation-coord")
    script = _script([
        FakeProviderEntry(
            match=FakeProviderMatch(agent_id="a", expected_output_schema="turn_structured_output"),
            result=_ok(_turn("t1")),
        ),
        FakeProviderEntry(
            match=FakeProviderMatch(agent_id="coord", expected_output_schema="verdict"),
            result=_err("invalid_request"),  # classifies to schema_error
        ),
    ])
    artifact = run_session(
        config, {"default": FakeProvider(script=script)}, runs_root=str(tmp_path)
    )
    assert artifact.outcome.kind == "termination"
    term = artifact.outcome.termination_artifact
    assert term.reason == "schema_error"
    assert term.last_provider_failure is not None
    assert term.last_provider_failure.kind == "invalid_request"


def test_last_member_contraction_terminates_as_schema_error():
    """Contracting the last panel member escalates with the real cause too."""
    config = _config(agent_ids=["a"], session_id="escalation-empty-panel")
    script = _script([
        FakeProviderEntry(
            match=FakeProviderMatch(agent_id="a", expected_output_schema="turn_structured_output"),
            result=_err("malformed_response"),
        ),
    ])
    artifact = run_session(config, {"default": FakeProvider(script=script)})
    assert artifact.outcome.kind == "termination"
    term = artifact.outcome.termination_artifact
    assert term.reason == "schema_error"
    # The contraction record still names the agent.
    contractions = [
        m for m in artifact.canonical_transcript if m.type == "panel_contraction"
    ]
    assert len(contractions) == 1
    assert contractions[0].content["agent_id"] == "a"
    assert contractions[0].content["reason"] == "schema_error"


# ---------------------------------------------------------------------------
# per-session source pinning
# ---------------------------------------------------------------------------


def test_run_session_injected_sources_pin_every_message_field(tmp_path):
    config = _config(agent_ids=["a"], session_id="pins-001")
    script = _script([
        FakeProviderEntry(
            match=FakeProviderMatch(agent_id="a", expected_output_schema="turn_structured_output"),
            result=_ok(_turn("t1")),
        ),
        FakeProviderEntry(
            match=FakeProviderMatch(agent_id="coord", expected_output_schema="verdict"),
            result=_ok(_verdict("finalize")),
        ),
        FakeProviderEntry(
            match=FakeProviderMatch(agent_id="coord", expected_output_schema="synthesis_content"),
            result=_ok(_synthesis("done")),
        ),
    ])

    counter = iter(range(1000))
    artifact = run_session(
        config,
        {"default": FakeProvider(script=script)},
        runs_root=str(tmp_path),
        id_source=lambda: f"msg-{next(counter):03d}",
        clock_source=lambda: "2025-01-01T00:00:00Z",
        started_at="2024-12-31T23:59:00Z",
    )

    ids = [m.id for m in artifact.canonical_transcript]
    stamps = {m.timestamp for m in artifact.canonical_transcript}
    assert ids == ["msg-000", "msg-001", "msg-002", "msg-003"]
    assert stamps == {"2025-01-01T00:00:00Z"}
    assert artifact.started_at == "2024-12-31T23:59:00Z"

    # The persisted manifest carries the pinned start stamp as created_at.
    import json

    manifest = json.loads(
        (tmp_path / "pins-001" / "manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["created_at"] == "2024-12-31T23:59:00Z"


def test_session_monotonic_factory_drives_elapsed_seconds():
    config = _config(agent_ids=["a"], session_id="mono-001")
    tick = {"v": 10.0}

    def fake_monotonic() -> float:
        return tick["v"]

    session = Session(config=config, providers={}, monotonic_factory=fake_monotonic)
    assert session.started_monotonic == 10.0
    assert session.elapsed_seconds() == 0.0
    tick["v"] = 42.5
    assert session.elapsed_seconds() == pytest.approx(32.5)


def test_session_started_at_stays_real_wallclock_under_pinned_clock():
    """The message-timestamp stream must stay aligned: started_at does NOT
    consume from clock_factory (it would desync a replayed sequence)."""
    config = _config(agent_ids=["a"], session_id="startstamp-001")
    session = Session(config=config, providers={}, clock_factory=lambda: "fixed")
    assert session.clock_factory() == "fixed"
    assert session.started_at != "fixed"  # real wall-clock ISO stamp

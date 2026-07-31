"""Schema self-defense: non-conforming structured_output terminates, not crashes.

The FakeProvider (and any third-party adapter) can report a successful
result whose `structured_output` dict does not validate against the
expected schema. Pre-fix, the bare `model_validate` calls in the round
loop raised an uncaught ValidationError mid-run — no artifact, no
manifest update. These tests pin the schema_error termination path for
each of the three output schemas, plus the llm-selector failure wrapping.
"""

from __future__ import annotations

import json

from symposium.models import (
    AgentConfig,
    BudgetConfig,
    Config,
    FakeProviderScript,
    RuntimeConfig,
    SelectorBudget,
    SelectorConfig,
)
from symposium.personas import persona_by_id
from symposium.providers import FakeProvider
from symposium.scheduler import run_session


# ---------------------------------------------------------------------------
# builders
# ---------------------------------------------------------------------------


def _make_config(*, session_id: str, selector_strategy: str = "fixed") -> Config:
    selector_kwargs = dict(
        strategy=selector_strategy,
        default_deliberation_panel=["logician"],
        coordinator_agent="coordinator",
    )
    if selector_strategy == "llm":
        selector_kwargs["selector_budget"] = SelectorBudget(max_tokens=100000)
    return Config(
        schema_version="1.0.0",
        session_id=session_id,
        originator="test",
        problem_statement="P",
        selector=SelectorConfig(**selector_kwargs),
        agents=[
            AgentConfig(
                id="logician",
                persona_ref=persona_by_id("logician"),
                provider="fake",
                model="fake-1",
            )
        ],
        coordinator=AgentConfig(
            id="coordinator",
            persona_ref=persona_by_id("coordinator"),
            provider="fake",
            model="fake-1",
        ),
        runtime=RuntimeConfig(),
        budget=BudgetConfig(
            max_total_tokens=100000,
            max_total_cost_usd=10.0,
            max_rounds=4,
            max_wallclock_seconds=3600,
        ),
    )


def _result(structured) -> dict:
    return {
        "messages": [{"role": "assistant", "content": ""}],
        "tool_events": [],
        "usage": {
            "prompt_tokens": 10,
            "completion_tokens": 0,
            "total_tokens": 10,
            "cost_usd": 0.0,
        },
        "finish_reason": "stop",
        "structured_output": structured,
        "raw": None,
        "error": None,
    }


def _entry(agent_id: str, schema: str, structured) -> dict:
    return {
        "match": {"agent_id": agent_id, "expected_output_schema": schema},
        "result": _result(structured),
    }


def _script(entries) -> FakeProviderScript:
    return FakeProviderScript.model_validate(
        {"schema_version": "1.0.0", "on_exhaustion": "error", "entries": entries}
    )


def _good_verdict(next_action: str = "continue") -> dict:
    return {
        "next_action": next_action,
        "rationale": "r",
        "confidence": 0.5,
        "focus": "f",
        "next_agents": [],
        "resolved_disagreements": [],
        "unresolved_disagreements": [],
    }


# ---------------------------------------------------------------------------
# non-conforming structured_output per schema
# ---------------------------------------------------------------------------


def test_invalid_turn_structured_output_terminates_schema_error(tmp_path):
    config = _make_config(session_id="schema-bad-turn")
    script = _script(
        [_entry("logician", "turn_structured_output", {"wrong_key": "no text field"})]
    )
    art = run_session(
        config, {"default": FakeProvider(script=script)}, runs_root=str(tmp_path)
    )

    assert art.outcome.kind == "termination"
    assert art.outcome.termination_artifact.reason == "schema_error"
    run_dir = tmp_path / config.session_id
    assert (run_dir / "artifact.json").exists()
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["status"] == "terminated"
    assert not (run_dir / ".lock").exists()


def test_invalid_verdict_terminates_schema_error():
    config = _make_config(session_id="schema-bad-verdict")
    bad_verdict = _good_verdict()
    del bad_verdict["confidence"]  # required by the Verdict model
    script = _script(
        [
            _entry("logician", "turn_structured_output", {"text": "fine"}),
            _entry("coordinator", "verdict", bad_verdict),
        ]
    )
    art = run_session(config, {"default": FakeProvider(script=script)})

    assert art.outcome.kind == "termination"
    assert art.outcome.termination_artifact.reason == "schema_error"


def test_invalid_synthesis_terminates_schema_error_with_failure_snapshot():
    config = _make_config(session_id="schema-bad-synthesis")
    script = _script(
        [
            _entry("logician", "turn_structured_output", {"text": "fine"}),
            _entry("coordinator", "verdict", _good_verdict("finalize")),
            # integrated_answer must be non-empty — this fails validation.
            _entry(
                "coordinator",
                "synthesis_content",
                {
                    "integrated_answer": "",
                    "resolved_disagreements": [],
                    "unresolved_disagreements": [],
                },
            ),
        ]
    )
    art = run_session(config, {"default": FakeProvider(script=script)})

    assert art.outcome.kind == "termination"
    term = art.outcome.termination_artifact
    assert term.reason == "schema_error"
    assert term.last_provider_failure is not None
    assert term.last_provider_failure.kind == "malformed_response"


# ---------------------------------------------------------------------------
# llm selector failures surface as SelectorError → schema_error termination
# ---------------------------------------------------------------------------


class _RaisingProvider:
    name = "fake"

    def invoke(self, request):
        raise RuntimeError("selector adapter blew up")


def test_llm_selector_provider_exception_terminates_schema_error():
    config = _make_config(session_id="selector-raises", selector_strategy="llm")
    deliberation = FakeProvider(
        script=_script([_entry("logician", "turn_structured_output", {"text": "x"})])
    )
    art = run_session(
        config,
        {"default": deliberation},
        selector_providers={"default": _RaisingProvider()},
    )

    assert art.outcome.kind == "termination"
    assert art.outcome.termination_artifact.reason == "schema_error"
    # Terminated before round 1: only the problem_statement was recorded.
    assert [m.type for m in art.canonical_transcript] == ["problem_statement"]


def test_llm_selector_invalid_payload_terminates_schema_error():
    """A selection payload that passes the ad-hoc checks but fails
    SelectorOutput validation (non-string coordinator_agent) must surface
    as a SelectorError, not a raw pydantic ValidationError."""
    config = _make_config(session_id="selector-bad-payload", selector_strategy="llm")
    selector_script = _script(
        [
            {
                "match": {"agent_id": "coordinator"},
                "result": _result(
                    {"selected_agents": ["logician"], "coordinator_agent": 123}
                ),
            }
        ]
    )
    deliberation = FakeProvider(
        script=_script([_entry("logician", "turn_structured_output", {"text": "x"})])
    )
    art = run_session(
        config,
        {"default": deliberation},
        selector_providers={"default": FakeProvider(script=selector_script)},
    )

    assert art.outcome.kind == "termination"
    assert art.outcome.termination_artifact.reason == "schema_error"
    assert [m.type for m in art.canonical_transcript] == ["problem_statement"]

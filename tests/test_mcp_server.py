"""Symposium MCP server tools (spec §11.4 / §11.5 host integration).

The FastMCP tools are plain functions, so these tests call them directly
— no real transport, no network. Every path is driven by the
deterministic FakeProvider via the walking-skeleton script; CI never
needs ANTHROPIC_API_KEY / OPENAI_API_KEY.
"""

from __future__ import annotations

import pytest

# Skip the whole module cleanly if the optional `mcp` extra is absent.
pytest.importorskip("mcp")

from symposium.integrations.mcp_server import (  # noqa: E402
    deliberate,
    get_run_summary,
    list_personas,
)

# A problem statement whose keywords trigger all five default reasoning
# scopes (formal-structural / lateral-creative / evidence-based /
# adversarial-scrutiny / implementation-feasibility), so the `rules`
# selector keeps the full panel in declared order and the walking-skeleton
# script's ordinal entries still line up.
_ALL_SCOPES_PROBLEM = (
    "Prove the design is feasible: research the risk and the implementation cost."
)


@pytest.fixture
def script_path(repo_root) -> str:
    return str(repo_root / "examples" / "scripts" / "walking-skeleton.json")


def test_deliberate_fake_synthesis(tmp_path, script_path):
    """provider=fake + walking-skeleton → synthesis with a non-empty answer."""
    result = deliberate(
        "Should we adopt a structured deliberation protocol?",
        provider="fake",
        fake_script_path=script_path,
        output_dir=str(tmp_path),
    )
    assert "error" not in result, result
    assert result["outcome"] == "synthesis"
    assert isinstance(result["synthesis_answer"], str) and result["synthesis_answer"]
    assert result["selected_agents"] == [
        "logician",
        "visionary",
        "researcher",
        "critic",
        "engineer",
    ]
    assert result["rounds"] == 2
    # 64-hex digest + a persisted run dir.
    assert len(result["transcript_digest"]) == 64
    assert result["run_dir"].endswith(result["run_dir"].split("/")[-1])


def test_deliberate_rules_selector_synthesis(tmp_path, script_path):
    """selector_strategy=rules still reaches a valid synthesis (full panel)."""
    result = deliberate(
        _ALL_SCOPES_PROBLEM,
        provider="fake",
        selector_strategy="rules",
        fake_script_path=script_path,
        output_dir=str(tmp_path),
    )
    assert "error" not in result, result
    assert result["outcome"] == "synthesis"
    assert result["synthesis_answer"]
    # The rules selector kept all five declared agents in order.
    assert result["selected_agents"] == [
        "logician",
        "visionary",
        "researcher",
        "critic",
        "engineer",
    ]


def test_deliberate_budget_termination(tmp_path, script_path):
    """A budget cap yields a termination *result*, not an exception."""
    result = deliberate(
        "Should we adopt a structured deliberation protocol?",
        provider="fake",
        fake_script_path=script_path,
        max_rounds=1,  # round 1 = continue → round 2 open trips the cap
        output_dir=str(tmp_path),
    )
    assert "error" not in result, result
    assert result["outcome"] == "termination"
    assert result["termination_reason"] == "budget_exceeded"
    assert "synthesis_answer" not in result


def test_get_run_summary_reports_replay_ok(tmp_path, script_path):
    """get_run_summary recomputes metrics + verifies the §7.5 replay."""
    run = deliberate(
        "Should we adopt a structured deliberation protocol?",
        provider="fake",
        fake_script_path=script_path,
        output_dir=str(tmp_path),
    )
    assert "error" not in run, run

    summary = get_run_summary(run["run_dir"])
    assert "error" not in summary, summary
    assert summary["outcome"] == "synthesis"
    assert summary["digest_replay_ok"] is True
    assert summary["transcript_digest"] == run["transcript_digest"]
    assert summary["tokens"] > 0
    assert summary["cost"] >= 0.0
    assert summary["selected_agents"] == run["selected_agents"]


def test_list_personas_returns_six_builtins():
    personas = list_personas()
    assert isinstance(personas, list)
    ids = [p["id"] for p in personas]
    assert ids == [
        "logician",
        "visionary",
        "researcher",
        "critic",
        "engineer",
        "coordinator",
    ]
    for p in personas:
        assert p["reasoning_scope"]
        assert p["role_summary"]


def test_invalid_argument_returns_structured_error(tmp_path, script_path):
    """Bad arguments return a structured error, never crash the transport."""
    # Unknown persona id.
    unknown = deliberate(
        "anything",
        provider="fake",
        panel=["not-a-real-persona"],
        fake_script_path=script_path,
        output_dir=str(tmp_path),
    )
    assert "error" in unknown
    assert "not-a-real-persona" in unknown["error"]

    # provider="fake" without a script.
    missing_script = deliberate(
        "anything",
        provider="fake",
        output_dir=str(tmp_path),
    )
    assert "error" in missing_script
    assert "fake_script_path" in missing_script["error"]


def test_get_run_summary_missing_run_returns_error(tmp_path):
    summary = get_run_summary(str(tmp_path / "does-not-exist"))
    assert "error" in summary

"""Direct tests for Sartori's MCP room-control tools."""

from __future__ import annotations

import pytest

pytest.importorskip("mcp")

from symposium.integrations.mcp_server import (  # noqa: E402
    sartori_create_agent,
    sartori_create_room,
    sartori_dismiss_agent,
    sartori_invite_agent,
    sartori_switch_room,
    sartori_workspace_status,
)


def test_sartori_mcp_tools_complete_zeus_membership_lifecycle(tmp_path):
    state_dir = str(tmp_path / "state")
    room = sartori_create_room(
        "Zeus Focus Talking",
        "Project status and decisions",
        state_dir=state_dir,
    )
    assert room["room"]["id"] == "zeus-focus-talking"
    assert room["sartori_presence"] == "listening"

    agent = sartori_create_agent(
        "zeus-lead",
        "Responsabile Zeus",
        "Report facts, risks, blockers, and the next decision.",
        capabilities=["zeus-project-status"],
        state_dir=state_dir,
    )
    assert agent["agent"]["id"] == "zeus-lead"
    assert agent["avatar"].startswith("pool-")

    invited = sartori_invite_agent(
        "zeus-lead",
        room="zeus-focus-talking",
        onboarding_context="Only project status; no unrelated workspace context.",
        state_dir=state_dir,
    )
    assert invited["membership"]["presence"] == "listening"

    switched = sartori_switch_room("zeus-focus-talking", state_dir=state_dir)
    assert switched["active_room"]["id"] == "zeus-focus-talking"
    assert [p["id"] for p in switched["participants"]] == [
        "coordinator", "zeus-lead"
    ]

    dismissed = sartori_dismiss_agent("zeus-lead", state_dir=state_dir)
    assert dismissed["membership"]["presence"] == "offline"
    status = sartori_workspace_status(state_dir)
    assert [p["id"] for p in status["participants"]] == ["coordinator"]


def test_sartori_mcp_tool_failures_are_structured(tmp_path):
    result = sartori_dismiss_agent("missing-agent", state_dir=str(tmp_path / "state"))
    assert "error" in result
    assert "missing-agent" in result["error"]

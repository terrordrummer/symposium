"""CLI acceptance tests for Sartori's local room control plane."""

from __future__ import annotations

import json

from click.testing import CliRunner

from symposium.cli.main import main


def _run(runner: CliRunner, state_dir, *args: str):
    result = runner.invoke(main, [*args, "--state-dir", str(state_dir)])
    assert result.exit_code == 0, result.output
    return result


def test_cli_zeus_room_lifecycle(tmp_path):
    state_dir = tmp_path / "state"
    runner = CliRunner()

    initialized = _run(runner, state_dir, "workspace", "init")
    assert "active_room=symposium" in initialized.output
    assert "agents=6" in initialized.output

    created_room = _run(
        runner,
        state_dir,
        "room",
        "create",
        "Zeus Focus Talking",
        "--purpose",
        "Project status and decisions",
    )
    assert "created_room=zeus-focus-talking" in created_room.output
    assert "sartori=listening" in created_room.output

    created_agent = _run(
        runner,
        state_dir,
        "agent",
        "create",
        "zeus-lead",
        "--name",
        "Responsabile Zeus",
        "--instructions",
        "Report facts, risks, blockers, and the next decision.",
        "--capability",
        "zeus-project-status",
    )
    assert "created_agent=zeus-lead" in created_agent.output
    assert "avatar=pool-" in created_agent.output

    invited = _run(
        runner,
        state_dir,
        "room",
        "invite",
        "zeus-lead",
        "--room",
        "zeus-focus-talking",
        "--context",
        "Only current milestones and blockers.",
    )
    assert "presence=listening" in invited.output

    switched = _run(
        runner, state_dir, "room", "switch", "zeus-focus-talking"
    )
    assert "active_room=zeus-focus-talking" in switched.output

    status = _run(runner, state_dir, "workspace", "status", "--json-output")
    payload = json.loads(status.output)
    assert payload["active_room"]["id"] == "zeus-focus-talking"
    assert [p["id"] for p in payload["participants"]] == [
        "coordinator", "zeus-lead"
    ]

    dismissed = _run(runner, state_dir, "room", "dismiss", "zeus-lead")
    assert "dismissed_agent=zeus-lead" in dismissed.output
    status = _run(runner, state_dir, "workspace", "status", "--json-output")
    assert [p["id"] for p in json.loads(status.output)["participants"]] == [
        "coordinator"
    ]


def test_cli_reports_control_plane_errors_without_traceback(tmp_path):
    state_dir = tmp_path / "state"
    runner = CliRunner()
    _run(runner, state_dir, "workspace", "init")

    result = runner.invoke(
        main,
        ["room", "dismiss", "coordinator", "--state-dir", str(state_dir)],
    )
    assert result.exit_code == 1
    assert "Sartori cannot be dismissed" in result.output
    assert "Traceback" not in result.output

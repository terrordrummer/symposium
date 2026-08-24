"""Room-to-run execution binding for the Symposium 2.x browser workspace."""

from __future__ import annotations

import json
import threading
import types

import pytest

from symposium.control_plane import (
    ControlPlane,
    ControlPlaneError,
    RoomExecutionManager,
    build_room_config,
)
from symposium.models import FakeProviderScript
from symposium.providers import FakeProvider


def _briefing_script(agent_id: str) -> FakeProviderScript:
    def result(structured):
        return {
            "messages": [{"role": "assistant", "content": "ok"}],
            "tool_events": [],
            "usage": {
                "prompt_tokens": 10,
                "completion_tokens": 10,
                "total_tokens": 20,
                "cost_usd": 0.0,
            },
            "finish_reason": "stop",
            "structured_output": structured,
            "raw": None,
            "error": None,
        }

    return FakeProviderScript.model_validate({
        "schema_version": "1.0.0",
        "on_exhaustion": "error",
        "entries": [
            {
                "match": {
                    "agent_id": agent_id,
                    "expected_output_schema": "turn_structured_output",
                },
                "result": result({
                    "text": "Zeus è in linea con il piano; il blocco aperto è il collaudo.",
                }),
            },
            {
                "match": {
                    "agent_id": "coordinator",
                    "expected_output_schema": "verdict",
                },
                "result": result({
                    "next_action": "finalize",
                    "rationale": "Il responsabile ha fornito il briefing richiesto.",
                    "confidence": 0.9,
                    "focus": "Sintetizzare stato e blocco.",
                    "next_agents": [],
                    "resolved_disagreements": [],
                    "unresolved_disagreements": [],
                }),
            },
            {
                "match": {
                    "agent_id": "coordinator",
                    "expected_output_schema": "synthesis_content",
                },
                "result": result({
                    "integrated_answer": "Zeus procede; resta da chiudere il collaudo.",
                    "resolved_disagreements": [],
                    "unresolved_disagreements": [],
                    "confidence": 0.9,
                }),
            },
        ],
    })


def _workspace_with_guest(tmp_path):
    control = ControlPlane(tmp_path / ".symposium")
    control.ensure_initialized()
    control.create_room("Zeus Focus", "Aggiornamento operativo del progetto Zeus")
    control.create_agent(
        "zeus-lead",
        "Responsabile Zeus",
        "Riporta esclusivamente fatti verificati sul progetto Zeus.",
        capabilities=["stato progetto", "rischi delivery"],
    )
    control.invite_agent(
        "zeus-lead",
        room="zeus-focus",
        onboarding_context="Condividi milestone e blocchi, non dati commerciali.",
    )
    control.switch_room("zeus-focus")
    return control


def test_room_config_uses_only_speaking_members_and_their_private_context(tmp_path):
    control = _workspace_with_guest(tmp_path)
    control.create_agent("observer", "Osservatore", "Ascolta soltanto.")
    control.invite_agent("observer", room="zeus-focus", role="observer")

    config, room, participants = build_room_config(
        control,
        "Qual è lo stato?",
        session_id="room-session-test",
    )

    assert room.id == "zeus-focus"
    assert participants == ["zeus-lead"]
    assert [agent.id for agent in config.agents] == ["zeus-lead"]
    persona = config.agents[0].persona_ref
    assert not isinstance(persona, str)
    disclosed = "\n".join(persona.behavioral_constraints)
    assert "fatti verificati" in disclosed
    assert "milestone e blocchi" in disclosed
    assert room.purpose in disclosed
    assert config.coordinator.id == "coordinator"
    assert config.runtime.synthesize_on_terminate is True


def test_room_without_a_speaking_agent_cannot_start(tmp_path):
    control = ControlPlane(tmp_path / ".symposium")
    control.ensure_initialized()
    control.create_room("Vuota", "Solo Sartori ascolta")
    control.switch_room("vuota")

    with pytest.raises(ControlPlaneError, match="invita almeno un agente"):
        build_room_config(control, "C'è qualcuno?", session_id="empty-room")


def test_guest_briefing_runs_in_background_and_is_audited(tmp_path):
    control = _workspace_with_guest(tmp_path)

    def fake_router(config):
        fake = FakeProvider(script=_briefing_script("zeus-lead"))
        rewritten_agents = [
            agent.model_copy(update={"provider": "fake", "model": "fake-room"})
            for agent in config.agents
        ]
        coordinator = config.coordinator.model_copy(
            update={"provider": "fake", "model": "fake-room"}
        )
        routed = config.model_copy(
            update={"agents": rewritten_agents, "coordinator": coordinator}
        )
        return routed, {"default": fake}

    manager = RoomExecutionManager(
        control,
        tmp_path / "runs",
        provider_router=fake_router,
        id_factory=lambda: "1234567890abcdef1234567890abcdef",
    )
    started = manager.start("Qual è lo stato del progetto Zeus?")
    completed = manager.wait(started.id, timeout=5)

    assert completed.status == "completed"
    assert completed.outcome == "synthesis"
    run_dir = tmp_path / "runs" / completed.session_id
    assert (run_dir / "artifact.json").is_file()
    transcript = [
        json.loads(line)
        for line in (run_dir / "transcript.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert [message["speaker"] for message in transcript] == [
        "user:browser", "zeus-lead", "coordinator", "coordinator"
    ]
    assert "Zeus è in linea" in transcript[1]["content"]["text"]

    state = control.snapshot()
    assert [event.type for event in state.events[-2:]] == [
        "briefing_requested", "briefing_completed"
    ]
    assert state.events[-2].details["session_id"] == completed.session_id
    assert state.events[-2].details["participant_ids"] == ["zeus-lead"]
    assert state.events[-1].details["outcome"] == "synthesis"

    dismissed = control.dismiss_agent("zeus-lead")
    assert dismissed.presence == "offline"
    assert [event.type for event in control.snapshot().events[-3:]] == [
        "briefing_requested", "briefing_completed", "agent_dismissed"
    ]


def test_provider_preflight_failure_is_immediate_and_does_not_audit_a_run(tmp_path):
    control = _workspace_with_guest(tmp_path)
    before = len(control.snapshot().events)

    def unavailable(_config):
        raise RuntimeError("no supported CLI found on PATH")

    manager = RoomExecutionManager(
        control,
        tmp_path / "runs",
        provider_router=unavailable,
    )

    with pytest.raises(RuntimeError, match="no supported CLI"):
        manager.start("Qual è lo stato?")
    assert manager.public_jobs() == []
    assert len(control.snapshot().events) == before


def test_a_second_browser_run_is_rejected_while_one_is_active(tmp_path):
    control = _workspace_with_guest(tmp_path)
    entered = threading.Event()
    release = threading.Event()

    def router(config):
        return config, {}

    def blocking_runner(*_args, **_kwargs):
        entered.set()
        release.wait(timeout=5)
        raise RuntimeError("test worker released")

    manager = RoomExecutionManager(
        control,
        tmp_path / "runs",
        provider_router=router,
        runner=blocking_runner,
    )
    first = manager.start("Prima domanda")
    assert entered.wait(timeout=2)
    assert manager.has_active_job() is True

    with pytest.raises(ControlPlaneError, match="già in corso"):
        manager.start("Seconda domanda")

    release.set()
    assert manager.wait(first.id, timeout=2).status == "failed"
    assert manager.has_active_job() is False
    assert control.snapshot().events[-1].type == "briefing_failed"


def test_provider_termination_is_a_failed_job_with_actionable_error(tmp_path):
    control = _workspace_with_guest(tmp_path)

    def runner(*_args, **_kwargs):
        failure = types.SimpleNamespace(
            agent_id="zeus-lead",
            provider="claude-cli",
            message="upstream structured output failed",
        )
        termination = types.SimpleNamespace(
            reason="provider_unrecoverable",
            last_provider_failure=failure,
        )
        return types.SimpleNamespace(
            outcome=types.SimpleNamespace(
                kind="termination",
                termination_artifact=termination,
            )
        )

    manager = RoomExecutionManager(
        control,
        tmp_path / "runs",
        provider_router=lambda config: (config, {}),
        runner=runner,
        id_factory=lambda: "failed1234567890failed1234567890",
    )
    started = manager.start("Qual è lo stato?")
    failed = manager.wait(started.id, timeout=2)

    assert failed.status == "failed"
    assert failed.outcome == "termination"
    assert failed.termination_reason == "provider_unrecoverable"
    assert "zeus-lead" in failed.error
    assert "structured output failed" in failed.error
    event = control.snapshot().events[-1]
    assert event.type == "briefing_failed"
    assert event.details["termination_reason"] == "provider_unrecoverable"
    assert "structured output failed" in event.details["error"]

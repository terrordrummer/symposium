"""Persona-aware system prompt (v1.10.10+).

The pre-v1.10.10 system message was just `"persona={id}; round={N}"` —
minimal but uninformative. Live CLI personas (codex `visionary` especially)
read this as "no role information" and treated the deliberation as an
implementation task ("I'm blocked, sandbox is read-only"). v1.10.10
arricchisce il system prompt con persona charter + explicit deliberation
mode framing. These tests guard the contract.
"""

from __future__ import annotations

from symposium.models import ContextPacket, PanelDisclosureEntry
from symposium.personas import COORDINATOR, persona_by_id
from symposium.scheduler.loop import _build_persona_system_prompt


def _packet(persona, round_=1):
    return ContextPacket(
        persona_material=persona,
        problem_statement="solve X",
        round=round_,
        panel_disclosure=[PanelDisclosureEntry(id=persona.id, role_summary=persona.reasoning_scope)],
        current_round_messages=[],
        previous_verdict=None,
        parent_message=None,
        originating_direct_request=None,
    )


def test_system_prompt_carries_full_persona_charter():
    """Every charter field that informs the persona's behavior MUST
    appear verbatim in the system message — reasoning_scope,
    reasoning_style, behavioral_constraints, failure_modes,
    output_requirements. The pre-v1.10.10 prompt dropped all of them.
    """
    p = persona_by_id("logician")
    prompt = _build_persona_system_prompt(_packet(p), "turn_structured_output")

    assert p.id in prompt
    assert p.persona_class in prompt
    assert p.reasoning_scope in prompt
    assert p.reasoning_style in prompt
    for item in p.behavioral_constraints:
        assert item in prompt, f"missing behavioral_constraint: {item!r}"
    for item in p.failure_modes:
        assert item in prompt
    for item in p.output_requirements:
        assert item in prompt


def test_system_prompt_includes_deliberation_mode_framing():
    """The "DISCUSSANT not IMPLEMENTOR" framing is the most important bit
    for cli-auto runs: codex/claude under -s read-only will otherwise
    try to apply patches, get blocked, and waste their turn reporting
    the sandbox rejection instead of contributing analysis.
    """
    p = persona_by_id("visionary")
    prompt = _build_persona_system_prompt(_packet(p), "turn_structured_output")

    assert "DELIBERATION MODE" in prompt
    assert "DISCUSSANT" in prompt
    assert "IMPLEMENTOR" in prompt
    # Specific anti-pattern guidance — explicitly tells the persona NOT
    # to modify files / apply patches / spend turn on sandbox messages.
    assert "DO NOT modify" in prompt
    assert "apply patches" in prompt
    assert "read-only" in prompt
    assert "substantive analysis" in prompt


def test_system_prompt_coordinator_addendum_explains_next_action():
    """The coordinator-only addendum (Codex review T7-like for #C)
    documents what each `next_action` actually does at runtime — in
    particular that `continue` with phantom `next_agents` does NOT
    spawn new personas, and that `request_external_research` is the
    channel for panel expansion.
    """
    prompt = _build_persona_system_prompt(_packet(COORDINATOR), "verdict")

    assert "NEXT_ACTION GUIDANCE" in prompt
    # All 4 next_action enum values documented
    for action in ("continue", "finalize", "request_external_research",
                   "request_user_input"):
        assert f"`{action}`" in prompt, f"missing next_action doc: {action}"
    # The trap warning — next_agents can ONLY reference panel members
    assert "ALREADY IN THE PANEL" in prompt
    assert "Phantom IDs are silently" in prompt
    # The expansion channel guidance
    assert "expand the panel" in prompt or "expand the\n    panel" in prompt


def test_system_prompt_coordinator_addendum_omitted_for_other_personas():
    """The coordinator-only block MUST NOT appear on panel members'
    system prompts — they don't choose next_action and the noise would
    pollute their reasoning context.
    """
    p = persona_by_id("logician")
    prompt = _build_persona_system_prompt(_packet(p), "turn_structured_output")
    assert "NEXT_ACTION GUIDANCE" not in prompt


def test_system_prompt_carries_round_and_schema_context():
    """The runtime context (round number + expected output schema) MUST
    be in the prompt — the persona needs both to know which
    sub-schema to emit.
    """
    p = persona_by_id("engineer")
    prompt = _build_persona_system_prompt(_packet(p, round_=3), "synthesis_content")
    assert "Round: 3" in prompt
    assert "synthesis_content" in prompt


def test_system_prompt_output_guidance_is_schema_aware():
    """Pre-T9 the deliberation framing said "emit `text` + `direct_requests`"
    for EVERY call, which is wrong for the coordinator (`verdict`: emits
    next_action/rationale/etc) and synthesis (`synthesis_content`: emits
    integrated_answer/etc). The mismatch confused the very persona it
    tried to correct. Codex review T9 #3.
    """
    p = persona_by_id("logician")

    # turn_structured_output → text + direct_requests
    pt = _build_persona_system_prompt(_packet(p), "turn_structured_output")
    assert "`text`" in pt
    assert "`direct_requests`" in pt

    # verdict → next_action, rationale, etc — NO text, NO direct_requests as
    # output (the "general behavior" intro is fine without them)
    pv = _build_persona_system_prompt(_packet(COORDINATOR), "verdict")
    # output_guidance for verdict
    pv_content = pv
    assert "`next_action`" in pv_content
    assert "`rationale`" in pv_content
    assert "`next_agents`" in pv_content
    # Explicit "do not emit text/direct_requests" warning so a confused
    # model can't get away with mixing schemas.
    assert "DO NOT emit `text` or `direct_requests`" in pv_content

    # synthesis_content → integrated_answer, etc — same DO NOT warning
    ps = _build_persona_system_prompt(_packet(COORDINATOR), "synthesis_content")
    assert "`integrated_answer`" in ps
    assert "DO NOT emit `text` or `direct_requests`" in ps


def _packet_with_context(persona, *, previous_verdict=None, current_msgs=None):
    """Helper: ContextPacket with optional previous_verdict + current_round_messages."""
    return ContextPacket(
        persona_material=persona,
        problem_statement="solve X",
        round=2,
        panel_disclosure=[
            PanelDisclosureEntry(id="logician", role_summary="formal-structural"),
            PanelDisclosureEntry(id="visionary", role_summary="lateral-creative"),
        ],
        current_round_messages=current_msgs or [],
        previous_verdict=previous_verdict,
        parent_message=None,
        originating_direct_request=None,
    )


def test_user_prompt_serializes_full_packet_context():
    """Codex review T9 #6: pre-fix `build_provider_request` set
    `user.content = packet.problem_statement` — so the live persona only
    ever saw the original problem and was BLIND to panel disclosure,
    prior verdict, and current-round contributions. Especially the
    coordinator was asked to emit a verdict on a round whose
    contributions it could not see.
    """
    from symposium.models import (
        Message, Verdict, Usage,
    )
    from symposium.scheduler.loop import _build_packet_user_prompt

    prev_verdict = Verdict(
        next_action="continue",
        rationale="needs another round on numerical fidelity",
        focus="OTF sampling regime",
        next_agents=["logician", "engineer"],
        confidence=0.7,
        resolved_disagreements=[],
        unresolved_disagreements=[],
    )
    current_msgs = [
        Message(
            id="m1", speaker="logician", type="primary_turn",
            content={"text": "Logician here: the OTF FWHM should be 0.98λ/r0."},
            round=2, turn_index=1, branch_depth=0,
            timestamp="2026-05-28T00:00:00Z",
            usage=Usage(prompt_tokens=0, completion_tokens=0,
                        total_tokens=0, cost_usd=0.0),
        ),
        Message(
            id="m2", speaker="visionary", type="primary_turn",
            content={"text": "Visionary here: consider GPU batch FFT for throughput."},
            round=2, turn_index=2, branch_depth=0,
            timestamp="2026-05-28T00:00:00Z",
            usage=Usage(prompt_tokens=0, completion_tokens=0,
                        total_tokens=0, cost_usd=0.0),
        ),
    ]
    packet = _packet_with_context(
        COORDINATOR, previous_verdict=prev_verdict, current_msgs=current_msgs,
    )

    prompt = _build_packet_user_prompt(packet)

    # Problem statement first.
    assert "solve X" in prompt
    # Panel disclosure visible.
    assert "logician" in prompt and "formal-structural" in prompt
    assert "visionary" in prompt and "lateral-creative" in prompt
    # Previous verdict serialized.
    assert "PREVIOUS COORDINATOR VERDICT" in prompt
    assert "next_action: `continue`" in prompt
    assert "needs another round on numerical fidelity" in prompt
    assert "OTF sampling regime" in prompt
    assert "logician, engineer" in prompt
    # Current round contributions visible.
    assert "CURRENT ROUND" in prompt
    assert "Logician here:" in prompt
    assert "Visionary here:" in prompt
    assert "GPU batch FFT" in prompt

"""Orchestrator_runtime main loop — implements §4.11 pseudocode.

Walking-skeleton scope:
  - Single-provider-instance topology: every agent shares one
    `ProviderAdapter` instance (the FakeProvider). A future milestone
    will accept a provider registry keyed by agent_id.
  - `Selector` is fixed (R3): the default panel comes verbatim from
    `Config.selector.default_deliberation_panel`.
  - Hard caps: rounds, total tokens, total cost, wall-clock, branch
    depth, deferred queue length, per-agent token budget.
  - Failure handling: retries within the per-agent retry budget; on
    exhaustion, falls back to `on_agent_failure` (terminate /
    continue_without) per §4.9.
  - Synthesis: when `verdict.next_action = finalize`, one extra
    provider invocation is requested for the synthesis content.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from symposium.models import (
    AgentConfig,
    Artifact,
    Config,
    ContextPacket,
    DirectRequest,
    Message,
    PanelContractionContent,
    Persona,
    ProviderRequest,
    ProviderRequestMessage,
    ProviderResult,
    SchemaFailureRecord,
    SynthesisContent,
    SynthesisOutcome,
    TerminationArtifact,
    TerminationOutcome,
    TurnStructuredOutput,
    Usage,
    Verdict,
    now_utc_iso,
)
from symposium.providers.base import ProviderAdapter as _ProviderAdapter
from symposium.storage import RunDirectory, RunWriter, compute_transcript_digest


# ---------------------------------------------------------------------------
# Session state
# ---------------------------------------------------------------------------


@dataclass
class _Cumulative:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cost_usd: float = 0.0
    per_agent_tokens: Dict[str, int] = field(default_factory=dict)
    estimated_any: bool = False

    def add(self, agent_id: str, usage: Usage) -> None:
        self.prompt_tokens += usage.prompt_tokens
        self.completion_tokens += usage.completion_tokens
        self.total_tokens += usage.total_tokens
        self.cost_usd += usage.cost_usd
        if usage.estimated:
            self.estimated_any = True
        self.per_agent_tokens[agent_id] = (
            self.per_agent_tokens.get(agent_id, 0) + usage.total_tokens
        )

    def to_usage(self) -> Usage:
        return Usage(
            prompt_tokens=self.prompt_tokens,
            completion_tokens=self.completion_tokens,
            total_tokens=self.total_tokens,
            cost_usd=self.cost_usd,
            estimated=self.estimated_any,
        )


@dataclass
class Session:
    """Runtime session state (§4.1, §4.2)."""

    config: Config
    providers: Dict[str, _ProviderAdapter]
    transcript: List[Message] = field(default_factory=list)
    active_panel: List[str] = field(default_factory=list)
    deferred_queue: List[Tuple[DirectRequest, Message]] = field(default_factory=list)
    cumulative: _Cumulative = field(default_factory=_Cumulative)
    cumulative_unresolved: List = field(default_factory=list)
    round: int = 0
    turn_index: int = 0
    last_verdict: Optional[Verdict] = None
    started_at: str = field(default_factory=now_utc_iso)
    started_monotonic: float = field(default_factory=time.monotonic)

    # ------------------------------------------------------------------
    # Derived helpers
    # ------------------------------------------------------------------

    def agent_by_id(self, agent_id: str) -> AgentConfig:
        for a in self.config.agents:
            if a.id == agent_id:
                return a
        if self.config.coordinator.id == agent_id:
            return self.config.coordinator
        raise KeyError(f"agent {agent_id!r} not in config")

    def panel_disclosure(self) -> List[dict]:
        out: List[dict] = []
        for aid in self.active_panel:
            ac = self.agent_by_id(aid)
            persona_id = (
                ac.persona_ref.id if isinstance(ac.persona_ref, Persona) else ac.persona_ref
            )
            out.append({"id": ac.id, "role_summary": f"{persona_id} ({ac.provider}/{ac.model})"})
        return out

    def messages_this_round(self) -> List[Message]:
        return [m for m in self.transcript if m.round == self.round]


# ---------------------------------------------------------------------------
# §4.7 hard cap checks
# ---------------------------------------------------------------------------


def check_hard_caps(session: Session) -> Optional[str]:
    """Return a termination reason if any *terminating* cap is breached, else None.

    Per §8.1 + §4.7, the terminating caps are: max_rounds, max_wallclock_seconds,
    max_total_cost_usd, max_total_tokens, per_agent_token_budget.
    Non-terminating caps (max_branch_depth, max_deferred_queue_length,
    max_deferred_drains_per_round) are enforced at their use sites and
    cause defer / drop without calling terminate.
    """
    b = session.config.budget
    if session.cumulative.total_tokens > b.max_total_tokens:
        return "budget_exceeded"
    if session.cumulative.cost_usd > b.max_total_cost_usd:
        return "budget_exceeded"
    if (time.monotonic() - session.started_monotonic) > b.max_wallclock_seconds:
        return "timeout"
    if session.round > b.max_rounds:
        return "budget_exceeded"
    if b.per_agent_token_budget:
        for aid, cap in b.per_agent_token_budget.items():
            if session.cumulative.per_agent_tokens.get(aid, 0) > cap:
                return "budget_exceeded"
    return None


# ---------------------------------------------------------------------------
# §4.5 direct_request routability
# ---------------------------------------------------------------------------


def is_routable_direct_request(
    request: DirectRequest,
    originator: str,
    active_panel: List[str],
    coordinator: str,
) -> bool:
    """§4.5: target must name a current panel member other than originator,
    must not be the coordinator, and must not be an undeclared id."""
    if request.target == originator:
        return False
    if request.target == coordinator:
        return False
    if request.target not in active_panel:
        return False
    return True


# ---------------------------------------------------------------------------
# §4.3 context_packet derivation
# ---------------------------------------------------------------------------


def derive_context_packet(
    session: Session,
    *,
    agent_id: str,
    parent_message: Optional[Message] = None,
    originating_direct_request: Optional[DirectRequest] = None,
) -> ContextPacket:
    """Minimum content set per §4.3.

    Walking-skeleton: full transcript-of-this-round + previous verdict.
    No compression / windowing (those are allowed beyond the minimum).
    """
    ac = session.agent_by_id(agent_id)
    if isinstance(ac.persona_ref, Persona):
        persona = ac.persona_ref
    else:
        # In a richer implementation a persona registry resolves the id.
        # For the walking skeleton, inline-persona is the only supported form.
        raise ValueError(
            f"agent {agent_id!r} persona_ref is a string id; "
            "the MVP requires inline Persona objects"
        )
    return ContextPacket(
        problem_statement=session.config.problem_statement,
        round=max(session.round, 1),
        persona_material=persona,
        panel_disclosure=session.panel_disclosure(),
        previous_verdict=session.last_verdict,
        current_round_messages=session.messages_this_round(),
        parent_message=parent_message,
        originating_direct_request=originating_direct_request,
    )


# ---------------------------------------------------------------------------
# Provider request construction
# ---------------------------------------------------------------------------


def build_provider_request(
    session: Session,
    *,
    agent_id: str,
    expected_output_schema: str,
    packet: ContextPacket,
) -> ProviderRequest:
    ac = session.agent_by_id(agent_id)
    # The provider request shape is intentionally minimal in the walking
    # skeleton — the FakeProvider's match clause asserts on agent_id and
    # expected_output_schema only.
    messages = [
        ProviderRequestMessage(
            role="system",
            content=f"persona={packet.persona_material.id}; round={packet.round}",
        ),
        ProviderRequestMessage(
            role="user",
            content=packet.problem_statement,
        ),
    ]
    return ProviderRequest(
        provider=ac.provider,
        model=ac.model,
        agent_id=agent_id,
        messages=messages,
        sampling=None,
        tools=ac.tools,
        expected_output_schema=expected_output_schema,  # type: ignore[arg-type]
    )


# ---------------------------------------------------------------------------
# §4.9 invocation with retry budget
# ---------------------------------------------------------------------------


@dataclass
class _InvokeResult:
    ok: bool
    result: Optional[ProviderResult]
    terminating: Optional[str] = None  # termination reason (TerminationReason value)
    panel_contracted: bool = False
    contraction_reason: Optional[str] = None  # "provider_unrecoverable" | "schema_error"


def _provider_for(session: Session, agent_id: str) -> _ProviderAdapter:
    return session.providers.get(agent_id) or session.providers["default"]


def _invoke_with_retry(
    session: Session,
    *,
    agent_id: str,
    request: ProviderRequest,
) -> _InvokeResult:
    ac = session.agent_by_id(agent_id)
    budget = ac.retry_budget if ac.retry_budget is not None else session.config.runtime.per_agent_retry_budget
    attempts = 1 + budget

    last_err_kind: Optional[str] = None
    for _ in range(attempts):
        provider = _provider_for(session, agent_id)
        # If FakeProvider: stash round/turn_index hints so match assertions work.
        if hasattr(provider, "last_request_round"):
            provider.last_request_round = session.round  # type: ignore[attr-defined]
            provider.last_request_turn_index = session.turn_index  # type: ignore[attr-defined]
        result = provider.invoke(request)
        session.cumulative.add(agent_id, result.usage)

        if result.error is None and result.structured_output is not None:
            return _InvokeResult(ok=True, result=result)

        if result.error is not None:
            last_err_kind = result.error.kind
            if not result.error.retriable:
                # Map §6.6 / §4.9: non-retriable error → either schema_error
                # (malformed_response, invalid_request) or provider_unrecoverable
                # (everything else).
                reason = _classify_unrecoverable(result.error.kind)
                # §4.9 path: per on_agent_failure
                if session.config.runtime.on_agent_failure == "terminate":
                    return _InvokeResult(ok=False, result=result, terminating=reason)
                return _InvokeResult(
                    ok=False,
                    result=result,
                    panel_contracted=True,
                    contraction_reason=reason,  # type: ignore[arg-type]
                )
        else:
            # structured_output is None without error — treat as malformed
            last_err_kind = "malformed_response"

    # Retries exhausted.
    final_reason = _classify_unrecoverable(last_err_kind or "internal")
    if session.config.runtime.on_agent_failure == "terminate":
        return _InvokeResult(ok=False, result=None, terminating=final_reason)
    return _InvokeResult(
        ok=False,
        result=None,
        panel_contracted=True,
        contraction_reason=final_reason,  # type: ignore[arg-type]
    )


def _classify_unrecoverable(kind: str) -> str:
    if kind in ("malformed_response", "invalid_request"):
        return "schema_error"
    return "provider_unrecoverable"


# ---------------------------------------------------------------------------
# §4.11 main loop
# ---------------------------------------------------------------------------


def run_session(
    config: Config,
    providers: Dict[str, _ProviderAdapter],
    *,
    runs_root: Optional[str] = None,
) -> Artifact:
    """Run a session to completion. Returns the persisted Artifact.

    `providers` is a mapping from agent_id (or "default") to a ProviderAdapter
    instance. For the FakeProvider walking skeleton, pass `{"default": fp}`.

    If `runs_root` is given, the session is persisted under
    `runs_root/<session_id>/`.
    """
    session = Session(config=config, providers=providers)
    session.active_panel = list(config.selector.default_deliberation_panel)

    writer: Optional[RunWriter] = None
    if runs_root is not None:
        from pathlib import Path

        rd = RunDirectory.for_session(Path(runs_root), config.session_id)
        writer = RunWriter(rd)
        writer.start(config, session.started_at)

    # 1) problem_statement message (round 0, turn_index 0)
    problem_msg = Message(
        id=_new_id(),
        speaker=config.originator,
        type="problem_statement",
        content=config.problem_statement,
        round=0,
        turn_index=0,
        branch_depth=0,
        timestamp=now_utc_iso(),
        usage=_zero_usage(),
    )
    session.transcript.append(problem_msg)
    if writer:
        writer.append_message(problem_msg)

    # 2) round loop
    artifact, term = _run_rounds(session, writer)

    # 3) emit artifact
    ended_at = now_utc_iso()
    final_artifact = Artifact(
        session_id=config.session_id,
        config=config,
        canonical_transcript=session.transcript,
        outcome=artifact,  # SynthesisOutcome or TerminationOutcome
        cumulative_usage=session.cumulative.to_usage(),
        cumulative_unresolved=session.cumulative_unresolved,
        transcript_digest=compute_transcript_digest(session.transcript),
        started_at=session.started_at,
        ended_at=ended_at,
    )
    if writer:
        writer.finalize(final_artifact, termination=term, updated_at=ended_at)
    return final_artifact


def _run_rounds(session: Session, writer: Optional[RunWriter]):
    """Execute rounds until termination or finalize. Returns (Outcome, Optional[TerminationArtifact])."""
    while True:
        # Open a new round
        session.round += 1
        session.turn_index = 0

        breach = check_hard_caps(session)
        if breach:
            return _terminate(session, writer, reason=breach)

        # §4.6 deferred queue drain (at round-open, before any primary_turn)
        _drain_deferred_queue(session, writer)

        # §4.2 step 1–3: panel members in declared order
        for agent_id in list(session.active_panel):
            breach = check_hard_caps(session)
            if breach:
                return _terminate(session, writer, reason=breach)

            packet = derive_context_packet(session, agent_id=agent_id)
            req = build_provider_request(
                session,
                agent_id=agent_id,
                expected_output_schema="turn_structured_output",
                packet=packet,
            )
            session.turn_index += 1
            inv = _invoke_with_retry(session, agent_id=agent_id, request=req)
            if inv.terminating:
                return _terminate(session, writer, reason=inv.terminating)
            if inv.panel_contracted:
                _append_panel_contraction(session, writer, agent_id, inv.contraction_reason)
                # remove agent from panel; check if panel is non-empty
                if agent_id in session.active_panel:
                    session.active_panel.remove(agent_id)
                if not session.active_panel:
                    return _terminate(session, writer, reason="provider_unrecoverable")
                continue
            assert inv.result is not None and inv.result.structured_output is not None
            tso = TurnStructuredOutput.model_validate(inv.result.structured_output)

            primary_msg = Message(
                id=_new_id(),
                speaker=agent_id,
                type="primary_turn",
                content=tso.model_dump(exclude_none=True),
                round=session.round,
                turn_index=session.turn_index,
                branch_depth=0,
                timestamp=now_utc_iso(),
                usage=inv.result.usage,
            )

            # §4.5 fork dispatch over direct_requests
            schema_failures: List[SchemaFailureRecord] = []
            dispatched_inline = False
            deferred_for_msg: List[DirectRequest] = []
            for dr in tso.direct_requests or []:
                if not is_routable_direct_request(
                    dr, originator=agent_id,
                    active_panel=session.active_panel,
                    coordinator=session.config.coordinator.id,
                ):
                    schema_failures.append(SchemaFailureRecord(
                        offending_request=dr.model_dump(exclude_none=True),
                        reason="target not in active_panel, is originator, or is coordinator",
                    ))
                    continue
                if not dispatched_inline and session.config.runtime.max_branch_depth >= 1:
                    # in-line dispatch
                    _dispatch_branch(session, writer, parent_msg=primary_msg, request=dr)
                    dispatched_inline = True
                else:
                    deferred_for_msg.append(dr)

            # Apply §4.6 deferred queue with overflow drop
            dropped: List[DirectRequest] = []
            for dr in deferred_for_msg:
                if len(session.deferred_queue) >= session.config.runtime.max_deferred_queue_length:
                    dropped.append(dr)
                else:
                    session.deferred_queue.append((dr, primary_msg))

            if schema_failures:
                primary_msg.schema_failure = schema_failures
            if dropped:
                primary_msg.dropped_deferred = dropped

            session.transcript.append(primary_msg)
            if writer:
                writer.append_message(primary_msg)

        # §4.2 step 4: coordination_turn
        breach = check_hard_caps(session)
        if breach:
            return _terminate(session, writer, reason=breach)

        coord_id = session.config.coordinator.id
        packet = derive_context_packet(session, agent_id=coord_id)
        req = build_provider_request(
            session,
            agent_id=coord_id,
            expected_output_schema="verdict",
            packet=packet,
        )
        session.turn_index += 1
        inv = _invoke_with_retry(session, agent_id=coord_id, request=req)
        if inv.terminating:
            return _terminate(session, writer, reason=inv.terminating)
        if inv.panel_contracted:
            # Coordinator cannot be contracted per §4.9 — escalate.
            return _terminate(session, writer, reason="provider_unrecoverable")
        assert inv.result is not None and inv.result.structured_output is not None

        verdict = Verdict.model_validate(inv.result.structured_output)
        coord_msg = Message(
            id=_new_id(),
            speaker=coord_id,
            type="coordination_turn",
            content=verdict.model_dump(exclude_none=True),
            round=session.round,
            turn_index=session.turn_index,
            branch_depth=0,
            timestamp=now_utc_iso(),
            usage=inv.result.usage,
        )
        session.transcript.append(coord_msg)
        if writer:
            writer.append_message(coord_msg)
        session.last_verdict = verdict
        # Carry forward cumulative unresolved (M5).
        for ud in verdict.unresolved_disagreements:
            session.cumulative_unresolved.append(ud.model_dump(exclude_none=True))

        breach = check_hard_caps(session)
        if breach:
            return _terminate(session, writer, reason=breach)

        # §4.4 verdict dispatch
        if verdict.next_action == "continue":
            continue
        if verdict.next_action == "finalize":
            return _attempt_finalize(session, writer)
        if verdict.next_action == "request_user_input":
            return _terminate(
                session,
                writer,
                reason="user_input_required",
                pending=verdict.user_input_request,
            )
        if verdict.next_action == "request_external_research":
            return _terminate(
                session,
                writer,
                reason="external_research_required",
                pending=verdict.external_research_request,
            )


def _dispatch_branch(session: Session, writer: Optional[RunWriter], *, parent_msg: Message, request: DirectRequest) -> None:
    target = request.target
    packet = derive_context_packet(
        session,
        agent_id=target,
        parent_message=parent_msg,
        originating_direct_request=request,
    )
    req = build_provider_request(
        session,
        agent_id=target,
        expected_output_schema="turn_structured_output",
        packet=packet,
    )
    session.turn_index += 1
    inv = _invoke_with_retry(session, agent_id=target, request=req)
    if inv.terminating:
        # We cannot bubble up termination through _dispatch_branch without
        # restructuring. Pragmatic walking-skeleton: treat as panel contraction.
        _append_panel_contraction(session, writer, target, "provider_unrecoverable")
        if target in session.active_panel:
            session.active_panel.remove(target)
        return
    if inv.panel_contracted:
        _append_panel_contraction(session, writer, target, inv.contraction_reason)
        if target in session.active_panel:
            session.active_panel.remove(target)
        return
    assert inv.result is not None and inv.result.structured_output is not None
    tso = TurnStructuredOutput.model_validate(inv.result.structured_output)

    # §4.5 B→C suppression: branch-origin direct_requests become
    # `suggested_followups`, never dispatched.
    suggested_followups = list(tso.direct_requests or []) or None

    branch_msg = Message(
        id=_new_id(),
        speaker=target,
        type="branch_turn",
        content=TurnStructuredOutput(text=tso.text).model_dump(exclude_none=True),
        parent_id=parent_msg.id,
        round=session.round,
        turn_index=session.turn_index,
        branch_depth=1,
        timestamp=now_utc_iso(),
        usage=inv.result.usage,
        suggested_followups=suggested_followups,
    )
    session.transcript.append(branch_msg)
    if writer:
        writer.append_message(branch_msg)


def _drain_deferred_queue(session: Session, writer: Optional[RunWriter]) -> None:
    drains = 0
    max_drains = session.config.runtime.max_deferred_drains_per_round
    while session.deferred_queue and drains < max_drains:
        request, parent_msg = session.deferred_queue.pop(0)
        # Re-check routability at drain time (panel may have contracted).
        if not is_routable_direct_request(
            request,
            originator=parent_msg.speaker,
            active_panel=session.active_panel,
            coordinator=session.config.coordinator.id,
        ):
            continue
        _dispatch_branch(session, writer, parent_msg=parent_msg, request=request)
        drains += 1


def _attempt_finalize(session: Session, writer: Optional[RunWriter]):
    """§4.8 — invoke coordinator for a synthesis. On failure, fall back to terminate."""
    coord_id = session.config.coordinator.id
    packet = derive_context_packet(session, agent_id=coord_id)
    req = build_provider_request(
        session,
        agent_id=coord_id,
        expected_output_schema="synthesis_content",
        packet=packet,
    )
    session.turn_index += 1
    inv = _invoke_with_retry(session, agent_id=coord_id, request=req)
    if inv.ok and inv.result is not None and inv.result.structured_output is not None:
        sc = SynthesisContent.model_validate(inv.result.structured_output)
        synth_msg = Message(
            id=_new_id(),
            speaker=coord_id,
            type="synthesis",
            content=sc.model_dump(exclude_none=True),
            round=session.round,
            turn_index=session.turn_index,
            branch_depth=0,
            timestamp=now_utc_iso(),
            usage=inv.result.usage,
        )
        session.transcript.append(synth_msg)
        if writer:
            writer.append_message(synth_msg)
        return SynthesisOutcome(synthesis_message_id=synth_msg.id), None

    # Synthesis failed → terminate with provider_unrecoverable (R2).
    return _terminate(session, writer, reason="provider_unrecoverable")


def _terminate(session: Session, writer: Optional[RunWriter], *, reason: str, pending=None):
    """Build a TerminationOutcome and the per-§5.8 TerminationArtifact."""
    cumulative_usage = session.cumulative.to_usage()
    # Need the digest of the transcript *as terminated* (no synthesis added).
    digest = compute_transcript_digest(session.transcript)

    kwargs = dict(
        reason=reason,
        final_round=session.round,
        cumulative_usage=cumulative_usage,
        most_recent_verdict=session.last_verdict,
        unresolved_disagreements=session.last_verdict.unresolved_disagreements if session.last_verdict else [],
        transcript_digest=digest,
    )
    if reason == "user_input_required" and pending is not None:
        kwargs["pending_user_input_request"] = pending
    elif reason == "external_research_required" and pending is not None:
        kwargs["pending_external_research_request"] = pending

    term = TerminationArtifact(**kwargs)  # type: ignore[arg-type]
    outcome = TerminationOutcome(termination_artifact=term)
    return outcome, term


def _append_panel_contraction(
    session: Session,
    writer: Optional[RunWriter],
    agent_id: str,
    reason: Optional[str],
) -> None:
    session.turn_index += 1
    pc_msg = Message(
        id=_new_id(),
        speaker="runtime",
        type="panel_contraction",
        content=PanelContractionContent(
            agent_id=agent_id,
            reason=reason or "provider_unrecoverable",  # type: ignore[arg-type]
        ).model_dump(),
        round=session.round,
        turn_index=session.turn_index,
        branch_depth=0,
        timestamp=now_utc_iso(),
        usage=_zero_usage(),
    )
    session.transcript.append(pc_msg)
    if writer:
        writer.append_message(pc_msg)


# ---------------------------------------------------------------------------
# tiny helpers
# ---------------------------------------------------------------------------


def _zero_usage() -> Usage:
    return Usage(prompt_tokens=0, completion_tokens=0, total_tokens=0, cost_usd=0.0)


def _new_id() -> str:
    return uuid.uuid4().hex

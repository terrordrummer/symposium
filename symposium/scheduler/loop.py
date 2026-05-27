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

import random
import time
import uuid
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

# Exponential backoff bounds for retriable provider failures (§4.9). The
# initial sleep is small enough to not slow down tests against the
# FakeProvider; the cap is small enough to not blow past a session
# wallclock cap. Jitter is multiplicative ±25%.
_BACKOFF_BASE_SECONDS = 0.25
_BACKOFF_MAX_SECONDS = 8.0
_BACKOFF_JITTER = 0.25


def _backoff_delay(
    attempt_idx: int,
    retry_after: Optional[float] = None,
    rng: Optional[random.Random] = None,
) -> float:
    """Compute the sleep before attempt `attempt_idx` (1-indexed retry count).

    Honors `retry_after` from the upstream when present (e.g. rate_limit /
    quota_exhausted), bounded by `_BACKOFF_MAX_SECONDS` so a hostile server
    cannot pin a deliberation past its caps. Otherwise uses exponential
    backoff base*2**(attempt-1), clamped, with multiplicative jitter to
    avoid synchronized thundering-herd retries across agents.

    `rng` is a session-bound `random.Random` instance so that, with the
    same seed (derived from session_id), replays produce the same jitter
    sequence — keeping backoff sleeps out of the §7.6 replay-divergence
    surface (otherwise an unrelated retry could push wallclock past a cap
    and flip a termination decision between runs).
    """
    if retry_after is not None and retry_after >= 0:
        return min(float(retry_after), _BACKOFF_MAX_SECONDS)
    raw = _BACKOFF_BASE_SECONDS * (2 ** (max(attempt_idx, 1) - 1))
    bounded = min(raw, _BACKOFF_MAX_SECONDS)
    source = rng if rng is not None else random
    jitter = 1.0 + source.uniform(-_BACKOFF_JITTER, _BACKOFF_JITTER)
    return max(bounded * jitter, 0.0)

from symposium.models import (
    AgentConfig,
    Artifact,
    Config,
    ContextPacket,
    DirectRequest,
    LastProviderFailure,
    Message,
    PanelContractionContent,
    Persona,
    ProviderError,
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
from symposium.selector import (
    SelectorBudgetExceeded,
    SelectorError,
    run_selector,
)
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
    # Session-bound RNG used for retry-backoff jitter. Seeded deterministically
    # from a *replay-stable* identity so the jitter sequence in the original
    # run matches the replay (§7.6 replay-divergence guard). The default
    # derives from `session_id`, but execution_replay overrides via
    # `rng_seed=` to keep the seed identical across the original and the
    # `<session_id>-replay` reproduction.
    rng_seed: Optional[str] = None
    rng: random.Random = field(default=None)  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.rng is None:
            seed_token = self.rng_seed or self.config.session_id
            self.rng = random.Random(f"symposium:backoff:{seed_token}")

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


def _build_persona_system_prompt(packet: ContextPacket, expected_output_schema: str) -> str:
    """Render the persona's full charter + "deliberation mode" framing
    into the system message for a turn (v1.10.10+).

    The pre-v1.10.10 system message — `"persona={id}; round={N}"` —
    left the live CLI personas with zero awareness of their role,
    causing the codex `visionary` to misread the deliberation as an
    implementation task ("I'm blocked, the sandbox is read-only").
    """
    p = packet.persona_material

    def _bul(items):
        return "\n".join(f"  - {it}" for it in items) if items else "  (none)"

    role_block = (
        f"## YOUR ROLE — {p.id} ({p.persona_class})\n"
        f"Reasoning scope: {p.reasoning_scope}\n"
        f"Reasoning style: {p.reasoning_style}\n"
        f"\n## BEHAVIORAL CONSTRAINTS\n{_bul(p.behavioral_constraints)}\n"
        f"\n## KNOWN FAILURE MODES TO AVOID\n{_bul(p.failure_modes)}\n"
        f"\n## OUTPUT REQUIREMENTS\n{_bul(p.output_requirements)}\n"
    )

    # The "discussant, not implementor" framing is the most important
    # bit for cli-auto runs: codex/claude under `-s read-only` will
    # otherwise try to apply patches, get blocked, and waste their
    # turn reporting the sandbox rejection instead of contributing
    # analysis.
    #
    # Schema-aware output guidance (Codex review T9 #3): pre-fix the
    # framing said "emit `text` + `direct_requests`" for EVERY call,
    # which is wrong for the coordinator (`verdict`: emits
    # next_action/rationale/focus/next_agents/disagreement fields) and
    # the synthesis turn (`synthesis_content`: emits
    # integrated_answer/disagreements/confidence/open_questions). The
    # mismatch confused the very persona it tried to correct.
    deliberation_intro = (
        "\n## DELIBERATION MODE — IMPORTANT\n"
        "You are participating in a STRUCTURED MULTI-AGENT DELIBERATION as the "
        f"`{p.id}` panelist. Your job is to ANALYZE the problem statement from "
        "your perspective and CONTRIBUTE your viewpoint to the panel. You are a "
        "DISCUSSANT, not an IMPLEMENTOR.\n"
        "\n"
        "General behavior (same for every turn):\n"
        "  - DO read project files (Read, Grep, Glob, Bash) to ground your analysis\n"
        "    in the actual code.\n"
        "  - DO NOT modify files, apply patches, write code, run tests, or attempt\n"
        "    any side-effect on the project — the sandbox is read-only by design,\n"
        "    AND that's not your job here.\n"
        "  - DO NOT spend your turn reporting tool/sandbox limitations — focus on\n"
        "    substantive analysis.\n"
    )
    if expected_output_schema == "turn_structured_output":
        output_guidance = (
            "\nOutput shape for this turn (`turn_structured_output`):\n"
            "  - `text`: your reasoning, the substantive contribution. Required,\n"
            "    non-empty.\n"
            "  - `direct_requests`: list of `{target, type, content}` objects\n"
            "    to address another panelist directly when you need their\n"
            "    specific input. Use sparingly. **Emit `null` when you have\n"
            "    nothing to request** (the strict-mode schema requires the\n"
            "    field be present; the runtime treats `null` as 'no requests').\n"
        )
    elif expected_output_schema == "verdict":
        output_guidance = (
            "\nOutput shape for this turn (`verdict` — coordinator role):\n"
            "  - `next_action`: one of `continue` / `finalize` /\n"
            "    `request_user_input` / `request_external_research` — see\n"
            "    NEXT_ACTION GUIDANCE below.\n"
            "  - `rationale`: WHY you chose that next_action, grounded in the\n"
            "    round's contributions. Required.\n"
            "  - `focus`: what the panel should attend to next (free-text).\n"
            "  - `next_agents`: subset of CURRENT PANEL ids to speak next round\n"
            "    (only valid with `continue`).\n"
            "  - `resolved_disagreements` / `unresolved_disagreements`: list of\n"
            "    structured disagreement objects from this round.\n"
            "  - `user_input_request` / `external_research_request`: payload for\n"
            "    the matching next_action. **Emit `null` when next_action\n"
            "    doesn't require it** (strict schemas keep these fields present;\n"
            "    null = 'no payload').\n"
            "  - DO NOT emit `text` or `direct_requests` — those belong to\n"
            "    panel-member turns, not the verdict schema.\n"
        )
    elif expected_output_schema == "synthesis_content":
        output_guidance = (
            "\nOutput shape for this turn (`synthesis_content` — synthesis role):\n"
            "  - `integrated_answer`: the final synthesized answer the panel\n"
            "    converged on. Required, non-empty.\n"
            "  - `resolved_disagreements` / `unresolved_disagreements`: structured\n"
            "    summaries of where the panel landed.\n"
            "  - `confidence`: float in [0,1].\n"
            "  - `open_questions`: list of questions that remain after synthesis.\n"
            "  - DO NOT emit `text` or `direct_requests` — those belong to\n"
            "    panel-member turns, not the synthesis schema.\n"
        )
    else:
        output_guidance = ""
    deliberation_framing = deliberation_intro + output_guidance

    context_block = (
        f"\n## CONTEXT\nRound: {packet.round} of the deliberation. "
        f"Output schema: `{expected_output_schema}`.\n"
    )

    # Coordinator-only addendum: pre-v1.10.10 the coordinator had no
    # explicit guidance on what each `next_action` enum value means
    # operationally, and especially what `next_agents` accepts.
    # Observed in production: the coordinator emitted
    # `next_action="continue", next_agents=["fourier-optics-physicist",
    # "numerical-fft-methods-expert"]` — referencing experts that do
    # NOT exist in the panel, expecting the runtime to spawn them.
    # The runtime can only spawn dynamically via
    # `request_external_research` (the adaptive `_pending_need` reads
    # only that termination reason + `request_user_input`); a
    # `continue` with phantom next_agents just gets the existing panel
    # back. Spelling this out in the coordinator's system prompt
    # avoids the silent failure mode.
    coordinator_addendum = ""
    if p.id == "coordinator":
        coordinator_addendum = (
            "\n## NEXT_ACTION GUIDANCE (coordinator-only)\n"
            "Choose `next_action` from the 4-value enum and respect what each\n"
            "actually does at runtime — the host scheduler dispatches on this\n"
            "value:\n"
            "\n"
            "  - `continue`: panel keeps going for another round. `next_agents`\n"
            "    selects who speaks next, but can ONLY reference panel members\n"
            "    that are ALREADY IN THE PANEL. Phantom IDs are silently\n"
            "    ignored — they do NOT cause a new persona to be created.\n"
            "  - `finalize`: panel has converged; synthesis is triggered.\n"
            "    Use when no new substantive claims would emerge from another\n"
            "    round.\n"
            "  - `request_external_research`: the deliberation pauses pending\n"
            "    external info. **This is also the channel to expand the\n"
            "    panel with a new domain expert** — describe the missing\n"
            "    expertise as a `query` in the\n"
            "    `external_research_request` payload, and the runtime's\n"
            "    adaptive loop will spawn a `Persona` matching that need\n"
            "    and continue the deliberation with the augmented panel.\n"
            "  - `request_user_input`: pause for operator clarification. Use\n"
            "    only when the human-in-the-loop is genuinely needed; do not\n"
            "    use this as a substitute for `request_external_research`.\n"
        )

    return role_block + deliberation_framing + context_block + coordinator_addendum


def _build_packet_user_prompt(packet: ContextPacket) -> str:
    """Serialize the `ContextPacket` into the user-message content
    (v1.10.10+, Codex review T9 #6).

    Pre-fix `build_provider_request` set `user.content = packet.
    problem_statement` — so the live persona only ever saw the
    original problem and was BLIND to the panel disclosure, the prior
    verdict, the current round's exchange, and any branch-origin
    parent. Especially for the coordinator: it was asked to emit a
    verdict on a round whose contributions it could not see.

    This builder lays out the packet in a stable, human-readable
    Markdown shape: problem statement first, then panel, then
    previous verdict (if any), then current-round messages in
    speaker/type order, then branch context (if any).
    """
    parts: List[str] = ["# PROBLEM STATEMENT\n", packet.problem_statement, "\n"]

    if packet.panel_disclosure:
        parts.append("\n# PANEL\n")
        for entry in packet.panel_disclosure:
            parts.append(f"- `{entry.id}` — {entry.role_summary}\n")

    if packet.previous_verdict is not None:
        pv = packet.previous_verdict
        parts.append("\n# PREVIOUS COORDINATOR VERDICT\n")
        # Readable summary first…
        parts.append(f"- next_action: `{pv.next_action}`\n")
        if pv.rationale:
            parts.append(f"- rationale: {pv.rationale}\n")
        if pv.focus:
            parts.append(f"- focus: {pv.focus}\n")
        if pv.next_agents:
            parts.append(f"- next_agents: {', '.join(pv.next_agents)}\n")
        if pv.confidence is not None:
            parts.append(f"- confidence: {pv.confidence}\n")
        if pv.unresolved_disagreements:
            parts.append(
                f"- unresolved disagreements: "
                f"{len(pv.unresolved_disagreements)} item(s)\n"
            )
        if pv.resolved_disagreements:
            parts.append(
                f"- resolved disagreements: "
                f"{len(pv.resolved_disagreements)} item(s)\n"
            )
        # …then the full dump so the persona has access to disagreement
        # topics/positions and any user_input_request /
        # external_research_request payloads. Codex review T10 minor:
        # the readable summary lost the bodies of the disagreement
        # entries, which is exactly what the next-round panelist needs
        # to address.
        parts.append("- full verdict (json):\n")
        parts.append(
            f"```json\n{pv.model_dump_json(exclude_none=True)}\n```\n"
        )

    if packet.current_round_messages:
        parts.append("\n# CURRENT ROUND — CONTRIBUTIONS SO FAR\n")
        for msg in packet.current_round_messages:
            text = ""
            c = msg.content
            if isinstance(c, dict):
                # mirror the get_run_status fallback chain (Codex T7 #6)
                for key in ("text", "integrated_answer", "rationale", "focus"):
                    v = c.get(key)
                    if isinstance(v, str) and v:
                        text = v
                        break
            elif isinstance(c, str):
                text = c
            parts.append(f"\n## {msg.speaker} ({msg.type})\n{text}\n")

    if packet.parent_message is not None:
        pm = packet.parent_message
        parts.append("\n# BRANCH ORIGIN\n")
        pmtext = ""
        c = pm.content
        if isinstance(c, dict):
            pmtext = c.get("text") or c.get("rationale") or ""
        elif isinstance(c, str):
            pmtext = c
        parts.append(f"From `{pm.speaker}` ({pm.type}): {pmtext}\n")
    if packet.originating_direct_request is not None:
        odr = packet.originating_direct_request
        parts.append(
            f"\n# DIRECT REQUEST TO YOU\n"
            f"target=`{odr.target}` type=`{odr.type}` content={odr.content!r}\n"
        )

    return "".join(parts)


def build_provider_request(
    session: Session,
    *,
    agent_id: str,
    expected_output_schema: str,
    packet: ContextPacket,
) -> ProviderRequest:
    ac = session.agent_by_id(agent_id)
    # System prompt carries the persona's full charter + an explicit
    # "deliberation mode" framing (v1.10.10+). Pre-v1.10.10 the system
    # message was just `"persona={id}; round={N}"` — minimal enough to
    # match the walking-skeleton FakeProvider, but it left the live
    # claude/codex CLI personas without any awareness of their role.
    # Observed failure mode: with cli-auto on a coding problem, the
    # `visionary` persona repeatedly responded "I'm blocked — the
    # sandbox is read-only, I cannot apply the patch" — treating the
    # deliberation as an implementation task instead of as analysis.
    # The richer system prompt frames the panelist as a DISCUSSANT
    # who can read the project for grounding but must not implement.
    # FakeProvider matching is unaffected (it asserts on agent_id /
    # round / turn_index / expected_output_schema, not message content).
    messages = [
        ProviderRequestMessage(
            role="system",
            content=_build_persona_system_prompt(packet, expected_output_schema),
        ),
        ProviderRequestMessage(
            role="user",
            content=_build_packet_user_prompt(packet),
        ),
    ]
    return ProviderRequest(
        provider=ac.provider,
        model=ac.model,
        agent_id=agent_id,
        # §6.2: forward the optional reasoning_effort hint from AgentConfig
        # so adapters that consume it (OpenAI o-series, Anthropic extended-
        # thinking) actually see the operator's intent.
        reasoning_effort=ac.reasoning_effort,
        messages=messages,
        sampling=None,
        tools=list(ac.tools) if ac.tools else [],
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
    # The last ProviderError observed before retries were exhausted —
    # used by `_terminate` to populate
    # `TerminationArtifact.last_provider_failure`, so an operator sees the
    # upstream's actual complaint instead of just `provider_unrecoverable`.
    last_error: Optional[ProviderError] = None


def _provider_for(session: Session, agent_id: str) -> _ProviderAdapter:
    return session.providers.get(agent_id) or session.providers["default"]


def _invoke_with_retry(
    session: Session,
    *,
    agent_id: str,
    request: ProviderRequest,
    sleep=time.sleep,
) -> _InvokeResult:
    """Invoke `agent_id`'s provider with `per_agent_retry_budget` retries.

    Retry semantics:
      - Non-retriable errors short-circuit to the §4.9 failure-policy path
        (terminate or panel_contraction) without waiting.
      - Retriable errors trigger an exponential backoff (with jitter) before
        the next attempt. If the upstream supplied `error.details.retry_after`
        (or `retry_after_seconds`), it is honored, capped to keep wallclock
        bounded.

    NOTE — adapter-internal corrective retry: the openai / anthropic /
    claude-cli / codex-cli adapters currently perform ONE internal corrective
    call on `malformed_response` before returning. That extra upstream call
    does NOT count against `per_agent_retry_budget`. Reconciling this with
    the spec (runtime-owned corrective retry, budget-counted) is scheduled
    for the next minor (Codex ↔ Claude review T2 — "Deferred To Next Minor").
    """
    ac = session.agent_by_id(agent_id)
    budget = ac.retry_budget if ac.retry_budget is not None else session.config.runtime.per_agent_retry_budget
    attempts = 1 + budget

    last_err_kind: Optional[str] = None
    last_error: Optional[ProviderError] = None
    for attempt_idx in range(attempts):
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
            last_error = result.error
            if not result.error.retriable:
                # Map §6.6 / §4.9: non-retriable error → either schema_error
                # (malformed_response, invalid_request) or provider_unrecoverable
                # (everything else).
                reason = _classify_unrecoverable(result.error.kind)
                # §4.9 path: per on_agent_failure
                if session.config.runtime.on_agent_failure == "terminate":
                    return _InvokeResult(
                        ok=False, result=result, terminating=reason, last_error=last_error,
                    )
                return _InvokeResult(
                    ok=False,
                    result=result,
                    panel_contracted=True,
                    contraction_reason=reason,  # type: ignore[arg-type]
                    last_error=last_error,
                )
            # Retriable failure: backoff before next attempt (unless we're done).
            if attempt_idx + 1 < attempts:
                retry_after = _extract_retry_after(result.error.details)
                sleep(_backoff_delay(attempt_idx + 1, retry_after, rng=session.rng))
        else:
            # structured_output is None without error — treat as malformed
            last_err_kind = "malformed_response"
            if attempt_idx + 1 < attempts:
                sleep(_backoff_delay(attempt_idx + 1, rng=session.rng))

    # Retries exhausted.
    final_reason = _classify_unrecoverable(last_err_kind or "internal")
    if session.config.runtime.on_agent_failure == "terminate":
        return _InvokeResult(
            ok=False, result=None, terminating=final_reason, last_error=last_error,
        )
    return _InvokeResult(
        ok=False,
        result=None,
        panel_contracted=True,
        contraction_reason=final_reason,  # type: ignore[arg-type]
        last_error=last_error,
    )


def _classify_unrecoverable(kind: str) -> str:
    if kind in ("malformed_response", "invalid_request"):
        return "schema_error"
    return "provider_unrecoverable"


def _extract_retry_after(details: Optional[Dict[str, object]]) -> Optional[float]:
    """Pull a `retry_after_seconds` (or `retry_after`) hint out of error.details.

    Adapters set this when an upstream surface (`Retry-After` header or
    vendor-specific field) suggests a wait time. The runtime honors it as
    the floor for the next backoff sleep, capped by `_BACKOFF_MAX_SECONDS`.
    """
    if not isinstance(details, dict):
        return None
    for key in ("retry_after_seconds", "retry_after"):
        v = details.get(key)
        if v is None:
            continue
        try:
            return float(v)
        except (TypeError, ValueError):
            continue
    return None


# ---------------------------------------------------------------------------
# §4.11 main loop
# ---------------------------------------------------------------------------


def run_session(
    config: Config,
    providers: Dict[str, _ProviderAdapter],
    *,
    runs_root: Optional[str] = None,
    selector_providers: Optional[Dict[str, _ProviderAdapter]] = None,
    rng_seed: Optional[str] = None,
) -> Artifact:
    """Run a session to completion. Returns the persisted Artifact.

    `providers` is a mapping from agent_id (or "default") to a ProviderAdapter
    instance. For the FakeProvider walking skeleton, pass `{"default": fp}`.

    `selector_providers` is an OPTIONAL distinct provider map used only for
    the §4.1 `llm` selector invocation (it defaults to `providers`). Supply
    it when the selector must be driven by a different FakeProvider script
    than the deliberation — e.g. the CLI's `--selector-script`. `fixed` /
    `rules` make no provider call, so the value is irrelevant for them.

    `rng_seed` is an OPTIONAL replay-stable seed for the retry-backoff
    jitter RNG (§7.6). Defaults to `config.session_id`; execution_replay
    overrides with the *original* session id so the replay's backoff
    sequence matches the original even though the replay run dir uses a
    `-replay`-suffixed session id.

    If `runs_root` is given, the session is persisted under
    `runs_root/<session_id>/`.
    """
    session = Session(config=config, providers=providers, rng_seed=rng_seed)

    writer: Optional[RunWriter] = None
    if runs_root is not None:
        from pathlib import Path

        rd = RunDirectory.for_session(Path(runs_root), config.session_id)
        writer = RunWriter(rd)
        writer.start(config, session.started_at)

    # 1) problem_statement message (round 0, turn_index 0). §4.1 appends it
    #    during **init**, BEFORE the **selector** phase — so even a selector
    #    failure persists a valid 1-message canonical_transcript.
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

    # 2) §4.1 selector phase — choose the active_deliberation_panel + coordinator.
    #    `fixed` / `rules` make no provider call; `llm` makes one bounded call
    #    whose usage is budgeted against selector_budget and never enters
    #    Artifact.cumulative_usage or the transcript_digest. A selector failure
    #    terminates BEFORE round 1 opens, with the seed (problem_statement-only)
    #    transcript.
    try:
        selection = run_selector(config, providers=selector_providers or providers)
    except SelectorError:
        outcome, term = _terminate(session, writer, reason="schema_error")
        return _emit_artifact(session, config, outcome, term, writer)
    except SelectorBudgetExceeded:
        outcome, term = _terminate(session, writer, reason="budget_exceeded")
        return _emit_artifact(session, config, outcome, term, writer)

    session.active_panel = list(selection.selected_agents)
    # coordinator id is unchanged: run_selector enforces
    # selection.coordinator_agent == config.coordinator.id.
    if writer:
        writer.write_selector_output(selection)

    # 3) round loop
    outcome, term = _run_rounds(session, writer)

    # 4) emit artifact
    return _emit_artifact(session, config, outcome, term, writer)


def _emit_artifact(session, config, outcome, term, writer):
    """Build, persist, and return the final Artifact (§5.10).

    Shared by the normal round-loop exit and the §4.1 selector-failure path
    so both produce a schema-valid Artifact over the same transcript.
    """
    ended_at = now_utc_iso()
    final_artifact = Artifact(
        session_id=config.session_id,
        config=config,
        canonical_transcript=session.transcript,
        outcome=outcome,  # SynthesisOutcome or TerminationOutcome
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
        bt = _drain_deferred_queue(session, writer)
        if bt is not None:
            return _terminate(
                session, writer, reason=bt.reason,
                last_provider_failure=_failure_from(session, bt.target, bt.last_error),
            )

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
                return _terminate(
                    session, writer, reason=inv.terminating,
                    last_provider_failure=_failure_from(session, agent_id, inv.last_error),
                )
            if inv.panel_contracted:
                _append_panel_contraction(session, writer, agent_id, inv.contraction_reason)
                # remove agent from panel; check if panel is non-empty
                if agent_id in session.active_panel:
                    session.active_panel.remove(agent_id)
                if not session.active_panel:
                    return _terminate(
                        session, writer, reason="provider_unrecoverable",
                        last_provider_failure=_failure_from(session, agent_id, inv.last_error),
                    )
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

            # §4.5 classify direct_requests (do NOT dispatch yet — the
            # primary_turn MUST be appended to the canonical_transcript before
            # any branch_turn it parents, so the execution order is preserved
            # and `branch_turn.parent_id` never points forward in the journal.
            schema_failures: List[SchemaFailureRecord] = []
            inline_request: Optional[DirectRequest] = None
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
                if inline_request is None and session.config.runtime.max_branch_depth >= 1:
                    inline_request = dr
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

            # APPEND primary_msg BEFORE dispatching any branch — required so
            # branch_turn.parent_id always refers to a transcript entry that
            # precedes it (spec §5.4 + §4.5).
            session.transcript.append(primary_msg)
            if writer:
                writer.append_message(primary_msg)

            # Now dispatch the at-most-one inline branch. Propagate any
            # termination it bubbles up (on_agent_failure="terminate").
            if inline_request is not None:
                bt = _dispatch_branch(
                    session, writer, parent_msg=primary_msg, request=inline_request,
                )
                if bt is not None:
                    return _terminate(
                        session, writer, reason=bt.reason,
                        last_provider_failure=_failure_from(session, bt.target, bt.last_error),
                    )

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
            return _terminate(
                session, writer, reason=inv.terminating,
                last_provider_failure=_failure_from(session, coord_id, inv.last_error),
            )
        if inv.panel_contracted:
            # Coordinator cannot be contracted per §4.9 — escalate.
            return _terminate(
                session, writer, reason="provider_unrecoverable",
                last_provider_failure=_failure_from(session, coord_id, inv.last_error),
            )
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


@dataclass
class _BranchTermination:
    """Branch-dispatch termination payload (Codex review T2 #2b).

    `_dispatch_branch` no longer returns just a reason string — the
    last `ProviderError` from the branch's invocation MUST also bubble
    up so the outer loop can build a `LastProviderFailure` with it. The
    main loop's primary-turn path already does this; pre-T2, the branch
    path silently dropped the error.
    """
    reason: str
    last_error: Optional[ProviderError]
    target: str


def _dispatch_branch(
    session: Session,
    writer: Optional[RunWriter],
    *,
    parent_msg: Message,
    request: DirectRequest,
) -> Optional[_BranchTermination]:
    """Dispatch one branch_turn. Returns a `_BranchTermination` if
    `on_agent_failure="terminate"` was honored mid-branch; otherwise None.

    Callers MUST check the return value and propagate termination up to the
    main loop (§4.9 — failure policy is global, not modulated by whether the
    failing invocation happened to be a branch).
    """
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
        # §4.9: honor the failure policy — propagate termination instead of
        # silently demoting the failure to panel_contraction. Codex T2
        # #2b: carry the provider error so the outer loop can build a
        # `LastProviderFailure` (was dropped pre-T2).
        return _BranchTermination(
            reason=inv.terminating, last_error=inv.last_error, target=target,
        )
    if inv.panel_contracted:
        _append_panel_contraction(session, writer, target, inv.contraction_reason)
        if target in session.active_panel:
            session.active_panel.remove(target)
        return None
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
    return None


def _drain_deferred_queue(
    session: Session, writer: Optional[RunWriter],
) -> Optional[_BranchTermination]:
    """Drain queued direct_requests as §4.6 branch_turns at round-open.

    Returns a `_BranchTermination` if any drained branch hits the failure
    policy `terminate`; otherwise None. Callers MUST honor the return value
    (Codex T2 #2b — the branch path's last_error now bubbles up too).
    """
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
        bt = _dispatch_branch(
            session, writer, parent_msg=parent_msg, request=request,
        )
        drains += 1
        if bt is not None:
            return bt
    return None


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
    # Codex T2 #2b: carry the coordinator's last error into the artifact
    # so synthesis-time provider failures are diagnosable too.
    return _terminate(
        session, writer, reason="provider_unrecoverable",
        last_provider_failure=_failure_from(session, coord_id, inv.last_error),
    )


def _terminate(
    session: Session,
    writer: Optional[RunWriter],
    *,
    reason: str,
    pending=None,
    last_provider_failure: Optional["LastProviderFailure"] = None,
):
    """Build a TerminationOutcome and the per-§5.8 TerminationArtifact.

    `last_provider_failure` (Codex review T1 #2): when a provider-side
    failure terminated the run, the upstream's actual complaint is
    forwarded into the artifact so the operator sees actionable
    diagnostics instead of the bare `provider_unrecoverable` string.
    Callers receive the original `ProviderError` from `_InvokeResult.
    last_error` and pass it through `_failure_from(...)`.
    """
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
    if last_provider_failure is not None:
        kwargs["last_provider_failure"] = last_provider_failure

    term = TerminationArtifact(**kwargs)  # type: ignore[arg-type]
    outcome = TerminationOutcome(termination_artifact=term)
    return outcome, term


def _failure_from(
    session: Session, agent_id: str, error: Optional[ProviderError]
) -> Optional["LastProviderFailure"]:
    """Snapshot a per-agent ProviderError into a `LastProviderFailure`.

    Returns ``None`` if the failure didn't carry a structured error
    (e.g. all attempts produced `structured_output=None` without
    `ProviderError`); the bare termination reason is the best we can do
    in that path.
    """
    if error is None:
        return None
    ac = session.agent_by_id(agent_id)
    return LastProviderFailure(
        agent_id=agent_id,
        provider=ac.provider,
        model=ac.model,
        kind=error.kind,
        message=error.message,
        details=error.details,
    )


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

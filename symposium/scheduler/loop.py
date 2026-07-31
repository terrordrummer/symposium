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

# Wall-clock reserved (in seconds) so a best-effort synthesis can still run
# after the deliberation has consumed its turn budget. Only enforced when
# `runtime.synthesize_on_terminate` is set: the soft deadline = hard
# wallclock − this reserve. Without the reserve, the last panel turn would
# eat the whole clock and the salvage synthesis would have no window — the
# exact "wired the flag but it never produces output" trap.
# This constant is a CEILING: the effective reserve is scaled to the budget
# by `_synthesis_reserve_seconds` so small wall-clock budgets still open
# turns at all (see that helper).
_SYNTHESIS_RESERVE_SECONDS = 120.0

# Floor on the budget a normal turn must have before we open it. If less than
# this remains above the synthesis reserve, we stop opening turns and go
# straight to synthesis rather than spend the budget on a turn that would be
# timed out mid-flight anyway.
_MIN_TURN_TIMEOUT_SECONDS = 30.0


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
    and flip a termination decision between runs). It is REQUIRED whenever
    jitter is computed: a fallback to the process-global `random` module
    would leak unseeded nondeterminism into that surface. Only the
    `retry_after` early return may be reached without one.
    """
    if retry_after is not None and retry_after >= 0:
        return min(float(retry_after), _BACKOFF_MAX_SECONDS)
    if rng is None:
        raise TypeError("_backoff_delay requires a session-bound rng for jitter")
    raw = _BACKOFF_BASE_SECONDS * (2 ** (max(attempt_idx, 1) - 1))
    bounded = min(raw, _BACKOFF_MAX_SECONDS)
    jitter = 1.0 + rng.uniform(-_BACKOFF_JITTER, _BACKOFF_JITTER)
    return max(bounded * jitter, 0.0)

from pydantic import ValidationError

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


def _remaining_wallclock_seconds(session: Session) -> float:
    """Seconds left before the hard wall-clock cap trips."""
    elapsed = time.monotonic() - session.started_monotonic
    return session.config.budget.max_wallclock_seconds - elapsed


def _synthesis_reserve_seconds(session: Session) -> float:
    """Effective synthesis reserve for this session's wall-clock budget.

    `_SYNTHESIS_RESERVE_SECONDS` is a ceiling: with a small budget, a fixed
    120s reserve (+ the 30s min-turn floor) would trip the soft deadline at
    the very first round-open, and `_has_substantive_turn` would then block
    the salvage over the empty transcript — zero deliberation. Scaling the
    reserve to a quarter of the budget keeps small runs deliberating;
    budgets of 480s and above get the full fixed reserve, so behavior for
    normal-sized runs is unchanged.
    """
    return min(
        _SYNTHESIS_RESERVE_SECONDS,
        0.25 * session.config.budget.max_wallclock_seconds,
    )


def _budget_gate(session: Session) -> Optional[str]:
    """Combined cap check for the points where a new turn may be opened.

    Returns a termination reason, or None to proceed. Layers a *soft*
    wall-clock deadline on top of `check_hard_caps`: when
    `runtime.synthesize_on_terminate` is set, we stop opening new turns once
    less than `_synthesis_reserve_seconds + _MIN_TURN_TIMEOUT_SECONDS`
    remains, so the reserved window survives for a salvage synthesis. When
    the flag is off, behavior is unchanged (hard caps only) — the soft
    deadline would otherwise cut deliberation short for no benefit.
    """
    hard = check_hard_caps(session)
    if hard:
        return hard
    if session.config.runtime.synthesize_on_terminate:
        remaining = _remaining_wallclock_seconds(session)
        if remaining <= _synthesis_reserve_seconds(session) + _MIN_TURN_TIMEOUT_SECONDS:
            return "timeout"
    return None


def _turn_timeout(session: Session) -> Optional[float]:
    """Per-turn provider timeout hint: the wall-clock budget for a normal
    turn, leaving the synthesis reserve intact. Returns None when
    `synthesize_on_terminate` is off (adapters use their own default).

    The value is a *ceiling* the adapter clamps against its own timeout
    (the request can only tighten, never extend, the provider timeout).
    """
    if not session.config.runtime.synthesize_on_terminate:
        return None
    budget = _remaining_wallclock_seconds(session) - _synthesis_reserve_seconds(session)
    return max(budget, _MIN_TURN_TIMEOUT_SECONDS)


def _synthesis_timeout(session: Session) -> Optional[float]:
    """Per-call provider timeout hint for a synthesis turn: use all the
    remaining wall-clock (which, at salvage time, is ~the reserve). Returns
    None when the flag is off so the clean-finalize path keeps adapter
    defaults.
    """
    if not session.config.runtime.synthesize_on_terminate:
        return None
    # Floor at the reserve: a salvage synthesis triggered right at (or just
    # past) the hard cap still deserves a usable window — overrunning the
    # wall-clock by up to the reserve to actually produce an answer is the
    # whole point of synthesize_on_terminate.
    return max(_remaining_wallclock_seconds(session), _synthesis_reserve_seconds(session))


def _has_substantive_turn(session: Session) -> bool:
    """True if at least one panelist contribution exists in the transcript.

    Gates the salvage synthesis: synthesizing over a transcript that holds
    only the problem statement (e.g. a round-0 provider failure) would
    produce nothing useful, so we let those terminate normally.
    """
    return any(
        m.type in ("primary_turn", "branch_turn") for m in session.transcript
    )


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
            "  - `confidence`: float in [0,1] — your confidence in the\n"
            "    next_action decision. Required.\n"
            "  - `focus`: what the panel should attend to next (free-text).\n"
            "  - `next_agents`: ADVISORY subset of CURRENT PANEL ids you\n"
            "    consider most relevant next round. Recorded in the verdict\n"
            "    for audit, but it does NOT alter scheduling — every round\n"
            "    runs the full panel.\n"
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
            "    is ADVISORY ONLY: it is recorded in the verdict but does NOT\n"
            "    alter scheduling — every round runs the FULL panel. Reference\n"
            "    only members ALREADY IN THE PANEL. Phantom IDs are silently\n"
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
    timeout_seconds: Optional[float] = None,
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
    # Deadline-aware invocation: pass the per-turn wall-clock budget through
    # request metadata. CLI adapters clamp their subprocess timeout to
    # min(default, this) so a turn can never overrun the budget the loop
    # has reserved for the eventual synthesis.
    metadata = (
        {"symposium_timeout_seconds": float(timeout_seconds)}
        if timeout_seconds is not None
        else None
    )
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
        metadata=metadata,
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
    attempts_override: Optional[int] = None,
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
    # `attempts_override` is used by the salvage synthesis path to force a
    # single no-retry attempt: a best-effort synthesis must not consume the
    # reserved window with retry backoffs.
    attempts = attempts_override if attempts_override is not None else 1 + budget

    last_err_kind: Optional[str] = None
    last_error: Optional[ProviderError] = None
    for attempt_idx in range(attempts):
        # Deadline-aware retries (Codex PR1 review #1): when
        # synthesize_on_terminate is on, a retry must never eat into the
        # synthesis reserve. Before any retry (attempt_idx > 0) stop if the
        # reserve is all that's left, and otherwise refresh the per-attempt
        # timeout from the CURRENT remaining wall-clock (the value baked into
        # request.metadata at build time is stale after the first attempt +
        # its backoff). The first attempt keeps its as-built timeout — which
        # for a salvage synthesis is the floored reserve, so this never
        # shrinks the salvage call.
        if (
            attempt_idx > 0
            and session.config.runtime.synthesize_on_terminate
        ):
            remaining = _remaining_wallclock_seconds(session)
            reserve = _synthesis_reserve_seconds(session)
            if remaining <= reserve + _MIN_TURN_TIMEOUT_SECONDS:
                break
            if request.metadata and "symposium_timeout_seconds" in request.metadata:
                request.metadata["symposium_timeout_seconds"] = max(
                    remaining - reserve, _MIN_TURN_TIMEOUT_SECONDS
                )
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
    # Fail fast on ambiguous identities: `agent_by_id` returns the FIRST
    # match, so a duplicated id would silently shadow its twin for the whole
    # deliberation. This is a runtime check, not a Config model validator —
    # schema-valid configs must still parse.
    _check_agent_id_uniqueness(config)

    session = Session(config=config, providers=providers, rng_seed=rng_seed)

    writer: Optional[RunWriter] = None
    if runs_root is not None:
        from pathlib import Path

        rd = RunDirectory.for_session(Path(runs_root), config.session_id)
        writer = RunWriter(rd)
        writer.start(config, session.started_at)

    try:
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
    except BaseException:
        # Crash containment: an exception escaping the round loop (or the
        # artifact emit) would otherwise leave the manifest `in_progress`
        # forever, the journal fd open, and the lock released only by GC.
        # Mark the manifest `crashed` and tear the writer down, then let the
        # exception surface to the caller.
        if writer is not None:
            try:
                writer.mark_crashed()
            except Exception:  # noqa: BLE001 — teardown must not mask the crash
                pass
        raise
    finally:
        _shutdown_providers(providers, selector_providers)


def _check_agent_id_uniqueness(config: Config) -> None:
    """Reject duplicate agent ids / panel entries before any turn runs."""
    seen: set = set()
    for ac in config.agents:
        if ac.id in seen:
            raise ValueError(f"config.agents contains duplicate agent id {ac.id!r}")
        seen.add(ac.id)
    if config.coordinator.id in seen:
        raise ValueError(
            f"coordinator id {config.coordinator.id!r} collides with a panel agent id"
        )
    panel = config.selector.default_deliberation_panel
    if len(set(panel)) != len(panel):
        dupes = sorted({aid for aid in panel if panel.count(aid) > 1})
        raise ValueError(
            f"selector.default_deliberation_panel contains duplicate entries: {dupes}"
        )


def _shutdown_providers(*provider_maps: Optional[Dict[str, _ProviderAdapter]]) -> None:
    """Best-effort `shutdown()` of session-owned adapters at run teardown.

    HTTP-backed adapters own one `httpx.Client` each; without this call
    every session leaked a client per provider. Adapters without a
    `shutdown` method (FakeProvider, CLI adapters) are skipped, and
    failures are swallowed — teardown must never mask the session outcome.
    """
    released: set = set()
    for pmap in provider_maps:
        if not pmap:
            continue
        for adapter in pmap.values():
            if id(adapter) in released:
                continue
            released.add(id(adapter))
            shutdown = getattr(adapter, "shutdown", None)
            if callable(shutdown):
                try:
                    shutdown()
                except Exception:  # noqa: BLE001
                    pass


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

        breach = _budget_gate(session)
        if breach:
            # The breach fired at round-open, before any turn of the new
            # round was held: report the last round that actually ran, not
            # the empty one (final_round would otherwise over-count by one).
            session.round -= 1
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
            # The iteration runs over a snapshot while contraction edits the
            # LIVE panel: an agent contracted mid-round (e.g. as the failed
            # target of an earlier member's inline branch) must not still
            # take its primary turn this round.
            if agent_id not in session.active_panel:
                continue
            breach = _budget_gate(session)
            if breach:
                return _terminate(session, writer, reason=breach)

            packet = derive_context_packet(session, agent_id=agent_id)
            req = build_provider_request(
                session,
                agent_id=agent_id,
                expected_output_schema="turn_structured_output",
                packet=packet,
                timeout_seconds=_turn_timeout(session),
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
            try:
                tso = TurnStructuredOutput.model_validate(inv.result.structured_output)
            except ValidationError:
                # A provider reporting success with a non-conforming dict
                # (unvalidated FakeProvider script, third-party adapter) must
                # terminate as a schema error with a persisted artifact —
                # not crash the run (§4.9).
                return _terminate(session, writer, reason="schema_error")

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

        # §4.2 step 4: coordination_turn. At the soft deadline we skip the
        # verdict and go straight to salvage synthesis (the salvage path is
        # itself a coordinator call) rather than spend the reserve on a
        # verdict that would only ask for another round we cannot afford.
        breach = _budget_gate(session)
        if breach:
            return _terminate(session, writer, reason=breach)

        coord_id = session.config.coordinator.id
        packet = derive_context_packet(session, agent_id=coord_id)
        req = build_provider_request(
            session,
            agent_id=coord_id,
            expected_output_schema="verdict",
            packet=packet,
            timeout_seconds=_turn_timeout(session),
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

        try:
            verdict = Verdict.model_validate(inv.result.structured_output)
        except ValidationError:
            # Same self-defense as the panel turns: a non-conforming verdict
            # dict terminates with schema_error instead of crashing mid-run.
            return _terminate(session, writer, reason="schema_error")
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
        # next_action == "continue"
        # Force-finalize (Codex review): a `continue` on the last allowed
        # round, or one that leaves no wall-clock room for another round,
        # must synthesize NOW instead of opening a round that cannot finish
        # (the pre-fix behavior re-looped, hit a cap at round-open, and
        # terminated with no synthesis). Routed through the salvage path so
        # it shares the bounded no-retry synthesis + termination fallback;
        # the reserve guarantees the synthesis has a window.
        if session.round >= session.config.budget.max_rounds:
            return _terminate(session, writer, reason="budget_exceeded")
        soft = _budget_gate(session)
        if soft:
            return _terminate(session, writer, reason=soft)
        continue


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
    # Budget gate (Codex PR1 review #3): branches are optional turns and must
    # not be opened past a hard cap or the soft synthesis-reserve deadline.
    # Without this, a primary turn that leaves only the reserve could still
    # spawn an inline/deferred branch and eat the salvage window. Surfacing a
    # termination here routes the outer loop to the salvage-synthesis path.
    breach = _budget_gate(session)
    if breach:
        return _BranchTermination(reason=breach, last_error=None, target=target)
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
        timeout_seconds=_turn_timeout(session),
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
        if not session.active_panel:
            # Mirror the primary path's empty-panel check: contracting the
            # last member must terminate the run — the next ContextPacket
            # would otherwise be derived over an empty panel_disclosure
            # (min_length 1) and crash with an uncaught ValidationError.
            return _BranchTermination(
                reason="provider_unrecoverable",
                last_error=inv.last_error,
                target=target,
            )
        return None
    assert inv.result is not None and inv.result.structured_output is not None
    try:
        tso = TurnStructuredOutput.model_validate(inv.result.structured_output)
    except ValidationError:
        # Same self-defense as the primary path: a non-conforming branch
        # payload terminates with schema_error instead of crashing.
        return _BranchTermination(
            reason="schema_error", last_error=inv.last_error, target=target,
        )

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
            # Record the drop on the originating message, mirroring the
            # enqueue-time schema_failure records — pre-fix these requests
            # were popped with no transcript trace at all. The parent's
            # journal line was written at append time, so the record
            # surfaces in the canonical_transcript (the authoritative
            # artifact), which is also where the enqueue-time records live.
            record = SchemaFailureRecord(
                offending_request=request.model_dump(exclude_none=True),
                reason=(
                    "deferred direct_request no longer routable at drain time "
                    "(target left active_panel, is originator, or is coordinator)"
                ),
            )
            if parent_msg.schema_failure:
                parent_msg.schema_failure.append(record)
            else:
                parent_msg.schema_failure = [record]
            continue
        bt = _dispatch_branch(
            session, writer, parent_msg=parent_msg, request=request,
        )
        drains += 1
        if bt is not None:
            return bt
    return None


def _try_synthesis(
    session: Session,
    writer: Optional[RunWriter],
    *,
    timeout_seconds: Optional[float] = None,
    attempts_override: Optional[int] = None,
) -> Tuple[Optional[SynthesisOutcome], Optional[ProviderError]]:
    """Invoke the coordinator for a synthesis and append it on success.

    Returns ``(SynthesisOutcome, None)`` on success or ``(None, last_error)``
    on failure — leaving termination handling to the caller. Shared by the
    clean §4.8 finalize path (`_attempt_finalize`, full retry budget) and the
    salvage-on-terminate path (`_terminate`, single no-retry attempt with a
    bounded timeout so it cannot overrun the reserved window).
    """
    coord_id = session.config.coordinator.id
    packet = derive_context_packet(session, agent_id=coord_id)
    req = build_provider_request(
        session,
        agent_id=coord_id,
        expected_output_schema="synthesis_content",
        packet=packet,
        timeout_seconds=timeout_seconds,
    )
    session.turn_index += 1
    inv = _invoke_with_retry(
        session, agent_id=coord_id, request=req, attempts_override=attempts_override,
    )
    if inv.ok and inv.result is not None and inv.result.structured_output is not None:
        try:
            sc = SynthesisContent.model_validate(inv.result.structured_output)
        except ValidationError as exc:
            # Report the malformed synthesis as a provider-shaped failure so
            # `_attempt_finalize` classifies it as schema_error and the
            # salvage path falls through to a normal termination.
            return None, ProviderError(
                kind="malformed_response",
                message=f"synthesis structured_output failed schema validation: {exc}",
                retriable=False,
            )
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
    return None, inv.last_error


def _attempt_finalize(session: Session, writer: Optional[RunWriter]):
    """§4.8 — invoke coordinator for a synthesis. On failure, fall back to terminate."""
    coord_id = session.config.coordinator.id
    outcome, last_error = _try_synthesis(
        session, writer, timeout_seconds=_synthesis_timeout(session),
    )
    if outcome is not None:
        return outcome, None

    # Synthesis failed → terminate (R2). malformed_response / invalid_request
    # classify as schema_error; everything else stays provider_unrecoverable.
    # Codex T2 #2b: carry the coordinator's last error into the artifact
    # so synthesis-time provider failures are diagnosable too.
    # allow_synthesis=False: we just tried to synthesize and it failed; the
    # salvage path inside _terminate must not attempt it a second time.
    reason = (
        _classify_unrecoverable(last_error.kind)
        if last_error is not None
        else "provider_unrecoverable"
    )
    return _terminate(
        session, writer, reason=reason, allow_synthesis=False,
        last_provider_failure=_failure_from(session, coord_id, last_error),
    )


# Termination reasons eligible for a best-effort salvage synthesis when
# `runtime.synthesize_on_terminate` is set. user_input_required /
# external_research_required are deliberately excluded: the adaptive loop
# keys panel expansion off those terminations, so synthesizing over them
# would break expansion.
_SALVAGEABLE_TERMINATION_REASONS = (
    "timeout",
    "budget_exceeded",
    "provider_unrecoverable",
)


def _terminate(
    session: Session,
    writer: Optional[RunWriter],
    *,
    reason: str,
    pending=None,
    last_provider_failure: Optional["LastProviderFailure"] = None,
    allow_synthesis: bool = True,
):
    """Build a TerminationOutcome and the per-§5.8 TerminationArtifact.

    `last_provider_failure` (Codex review T1 #2): when a provider-side
    failure terminated the run, the upstream's actual complaint is
    forwarded into the artifact so the operator sees actionable
    diagnostics instead of the bare `provider_unrecoverable` string.
    Callers receive the original `ProviderError` from `_InvokeResult.
    last_error` and pass it through `_failure_from(...)`.

    Best-effort salvage (spec §4.8 synthesize-on-terminate): when
    `allow_synthesis` and `runtime.synthesize_on_terminate` are set, the
    reason is salvageable, and at least one substantive turn exists, attempt
    ONE bounded no-retry synthesis BEFORE terminating. On success the run
    ends with a `synthesis` outcome instead of an empty termination — so a
    long deliberation that ran out of wall-clock/budget still yields an
    answer. On failure we fall through to the normal termination artifact.
    `allow_synthesis=False` is passed by the clean finalize path, which has
    already attempted (and failed) a synthesis, to avoid a double attempt.
    """
    if (
        allow_synthesis
        and session.config.runtime.synthesize_on_terminate
        and reason in _SALVAGEABLE_TERMINATION_REASONS
        and _has_substantive_turn(session)
    ):
        outcome, _ = _try_synthesis(
            session,
            writer,
            timeout_seconds=_synthesis_timeout(session),
            attempts_override=1,
        )
        if outcome is not None:
            return outcome, None

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

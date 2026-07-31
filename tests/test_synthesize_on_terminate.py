"""PR1 — best-effort salvage synthesis on terminate + deadline reserve.

These tests pin the fix for the recurring "deliberation times out after a
couple of rounds and produces no synthesis" failure. They exercise the
runtime directly with the FakeProvider (no CLIs):

  * `synthesize_on_terminate` salvages a synthesis when the run would
    otherwise terminate empty (force-finalize on the last round).
  * the same scenario with the flag off still terminates with no synthesis
    (no behavior change for the spec default).
  * the soft wall-clock deadline reserves a synthesis window instead of
    spending the last seconds opening another turn.
"""

from __future__ import annotations

import symposium.scheduler.loop as loop_mod
from symposium.models import (
    AgentConfig,
    BudgetConfig,
    Config,
    FakeProviderScript,
    ProviderError,
    ProviderRawMessage,
    ProviderResult,
    RuntimeConfig,
    SelectorConfig,
    Usage,
)
from symposium.personas import persona_by_id
from symposium.providers import FakeProvider
from symposium.providers.base import ProviderAdapter
from symposium.scheduler import run_session


# ---------------------------------------------------------------------------
# builders
# ---------------------------------------------------------------------------


def _make_config(
    *,
    max_rounds: int,
    max_wallclock_seconds: int,
    synthesize_on_terminate: bool,
    panel=("logician",),
) -> Config:
    agents = [
        AgentConfig(
            id=pid,
            persona_ref=persona_by_id(pid),
            provider="fake",
            model="fake-deterministic",
        )
        for pid in panel
    ]
    coordinator = AgentConfig(
        id="coordinator",
        persona_ref=persona_by_id("coordinator"),
        provider="fake",
        model="fake-deterministic",
    )
    return Config(
        schema_version="1.0.0",
        session_id="test-salvage",
        originator="test",
        problem_statement="Should we ship the thing?",
        selector=SelectorConfig(
            strategy="fixed",
            default_deliberation_panel=list(panel),
            coordinator_agent="coordinator",
        ),
        agents=agents,
        coordinator=coordinator,
        runtime=RuntimeConfig(synthesize_on_terminate=synthesize_on_terminate),
        budget=BudgetConfig(
            max_total_tokens=10_000_000,
            max_total_cost_usd=1000.0,
            max_rounds=max_rounds,
            max_wallclock_seconds=max_wallclock_seconds,
        ),
    )


def _result(structured: dict, *, tokens: int = 100) -> dict:
    return {
        "messages": [{"role": "assistant", "content": ""}],
        "tool_events": [],
        "usage": {
            "prompt_tokens": tokens,
            "completion_tokens": 0,
            "total_tokens": tokens,
            "cost_usd": 0.0,
        },
        "finish_reason": "stop",
        "structured_output": structured,
        "raw": None,
        "error": None,
    }


def _turn(agent_id: str, text: str) -> dict:
    return {
        "match": {"agent_id": agent_id, "expected_output_schema": "turn_structured_output"},
        "result": _result({"text": text}),
    }


def _verdict(next_action: str, *, rationale: str = "keep going") -> dict:
    return {
        "match": {"agent_id": "coordinator", "expected_output_schema": "verdict"},
        "result": _result(
            {
                "next_action": next_action,
                "rationale": rationale,
                "confidence": 0.5,
                "focus": "the core question",
                "next_agents": [],
                "resolved_disagreements": [],
                "unresolved_disagreements": [],
            }
        ),
    }


def _synthesis(answer: str = "Ship it, with caveats.") -> dict:
    return {
        "match": {"agent_id": "coordinator", "expected_output_schema": "synthesis_content"},
        "result": _result(
            {
                "integrated_answer": answer,
                "resolved_disagreements": [],
                "unresolved_disagreements": [],
                "confidence": 0.7,
            }
        ),
    }


def _script(entries) -> FakeProviderScript:
    return FakeProviderScript.model_validate(
        {"schema_version": "1.0.0", "on_exhaustion": "error", "entries": entries}
    )


# ---------------------------------------------------------------------------
# force-finalize on the last round (flag on vs off)
# ---------------------------------------------------------------------------


def test_continue_on_last_round_salvages_synthesis_when_flag_on():
    """max_rounds=1, coordinator says `continue`: instead of re-looping into a
    round it can't open (and terminating empty), the runtime force-finalizes
    through the salvage path and ends with a synthesis."""
    config = _make_config(
        max_rounds=1, max_wallclock_seconds=3600, synthesize_on_terminate=True
    )
    script = _script(
        [
            _turn("logician", "round-1 analysis"),
            _verdict("continue"),
            _synthesis(),
        ]
    )
    art = run_session(config, {"default": FakeProvider(script=script)})

    assert art.outcome.kind == "synthesis"
    synth = [m for m in art.canonical_transcript if m.type == "synthesis"]
    assert len(synth) == 1
    assert synth[0] is art.canonical_transcript[-1]
    assert art.outcome.synthesis_message_id == synth[0].id


def test_continue_on_last_round_terminates_when_flag_off():
    """Same scenario with the flag off: spec-default behavior — terminate with
    no synthesis."""
    config = _make_config(
        max_rounds=1, max_wallclock_seconds=3600, synthesize_on_terminate=False
    )
    script = _script(
        [
            _turn("logician", "round-1 analysis"),
            _verdict("continue"),
            _synthesis(),  # present but must NOT be consumed
        ]
    )
    art = run_session(config, {"default": FakeProvider(script=script)})

    assert art.outcome.kind == "termination"
    assert not any(m.type == "synthesis" for m in art.canonical_transcript)


def test_no_salvage_without_a_substantive_turn():
    """A run that dies before any panelist speaks must NOT fabricate a
    synthesis over an empty transcript — it terminates normally."""
    config = _make_config(
        max_rounds=1, max_wallclock_seconds=3600, synthesize_on_terminate=True
    )
    # First (and only) invocation is the logician turn, scripted as a
    # non-retriable provider error → terminates at round 1 with no
    # substantive turn in the transcript.
    err_entry = {
        "match": {"agent_id": "logician", "expected_output_schema": "turn_structured_output"},
        "result": {
            "messages": [{"role": "assistant", "content": ""}],
            "tool_events": [],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "cost_usd": 0.0},
            "finish_reason": "error",
            "structured_output": None,
            "raw": None,
            "error": {"kind": "internal", "message": "boom", "retriable": False},
        },
    }
    art = run_session(config, {"default": FakeProvider(script=_script([err_entry]))})
    assert art.outcome.kind == "termination"
    assert not any(m.type == "synthesis" for m in art.canonical_transcript)


# ---------------------------------------------------------------------------
# soft deadline reserves a synthesis window
# ---------------------------------------------------------------------------


class _ClockAdvancingProvider(FakeProvider):
    """FakeProvider that advances a shared fake clock by `per_turn` seconds on
    every invocation, so a multi-turn run deterministically marches toward the
    wall-clock cap without real time passing."""

    def __init__(self, script, state, per_turn):
        super().__init__(script=script)
        self._state = state
        self._per_turn = per_turn

    def invoke(self, request):
        result = super().invoke(request)
        self._state["elapsed"] += self._per_turn
        return result


def test_soft_deadline_reserves_synthesis_instead_of_opening_a_turn(monkeypatch):
    """With the flag on, once less than the synthesis reserve (+ a min turn)
    remains, the runtime stops opening turns and salvages a synthesis — rather
    than spending the last seconds on another panel/coordinator turn and
    terminating empty.

    Wall-clock is driven by a fake clock advanced per provider call. Budget =
    400s, so the scaled reserve is min(120, 0.25*400) = 100s; with the 30s
    min turn the soft deadline trips when remaining <= 130s. Each turn =
    100s. The clock lazily anchors to the real monotonic value Session
    captured at creation (its default_factory binds the original function,
    so a plain patch wouldn't move the start point).
    """
    real_monotonic = loop_mod.time.monotonic
    state = {"base": None, "elapsed": 0.0}

    def fake_monotonic():
        if state["base"] is None:
            state["base"] = real_monotonic()
        return state["base"] + state["elapsed"]

    monkeypatch.setattr(loop_mod.time, "monotonic", fake_monotonic)

    config = _make_config(
        max_rounds=4, max_wallclock_seconds=400, synthesize_on_terminate=True
    )
    # Invocation order with a 1-member panel:
    #   r1: logician turn, coordinator verdict(continue)
    #   r2: logician turn, then the soft deadline trips at the coordinator
    #       gate (remaining 100s <= 130s) -> salvage synthesis (no r2 verdict).
    script = _script(
        [
            _turn("logician", "r1"),
            _verdict("continue"),
            _turn("logician", "r2"),
            _synthesis("salvaged answer"),
        ]
    )
    provider = _ClockAdvancingProvider(script, state, per_turn=100.0)
    art = run_session(config, {"default": provider})

    assert art.outcome.kind == "synthesis"
    # Exactly one coordination_turn (round 1). The round-2 verdict was never
    # opened — the reserve was spent on the synthesis instead.
    coord_turns = [m for m in art.canonical_transcript if m.type == "coordination_turn"]
    assert len(coord_turns) == 1
    synth = [m for m in art.canonical_transcript if m.type == "synthesis"]
    assert len(synth) == 1 and synth[0] is art.canonical_transcript[-1]


class _RoleProvider(ProviderAdapter):
    """Per-agent scripted adapter that advances a shared fake clock on every
    call and counts invocations per agent. `logician` succeeds, `critic`
    always returns a RETRIABLE error (to drive runtime retries), and the
    coordinator's synthesis succeeds."""

    name = "fake"

    def __init__(self, state, per_turn):
        self._state = state
        self._per_turn = per_turn
        self.calls = {}
        self.last_request_round = None
        self.last_request_turn_index = None

    def invoke(self, request):
        self.calls[request.agent_id] = self.calls.get(request.agent_id, 0) + 1
        self._state["elapsed"] += self._per_turn
        usage = Usage(prompt_tokens=10, completion_tokens=0, total_tokens=10, cost_usd=0.0)
        base = dict(
            messages=[ProviderRawMessage(role="assistant", content="")],
            tool_events=[],
            usage=usage,
            raw=None,
        )
        schema = request.expected_output_schema
        if schema == "synthesis_content":
            return ProviderResult(
                finish_reason="stop",
                structured_output={
                    "integrated_answer": "salvaged after retries",
                    "resolved_disagreements": [],
                    "unresolved_disagreements": [],
                    "confidence": 0.6,
                },
                error=None,
                **base,
            )
        if request.agent_id == "critic":
            return ProviderResult(
                finish_reason="error",
                structured_output=None,
                error=ProviderError(kind="rate_limit", message="slow down", retriable=True),
                **base,
            )
        if schema == "verdict":
            return ProviderResult(
                finish_reason="stop",
                structured_output={
                    "next_action": "continue",
                    "rationale": "keep going",
                    "confidence": 0.5,
                    "focus": "the question",
                    "next_agents": [],
                    "resolved_disagreements": [],
                    "unresolved_disagreements": [],
                },
                error=None,
                **base,
            )
        return ProviderResult(
            finish_reason="stop",
            structured_output={"text": "a substantive contribution"},
            error=None,
            **base,
        )


def test_retries_do_not_consume_synthesis_reserve(monkeypatch):
    """Codex PR1 review #1: a failing turn's retries must not eat the synthesis
    reserve. With a substantive logician turn already on record, the critic's
    retriable failures should stop as soon as the soft deadline is reached —
    leaving room for the salvage synthesis — rather than burning the full retry
    budget past the reserve.

    Budget=480s, reserve=120s (the scaled reserve hits its ceiling at 480s),
    min_turn=30s -> soft deadline at remaining<=150s. Each provider call
    advances the clock 120s. retry budget defaults to 2 (=> up to 3 critic
    attempts without the fix).
    """
    real_monotonic = loop_mod.time.monotonic
    state = {"base": None, "elapsed": 0.0}

    def fake_monotonic():
        if state["base"] is None:
            state["base"] = real_monotonic()
        return state["base"] + state["elapsed"]

    monkeypatch.setattr(loop_mod.time, "monotonic", fake_monotonic)
    # Keep backoff sleeps out of real wall-clock for the retriable path.
    # NOTE: patching `loop_mod.time.sleep` would be ineffective here —
    # `_invoke_with_retry` bound `sleep=time.sleep` as a default at def time,
    # so the module-level patch never reaches it and the backoffs would run
    # for real. Zeroing the computed delay is what actually removes the wait.
    monkeypatch.setattr(loop_mod, "_backoff_delay", lambda *a, **k: 0.0)

    config = _make_config(
        max_rounds=4,
        max_wallclock_seconds=480,
        synthesize_on_terminate=True,
        panel=("logician", "critic"),
    )
    provider = _RoleProvider(state, per_turn=120.0)
    art = run_session(config, {"default": provider})

    # logician (120->360 remaining) succeeds; critic attempt #1 (->240) and #2
    # (->120) fail, then the deadline guard stops further retries (would have
    # been a 3rd attempt without the fix). Salvage synthesis then runs.
    assert provider.calls.get("critic") == 2, provider.calls
    assert art.outcome.kind == "synthesis"
    assert art.canonical_transcript[-1].type == "synthesis"


# ---------------------------------------------------------------------------
# the synthesis reserve scales down with small wall-clock budgets
# ---------------------------------------------------------------------------


def test_small_wallclock_budget_still_deliberates():
    """Regression: with the fixed 120s reserve, any budget <= 150s tripped the
    soft deadline at the very first round-open, and `_has_substantive_turn`
    then blocked the salvage over the empty transcript — zero deliberation.
    The reserve now scales to a quarter of the budget (120s budget -> 30s
    reserve, deadline at remaining <= 60s), so the first round opens
    normally and the run completes."""
    config = _make_config(
        max_rounds=2, max_wallclock_seconds=120, synthesize_on_terminate=True
    )
    script = _script(
        [
            _turn("logician", "small-budget analysis"),
            _verdict("finalize", rationale="converged"),
            _synthesis("answer under a small budget"),
        ]
    )
    art = run_session(config, {"default": FakeProvider(script=script)})

    assert any(m.type == "primary_turn" for m in art.canonical_transcript)
    assert art.outcome.kind == "synthesis"


# ---------------------------------------------------------------------------
# final_round accounting at a round-open breach
# ---------------------------------------------------------------------------


def test_round_open_breach_reports_previous_round_as_final():
    """A breach at the open of round N+1 (no turns held in it) must report
    final_round = N. Here the soft deadline trips at the very first
    round-open (budget 40s <= 0.25*40 + 30), so no round ever held a turn
    and final_round must be 0 — pre-fix it reported 1."""
    config = _make_config(
        max_rounds=4, max_wallclock_seconds=40, synthesize_on_terminate=True
    )
    script = _script([_turn("logician", "never invoked")])
    art = run_session(config, {"default": FakeProvider(script=script)})

    assert art.outcome.kind == "termination"
    term = art.outcome.termination_artifact
    assert term.reason == "timeout"
    assert term.final_round == 0
    assert not any(m.type in ("primary_turn", "branch_turn") for m in art.canonical_transcript)

"""Symposium MCP server tools (spec §11.4 / §11.5 host integration).

The FastMCP tools are plain functions, so these tests call them directly
— no real transport, no network. Every path is driven by the
deterministic FakeProvider via the walking-skeleton script; CI never
needs ANTHROPIC_API_KEY / OPENAI_API_KEY.
"""

from __future__ import annotations

import asyncio
import json

import pytest

# Skip the whole module cleanly if the optional `mcp` extra is absent.
pytest.importorskip("mcp")

from symposium.integrations.mcp_server import (  # noqa: E402
    deliberate,
    deliberate_streaming,
    get_run_summary,
    list_personas,
    stream_adaptive,
    stream_deliberation,
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
    result = asyncio.run(deliberate(
        "Should we adopt a structured deliberation protocol?",
        provider="fake",
        fake_script_path=script_path,
        output_dir=str(tmp_path),
    ))
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
    result = asyncio.run(deliberate(
        _ALL_SCOPES_PROBLEM,
        provider="fake",
        selector_strategy="rules",
        fake_script_path=script_path,
        output_dir=str(tmp_path),
    ))
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
    result = asyncio.run(deliberate(
        "Should we adopt a structured deliberation protocol?",
        provider="fake",
        fake_script_path=script_path,
        max_rounds=1,  # round 1 = continue → round 2 open trips the cap
        output_dir=str(tmp_path),
    ))
    assert "error" not in result, result
    assert result["outcome"] == "termination"
    assert result["termination_reason"] == "budget_exceeded"
    assert "synthesis_answer" not in result


def test_get_run_summary_reports_replay_ok(tmp_path, script_path):
    """get_run_summary recomputes metrics + verifies the §7.5 replay."""
    run = asyncio.run(deliberate(
        "Should we adopt a structured deliberation protocol?",
        provider="fake",
        fake_script_path=script_path,
        output_dir=str(tmp_path),
    ))
    assert "error" not in run, run

    summary = get_run_summary(run["run_dir"])
    assert "error" not in summary, summary
    assert summary["outcome"] == "synthesis"
    assert summary["digest_replay_ok"] is True
    assert summary["transcript_digest"] == run["transcript_digest"]
    assert summary["tokens"] > 0
    assert summary["cost"] >= 0.0
    assert summary["selected_agents"] == run["selected_agents"]


def test_get_version_reports_runtime_state():
    """`get_version` MUST report the live package version + key budget
    defaults, derived from the actual signature of
    `deliberate_adaptive_streaming` (so a future signature drift is
    caught by an observable mismatch, not a silent diagnostic lie).
    """
    import inspect
    import symposium

    from symposium.integrations.mcp_server import (
        deliberate_adaptive_streaming,
        get_version,
    )

    info = get_version()
    assert info["version"] == symposium.__version__
    assert info["schema_version"] == symposium.SCHEMA_VERSION
    assert isinstance(info["pid"], int) and info["pid"] > 0
    assert info["package_path"].endswith("/symposium")
    assert info["mcp_server_module"].endswith("/mcp_server.py")
    assert info["mcp_server_mtime"]  # ISO timestamp, non-empty
    # Optional diagnostic fields (added v1.10.7 per Codex review T1 #10).
    assert "git_commit" in info
    assert isinstance(info["clis"], dict) and set(info["clis"]) == {"claude", "codex"}
    assert isinstance(info["cli_auto_routing"], dict)
    # Default routing must include all built-in panel members + coordinator.
    for required in ("logician", "visionary", "researcher", "critic", "engineer", "coordinator"):
        assert required in info["cli_auto_routing"]
    assert info["cli_auto_routing"]["visionary"] == "codex-cli"
    assert info["cli_auto_routing"]["logician"] == "claude-cli"

    # Budget defaults must match the canonical signature, not drift.
    sig = inspect.signature(deliberate_adaptive_streaming)
    bd = info["budget_defaults"]
    assert bd["max_total_tokens"] == sig.parameters["max_total_tokens"].default
    assert bd["max_total_cost_usd"] == sig.parameters["max_total_cost_usd"].default
    assert bd["max_rounds"] == sig.parameters["max_rounds"].default
    assert bd["max_wallclock_seconds"] == sig.parameters["max_wallclock_seconds"].default


def test_all_deliberate_signatures_share_the_same_budget_defaults():
    """The 4 public `deliberate*` MCP tools (`deliberate`, `deliberate_streaming`,
    `deliberate_adaptive`, `deliberate_adaptive_streaming`) MUST all expose
    the same budget defaults AND all expose `per_agent_token_budget`. A drift
    on any one of them silently turns the `get_version` report (derived from
    `deliberate_adaptive_streaming` only) into a diagnostic lie: the user
    reads "100M token cap" but the tool they called terminates at 100k.
    Codex review T1 item #10 + T2 item #3 (per_agent_token_budget MUST be
    on every deliberate* surface, not just `deliberate`).
    """
    import inspect
    from symposium.integrations.mcp_server import (
        deliberate,
        deliberate_adaptive,
        deliberate_adaptive_streaming,
        deliberate_streaming,
    )

    keys = ("max_total_tokens", "max_total_cost_usd", "max_rounds", "max_wallclock_seconds")
    reference = inspect.signature(deliberate_adaptive_streaming)
    expected = {k: reference.parameters[k].default for k in keys}

    for fn in (deliberate, deliberate_streaming, deliberate_adaptive,
               deliberate_adaptive_streaming):
        sig = inspect.signature(fn)
        observed = {k: sig.parameters[k].default for k in keys}
        assert observed == expected, (
            f"{fn.__name__} budget defaults diverged from "
            f"deliberate_adaptive_streaming: {observed} vs {expected}"
        )
        # Codex T2 #3 — per_agent_token_budget MUST exist on every public
        # deliberate* MCP signature (was only on `deliberate` after T1).
        assert "per_agent_token_budget" in sig.parameters, (
            f"{fn.__name__} missing per_agent_token_budget (Codex T2 #3)"
        )
        assert sig.parameters["per_agent_token_budget"].default is None


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
    unknown = asyncio.run(deliberate(
        "anything",
        provider="fake",
        panel=["not-a-real-persona"],
        fake_script_path=script_path,
        output_dir=str(tmp_path),
    ))
    assert "error" in unknown
    assert "not-a-real-persona" in unknown["error"]

    # provider="fake" without a script.
    missing_script = asyncio.run(deliberate(
        "anything",
        provider="fake",
        output_dir=str(tmp_path),
    ))
    assert "error" in missing_script
    assert "fake_script_path" in missing_script["error"]


def test_get_run_summary_missing_run_returns_error(tmp_path):
    summary = get_run_summary(str(tmp_path / "does-not-exist"))
    assert "error" in summary


# ---------------------------------------------------------------------------
# Streaming (deliberate_streaming + its sync core stream_deliberation)
# ---------------------------------------------------------------------------


class _FakeCtx:
    """Minimal stand-in for an MCP Context: records the streamed calls."""

    def __init__(self) -> None:
        self.logs: list[str] = []
        self.progress: list[float] = []
        self.progress_messages: list[str | None] = []

    async def info(self, message: str, **extra) -> None:
        self.logs.append(message)

    async def report_progress(self, progress: float, total=None, message=None) -> None:
        self.progress.append(progress)
        self.progress_messages.append(message)


def test_stream_deliberation_emits_messages_then_result(tmp_path, script_path):
    """The generator yields one ordered message event per turn, then a result."""
    events = list(
        stream_deliberation(
            "Should we adopt a structured deliberation protocol?",
            provider="fake",
            fake_script_path=script_path,
            output_dir=str(tmp_path),
        )
    )
    kinds = [e["event"] for e in events]
    # walking-skeleton synthesis: problem + 2×(5 primary + 1 coordination) + synthesis = 14
    assert kinds.count("message") == 14
    assert kinds.count("result") == 1
    assert kinds[-1] == "result"
    assert "error" not in kinds

    # The run_dir is disclosed up-front (before the first turn) so a
    # client can poll get_run_status on a still-running deliberation.
    assert kinds[0] == "run_started"
    assert events[0]["run_dir"]
    assert events[0]["run_dir"] == events[-1]["result"]["run_dir"]

    messages = [e for e in events if e["event"] == "message"]
    # indices are 1-based and contiguous, in transcript order
    assert [m["index"] for m in messages] == list(range(1, 15))
    assert messages[0]["message"]["type"] == "problem_statement"
    assert messages[-1]["message"]["type"] == "synthesis"
    assert isinstance(messages[0]["line"], str) and messages[0]["line"]

    result = events[-1]["result"]
    assert result["outcome"] == "synthesis"
    assert result["synthesis_answer"]
    # The streamed result has the same shape as the non-streaming tool.
    assert set(result) == {
        "outcome",
        "synthesis_answer",
        "selected_agents",
        "transcript_digest",
        "cumulative_usage",
        "run_dir",
        "rounds",
    }


def test_stream_deliberation_termination_is_a_result(tmp_path, script_path):
    """A budget cap streams turns then a termination *result*, not an error event."""
    events = list(
        stream_deliberation(
            "Should we adopt a structured deliberation protocol?",
            provider="fake",
            fake_script_path=script_path,
            max_rounds=1,
            output_dir=str(tmp_path),
        )
    )
    assert [e["event"] for e in events if e["event"] == "error"] == []
    assert any(e["event"] == "message" for e in events)
    result = events[-1]["result"]
    assert result["outcome"] == "termination"
    assert result["termination_reason"] == "budget_exceeded"


def test_stream_deliberation_error_event(tmp_path):
    """A build failure yields a single error event and no result."""
    events = list(
        stream_deliberation(
            "anything",
            provider="fake",  # missing fake_script_path
            output_dir=str(tmp_path),
        )
    )
    assert len(events) == 1
    assert events[0]["event"] == "error"
    assert "fake_script_path" in events[0]["error"]


def test_deliberate_streaming_forwards_each_turn_to_context(tmp_path, script_path):
    """The async tool pushes one log per turn + progress, and returns the result."""
    ctx = _FakeCtx()
    result = asyncio.run(
        deliberate_streaming(
            "Should we adopt a structured deliberation protocol?",
            provider="fake",
            fake_script_path=script_path,
            output_dir=str(tmp_path),
            ctx=ctx,
        )
    )
    assert "error" not in result, result
    assert result["outcome"] == "synthesis"
    assert result["synthesis_answer"]
    # One run_started disclosure + one log line per transcript message (14),
    # with progress ticks alongside the messages.
    assert len(ctx.logs) == 15
    assert ctx.logs[0].startswith("[run_started] run_dir=")
    assert result["run_dir"] in ctx.logs[0]
    assert ctx.logs[-1].startswith("[r2")
    assert len(ctx.progress) == 14
    assert ctx.progress == sorted(ctx.progress)  # monotonic
    # The turn preview is also routed into the progress `message` (some MCP
    # clients render that inline but hide the `ctx.info` log notifications).
    assert len(ctx.progress_messages) == 14
    assert all(m for m in ctx.progress_messages)  # every tick carries a preview
    assert ctx.progress_messages == ctx.logs[1:]  # same preview on both channels


def test_stream_adaptive_no_expansion_one_session(tmp_path):
    """stream_adaptive without experts and a synthesis on the first session
    yields: session_start → N message events → session_end → result.

    Injects `stream_one` so no real provider / CLI is involved.
    """
    from unittest.mock import MagicMock

    fake_artifact = MagicMock()
    fake_artifact.outcome.kind = "synthesis"

    fake_session_result = {
        "outcome": "synthesis",
        "synthesis_answer": "agreed.",
        "selected_agents": ["a", "b"],
        "transcript_digest": "deadbeef",
        "cumulative_usage": {"total_tokens": 0, "cost_usd": 0.0},
        "run_dir": str(tmp_path / "fake-run"),
        "rounds": 1,
    }

    def fake_stream_one(_cfg):
        yield {"event": "message", "index": 1, "line": "msg 1", "message": {"type": "primary_turn"}}
        yield {"event": "message", "index": 2, "line": "msg 2", "message": {"type": "coordination_turn"}}
        yield {"event": "message", "index": 3, "line": "msg 3", "message": {"type": "synthesis"}}
        yield {"event": "__artifact", "artifact": fake_artifact}
        yield {"event": "result", "result": fake_session_result}

    events = list(
        stream_adaptive(
            "Why?",
            output_dir=str(tmp_path),
            persona_caller=lambda *_a, **_k: {},  # never invoked in this scenario
            stream_one=fake_stream_one,
        )
    )

    kinds = [e["event"] for e in events]
    assert kinds[0] == "session_start"
    # session_start discloses the run_dir upfront (polling-friendly).
    assert events[0]["run_dir"].startswith(str(tmp_path))
    assert kinds.count("message") == 3
    assert "session_end" in kinds
    assert kinds[-1] == "result"
    # Internal __artifact must NOT leak to the client.
    assert "__artifact" not in kinds

    result = events[-1]["result"]
    assert result["sessions"] == [fake_session_result]
    assert result["final"] is fake_session_result
    assert result["expansions"] == 0
    assert result["generated_agents"] == []


def test_put_sentinel_breaks_through_a_full_queue():
    """The streaming `_DONE` sentinel MUST reach the consumer even if the
    queue is saturated with un-consumed messages — otherwise the async
    consumer would block on `events_q.get` forever.
    """
    import queue as _q

    from symposium.integrations.mcp_server import _put_sentinel, _STREAM_QUEUE_MAXSIZE

    q = _q.Queue(maxsize=_STREAM_QUEUE_MAXSIZE)
    for i in range(_STREAM_QUEUE_MAXSIZE):
        q.put({"event": "filler", "i": i})
    assert q.full(), "precondition: queue must be saturated"

    sentinel = {"event": "__done__"}
    _put_sentinel(q, sentinel)

    # Drain the queue — the sentinel MUST be in there somewhere; the helper
    # drops the oldest events to make room rather than wedge the producer.
    seen_sentinel = False
    while not q.empty():
        item = q.get_nowait()
        if item is sentinel:
            seen_sentinel = True
            break
    assert seen_sentinel, "sentinel was dropped — consumer would hang"


def test_deliberate_streaming_error_returns_structured_error(tmp_path):
    ctx = _FakeCtx()
    result = asyncio.run(
        deliberate_streaming(
            "anything",
            provider="fake",  # missing fake_script_path
            output_dir=str(tmp_path),
            ctx=ctx,
        )
    )
    assert "error" in result
    assert ctx.logs == []  # nothing streamed before the build failed


def test_per_agent_token_budget_validation_rejects_non_positive():
    """`BudgetConfig.per_agent_token_budget` MUST reject zero/negative
    caps. Codex review T1 item #3: Pydantic accepted `Dict[str, int]`
    without lower bound, while the JSON Schema requires `minimum: 1`.
    A zero cap would terminate every persona on its first byte,
    defeating the canary's whole purpose.
    """
    import pytest
    from pydantic import ValidationError
    from symposium.models import BudgetConfig

    # Valid: positive ints accepted.
    cfg = BudgetConfig(
        max_total_tokens=1000, max_total_cost_usd=1.0, max_rounds=1,
        max_wallclock_seconds=1, per_agent_token_budget={"logician": 100},
    )
    assert cfg.per_agent_token_budget == {"logician": 100}

    # Invalid: zero cap.
    with pytest.raises(ValidationError, match="positive integer"):
        BudgetConfig(
            max_total_tokens=1000, max_total_cost_usd=1.0, max_rounds=1,
            max_wallclock_seconds=1, per_agent_token_budget={"logician": 0},
        )

    # Invalid: negative cap.
    with pytest.raises(ValidationError, match="positive integer"):
        BudgetConfig(
            max_total_tokens=1000, max_total_cost_usd=1.0, max_rounds=1,
            max_wallclock_seconds=1, per_agent_token_budget={"logician": -1},
        )


def test_deliberate_mcp_signature_exposes_per_agent_token_budget():
    """`deliberate` MCP tool MUST expose `per_agent_token_budget` as a
    parameter, so an MCP client can set per-persona caps from outside
    the runtime. Codex review T1 item #3: the param existed in
    `BudgetConfig` but no MCP signature surfaced it, so canary caps
    were unreachable for the deliberation tools clients actually call.
    """
    import inspect
    from symposium.integrations.mcp_server import deliberate

    sig = inspect.signature(deliberate)
    assert "per_agent_token_budget" in sig.parameters, (
        "deliberate must expose per_agent_token_budget (Codex T1 #3)"
    )
    assert sig.parameters["per_agent_token_budget"].default is None


def test_artifact_carries_last_provider_failure_end_to_end(tmp_path, repo_root):
    """End-to-end: a non-retriable provider failure MUST surface as
    `artifact.outcome.termination_artifact.last_provider_failure`,
    `_build_result` MUST include it under the same key, and
    `get_run_summary` MUST too. Codex review T2 item #2/#2b — covers
    the path the regression test in test_retry_backoff stops short of.
    """
    import json
    from unittest.mock import patch

    from symposium.models import (
        ProviderError,
        ProviderRawMessage,
        ProviderResult,
        Usage,
    )
    from symposium.integrations.mcp_server import _build_result, get_run_summary
    from symposium.providers.fake import FakeProvider
    from symposium.scheduler import run_session
    from symposium.models import FakeProviderScript

    # FakeProvider entry that errors on the very first call (logician,
    # round 1). The match is intentionally loose so the same entry
    # matches every retry attempt (retry_budget=2 → 3 attempts, all
    # fail identically → terminate as provider_unrecoverable).
    error_entry = {
        "match": {"agent_id": "logician"},
        "result": {
            "messages": [],
            "tool_events": [],
            "usage": {
                "prompt_tokens": 0, "completion_tokens": 0,
                "total_tokens": 0, "cost_usd": 0.0,
            },
            "finish_reason": "error",
            "structured_output": None,
            "raw": {"stderr": "unknown variant `max`"},
            "error": {
                "kind": "internal",
                "message": "codex exited 1: unknown variant `max`, expected one of `none, minimal, low, medium, high, xhigh`",
                "retriable": False,
                "details": {"hint": "use xhigh in model_reasoning_effort"},
            },
        },
    }
    script_dict = {
        "schema_version": "1.0.0",
        "on_exhaustion": "loop",
        "entries": [error_entry],
    }
    script_path = tmp_path / "fake-script.json"
    script_path.write_text(json.dumps(script_dict))

    from symposium.integrations.mcp_server import deliberate

    result = asyncio.run(deliberate(
        "trigger codex failure",
        provider="fake",
        fake_script_path=str(script_path),
        output_dir=str(tmp_path / "runs"),
    ))

    assert "error" not in result, result
    assert result["outcome"] == "termination"
    assert result["termination_reason"] == "provider_unrecoverable"
    # The actionable diagnostic must round-trip end-to-end.
    assert "last_provider_failure" in result, (
        f"missing last_provider_failure in MCP result: {result}"
    )
    lpf = result["last_provider_failure"]
    assert lpf["agent_id"] == "logician"
    assert lpf["provider"] == "fake"
    assert lpf["kind"] == "internal"
    assert "unknown variant `max`" in lpf["message"]
    assert lpf["details"]["hint"] == "use xhigh in model_reasoning_effort"

    # And get_run_summary surfaces it too (operator can re-fetch after the fact).
    summary = get_run_summary(result["run_dir"])
    assert summary["termination_reason"] == "provider_unrecoverable"
    assert "last_provider_failure" in summary
    assert "unknown variant `max`" in summary["last_provider_failure"]["message"]


def test_get_run_status_streams_transcript_progressively(tmp_path):
    """`get_run_status` MUST read transcript entries from a since_index
    and report whether the run is still active. Designed for polling
    long-running deliberations: an agent calls it repeatedly with the
    previous `next_index` to fetch only new turns and show them live.
    """
    from symposium.integrations.mcp_server import get_run_status

    rd = tmp_path / "test-run"
    rd.mkdir()
    transcript = rd / "transcript.jsonl"
    # Simulate a transcript with 3 entries.
    entries = [
        {"id": "m0", "speaker": "mcp", "type": "problem_statement",
         "content": "Solve X", "round": 0, "turn_index": 0,
         "timestamp": "2026-05-27T22:31:00Z"},
        {"id": "m1", "speaker": "logician", "type": "primary_turn",
         "content": {"text": "Logician says A."}, "round": 1, "turn_index": 1,
         "timestamp": "2026-05-27T22:32:00Z"},
        {"id": "m2", "speaker": "visionary", "type": "primary_turn",
         "content": {"text": "Visionary says B."}, "round": 1, "turn_index": 2,
         "timestamp": "2026-05-27T22:33:00Z"},
    ]
    transcript.write_text("\n".join(json.dumps(e) for e in entries) + "\n")
    # Lock file → run_active=True
    (rd / ".lock").write_text("pid 1234")

    # First call: get all 3 from start.
    s1 = get_run_status(str(rd))
    assert "error" not in s1
    assert len(s1["messages"]) == 3
    assert s1["next_index"] == 3
    assert s1["remaining"] == 0
    assert s1["run_active"] is True
    assert s1["total_so_far"] == 3
    assert s1["messages"][0]["speaker"] == "mcp"
    assert s1["messages"][1]["text"] == "Logician says A."
    assert s1["messages"][2]["text"] == "Visionary says B."
    assert s1["messages"][0]["text"] == "Solve X"  # plain-string content

    # Second call: since_index=3, no new entries yet.
    s2 = get_run_status(str(rd), since_index=3)
    assert s2["messages"] == []
    assert s2["next_index"] == 3

    # Append a new entry → next poll sees it as delta.
    with open(transcript, "a") as f:
        f.write(json.dumps({"id": "m3", "speaker": "researcher",
                            "type": "primary_turn",
                            "content": {"text": "Researcher says C."},
                            "round": 1, "turn_index": 3,
                            "timestamp": "2026-05-27T22:34:00Z"}) + "\n")
    s3 = get_run_status(str(rd), since_index=s2["next_index"])
    assert len(s3["messages"]) == 1
    assert s3["messages"][0]["speaker"] == "researcher"
    assert s3["messages"][0]["index"] == 3
    assert s3["next_index"] == 4

    # Once .lock disappears, run_active=False (writer finished).
    (rd / ".lock").unlink()
    s4 = get_run_status(str(rd))
    assert s4["run_active"] is False


def test_get_run_status_clamps_limit_and_reports_remaining(tmp_path):
    """Defensive: huge `limit` requests get clamped. Caller still sees
    a non-zero `remaining` so they know to drain more.
    """
    from symposium.integrations.mcp_server import get_run_status

    rd = tmp_path / "big-run"
    rd.mkdir()
    transcript = rd / "transcript.jsonl"
    # 5 entries
    entries = [
        {"id": f"m{i}", "speaker": "x", "type": "primary_turn",
         "content": {"text": f"entry {i}"}, "round": 1, "turn_index": i,
         "timestamp": "2026-05-27T00:00:00Z"}
        for i in range(5)
    ]
    transcript.write_text("\n".join(json.dumps(e) for e in entries) + "\n")

    # limit=2 → returns 2, reports 3 remaining
    s = get_run_status(str(rd), limit=2)
    assert len(s["messages"]) == 2
    assert s["next_index"] == 2
    assert s["remaining"] == 3
    assert s["total_so_far"] == 5

    # limit=10000 → clamped, all returned (since total is 5 < clamp)
    s = get_run_status(str(rd), limit=10000)
    assert len(s["messages"]) == 5


def test_get_run_status_missing_run_returns_error(tmp_path):
    from symposium.integrations.mcp_server import get_run_status
    s = get_run_status(str(tmp_path / "no-such-run"))
    assert "error" in s


def test_get_run_status_treats_stale_lock_as_inactive(tmp_path, monkeypatch):
    """A `.lock` file with a dead PID MUST NOT trick `get_run_status`
    into reporting `run_active=True` — a polling agent would otherwise
    loop forever after a crashed RunWriter. Codex review T7 #2.

    Uses the same `_is_stale_lock` staleness check the storage writer
    uses to reclaim orphan locks, so the two views agree. The PID-alive
    probe is mocked deterministically (Codex T8 nit): a real PID like
    999999 is "almost certainly dead" but not guaranteed, which would
    leave the test theoretically flaky.
    """
    from symposium.integrations.mcp_server import get_run_status
    from symposium.storage import writer as writer_module

    rd = tmp_path / "crashed-run"
    rd.mkdir()
    (rd / "transcript.jsonl").write_text(
        json.dumps({"id": "m0", "speaker": "mcp", "type": "problem_statement",
                    "content": "x", "round": 0, "turn_index": 0,
                    "timestamp": "2026-05-27T00:00:00Z"}) + "\n"
    )
    (rd / ".lock").write_text("12345")  # any int — we mock os.kill

    # Force the writer's PID-alive probe to claim the process is gone.
    def _dead(pid, sig):
        raise ProcessLookupError(f"no such pid {pid}")
    monkeypatch.setattr(writer_module.os, "kill", _dead)

    s = get_run_status(str(rd))
    assert "error" not in s, s
    assert s["lock_stale"] is True, (
        "lock with dead PID should be flagged stale"
    )
    assert s["run_active"] is False, (
        "stale lock must NOT report run_active=True (Codex T7 #2)"
    )


def test_get_run_status_surfaces_coordinator_verdict_and_synthesis_content(tmp_path):
    """`get_run_status` MUST surface readable text for `coordination_turn`
    (Verdict — has `rationale` + `focus`, no `text`) AND `synthesis`
    (SynthesisContent — has `integrated_answer`, no `text`). Pre-T7
    fallback to `c.get("text", "")` silently dropped both, so the
    "live dialogue" view hid exactly the coordinator's verdict and the
    final synthesis — i.e., the two most important turns. Codex T7 #6.
    """
    from symposium.integrations.mcp_server import get_run_status

    rd = tmp_path / "verdict-run"
    rd.mkdir()
    entries = [
        # Verdict-shaped content (no `text` field)
        {"id": "v1", "speaker": "coordinator", "type": "coordination_turn",
         "content": {"next_action": "continue", "rationale": "the panel converges on X",
                     "confidence": 0.7, "next_agents": ["logician"],
                     "resolved_disagreements": [], "unresolved_disagreements": []},
         "round": 1, "turn_index": 5, "timestamp": "2026-05-27T00:00:00Z"},
        # Synthesis-shaped content (no `text`, has `integrated_answer`)
        {"id": "s1", "speaker": "coordinator", "type": "synthesis",
         "content": {"integrated_answer": "The final answer is 42.",
                     "resolved_disagreements": [], "unresolved_disagreements": []},
         "round": 2, "turn_index": 10, "timestamp": "2026-05-27T00:01:00Z"},
    ]
    (rd / "transcript.jsonl").write_text(
        "\n".join(json.dumps(e) for e in entries) + "\n"
    )

    s = get_run_status(str(rd))
    assert len(s["messages"]) == 2
    # coordination_turn → text falls back to `rationale`
    assert s["messages"][0]["type"] == "coordination_turn"
    assert s["messages"][0]["text"] == "the panel converges on X"
    # synthesis → text falls back to `integrated_answer`
    assert s["messages"][1]["type"] == "synthesis"
    assert s["messages"][1]["text"] == "The final answer is 42."


def test_get_run_status_next_index_anchors_to_last_returned(tmp_path):
    """`next_index` MUST point to the index immediately after the LAST
    returned message, NOT `since + len(messages)`. Codex review T7 #4:
    the pre-fix arithmetic over-advanced when a malformed/empty line
    was silently skipped, causing the next poll to miss valid entries
    or re-fetch already-seen ones.
    """
    from symposium.integrations.mcp_server import get_run_status

    rd = tmp_path / "partial-run"
    rd.mkdir()
    transcript = rd / "transcript.jsonl"
    # 3 valid entries
    valid = [
        {"id": f"m{i}", "speaker": "x", "type": "primary_turn",
         "content": {"text": f"turn {i}"}, "round": 1, "turn_index": i,
         "timestamp": "2026-05-27T00:00:00Z"}
        for i in range(3)
    ]
    transcript.write_text("\n".join(json.dumps(e) for e in valid) + "\n")

    s = get_run_status(str(rd))
    # All 3 valid → next_index = index of last (2) + 1 = 3
    assert s["next_index"] == 3
    assert s["messages"][-1]["index"] == 2

    # Empty result → next_index stays at since_index (don't lie about progress)
    s2 = get_run_status(str(rd), since_index=10)
    assert s2["messages"] == []
    assert s2["next_index"] == 10


# ---------------------------------------------------------------------------
# Event-loop responsiveness, notification guards, input bounds
# ---------------------------------------------------------------------------


_VALID_PERSONA = {
    "persona_class": "domain", "id": "dba",
    "reasoning_scope": "database tuning",
    "reasoning_style": "measure-first",
    "behavioral_constraints": ["quote the query plan"],
    "failure_modes": ["index cargo-culting"],
    "domain_scope": ["databases"],
    "forbidden_domains": ["ui design"],
    "must_delegate": {"legal compliance": "a legal expert"},
}


def test_muted_deliberate_leaves_event_loop_responsive(tmp_path, script_path, monkeypatch):
    """While a muted deliberation is in flight the server MUST still answer
    other tools. Pre-fix, the muted tools were plain sync functions and the
    SDK executed them directly on the event loop — a long run froze
    get_run_status / get_version / pings for its whole duration (the
    historical "server looks dead / stale" failure).

    The gate parks `run_session` in its worker thread until released, so
    the test can prove get_version / get_run_status answer *through the
    server* while the deliberation is provably still running.
    """
    import threading

    import symposium.integrations.mcp_server as mcp_mod

    started = threading.Event()
    release = threading.Event()
    real_run_session = mcp_mod.run_session

    def gated_run_session(*args, **kwargs):
        started.set()
        assert release.wait(timeout=30.0), "test gate never released"
        return real_run_session(*args, **kwargs)

    monkeypatch.setattr(mcp_mod, "run_session", gated_run_session)

    async def scenario():
        task = asyncio.create_task(deliberate(
            "Should we adopt a structured deliberation protocol?",
            provider="fake",
            fake_script_path=script_path,
            output_dir=str(tmp_path),
        ))
        # Wait (off-loop) until the deliberation body is parked inside
        # run_session — i.e. the muted tool is genuinely mid-flight.
        assert await asyncio.to_thread(started.wait, 30.0)
        assert not task.done()

        version = await asyncio.wait_for(
            mcp_mod.mcp.call_tool("get_version", {}), timeout=10.0
        )
        assert version, "get_version did not answer during a muted run"
        status = await asyncio.wait_for(
            mcp_mod.mcp.call_tool(
                "get_run_status", {"run_dir": str(tmp_path / "missing")}
            ),
            timeout=10.0,
        )
        assert status, "get_run_status did not answer during a muted run"
        assert not task.done(), "deliberation finished before the gate opened"

        release.set()
        return await asyncio.wait_for(task, timeout=30.0)

    result = asyncio.run(scenario())
    assert "error" not in result, result
    assert result["outcome"] == "synthesis"


class _FlakyCtx(_FakeCtx):
    """Ctx whose notifications start failing after `fail_after` infos —
    models a client that disconnected mid-run."""

    def __init__(self, fail_after: int) -> None:
        super().__init__()
        self._fail_after = fail_after

    async def info(self, message: str, **extra) -> None:
        if len(self.logs) >= self._fail_after:
            raise ConnectionError("client went away")
        await super().info(message, **extra)


def test_deliberate_streaming_survives_notification_failure(tmp_path, script_path):
    """A ctx.info failure mid-stream MUST NOT surface as an opaque MCP
    error: streaming stops, the queue keeps draining, and the final
    result (with its run_dir) is still returned.
    """
    ctx = _FlakyCtx(fail_after=3)
    result = asyncio.run(
        deliberate_streaming(
            "Should we adopt a structured deliberation protocol?",
            provider="fake",
            fake_script_path=script_path,
            output_dir=str(tmp_path),
            ctx=ctx,
        )
    )
    assert "error" not in result, result
    assert result["outcome"] == "synthesis"
    assert result["run_dir"]
    # Streaming stopped at the first refused notification…
    assert len(ctx.logs) == 3
    # …and no further progress ticks were attempted either.
    assert len(ctx.progress) <= 3


def test_adaptive_streaming_survives_notification_failure(tmp_path, monkeypatch):
    """Same guard on the adaptive tool: every ctx.info (agent_generated /
    session_start / session_end / message) is best-effort; the aggregate
    result is returned even when the very first notification fails.
    """
    import symposium.integrations.mcp_server as mcp_mod

    final = {
        "final": {"outcome": "synthesis", "synthesis_answer": "ok"},
        "sessions": [{"outcome": "synthesis"}],
        "generated_agents": [],
        "expansions": 0,
        "panel_final": ["logician"],
    }

    def fake_stream_adaptive(problem, **kwargs):
        yield {"event": "session_start", "session_index": 1, "session_id": "s1",
               "run_dir": str(tmp_path / "s1"), "panel": ["logician"]}
        yield {"event": "message", "index": 1, "line": "msg 1", "message": {}}
        yield {"event": "message", "index": 2, "line": "msg 2", "message": {}}
        yield {"event": "session_end", "session_index": 1, "session_id": "s1",
               "outcome": "synthesis"}
        yield {"event": "result", "result": final}

    monkeypatch.setattr(mcp_mod, "stream_adaptive", fake_stream_adaptive)

    ctx = _FlakyCtx(fail_after=1)
    result = asyncio.run(mcp_mod.deliberate_adaptive_streaming("p", ctx=ctx))
    assert result == final
    assert len(ctx.logs) == 1  # streaming stopped after the refused info


def test_generate_persona_rejects_oversized_need():
    from symposium.integrations.mcp_server import (
        _MAX_NEED_LENGTH_CHARS,
        generate_persona,
    )

    result = asyncio.run(
        generate_persona("x" * (_MAX_NEED_LENGTH_CHARS + 1))
    )
    assert "error" in result
    assert str(_MAX_NEED_LENGTH_CHARS) in result["error"]


def test_generate_persona_rejects_empty_need():
    from symposium.integrations.mcp_server import generate_persona

    result = asyncio.run(generate_persona("   "))
    assert "error" in result
    assert "non-empty" in result["error"]


def test_generate_persona_rejects_unknown_prefer_cli():
    from symposium.integrations.mcp_server import generate_persona

    result = asyncio.run(generate_persona("need a dba", prefer_cli="gemini"))
    assert "error" in result
    assert "prefer_cli" in result["error"]


def test_generate_persona_normalizes_prefer_cli_case(monkeypatch):
    """prefer_cli="Claude" MUST keep the claude preference, not silently
    flip to codex (the pre-fix behavior of the exact-match check)."""
    import symposium.integrations.mcp_server as mcp_mod

    seen = {}

    def fake_make_caller(*, prefer):
        seen["prefer"] = prefer
        return lambda prompt, schema: dict(_VALID_PERSONA)

    monkeypatch.setattr(mcp_mod, "make_cli_persona_caller", fake_make_caller)

    result = asyncio.run(
        mcp_mod.generate_persona("need a dba", prefer_cli="Claude")
    )
    assert "error" not in result, result
    assert seen["prefer"] == "claude"
    assert result["persona"]["id"] == "dba"


def test_pending_need_is_truncated_to_the_cap():
    """LLM-authored expansion needs feed the persona generator; they get
    the same length bound as caller-supplied needs."""
    import types

    from symposium.integrations.mcp_server import (
        _MAX_NEED_LENGTH_CHARS,
        _pending_need,
    )

    long_q = "q" * (_MAX_NEED_LENGTH_CHARS + 500)
    ta = types.SimpleNamespace(
        reason="user_input_required",
        pending_user_input_request=types.SimpleNamespace(question=long_q),
        pending_external_research_request=None,
    )
    artifact = types.SimpleNamespace(
        outcome=types.SimpleNamespace(kind="termination", termination_artifact=ta)
    )
    need = _pending_need(artifact)
    assert need is not None
    assert len(need) == _MAX_NEED_LENGTH_CHARS


def test_adaptive_failure_returns_partial_sessions(tmp_path):
    """A failure in session N MUST NOT discard sessions 1..N-1: they are
    persisted on disk and the caller needs their run_dirs. The aggregate
    carries the error alongside everything that completed.
    """
    import types

    from symposium.integrations.mcp_server import _run_adaptive

    ta = types.SimpleNamespace(
        reason="user_input_required",
        pending_user_input_request=types.SimpleNamespace(
            question="need a cryptographer"
        ),
        pending_external_research_request=None,
    )
    termination = types.SimpleNamespace(
        outcome=types.SimpleNamespace(kind="termination", termination_artifact=ta)
    )
    s1_result = {
        "outcome": "termination",
        "termination_reason": "user_input_required",
        "run_dir": str(tmp_path / "s1"),
    }

    calls = {"n": 0}

    def runner(cfg):
        calls["n"] += 1
        if calls["n"] == 1:
            return termination, s1_result
        raise RuntimeError("provider exploded")

    out = _run_adaptive(
        problem="p", panel=["logician"], coordinator="coordinator",
        provider="fake", experts=None, max_expansions=2, max_rounds=2,
        max_total_tokens=1000, max_total_cost_usd=1.0,
        max_wallclock_seconds=10, output_dir=str(tmp_path),
        persona_caller=lambda prompt, schema: dict(_VALID_PERSONA),
        run_one=runner,
    )
    assert "error" in out
    assert "provider exploded" in out["error"]
    # Session 1 (and its run_dir) survives the session-2 failure.
    assert out["sessions"] == [s1_result]
    assert out["final"] == s1_result
    assert [g["id"] for g in out["generated_agents"]] == ["dba"]
    assert out["expansions"] == 1


def test_cli_auto_threads_model_to_claude_routing(tmp_path, monkeypatch):
    """provider="cli-auto" + model="sonnet" MUST reach the router as
    claude_model="sonnet" (pre-fix the model was silently dropped and
    "opus" stamped unconditionally). No model → the "opus" default."""
    import symposium.integrations.cli_routing as cli_routing
    from symposium.integrations.mcp_server import _prepare

    seen = {}

    def fake_route(config, **kwargs):
        seen["claude_model"] = kwargs.get("claude_model")
        return config, {"default": object()}

    monkeypatch.setattr(cli_routing, "route_cli_providers", fake_route)

    common = dict(
        problem="p", panel=None, coordinator="coordinator", provider="cli-auto",
        selector_strategy="fixed", max_rounds=1, max_total_tokens=1000,
        max_total_cost_usd=1.0, max_wallclock_seconds=10,
        fake_script_path=None, selector_fake_script_path=None,
        output_dir=str(tmp_path),
    )

    config, _, _, _, _ = _prepare(model="sonnet", **common)
    assert seen["claude_model"] == "sonnet"
    assert {a.model for a in config.agents} == {"sonnet"}

    _prepare(model=None, **common)
    assert seen["claude_model"] == "opus"


def test_stream_adaptive_defers_cli_detection_until_first_generation(tmp_path, monkeypatch):
    """An adaptive run that never generates a persona MUST NOT probe the
    CLIs at all — pre-fix the caller was built eagerly at tool entry, so
    max_expansions=0 on a host without claude/codex failed upfront."""
    from unittest.mock import MagicMock

    import symposium.integrations.mcp_server as mcp_mod

    def boom(*args, **kwargs):
        raise RuntimeError("no CLI installed")

    monkeypatch.setattr(mcp_mod, "make_cli_persona_caller", boom)

    fake_artifact = MagicMock()
    fake_artifact.outcome.kind = "synthesis"
    session_result = {"outcome": "synthesis", "synthesis_answer": "ok"}

    def fake_stream_one(_cfg):
        yield {"event": "message", "index": 1, "line": "msg 1", "message": {}}
        yield {"event": "__artifact", "artifact": fake_artifact}
        yield {"event": "result", "result": session_result}

    events = list(
        mcp_mod.stream_adaptive(
            "Why?", output_dir=str(tmp_path), stream_one=fake_stream_one
        )
    )
    kinds = [e["event"] for e in events]
    assert "error" not in kinds
    assert kinds[-1] == "result"


def test_adaptive_muted_defers_cli_detection(monkeypatch):
    """Same laziness on the muted tool: the persona caller handed to
    `_run_adaptive` only touches the CLIs when actually invoked."""
    import symposium.integrations.mcp_server as mcp_mod

    def boom(*args, **kwargs):
        raise RuntimeError("no CLI installed")

    monkeypatch.setattr(mcp_mod, "make_cli_persona_caller", boom)

    canned = {"final": {"outcome": "synthesis"}, "sessions": [],
              "generated_agents": [], "expansions": 0, "panel_final": []}
    seen = {}

    def fake_run_adaptive(**kwargs):
        seen["persona_caller"] = kwargs["persona_caller"]
        return canned

    monkeypatch.setattr(mcp_mod, "_run_adaptive", fake_run_adaptive)

    result = asyncio.run(mcp_mod.deliberate_adaptive("p", max_expansions=0))
    assert result == canned
    # CLI detection is deferred to the caller's first use.
    with pytest.raises(RuntimeError, match="no CLI installed"):
        seen["persona_caller"]("prompt", {})


def test_adaptive_garbage_max_expansions_is_a_structured_error():
    """A non-numeric max_expansions must yield {"error": ...} on BOTH
    adaptive variants (the streaming twin used to clamp outside the try,
    leaking a raw exception through the transport)."""
    import symposium.integrations.mcp_server as mcp_mod

    muted = asyncio.run(mcp_mod.deliberate_adaptive("p", max_expansions="lots"))
    assert "error" in muted

    streamed = asyncio.run(
        mcp_mod.deliberate_adaptive_streaming(
            "p", max_expansions="lots", ctx=_FakeCtx()
        )
    )
    assert "error" in streamed


def test_wallclock_docstrings_match_the_real_default():
    """The muted deliberate docstring used to claim an 1800s cli-auto
    wallclock default while the signature default is 3600."""
    import inspect

    import symposium.integrations.mcp_server as mcp_mod

    default = inspect.signature(mcp_mod.deliberate).parameters[
        "max_wallclock_seconds"
    ].default
    doc = mcp_mod.deliberate.__doc__ or ""
    assert "1800s" not in doc
    assert f"{default}s" in doc


def test_viewer_import_is_function_local():
    """A viewer import failure must not break every MCP tool at import
    time: no module-level import from symposium.viewer."""
    import ast
    from pathlib import Path as _Path

    import symposium.integrations.mcp_server as mcp_mod

    tree = ast.parse(_Path(mcp_mod.__file__).read_text())
    offenders = [
        node for node in tree.body
        if isinstance(node, ast.ImportFrom)
        and (node.module or "").startswith("symposium.viewer")
    ]
    assert offenders == []

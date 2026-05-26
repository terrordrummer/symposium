"""ClaudeCliProvider — drives the `claude` terminal CLI (no API key).

Every test injects a fake `subprocess.run`-shaped runner, so nothing
here spawns the real CLI or touches the network. The runner records the
argv / stdin it was called with and returns a canned CLI-shaped JSON
payload, letting us assert the request translation, the structured-output
extraction, usage/cost mapping, error mapping, the corrective retry, and
a full `run_session` loop end-to-end.
"""

from __future__ import annotations

import json
import subprocess

import pytest

from symposium.models import ProviderRequest, ProviderRequestMessage
from symposium.providers import default_registry
from symposium.providers.claude_cli import ClaudeCliProvider


def _cli_json(*, structured=None, result="prose", stop_reason="end_turn",
              cost=0.01, input_tokens=100, output_tokens=50, cache_read=10,
              is_error=False, subtype="success") -> str:
    payload = {
        "type": "result",
        "subtype": subtype,
        "is_error": is_error,
        "result": result,
        "stop_reason": stop_reason,
        "total_cost_usd": cost,
        "usage": {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cache_read_input_tokens": cache_read,
        },
    }
    if structured is not None:
        payload["structured_output"] = structured
    return json.dumps(payload)


def _completed(stdout: str, returncode: int = 0, stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=["claude"], returncode=returncode,
                                       stdout=stdout, stderr=stderr)


class _RecordingRunner:
    """A subprocess.run stand-in that records calls and replays canned outputs."""

    def __init__(self, outputs):
        self._outputs = list(outputs)
        self.calls = []

    def __call__(self, argv, *, input=None, capture_output=None, text=None, timeout=None):
        self.calls.append({"argv": argv, "input": input, "timeout": timeout})
        out = self._outputs.pop(0)
        if isinstance(out, Exception):
            raise out
        return out


def _turn_request(expected="turn_structured_output", model="sonnet"):
    return ProviderRequest(
        provider="claude-cli",
        model=model,
        agent_id="logician",
        messages=[
            ProviderRequestMessage(role="system", content="You are the logician."),
            ProviderRequestMessage(role="user", content="Should we adopt the protocol?"),
        ],
        expected_output_schema=expected,
    )


def test_registered_in_default_registry():
    assert default_registry().has("claude-cli")


def test_successful_turn_extracts_structured_output_and_usage():
    runner = _RecordingRunner([_completed(_cli_json(structured={"text": "My turn."}))])
    provider = ClaudeCliProvider(runner=runner)
    result = provider.invoke(_turn_request())

    assert result.error is None
    assert result.structured_output == {"text": "My turn."}
    assert result.finish_reason == "stop"
    # prompt(input)=input_tokens+cache_read=110, completion=output_tokens=50
    assert result.usage.prompt_tokens == 110
    assert result.usage.completion_tokens == 50
    assert result.usage.total_tokens == 160
    # cost is the CLI's API-equivalent figure, recorded as estimated (under a
    # subscription login it is a reference, not a metered charge)
    assert result.usage.cost_usd == 0.01
    assert result.usage.estimated is True


def test_argv_and_stdin_translation():
    runner = _RecordingRunner([_completed(_cli_json(structured={"text": "t"}))])
    ClaudeCliProvider(runner=runner).invoke(_turn_request(model="opus"))

    argv = runner.calls[0]["argv"]
    assert argv[:4] == ["claude", "-p", "--output-format", "json"]
    assert "--model" in argv and argv[argv.index("--model") + 1] == "opus"
    # system message → --system-prompt; it is NOT in the stdin body
    assert "--system-prompt" in argv
    assert argv[argv.index("--system-prompt") + 1] == "You are the logician."
    # the expected schema is passed via --json-schema
    assert "--json-schema" in argv
    schema = json.loads(argv[argv.index("--json-schema") + 1])
    assert "text" in schema["properties"]
    # the user content is fed on stdin, not the system content
    assert runner.calls[0]["input"] == "Should we adopt the protocol?"


def test_verdict_schema_is_passed():
    runner = _RecordingRunner([
        _completed(_cli_json(structured={
            "next_action": "continue", "rationale": "more", "confidence": 0.5,
            "focus": "x", "next_agents": [], "resolved_disagreements": [],
            "unresolved_disagreements": [],
        })),
    ])
    result = ClaudeCliProvider(runner=runner).invoke(_turn_request(expected="verdict"))
    assert result.error is None
    assert result.structured_output["next_action"] == "continue"
    schema = json.loads(runner.calls[0]["argv"][runner.calls[0]["argv"].index("--json-schema") + 1])
    assert "next_action" in schema["properties"]


def test_null_schema_free_text_path():
    """The §4.1 llm-selector free-text path: no --json-schema, text result."""
    runner = _RecordingRunner([_completed(_cli_json(result="free text answer"))])
    req = _turn_request(expected="null")
    result = ClaudeCliProvider(runner=runner).invoke(req)
    assert result.error is None
    assert result.structured_output is None
    assert result.messages[0].content == "free text answer"
    assert "--json-schema" not in runner.calls[0]["argv"]


def test_missing_structured_output_then_corrective_retry_succeeds():
    runner = _RecordingRunner([
        _completed(_cli_json(structured=None)),                 # 1st: no structured_output
        _completed(_cli_json(structured={"text": "fixed"})),    # 2nd: corrective retry ok
    ])
    result = ClaudeCliProvider(runner=runner).invoke(_turn_request())
    assert result.error is None
    assert result.structured_output == {"text": "fixed"}
    assert len(runner.calls) == 2
    # the corrective retry restates the schema requirement on stdin
    assert "did not conform" in runner.calls[1]["input"]


def test_schema_invalid_after_retry_returns_malformed():
    bad = _completed(_cli_json(structured={"not_text": "wrong"}))
    runner = _RecordingRunner([bad, bad])
    result = ClaudeCliProvider(runner=runner).invoke(_turn_request())
    assert result.error is not None
    assert result.error.kind == "malformed_response"
    assert result.structured_output is None


def test_nonzero_exit_is_error():
    runner = _RecordingRunner([_completed("", returncode=1, stderr="boom")])
    result = ClaudeCliProvider(runner=runner).invoke(_turn_request())
    assert result.error is not None
    assert result.error.kind == "internal"
    assert "boom" in result.error.message


def test_timeout_maps_to_timeout_error():
    runner = _RecordingRunner([subprocess.TimeoutExpired(cmd="claude", timeout=5)])
    result = ClaudeCliProvider(runner=runner).invoke(_turn_request())
    assert result.error is not None
    assert result.error.kind == "timeout"
    assert result.error.retriable is True


def test_cli_is_error_payload_maps_to_error():
    runner = _RecordingRunner([_completed(_cli_json(structured={"text": "x"},
                                                    is_error=True, subtype="error_during_execution"))])
    result = ClaudeCliProvider(runner=runner).invoke(_turn_request())
    assert result.error is not None
    assert result.error.kind == "internal"


def test_missing_binary_raises_at_construction():
    with pytest.raises(FileNotFoundError):
        ClaudeCliProvider(binary="claude-does-not-exist-xyz")


def test_end_to_end_run_session_with_claude_cli(tmp_path):
    """A full deliberation loop driven by a mocked claude CLI → synthesis."""
    from symposium.models import (
        AgentConfig, BudgetConfig, Config, SelectorConfig,
    )
    from symposium.personas import COORDINATOR, persona_by_id
    from symposium.scheduler import run_session

    def _runner(argv, *, input=None, capture_output=None, text=None, timeout=None):
        # Decide the canned structured_output from the requested schema.
        schema = json.loads(argv[argv.index("--json-schema") + 1])
        props = schema.get("properties", {})
        if "integrated_answer" in props:
            structured = {
                "integrated_answer": "Adopt the protocol.",
                "resolved_disagreements": [], "unresolved_disagreements": [],
                "confidence": 0.9,
            }
        elif "next_action" in props:
            structured = {
                "next_action": "finalize", "rationale": "converged",
                "confidence": 0.9, "focus": "integrate", "next_agents": [],
                "resolved_disagreements": [], "unresolved_disagreements": [],
            }
        else:
            structured = {"text": "A considered turn."}
        return _completed(_cli_json(structured=structured))

    provider = ClaudeCliProvider(runner=_runner)
    panel = ["logician", "critic"]
    config = Config(
        schema_version="1.0.0",
        session_id="claude-cli-e2e",
        originator="test",
        problem_statement="Should we adopt the protocol?",
        selector=SelectorConfig(strategy="fixed", default_deliberation_panel=panel,
                                coordinator_agent="coordinator"),
        agents=[AgentConfig(id=p, persona_ref=persona_by_id(p), provider="claude-cli",
                            model="sonnet") for p in panel],
        coordinator=AgentConfig(id="coordinator", persona_ref=COORDINATOR,
                                provider="claude-cli", model="sonnet"),
        budget=BudgetConfig(max_total_tokens=100000, max_total_cost_usd=5.0,
                            max_rounds=4, max_wallclock_seconds=60),
    )

    artifact = run_session(config, {"default": provider}, runs_root=str(tmp_path))
    assert artifact.outcome.kind == "synthesis"
    synth = next(m for m in artifact.canonical_transcript
                 if m.id == artifact.outcome.synthesis_message_id)
    assert synth.content["integrated_answer"] == "Adopt the protocol."

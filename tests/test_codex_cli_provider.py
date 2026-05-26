"""CodexCliProvider — drives the `codex exec` CLI (no API key).

Every test injects a fake `subprocess.run`-shaped runner returning a
canned `codex exec --json` JSONL stream, so nothing spawns the real CLI
or touches the network.
"""

from __future__ import annotations

import json
import subprocess

import pytest

from symposium.models import ProviderRequest, ProviderRequestMessage
from symposium.providers import default_registry
from symposium.providers.codex_cli import CodexCliProvider


def _codex_stdout(*, structured=None, text=None, usage=None, failed=False) -> str:
    lines = ['{"type":"thread.started","thread_id":"t"}', '{"type":"turn.started"}']
    if failed:
        lines.append(json.dumps({"type": "turn.failed", "error": {"message": "boom"}}))
    else:
        msg = text if text is not None else (json.dumps(structured) if structured is not None else "")
        lines.append(json.dumps(
            {"type": "item.completed", "item": {"id": "item_0", "type": "agent_message", "text": msg}}
        ))
        u = usage or {"input_tokens": 100, "cached_input_tokens": 20,
                      "output_tokens": 50, "reasoning_output_tokens": 5}
        lines.append(json.dumps({"type": "turn.completed", "usage": u}))
    return "\n".join(lines) + "\n"


def _completed(stdout: str, returncode: int = 0, stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=["codex"], returncode=returncode,
                                       stdout=stdout, stderr=stderr)


class _RecordingRunner:
    def __init__(self, outputs):
        self._outputs = list(outputs)
        self.calls = []

    def __call__(self, argv, *, input=None, capture_output=None, text=None, timeout=None):
        self.calls.append({"argv": argv, "input": input})
        out = self._outputs.pop(0)
        if isinstance(out, Exception):
            raise out
        return out


def _req(expected="turn_structured_output", model="auto"):
    return ProviderRequest(
        provider="codex-cli", model=model, agent_id="visionary",
        messages=[
            ProviderRequestMessage(role="system", content="You are the visionary."),
            ProviderRequestMessage(role="user", content="Should we adopt the protocol?"),
        ],
        expected_output_schema=expected,
    )


def test_registered_in_default_registry():
    assert default_registry().has("codex-cli")


def test_successful_turn_extracts_structured_output_and_usage():
    runner = _RecordingRunner([_completed(_codex_stdout(structured={"text": "My reframing."}))])
    result = CodexCliProvider(runner=runner).invoke(_req())
    assert result.error is None
    assert result.structured_output == {"text": "My reframing."}
    assert result.finish_reason == "stop"
    # prompt = input_tokens + cached_input_tokens = 120; completion = output + reasoning = 55
    assert result.usage.prompt_tokens == 120
    assert result.usage.completion_tokens == 55
    assert result.usage.cost_usd == 0.0
    assert result.usage.estimated is True  # codex reports no cost


def test_argv_translation_and_system_folded_into_prompt():
    runner = _RecordingRunner([_completed(_codex_stdout(structured={"text": "t"}))])
    CodexCliProvider(runner=runner).invoke(_req(model="gpt-5"))
    argv = runner.calls[0]["argv"]
    assert argv[:3] == ["codex", "exec", "--json"]
    assert "--output-schema" in argv  # schema passed as a file path
    assert "-m" in argv and argv[argv.index("-m") + 1] == "gpt-5"
    # The prompt is sent via stdin (positional "-") to avoid leaking it via `ps`.
    assert argv[-1] == "-"
    prompt = runner.calls[0]["input"]
    assert "[SYSTEM]" in prompt and "You are the visionary." in prompt
    assert "Should we adopt the protocol?" in prompt


def test_auto_model_omits_dash_m():
    runner = _RecordingRunner([_completed(_codex_stdout(structured={"text": "t"}))])
    CodexCliProvider(runner=runner).invoke(_req(model="auto"))
    assert "-m" not in runner.calls[0]["argv"]


def test_null_schema_free_text_path():
    runner = _RecordingRunner([_completed(_codex_stdout(text="free text"))])
    result = CodexCliProvider(runner=runner).invoke(_req(expected="null"))
    assert result.error is None
    assert result.structured_output is None
    assert result.messages[0].content == "free text"
    assert "--output-schema" not in runner.calls[0]["argv"]


def test_corrective_retry_then_success():
    runner = _RecordingRunner([
        _completed(_codex_stdout(text="not json at all")),
        _completed(_codex_stdout(structured={"text": "fixed"})),
    ])
    result = CodexCliProvider(runner=runner).invoke(_req())
    assert result.error is None
    assert result.structured_output == {"text": "fixed"}
    assert len(runner.calls) == 2
    assert "did not conform" in runner.calls[1]["input"]


def test_schema_invalid_after_retry_returns_malformed():
    bad = _completed(_codex_stdout(structured={"wrong": "x"}))
    result = CodexCliProvider(runner=_RecordingRunner([bad, bad])).invoke(_req())
    assert result.error is not None and result.error.kind == "malformed_response"


def test_nonzero_exit_is_error():
    runner = _RecordingRunner([_completed("", returncode=1, stderr="kaput")])
    result = CodexCliProvider(runner=runner).invoke(_req())
    assert result.error is not None and result.error.kind == "internal"
    assert "kaput" in result.error.message


def test_turn_failed_event_is_error():
    runner = _RecordingRunner([_completed(_codex_stdout(failed=True))])
    result = CodexCliProvider(runner=runner).invoke(_req())
    assert result.error is not None and result.error.kind == "internal"


def test_timeout_maps_to_timeout_error():
    runner = _RecordingRunner([subprocess.TimeoutExpired(cmd="codex", timeout=5)])
    result = CodexCliProvider(runner=runner).invoke(_req())
    assert result.error is not None and result.error.kind == "timeout"


def test_missing_binary_raises_at_construction():
    with pytest.raises(FileNotFoundError):
        CodexCliProvider(binary="codex-does-not-exist-xyz")


def test_end_to_end_run_session_with_codex_cli(tmp_path):
    from symposium.models import AgentConfig, BudgetConfig, Config, SelectorConfig
    from symposium.personas import COORDINATOR, persona_by_id
    from symposium.scheduler import run_session

    def _runner(argv, *, input="", capture_output=None, text=None, timeout=None):
        schema = json.loads(open(argv[argv.index("--output-schema") + 1]).read())
        props = schema.get("properties", {})
        if "integrated_answer" in props:
            structured = {"integrated_answer": "Adopt it.", "resolved_disagreements": [],
                          "unresolved_disagreements": [], "confidence": 0.9}
        elif "next_action" in props:
            structured = {"next_action": "finalize", "rationale": "ok", "confidence": 0.9,
                          "focus": "f", "next_agents": [], "resolved_disagreements": [],
                          "unresolved_disagreements": []}
        else:
            structured = {"text": "A turn."}
        return _completed(_codex_stdout(structured=structured))

    provider = CodexCliProvider(runner=_runner)
    panel = ["visionary", "logician"]
    config = Config(
        schema_version="1.0.0", session_id="codex-e2e", originator="t",
        problem_statement="Should we adopt the protocol?",
        selector=SelectorConfig(strategy="fixed", default_deliberation_panel=panel,
                                coordinator_agent="coordinator"),
        agents=[AgentConfig(id=p, persona_ref=persona_by_id(p), provider="codex-cli",
                            model="auto") for p in panel],
        coordinator=AgentConfig(id="coordinator", persona_ref=COORDINATOR,
                                provider="codex-cli", model="auto"),
        budget=BudgetConfig(max_total_tokens=100000, max_total_cost_usd=5.0,
                            max_rounds=4, max_wallclock_seconds=60),
    )
    artifact = run_session(config, {"default": provider}, runs_root=str(tmp_path))
    assert artifact.outcome.kind == "synthesis"

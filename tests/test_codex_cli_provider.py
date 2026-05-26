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

    def __call__(self, argv, *, input=None, capture_output=None, text=None, timeout=None, env=None):
        self.calls.append({"argv": argv, "input": input, "env": env})
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


def test_isolated_flags_added_by_default():
    """`--ignore-user-config` + `--ignore-rules` keep the child off the
    operator's interactive customizations.
    """
    runner = _RecordingRunner([_completed(_codex_stdout(structured={"text": "t"}))])
    CodexCliProvider(runner=runner).invoke(_req())
    argv = runner.calls[0]["argv"]
    assert "--ignore-user-config" in argv
    assert "--ignore-rules" in argv


def test_isolated_flags_can_be_disabled():
    runner = _RecordingRunner([_completed(_codex_stdout(structured={"text": "t"}))])
    CodexCliProvider(runner=runner, isolated=False).invoke(_req())
    argv = runner.calls[0]["argv"]
    assert "--ignore-user-config" not in argv
    assert "--ignore-rules" not in argv


def test_env_scrubs_inherited_claude_code_state(monkeypatch):
    """Same shared blocklist as the claude-cli provider — see
    `symposium.providers._cli_env` for rationale. Matters even for codex
    because the runtime may be hosted inside a Claude Code session, and
    inherited `CLAUDE_*` state has been observed to slow / break
    descendant CLI processes.
    """
    monkeypatch.setenv("CLAUDECODE", "1")
    monkeypatch.setenv("CLAUDE_CODE_EFFORT_LEVEL", "xhigh")
    monkeypatch.setenv("CLAUDE_EFFORT", "xhigh")
    monkeypatch.setenv("CLAUDE_CODE_PROVIDER_MANAGED_BY_HOST", "1")
    monkeypatch.setenv("AI_AGENT", "claude-code_agent")
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    monkeypatch.setenv("CODEX_HOME", "/x")  # codex auth, must survive

    runner = _RecordingRunner([_completed(_codex_stdout(structured={"text": "t"}))])
    CodexCliProvider(runner=runner).invoke(_req())

    env = runner.calls[0]["env"]
    assert env is not None
    for blocked in (
        "CLAUDECODE",
        "CLAUDE_CODE_EFFORT_LEVEL",
        "CLAUDE_EFFORT",
        "CLAUDE_CODE_PROVIDER_MANAGED_BY_HOST",
        "AI_AGENT",
    ):
        assert blocked not in env
    assert env.get("PATH") == "/usr/bin:/bin"
    assert env.get("CODEX_HOME") == "/x"
    # CLAUDE_CODE_DISABLE_* are set too: harmless for codex itself, but
    # they matter for any descendant `claude` invocation the child might
    # fork (same headless_child_env helper).
    assert env.get("CLAUDE_CODE_DISABLE_CLAUDE_MDS") == "1"
    assert env.get("CLAUDE_CODE_DISABLE_AUTO_MEMORY") == "1"


def test_env_override_replaces_scrubbed_default(monkeypatch):
    monkeypatch.setenv("CLAUDECODE", "1")
    runner = _RecordingRunner([_completed(_codex_stdout(structured={"text": "t"}))])
    custom = {"PATH": "/custom/bin", "CODEX_HOME": "/x"}
    CodexCliProvider(runner=runner, env=custom).invoke(_req())
    assert runner.calls[0]["env"] == custom


def test_corrective_retry_passes_env_and_does_not_duplicate_flags(monkeypatch):
    """§6.7 corrective retry: scrub applies to BOTH calls, and isolation
    flags must not double-up across the retry."""
    monkeypatch.setenv("CLAUDECODE", "1")
    runner = _RecordingRunner([
        _completed(_codex_stdout(text="not json at all")),
        _completed(_codex_stdout(structured={"text": "fixed"})),
    ])
    CodexCliProvider(runner=runner).invoke(_req())
    assert len(runner.calls) == 2
    for call in runner.calls:
        assert "CLAUDECODE" not in (call["env"] or {})
        assert call["argv"].count("--ignore-user-config") == 1
        assert call["argv"].count("--ignore-rules") == 1


def test_end_to_end_run_session_with_codex_cli(tmp_path):
    from symposium.models import AgentConfig, BudgetConfig, Config, SelectorConfig
    from symposium.personas import COORDINATOR, persona_by_id
    from symposium.scheduler import run_session

    def _runner(argv, *, input="", capture_output=None, text=None, timeout=None, env=None):
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

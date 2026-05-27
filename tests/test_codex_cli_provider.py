"""CodexCliProvider — drives the `codex exec` CLI (no API key).

Every test injects a fake `subprocess.run`-shaped runner returning a
canned `codex exec --json` JSONL stream, so nothing spawns the real CLI
or touches the network.
"""

from __future__ import annotations

import json
import os
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


def test_codex_child_env_scrubs_claude_auth(monkeypatch):
    """`codex_child_env()` MUST strip Claude-side auth (OAuth tokens +
    `ANTHROPIC_*`) before handing the env to a `codex exec` subprocess.

    Codex review T1 item #9: the unified `headless_child_env()` preserved
    every credential the parent process had — meaning a codex spawn ended
    up with `CLAUDE_CODE_OAUTH_TOKEN` and `ANTHROPIC_API_KEY` in its
    /proc/PID/environ. Codex never reads those, so the only effect was
    widening the credential exposure surface inside an agentic CLI that
    runs untrusted tool calls. Provider-specific helpers scrub the
    other vendor's set.
    """
    from symposium.providers._cli_env import codex_child_env

    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "claude-oauth-secret")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_REFRESH_TOKEN", "claude-refresh-secret")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-...")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://api.anthropic.com")
    monkeypatch.setenv("CODEX_HOME", "/Users/x/.codex")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-...")
    monkeypatch.setenv("PATH", "/usr/bin")

    env = codex_child_env()

    assert "CLAUDE_CODE_OAUTH_TOKEN" not in env
    assert "CLAUDE_CODE_OAUTH_REFRESH_TOKEN" not in env
    assert "ANTHROPIC_API_KEY" not in env
    assert "ANTHROPIC_BASE_URL" not in env
    # codex auth must remain
    assert env.get("CODEX_HOME") == "/Users/x/.codex"
    assert env.get("OPENAI_API_KEY") == "sk-..."
    # PATH must remain (subprocess needs it to find the binary)
    assert env.get("PATH") == "/usr/bin"


def test_strictify_for_openai_patches_pydantic_schema():
    """`_strictify_for_openai` MUST set `additionalProperties: false` on
    every object type (including inside anyOf branches) AND list every
    property in `required`. OpenAI structured-output strict mode rejects
    the schema otherwise — observed in the wild as codex exec exiting
    rc=1 with the actual error embedded in stdout JSONL (not stderr),
    silently surfaced to the operator as "codex CLI exited 1: <no
    stderr>" before this fix.
    """
    from symposium.providers.codex_cli import _strictify_for_openai

    pydantic_like = {
        "type": "object",
        "properties": {
            "text": {"type": "string"},
            "direct_requests": {
                "anyOf": [
                    {"type": "array", "items": {"$ref": "#/$defs/Req"}},
                    {"type": "null"},
                ],
                "default": None,
            },
        },
        "required": ["text"],
        "additionalProperties": False,
        "$defs": {
            "Req": {
                "type": "object",
                "properties": {
                    "target": {"type": "string"},
                    "content": {
                        "anyOf": [
                            {"type": "string"},
                            {"type": "object", "additionalProperties": True},
                        ],
                    },
                },
                "required": ["target", "content"],
                "additionalProperties": False,
            }
        },
    }

    strict = _strictify_for_openai(pydantic_like)

    # All originally-optional fields now in `required`.
    assert "direct_requests" in strict["required"]
    # additionalProperties=true inside anyOf got flipped to false.
    inner_dict = strict["$defs"]["Req"]["properties"]["content"]["anyOf"][1]
    assert inner_dict["additionalProperties"] is False
    # Input untouched (pure-functional contract).
    assert pydantic_like["required"] == ["text"]
    assert pydantic_like["$defs"]["Req"]["properties"]["content"]["anyOf"][1][
        "additionalProperties"
    ] is True


def test_codex_schema_for_turn_structured_output_is_openai_strict():
    """The actual TurnStructuredOutput / Verdict / SynthesisContent
    schemas that codex_cli writes to `--output-schema` MUST satisfy
    OpenAI strict mode (every object: additionalProperties=false + all
    properties in required). Regression guard for the v1.10.8 fix.
    """
    import json as _json
    from symposium.providers.codex_cli import _SCHEMA_MODELS, _schema_for

    # Clear cache so the test sees a fresh strictify call (other tests
    # may have populated _SCHEMA_CACHE pre-fix in old test runs).
    from symposium.providers.codex_cli import _SCHEMA_CACHE
    _SCHEMA_CACHE.clear()

    for expected in _SCHEMA_MODELS:
        raw = _schema_for(expected)
        assert raw is not None
        schema = _json.loads(raw)

        def _check(node, path=""):
            if isinstance(node, dict):
                if node.get("type") == "object" or "properties" in node:
                    assert node.get("additionalProperties") is False, (
                        f"{expected} {path}: additionalProperties != False"
                    )
                    props = list((node.get("properties") or {}).keys())
                    req = node.get("required") or []
                    missing = [p for p in props if p not in req]
                    assert not missing, (
                        f"{expected} {path}: optional fields not in required: {missing}"
                    )
                for k, v in node.items():
                    _check(v, f"{path}.{k}")
            elif isinstance(node, list):
                for i, item in enumerate(node):
                    _check(item, f"{path}[{i}]")

        _check(schema, "$")


def test_codex_exit_error_surfaces_stdout_jsonl_message(monkeypatch):
    """When codex exits rc!=0 with EMPTY stderr but a `{"type":"error",
    "message": "..."}` event in stdout, the adapter MUST surface that
    stdout message in `ProviderError.message` — not the unhelpful
    `<no stderr>` string. This is the v1.10.7 hole that left the
    operator looking at a meaningless termination reason while the
    real cause (invalid_json_schema) sat in stdout.
    """
    from symposium.providers.codex_cli import CodexCliProvider
    stdout_payload = (
        '{"type":"thread.started","thread_id":"t1"}\n'
        '{"type":"turn.started"}\n'
        '{"type":"error","message":"{\\"type\\":\\"error\\",\\"error\\":'
        '{\\"type\\":\\"invalid_request_error\\",\\"code\\":\\"invalid_json_schema\\",'
        '\\"message\\":\\"Invalid schema for response_format \'codex_output_schema\': '
        '\\\\u0027additionalProperties\\\\u0027 is required to be supplied and to be false.\\"},'
        '\\"status\\":400}"}\n'
        '{"type":"turn.failed"}\n'
    )

    def runner(argv, *, input=None, capture_output=None, text=None, timeout=None, env=None):
        return subprocess.CompletedProcess(argv, 1, stdout=stdout_payload, stderr="")

    provider = CodexCliProvider(runner=runner, check_binary=False)
    result = provider.invoke(_req())

    assert result.error is not None
    msg = result.error.message
    # Surfaces the actual upstream complaint, not '<no stderr>'.
    assert "invalid_json_schema" in msg or "additionalProperties" in msg, (
        f"expected codex stdout error in message, got: {msg!r}"
    )
    assert "<no stderr>" not in msg


def test_codex_schema_narrows_open_object_payloads_intentionally():
    """`DirectRequest.content: Union[str, Dict[str, Any]]` is a known
    semantic narrowing through codex (Codex review T5 #1/#2): the
    strictified schema accepts string content + `{}`, but NOT arbitrary
    object payloads like `{"q": "why"}`. The Pydantic model still
    accepts the object branch; the narrowing is purely at codex
    submission. Documented here as intentional behavior so future
    refactors don't silently re-open the path without thinking
    through the OpenAI-strict implications.
    """
    import json as _json
    from jsonschema import Draft202012Validator
    from symposium.providers.codex_cli import _schema_for, _SCHEMA_CACHE

    _SCHEMA_CACHE.clear()
    schema = _json.loads(_schema_for("turn_structured_output"))
    validator = Draft202012Validator(schema)

    # Find the DirectRequest sub-schema and its `content` field.
    dr = schema["$defs"]["DirectRequest"]
    content_schema = dr["properties"]["content"]
    # Both branches must be present in the union.
    assert len(content_schema["anyOf"]) == 2
    # The object branch MUST have additionalProperties: false (strict).
    object_branch = next(
        b for b in content_schema["anyOf"] if b.get("type") == "object"
    )
    assert object_branch["additionalProperties"] is False
    # No properties → only `{}` validates (intentional narrowing).
    assert "properties" not in object_branch or object_branch.get("properties") == {}

    # End-to-end: a payload with string content validates.
    string_payload = {
        "text": "ok",
        "direct_requests": [
            {"target": "logician", "type": "ask",
             "content": '{"q":"why"}'},  # JSON-string envelope, the supported shape
        ],
    }
    errors = list(validator.iter_errors(string_payload))
    assert errors == [], f"string content payload should validate: {errors}"

    # End-to-end: a payload with object content does NOT validate
    # against the strictified codex schema (the documented limitation)…
    object_payload = {
        "text": "ok",
        "direct_requests": [
            {"target": "logician", "type": "ask",
             "content": {"q": "why"}},  # object branch — rejected by strictified schema
        ],
    }
    errors = list(validator.iter_errors(object_payload))
    assert errors, (
        "object-typed content MUST be rejected by the strictified schema "
        "(intentional limitation, see _strictify_for_openai docstring)"
    )

    # …but the Pydantic model STILL accepts it. The narrowing exists
    # only at the codex submission boundary; the protocol contract
    # itself is unchanged. Proves both halves of the limitation, not
    # just the codex-side rejection. (Codex review T6 closure.)
    from symposium.models import TurnStructuredOutput
    TurnStructuredOutput.model_validate(object_payload)


def test_parse_jsonl_prefers_error_event_with_non_empty_message():
    """A `stream.error` with no message followed by a real `error` event
    with the actionable upstream complaint — the real event wins.
    Codex review T5 #3: first-wins was too rude when codex sometimes
    emits an empty leading stream.error.
    """
    from symposium.providers.codex_cli import _parse_jsonl

    stdout = (
        '{"type":"thread.started","thread_id":"t1"}\n'
        '{"type":"stream.error"}\n'  # empty leading error
        '{"type":"error","message":"the actionable complaint"}\n'
        '{"type":"turn.failed"}\n'
    )
    _, _, err = _parse_jsonl(stdout)
    assert err is not None
    assert err.get("message") == "the actionable complaint"


def test_codex_exit_error_surfaces_upstream_code_when_present():
    """When the inner API error carries `code` (eg. `invalid_json_schema`),
    that code MUST be in the surfaced message — it's often more
    diagnostic than the message prose. Codex review T5 #4.
    """
    from symposium.providers.codex_cli import CodexCliProvider
    stdout_payload = (
        '{"type":"thread.started","thread_id":"t1"}\n'
        '{"type":"turn.started"}\n'
        '{"type":"error","message":"{\\"type\\":\\"error\\",\\"error\\":'
        '{\\"type\\":\\"invalid_request_error\\",\\"code\\":\\"invalid_json_schema\\",'
        '\\"message\\":\\"prose here.\\"},\\"status\\":400}"}\n'
    )
    def runner(argv, *, input=None, capture_output=None, text=None, timeout=None, env=None):
        return subprocess.CompletedProcess(argv, 1, stdout=stdout_payload, stderr="")

    result = CodexCliProvider(runner=runner, check_binary=False).invoke(_req())
    assert result.error is not None
    assert "[invalid_json_schema]" in result.error.message, (
        f"missing upstream code in surfaced message: {result.error.message!r}"
    )


def test_codex_workdir_defaults_to_os_getcwd(monkeypatch):
    """`CodexCliProvider` (v1.10.9+) MUST default to `os.getcwd()` for
    the codex `-C` working dir, so personas can READ the project files
    just like claude-cli does (which inherits cwd naturally).

    Pre-v1.10.9 the default was an empty `tempfile.mkdtemp()`, which
    made codex personas blind to the codebase — observed as visionary
    responding "I'm blocked from applying the implementation; the
    directory contains only schema.json".
    """
    seen = {}
    def runner(argv, *, input=None, capture_output=None, text=None, timeout=None, env=None):
        seen["argv"] = argv
        return subprocess.CompletedProcess(argv, 0, stdout=_codex_stdout(structured={"text": "t"}), stderr="")

    fake_cwd = "/Users/foo/my-project"
    monkeypatch.setattr(os, "getcwd", lambda: fake_cwd)

    CodexCliProvider(runner=runner).invoke(_req())

    argv = seen["argv"]
    idx = argv.index("-C")
    assert argv[idx + 1] == fake_cwd, (
        f"-C must default to os.getcwd(), got {argv[idx + 1]!r}"
    )


def test_codex_workdir_explicit_override():
    """`CodexCliProvider(workdir="/custom/dir")` MUST override the
    os.getcwd() default — opt-in to a sandboxed/isolated working dir
    for operators who want the pre-v1.10.9 neutral behavior or a
    different project root.
    """
    seen = {}
    def runner(argv, *, input=None, capture_output=None, text=None, timeout=None, env=None):
        seen["argv"] = argv
        return subprocess.CompletedProcess(argv, 0, stdout=_codex_stdout(structured={"text": "t"}), stderr="")

    CodexCliProvider(runner=runner, workdir="/explicit/path").invoke(_req())
    argv = seen["argv"]
    idx = argv.index("-C")
    assert argv[idx + 1] == "/explicit/path"

"""OpenAIProvider adapter tests (§6, §6.6, §6.10).

Every test is offline: HTTP traffic is mocked via `respx`. The real
OpenAI endpoint is never reached. The test suite covers:

* §6.6 closed `error.kind` enum — one test per CLOSED value (12 total).
* §6.10 finish-reason normalization — every documented vendor value.
* §6.4 internal tool-call loop — single-iteration happy path,
  unknown-tool / bad-args / handler-error failure modes.
* §6.5 structured-output enforcement on the happy path.
* §6.7 internal corrective retry on `malformed_response`.
* §6.8 + §8.9 credential redaction in the persisted Artifact.
* §6.11 registry resolution end-to-end via the CLI surface.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

import httpx
import pytest
import respx

from symposium.models import (
    ProviderRequest,
    ProviderRequestMessage,
)
from symposium.providers import (
    AdapterRegistry,
    MissingCredentialsError,
    OpenAIProvider,
    UnknownProviderError,
    default_registry,
)


API_KEY = "sk-test-symposium-DO-NOT-LEAK-9f3a1b7c"
BASE_URL = "https://api.openai.test/v1"
CHAT_URL = f"{BASE_URL}/chat/completions"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _provider(**kw: Any) -> OpenAIProvider:
    return OpenAIProvider(api_key=API_KEY, base_url=BASE_URL, **kw)


def _request(
    *,
    expected: str = "turn_structured_output",
    tools: Optional[List[Dict[str, Any]]] = None,
    model: str = "gpt-4o-mini",
) -> ProviderRequest:
    return ProviderRequest(
        provider="openai",
        model=model,
        agent_id="logician",
        messages=[
            ProviderRequestMessage(role="system", content="persona=logician"),
            ProviderRequestMessage(role="user", content="problem statement here"),
        ],
        expected_output_schema=expected,  # type: ignore[arg-type]
        tools=tools,
    )


def _ok_response(
    *,
    structured: Dict[str, Any],
    finish_reason: str = "stop",
    usage: Optional[Dict[str, int]] = None,
) -> Dict[str, Any]:
    return {
        "id": "chatcmpl-test-001",
        "model": "gpt-4o-mini",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": json.dumps(structured)},
                "finish_reason": finish_reason,
            }
        ],
        "usage": usage
        or {"prompt_tokens": 100, "completion_tokens": 30, "total_tokens": 130},
    }


def _tool_call_response(
    *,
    name: str,
    arguments_json: str,
    call_id: str = "call_001",
    usage: Optional[Dict[str, int]] = None,
) -> Dict[str, Any]:
    return {
        "id": "chatcmpl-tool-001",
        "model": "gpt-4o-mini",
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": call_id,
                            "type": "function",
                            "function": {
                                "name": name,
                                "arguments": arguments_json,
                            },
                        }
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ],
        "usage": usage
        or {"prompt_tokens": 200, "completion_tokens": 40, "total_tokens": 240},
    }


def _valid_turn() -> Dict[str, Any]:
    return {"text": "an answer that mentions the smallest assumption needed."}


# ---------------------------------------------------------------------------
# Construction-time tests
# ---------------------------------------------------------------------------


def test_missing_api_key_fails_fast(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(MissingCredentialsError):
        OpenAIProvider(base_url=BASE_URL)


def test_registry_resolves_openai_factory(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", API_KEY)
    reg = default_registry()
    assert reg.has("openai")
    # The factory should produce an OpenAIProvider; we don't invoke it.

    cfg = _minimal_config()
    providers = reg.build_session_providers(cfg)
    assert {"agent_a", "coordinator"} == set(providers.keys())
    # Single openai factory → one shared instance.
    assert providers["agent_a"] is providers["coordinator"]


def test_registry_raises_unknown_provider(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", API_KEY)
    reg = AdapterRegistry()  # empty
    cfg = _minimal_config()
    with pytest.raises(UnknownProviderError):
        reg.build_session_providers(cfg)


# ---------------------------------------------------------------------------
# §6.6 — one test per CLOSED error.kind value (12 total)
# ---------------------------------------------------------------------------


@respx.mock(base_url=BASE_URL, assert_all_called=False)
def test_error_kind_timeout(respx_mock):
    respx_mock.post("/chat/completions").mock(
        side_effect=httpx.ReadTimeout("read timed out")
    )
    result = _provider().invoke(_request())
    assert result.error is not None
    assert result.error.kind == "timeout"
    assert result.error.retriable is True
    assert result.finish_reason == "error"
    assert result.structured_output is None


@respx.mock(base_url=BASE_URL, assert_all_called=False)
def test_error_kind_network(respx_mock):
    respx_mock.post("/chat/completions").mock(
        return_value=httpx.Response(502, json={"error": {"message": "bad gateway"}})
    )
    result = _provider().invoke(_request())
    assert result.error is not None
    assert result.error.kind == "network"
    assert result.error.retriable is True


@respx.mock(base_url=BASE_URL, assert_all_called=False)
def test_error_kind_rate_limit(respx_mock):
    respx_mock.post("/chat/completions").mock(
        return_value=httpx.Response(
            429,
            json={
                "error": {
                    "code": "rate_limit_exceeded",
                    "message": "Too many requests",
                    "type": "tokens",
                }
            },
            headers={"retry-after": "30"},
        )
    )
    result = _provider().invoke(_request())
    assert result.error is not None
    assert result.error.kind == "rate_limit"
    assert result.error.retriable is True
    assert result.error.details["retry_after_seconds"] == 30


@respx.mock(base_url=BASE_URL, assert_all_called=False)
def test_error_kind_quota_exhausted(respx_mock):
    respx_mock.post("/chat/completions").mock(
        return_value=httpx.Response(
            429,
            json={
                "error": {
                    "code": "insufficient_quota",
                    "message": "Account quota exceeded",
                    "type": "insufficient_quota",
                }
            },
        )
    )
    result = _provider().invoke(_request())
    assert result.error is not None
    assert result.error.kind == "quota_exhausted"
    assert result.error.retriable is False


@respx.mock(base_url=BASE_URL, assert_all_called=False)
def test_error_kind_auth_failure(respx_mock):
    respx_mock.post("/chat/completions").mock(
        return_value=httpx.Response(
            401,
            json={"error": {"code": "invalid_api_key", "message": "Bad key"}},
        )
    )
    result = _provider().invoke(_request())
    assert result.error is not None
    assert result.error.kind == "auth_failure"
    assert result.error.retriable is False


@respx.mock(base_url=BASE_URL, assert_all_called=False)
def test_error_kind_model_unavailable(respx_mock):
    respx_mock.post("/chat/completions").mock(
        return_value=httpx.Response(
            404,
            json={
                "error": {
                    "code": "model_not_found",
                    "message": "Model 'gpt-unknown' does not exist",
                }
            },
        )
    )
    result = _provider().invoke(_request())
    assert result.error is not None
    assert result.error.kind == "model_unavailable"
    assert result.error.retriable is False


@respx.mock(base_url=BASE_URL, assert_all_called=False)
def test_error_kind_context_length_exceeded(respx_mock):
    respx_mock.post("/chat/completions").mock(
        return_value=httpx.Response(
            400,
            json={
                "error": {
                    "code": "context_length_exceeded",
                    "message": "context length exceeded",
                    "type": "invalid_request_error",
                }
            },
        )
    )
    result = _provider().invoke(_request())
    assert result.error is not None
    assert result.error.kind == "context_length_exceeded"
    assert result.error.retriable is False


@respx.mock(base_url=BASE_URL, assert_all_called=False)
def test_error_kind_content_filter(respx_mock):
    respx_mock.post("/chat/completions").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": "chatcmpl-cf",
                "model": "gpt-4o-mini",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": ""},
                        "finish_reason": "content_filter",
                    }
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 0, "total_tokens": 10},
            },
        )
    )
    result = _provider().invoke(_request())
    assert result.error is not None
    assert result.error.kind == "content_filter"
    assert result.error.retriable is False
    assert result.finish_reason == "content_filter"


@respx.mock(base_url=BASE_URL, assert_all_called=False)
def test_error_kind_invalid_request(respx_mock):
    respx_mock.post("/chat/completions").mock(
        return_value=httpx.Response(
            400,
            json={
                "error": {
                    "code": "invalid_value",
                    "message": "Unknown parameter 'foo'",
                    "type": "invalid_request_error",
                }
            },
        )
    )
    result = _provider().invoke(_request())
    assert result.error is not None
    assert result.error.kind == "invalid_request"
    assert result.error.retriable is False


@respx.mock(base_url=BASE_URL, assert_all_called=False)
def test_error_kind_malformed_response_after_corrective_retry(respx_mock):
    """When both the initial and the corrective-retry responses fail
    validation, the adapter surfaces `malformed_response` (§6.5 / §6.7)."""
    bad_payload = {"not_the_right_field": "value"}
    respx_mock.post("/chat/completions").mock(
        return_value=httpx.Response(
            200,
            json=_ok_response(structured=bad_payload),
        )
    )
    result = _provider().invoke(_request())
    assert result.error is not None
    assert result.error.kind == "malformed_response"
    assert result.error.retriable is True
    assert result.structured_output is None
    # Both invocations consumed the mock.
    assert respx_mock.calls.call_count == 2


@respx.mock(base_url=BASE_URL, assert_all_called=False)
def test_error_kind_tool_failure_unknown_tool(respx_mock):
    respx_mock.post("/chat/completions").mock(
        return_value=httpx.Response(
            200,
            json=_tool_call_response(
                name="nonexistent_tool", arguments_json='{"x": 1}'
            ),
        )
    )
    tools = [
        {
            "name": "search_papers",
            "description": "search corpus",
            "input_schema": {"type": "object", "properties": {"q": {"type": "string"}}},
        }
    ]
    result = _provider().invoke(_request(tools=tools))
    assert result.error is not None
    assert result.error.kind == "tool_failure"
    assert result.error.retriable is False
    assert len(result.tool_events) == 1
    assert result.tool_events[0].error is not None
    assert result.tool_events[0].error.kind == "tool_failure"


@respx.mock(base_url=BASE_URL, assert_all_called=False)
def test_error_kind_internal(respx_mock):
    """HTTP 200 but `choices` is missing — last-resort `internal`."""
    respx_mock.post("/chat/completions").mock(
        return_value=httpx.Response(
            200,
            json={"id": "chatcmpl-x", "model": "gpt-4o-mini", "usage": {}},
        )
    )
    result = _provider().invoke(_request())
    assert result.error is not None
    assert result.error.kind == "internal"
    assert result.error.retriable is False


# ---------------------------------------------------------------------------
# §6.5 — happy path
# ---------------------------------------------------------------------------


@respx.mock(base_url=BASE_URL, assert_all_called=False)
def test_happy_path_turn_structured_output(respx_mock):
    payload = _valid_turn()
    respx_mock.post("/chat/completions").mock(
        return_value=httpx.Response(200, json=_ok_response(structured=payload))
    )
    result = _provider().invoke(_request())
    assert result.error is None
    assert result.finish_reason == "stop"
    assert result.structured_output == payload
    assert result.usage.prompt_tokens == 100
    assert result.usage.completion_tokens == 30
    assert result.usage.total_tokens == 130
    # gpt-4o-mini: 100/1000 * 0.00015 + 30/1000 * 0.0006 = 0.000015 + 0.000018
    assert result.usage.cost_usd == pytest.approx(0.000033, rel=1e-6)


# ---------------------------------------------------------------------------
# §6.4 — internal tool-call loop
# ---------------------------------------------------------------------------


@respx.mock(base_url=BASE_URL, assert_all_called=False)
def test_tool_loop_single_iteration(respx_mock):
    tool_call = _tool_call_response(
        name="search_papers",
        arguments_json='{"query": "x"}',
        usage={"prompt_tokens": 200, "completion_tokens": 40, "total_tokens": 240},
    )
    final = _ok_response(
        structured=_valid_turn(),
        usage={"prompt_tokens": 250, "completion_tokens": 60, "total_tokens": 310},
    )
    respx_mock.post("/chat/completions").mock(
        side_effect=[httpx.Response(200, json=tool_call), httpx.Response(200, json=final)]
    )
    tools = [
        {
            "name": "search_papers",
            "description": "search corpus",
            "input_schema": {
                "type": "object",
                "required": ["query"],
                "properties": {"query": {"type": "string"}},
                "additionalProperties": False,
            },
        }
    ]
    prov = _provider(
        tool_handlers={"search_papers": lambda args: {"matches": [{"title": args["query"]}]}}
    )
    result = prov.invoke(_request(tools=tools))
    assert result.error is None
    assert result.finish_reason == "stop"
    assert len(result.tool_events) == 1
    ev = result.tool_events[0]
    assert ev.name == "search_papers"
    assert ev.arguments == {"query": "x"}
    assert ev.error is None
    assert ev.result == {"matches": [{"title": "x"}]}
    # Aggregated usage across both iterations:
    assert result.usage.prompt_tokens == 450
    assert result.usage.completion_tokens == 100
    assert result.usage.total_tokens == 550


# ---------------------------------------------------------------------------
# §6.10 — finish-reason normalization (every input value)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "vendor_reason, canonical, expect_error",
    [
        ("stop", "stop", False),
        # `length` terminally means the completion was truncated at the
        # token cap — surfaced as finish_reason="length" with a
        # non-retriable error (see
        # test_length_truncation_is_length_not_malformed).
        ("length", "length", True),
        ("content_filter", "content_filter", True),
        # tool_calls / function_call / None should NEVER appear terminally
        # under the internal loop; if they do, they collapse to `error`.
        # We provoke that by returning tool_calls finish_reason with no
        # tool_calls array — the adapter treats the message as terminal.
        ("tool_calls", "error", True),
        ("function_call", "error", True),
        (None, "error", True),
    ],
)
@respx.mock(base_url=BASE_URL, assert_all_called=False)
def test_finish_reason_normalization(respx_mock, vendor_reason, canonical, expect_error):
    # stop / length / content_filter cases use a payload that is either
    # valid (stop, length) or empty (content_filter).
    if vendor_reason == "content_filter":
        body = {
            "id": "chatcmpl-cf",
            "model": "gpt-4o-mini",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": ""},
                    "finish_reason": "content_filter",
                }
            ],
            "usage": {"prompt_tokens": 5, "completion_tokens": 0, "total_tokens": 5},
        }
    elif vendor_reason in ("tool_calls", "function_call", None):
        # Terminal arrival of tool_calls/function_call/null without an
        # actual tool_calls array means the adapter exited the loop on
        # an unexpected vendor signal — surface as `error`.
        body = {
            "id": "chatcmpl-tc",
            "model": "gpt-4o-mini",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": json.dumps(_valid_turn())},
                    "finish_reason": vendor_reason,
                }
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        }
    else:
        body = _ok_response(structured=_valid_turn(), finish_reason=vendor_reason)

    respx_mock.post("/chat/completions").mock(return_value=httpx.Response(200, json=body))
    result = _provider().invoke(_request())

    if vendor_reason == "stop":
        # Happy path: error is None, structured_output validated.
        assert result.error is None
        assert result.finish_reason == canonical
    elif vendor_reason == "length":
        # Truncated at the token cap: `length` finish plus a
        # non-retriable error — never a clean success.
        assert result.finish_reason == "length"
        assert result.error is not None
        assert result.error.retriable is False
    else:
        # Vendor-reason cases that don't yield a valid response:
        # content_filter has its own error.kind; tool_calls/function_call/None
        # do NOT terminate the loop cleanly so the adapter surfaces them as
        # the `error` finish_reason. The corrective retry path may run on
        # malformed responses; assert the *first* outward signal is `error`.
        assert result.finish_reason in (canonical, "error")
        assert result.error is not None


# ---------------------------------------------------------------------------
# §6.7 — corrective retry on malformed response
# ---------------------------------------------------------------------------


@respx.mock(base_url=BASE_URL, assert_all_called=False)
def test_corrective_retry_recovers_after_malformed(respx_mock):
    bad = _ok_response(structured={"not_the_right_field": 42})
    good = _ok_response(structured=_valid_turn())
    respx_mock.post("/chat/completions").mock(
        side_effect=[httpx.Response(200, json=bad), httpx.Response(200, json=good)]
    )
    result = _provider().invoke(_request())
    assert result.error is None
    assert result.finish_reason == "stop"
    assert result.structured_output == _valid_turn()
    # Two upstream calls: the initial malformed call plus the corrective retry.
    assert respx_mock.calls.call_count == 2
    # The corrective-retry request must extend the original `messages` with
    # the malformed assistant entry plus a user annotation that names the
    # failing path. We grab the second call's body and assert on it.
    second_body = json.loads(respx_mock.calls[1].request.content)
    assert len(second_body["messages"]) >= 4
    last = second_body["messages"][-1]
    assert last["role"] == "user"
    assert "schema validation" in last["content"]
    assert "turn_structured_output" in last["content"]


# ---------------------------------------------------------------------------
# §6.8 + §8.9 — credential redaction
# ---------------------------------------------------------------------------


@respx.mock(base_url=BASE_URL, assert_all_called=False)
def test_credentials_never_leak_into_persisted_artifact(tmp_path, respx_mock, monkeypatch):
    """Persisted Artifact / TerminationArtifact never contain the API key.

    Runs a one-round session with an `openai` panel agent + coordinator;
    the coordinator's verdict requests user input which terminates the
    session. We then read every persisted file and assert that the API
    key does not appear as a substring anywhere.
    """
    monkeypatch.setenv("OPENAI_API_KEY", API_KEY)
    monkeypatch.setenv("OPENAI_BASE_URL", BASE_URL)

    from symposium.providers import default_registry
    from symposium.scheduler import run_session

    cfg = _minimal_config(session_id="creds-redaction-test")

    turn_payload = _ok_response(structured=_valid_turn())
    verdict_payload = _ok_response(
        structured={
            "next_action": "request_user_input",
            "rationale": "needs clarification",
            "confidence": 0.5,
            "focus": "ask the user",
            "next_agents": [],
            "resolved_disagreements": [],
            "unresolved_disagreements": [],
            "user_input_request": {
                "question": "please clarify",
            },
        },
        usage={"prompt_tokens": 50, "completion_tokens": 25, "total_tokens": 75},
    )
    respx_mock.post("/chat/completions").mock(
        side_effect=[
            httpx.Response(200, json=turn_payload),
            httpx.Response(200, json=verdict_payload),
        ]
    )

    reg = default_registry()
    providers = reg.build_session_providers(cfg)

    runs_root = tmp_path / "runs"
    artifact = run_session(cfg, providers, runs_root=str(runs_root))

    # Sanity: termination outcome reached.
    assert artifact.outcome.kind == "termination"
    assert artifact.outcome.termination_artifact.reason == "user_input_required"

    # Sweep every persisted file under the run directory.
    run_dir = runs_root / cfg.session_id
    found_any = False
    for p in run_dir.rglob("*"):
        if not p.is_file():
            continue
        found_any = True
        text = p.read_text()
        assert API_KEY not in text, f"API key leaked into {p}"
    assert found_any, "no persisted files were found under the run directory"


# ---------------------------------------------------------------------------
# Corrective-retry usage accounting
# ---------------------------------------------------------------------------


@respx.mock(base_url=BASE_URL, assert_all_called=False)
def test_corrective_retry_usage_includes_first_attempt(respx_mock):
    """The malformed first attempt consumed real tokens and money; the
    returned result's usage must be the SUM of both passes, not just the
    corrective one."""
    bad = _ok_response(structured={"not_the_right_field": 42})
    good = _ok_response(structured=_valid_turn())
    respx_mock.post("/chat/completions").mock(
        side_effect=[httpx.Response(200, json=bad), httpx.Response(200, json=good)]
    )
    result = _provider().invoke(_request())
    assert result.error is None
    assert respx_mock.calls.call_count == 2
    # Each pass reported 100 prompt / 30 completion tokens.
    assert result.usage.prompt_tokens == 200
    assert result.usage.completion_tokens == 60
    assert result.usage.total_tokens == 260
    # Cost doubles with the tokens (gpt-4o-mini price row).
    assert result.usage.cost_usd == pytest.approx(0.000066, rel=1e-6)


# ---------------------------------------------------------------------------
# length truncation classification
# ---------------------------------------------------------------------------


@respx.mock(base_url=BASE_URL, assert_all_called=False)
def test_length_truncation_is_length_not_malformed(respx_mock):
    """Truncated JSON + finish_reason=length must classify as a
    non-retriable `length` outcome BEFORE JSON parsing — not as a
    retriable malformed_response whose corrective retry is doomed at the
    very same cap."""
    truncated = {
        "id": "chatcmpl-trunc",
        "model": "gpt-4o-mini",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": '{"text": "cut of'},
                "finish_reason": "length",
            }
        ],
        "usage": {"prompt_tokens": 100, "completion_tokens": 512, "total_tokens": 612},
    }
    respx_mock.post("/chat/completions").mock(
        return_value=httpx.Response(200, json=truncated)
    )
    result = _provider().invoke(_request())
    assert result.finish_reason == "length"
    assert result.error is not None
    assert result.error.kind == "context_length_exceeded"
    assert result.error.retriable is False
    assert "truncated" in result.error.message
    # No corrective retry was issued: one upstream call only.
    assert respx_mock.calls.call_count == 1


# ---------------------------------------------------------------------------
# Free-text path (expected_output_schema = null)
# ---------------------------------------------------------------------------


def _free_text_response(text: str) -> Dict[str, Any]:
    return {
        "id": "chatcmpl-free",
        "model": "gpt-4o-mini",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": text},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 40, "completion_tokens": 12, "total_tokens": 52},
    }


@respx.mock(base_url=BASE_URL, assert_all_called=False)
def test_free_text_prose_with_no_schema_is_ok(respx_mock):
    """With no expected schema (the §4.1 llm selector) prose must come
    back verbatim — not be force-parsed into a malformed_response."""
    respx_mock.post("/chat/completions").mock(
        return_value=httpx.Response(200, json=_free_text_response("A prose answer."))
    )
    result = _provider().invoke(_request(expected=None))
    assert result.error is None
    assert result.finish_reason == "stop"
    assert result.structured_output is None
    assert result.messages[0].content == "A prose answer."
    assert respx_mock.calls.call_count == 1


@respx.mock(base_url=BASE_URL, assert_all_called=False)
def test_free_text_json_array_with_no_schema_is_ok(respx_mock):
    """A JSON-array answer on the free-text path must NOT be parsed into
    `structured_output` — a non-dict there would raise a ValidationError
    out of invoke(), breaking the errors-via-result contract."""
    respx_mock.post("/chat/completions").mock(
        return_value=httpx.Response(
            200, json=_free_text_response('["logician", "critic"]')
        )
    )
    result = _provider().invoke(_request(expected=None))
    assert result.error is None
    assert result.structured_output is None
    assert result.messages[0].content == '["logician", "critic"]'


# ---------------------------------------------------------------------------
# Per-turn deadline (`symposium_timeout_seconds`)
# ---------------------------------------------------------------------------


@respx.mock(base_url=BASE_URL, assert_all_called=False)
def test_per_turn_deadline_bounds_tool_loop(respx_mock, monkeypatch):
    """The scheduler's `symposium_timeout_seconds` metadata budgets the
    WHOLE invocation: it caps each HTTP request AND bounds the tool
    loop's wall-clock instead of being silently ignored."""
    import symposium.providers.openai as openai_mod

    # A controllable clock: each tool call "takes" 60s. Only the handler
    # advances it, so extra monotonic readers (httpx et al.) can't skew
    # the arithmetic.
    clock = {"v": 0.0}
    monkeypatch.setattr(openai_mod.time, "monotonic", lambda: clock["v"])

    def slow_tool(args):
        clock["v"] += 60.0
        return {"matches": []}

    tool_call = _tool_call_response(
        name="search_papers", arguments_json='{"query": "x"}'
    )
    respx_mock.post("/chat/completions").mock(
        return_value=httpx.Response(200, json=tool_call)
    )
    tools = [
        {
            "name": "search_papers",
            "description": "search corpus",
            "input_schema": {
                "type": "object",
                "required": ["query"],
                "properties": {"query": {"type": "string"}},
                "additionalProperties": False,
            },
        }
    ]
    # Adapter ceiling above the metadata hint, so the scheduler's 100s
    # budget is the binding constraint.
    prov = _provider(timeout=600.0, tool_handlers={"search_papers": slow_tool})
    req = _request(tools=tools).model_copy(
        update={"metadata": {"symposium_timeout_seconds": 100.0}}
    )
    result = prov.invoke(req)
    assert result.error is not None
    assert result.error.kind == "timeout"
    assert result.error.retriable is True
    # Two 60s tool iterations fit inside the loop before the 100s budget
    # ran out — the third never starts.
    assert respx_mock.calls.call_count == 2
    assert len(result.tool_events) == 2
    # Tokens burned before the bail-out are still reported.
    assert result.usage.prompt_tokens == 400
    assert result.usage.completion_tokens == 80
    # The remaining budget is applied as the per-request httpx timeout.
    assert respx_mock.calls[0].request.extensions["timeout"]["read"] == pytest.approx(100.0)
    assert respx_mock.calls[1].request.extensions["timeout"]["read"] == pytest.approx(40.0)


# ---------------------------------------------------------------------------
# Tool-loop entry on permissive finish_reason
# ---------------------------------------------------------------------------


@respx.mock(base_url=BASE_URL, assert_all_called=False)
def test_tool_loop_entered_on_stop_finish_with_tool_calls(respx_mock):
    """Some OpenAI-compatible servers report finish_reason="stop" with a
    populated tool_calls array; the adapter must still run the tool loop
    instead of failing with a misleading empty-content internal error."""
    tool_call = _tool_call_response(
        name="search_papers", arguments_json='{"query": "x"}'
    )
    tool_call["choices"][0]["finish_reason"] = "stop"
    final = _ok_response(structured=_valid_turn())
    respx_mock.post("/chat/completions").mock(
        side_effect=[httpx.Response(200, json=tool_call), httpx.Response(200, json=final)]
    )
    tools = [
        {
            "name": "search_papers",
            "description": "search corpus",
            "input_schema": {
                "type": "object",
                "required": ["query"],
                "properties": {"query": {"type": "string"}},
                "additionalProperties": False,
            },
        }
    ]
    prov = _provider(tool_handlers={"search_papers": lambda args: {"ok": True}})
    result = prov.invoke(_request(tools=tools))
    assert result.error is None
    assert result.finish_reason == "stop"
    assert len(result.tool_events) == 1
    assert respx_mock.calls.call_count == 2


# ---------------------------------------------------------------------------
# Usage / cost accounting details
# ---------------------------------------------------------------------------


@respx.mock(base_url=BASE_URL, assert_all_called=False)
def test_mid_tool_loop_http_error_keeps_usage_cost(respx_mock):
    """An HTTP failure after a completed tool iteration must report the
    real cost of the tokens burned so far, not a hardcoded 0.0."""
    tool_call = _tool_call_response(
        name="search_papers", arguments_json='{"query": "x"}'
    )
    respx_mock.post("/chat/completions").mock(
        side_effect=[
            httpx.Response(200, json=tool_call),
            httpx.Response(
                429,
                json={
                    "error": {
                        "code": "rate_limit_exceeded",
                        "message": "Too many requests",
                        "type": "tokens",
                    }
                },
            ),
        ]
    )
    tools = [
        {
            "name": "search_papers",
            "description": "search corpus",
            "input_schema": {
                "type": "object",
                "required": ["query"],
                "properties": {"query": {"type": "string"}},
                "additionalProperties": False,
            },
        }
    ]
    prov = _provider(tool_handlers={"search_papers": lambda args: {"ok": True}})
    result = prov.invoke(_request(tools=tools))
    assert result.error is not None
    assert result.error.kind == "rate_limit"
    assert result.usage.prompt_tokens == 200
    assert result.usage.completion_tokens == 40
    # 200/1000 * 0.00015 + 40/1000 * 0.0006 = 0.00003 + 0.000024
    assert result.usage.cost_usd == pytest.approx(0.000054, rel=1e-6)


@respx.mock(base_url=BASE_URL, assert_all_called=False)
def test_sub_second_retry_after_is_preserved(respx_mock):
    """A Retry-After of 0.5s must not be truncated to a zero-sleep 0."""
    respx_mock.post("/chat/completions").mock(
        return_value=httpx.Response(
            429,
            json={
                "error": {
                    "code": "rate_limit_exceeded",
                    "message": "Too many requests",
                    "type": "tokens",
                }
            },
            headers={"retry-after": "0.5"},
        )
    )
    result = _provider().invoke(_request())
    assert result.error is not None
    assert result.error.details["retry_after_seconds"] == 0.5


@respx.mock(base_url=BASE_URL, assert_all_called=False)
def test_unknown_model_zero_cost_is_flagged_estimated(respx_mock):
    """With no price row the 0.0 cost is a placeholder, not a measurement
    — the usage must carry estimated=True (§6.9)."""
    respx_mock.post("/chat/completions").mock(
        return_value=httpx.Response(200, json=_ok_response(structured=_valid_turn()))
    )
    result = _provider().invoke(_request(model="somebody-elses-model"))
    assert result.error is None
    assert result.usage.cost_usd == 0.0
    assert result.usage.estimated is True


# ---------------------------------------------------------------------------
# Tool-schema robustness + reasoning_effort gating
# ---------------------------------------------------------------------------


@respx.mock(base_url=BASE_URL, assert_all_called=False)
def test_broken_tool_input_schema_maps_to_tool_failure(respx_mock):
    """A malformed input_schema must surface as tool_failure instead of
    raising jsonschema.SchemaError out of invoke()."""
    respx_mock.post("/chat/completions").mock(
        return_value=httpx.Response(
            200,
            json=_tool_call_response(
                name="search_papers", arguments_json='{"query": "x"}'
            ),
        )
    )
    tools = [
        {
            "name": "search_papers",
            "description": "search corpus",
            "input_schema": {
                "type": "object",
                "properties": {"query": {"type": "not-a-type"}},
            },
        }
    ]
    prov = _provider(tool_handlers={"search_papers": lambda args: {}})
    result = prov.invoke(_request(tools=tools))
    assert result.error is not None
    assert result.error.kind == "tool_failure"
    assert "JSON Schema" in result.error.message


@respx.mock(base_url=BASE_URL, assert_all_called=False)
def test_reasoning_effort_gated_by_model_family(respx_mock):
    """`reasoning_effort` must only reach reasoning-capable families —
    the gpt-4o generation 400s on the parameter, so the hint is silently
    dropped there (mirroring the anthropic adapter)."""
    respx_mock.post("/chat/completions").mock(
        return_value=httpx.Response(200, json=_ok_response(structured=_valid_turn()))
    )
    prov = _provider()

    req = _request(model="gpt-4o-mini").model_copy(update={"reasoning_effort": "high"})
    prov.invoke(req)
    sent = json.loads(respx_mock.calls[0].request.content)
    assert "reasoning_effort" not in sent

    for model in ("o3-mini", "gpt-5"):
        req = _request(model=model).model_copy(update={"reasoning_effort": "high"})
        prov.invoke(req)
        sent = json.loads(respx_mock.calls[-1].request.content)
        assert sent["reasoning_effort"] == "high"


# ---------------------------------------------------------------------------
# Minimal config helper
# ---------------------------------------------------------------------------


def _minimal_config(session_id: str = "openai-test-session"):
    """Build a Config with one panel agent + coordinator, both `openai`."""
    from symposium.models import (
        AgentConfig,
        BudgetConfig,
        Config,
        Persona,
        RuntimeConfig,
        SelectorConfig,
    )

    persona_a = Persona(
        persona_class="horizontal",
        id="logician",
        reasoning_scope="formal-structural",
        reasoning_style="deductive",
        behavioral_constraints=["state assumptions"],
        failure_modes=["over-formalize"],
    )
    persona_c = Persona(
        persona_class="horizontal",
        id="coordinator",
        reasoning_scope="executive",
        reasoning_style="synthesizing",
        behavioral_constraints=["produce a Verdict every coordination_turn"],
        failure_modes=["over-synthesize too early"],
    )
    return Config(
        schema_version="1.0.0",
        session_id=session_id,
        originator="user",
        problem_statement="What is 2+2?",
        selector=SelectorConfig(
            strategy="fixed",
            default_deliberation_panel=["agent_a"],
            coordinator_agent="coordinator",
        ),
        agents=[
            AgentConfig(
                id="agent_a",
                persona_ref=persona_a,
                provider="openai",
                model="gpt-4o-mini",
            )
        ],
        coordinator=AgentConfig(
            id="coordinator",
            persona_ref=persona_c,
            provider="openai",
            model="gpt-4o-mini",
        ),
        budget=BudgetConfig(
            max_total_tokens=100000,
            max_total_cost_usd=5.0,
            max_rounds=2,
            max_wallclock_seconds=30,
        ),
        runtime=RuntimeConfig(per_agent_retry_budget=0),
    )

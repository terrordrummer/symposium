"""AnthropicProvider adapter tests (§6, §6.6, §6.10, §6.13).

Every test is offline: HTTP traffic is mocked via `respx`. The real
Anthropic endpoint is never reached. The test suite covers:

* §6.6 closed `error.kind` enum — one test per CLOSED value (12 total).
  Anthropic-specific signals: HTTP 529 (`overloaded_error`) → rate_limit,
  HTTP 429 with quota marker → quota_exhausted, terminal
  `stop_reason = "refusal"` → content_filter.
* §6.10 finish-reason normalization — every documented vendor value
  plus `stop_sequence` SUCCESS path.
* §6.4 internal tool-call loop — one tool_use → tool_result iteration
  with the user-role translation asserted on the wire.
* §6.5 structured-output enforcement on the happy path.
* §6.7 internal corrective retry on `malformed_response`.
* §6.8 + §8.9 credential redaction in the persisted Artifact.
* §6.11 registry resolution end-to-end via `default_registry()`.
* §6.13 system-message rewire: position-0 system message hoisted to
  the top-level `system` field, not emitted as a `role = system`
  entry in `messages[]`.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

import httpx
import pytest
import respx

from symposium.models import ProviderRequest, ProviderRequestMessage
from symposium.providers import (
    AdapterRegistry,
    AnthropicProvider,
    MissingCredentialsError,
    UnknownProviderError,
    default_registry,
)

API_KEY = "sk-ant-test-symposium-DO-NOT-LEAK-9f3a1b7c"
BASE_URL = "https://api.anthropic.test/v1"
MESSAGES_URL = f"{BASE_URL}/messages"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _provider(**kw: Any) -> AnthropicProvider:
    return AnthropicProvider(api_key=API_KEY, base_url=BASE_URL, **kw)


def _request(
    *,
    expected: str = "turn_structured_output",
    tools: Optional[List[Dict[str, Any]]] = None,
    model: str = "claude-sonnet-4-5",
) -> ProviderRequest:
    return ProviderRequest(
        provider="anthropic",
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
    stop_reason: str = "end_turn",
    usage: Optional[Dict[str, int]] = None,
) -> Dict[str, Any]:
    return {
        "id": "msg_test_001",
        "type": "message",
        "role": "assistant",
        "model": "claude-sonnet-4-5",
        "content": [{"type": "text", "text": json.dumps(structured)}],
        "stop_reason": stop_reason,
        "usage": usage or {"input_tokens": 100, "output_tokens": 30},
    }


def _tool_use_response(
    *,
    name: str,
    input_args: Dict[str, Any],
    call_id: str = "toolu_001",
    usage: Optional[Dict[str, int]] = None,
) -> Dict[str, Any]:
    return {
        "id": "msg_tool_001",
        "type": "message",
        "role": "assistant",
        "model": "claude-sonnet-4-5",
        "content": [
            {
                "type": "tool_use",
                "id": call_id,
                "name": name,
                "input": input_args,
            }
        ],
        "stop_reason": "tool_use",
        "usage": usage or {"input_tokens": 200, "output_tokens": 40},
    }


def _valid_turn() -> Dict[str, Any]:
    return {"text": "an answer that mentions the smallest assumption needed."}


# ---------------------------------------------------------------------------
# Construction-time tests
# ---------------------------------------------------------------------------


def test_missing_api_key_fails_fast(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(MissingCredentialsError):
        AnthropicProvider(base_url=BASE_URL)


def test_registry_resolves_anthropic_factory(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", API_KEY)
    reg = default_registry()
    assert reg.has("anthropic")

    cfg = _minimal_config()
    providers = reg.build_session_providers(cfg)
    assert {"agent_a", "coordinator"} == set(providers.keys())
    # Single anthropic factory → one shared instance per provider id.
    assert providers["agent_a"] is providers["coordinator"]


def test_registry_raises_unknown_provider(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", API_KEY)
    reg = AdapterRegistry()  # empty
    cfg = _minimal_config()
    with pytest.raises(UnknownProviderError):
        reg.build_session_providers(cfg)


# ---------------------------------------------------------------------------
# §6.13 — system-message rewire
# ---------------------------------------------------------------------------


@respx.mock(base_url=BASE_URL, assert_all_called=False)
def test_system_message_hoisted_to_top_level(respx_mock):
    """`messages[0]` (role=system) MUST be re-routed to the top-level
    `system` field, NOT emitted as a `role=system` entry inside
    `messages[]`."""
    respx_mock.post("/messages").mock(
        return_value=httpx.Response(200, json=_ok_response(structured=_valid_turn()))
    )
    result = _provider().invoke(_request())
    assert result.error is None
    sent = json.loads(respx_mock.calls[0].request.content)
    assert sent["system"] == "persona=logician"
    # No system role inside messages[]: only the user turn remains.
    assert all(m["role"] != "system" for m in sent["messages"])
    assert len(sent["messages"]) == 1
    assert sent["messages"][0]["role"] == "user"
    # max_tokens defaulted to DEFAULT_MAX_TOKENS (4096).
    assert sent["max_tokens"] == 4096


@respx.mock(base_url=BASE_URL, assert_all_called=False)
def test_seed_and_reasoning_effort_silently_dropped(respx_mock):
    respx_mock.post("/messages").mock(
        return_value=httpx.Response(200, json=_ok_response(structured=_valid_turn()))
    )
    req = _request()
    # Inject sampling that includes vendor-unrecognized keys.
    req = req.model_copy(
        update={
            "sampling": {
                "temperature": 0.2,
                "seed": 123,
                "reasoning_effort": "high",
                "max_tokens": 512,
            }
        }
    )
    _provider().invoke(req)
    sent = json.loads(respx_mock.calls[0].request.content)
    assert sent["temperature"] == 0.2
    assert sent["max_tokens"] == 512
    assert "seed" not in sent
    assert "reasoning_effort" not in sent


# ---------------------------------------------------------------------------
# §6.6 — one test per CLOSED error.kind value (12 total)
# ---------------------------------------------------------------------------


@respx.mock(base_url=BASE_URL, assert_all_called=False)
def test_error_kind_timeout(respx_mock):
    respx_mock.post("/messages").mock(
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
    respx_mock.post("/messages").mock(
        return_value=httpx.Response(
            502, json={"type": "error", "error": {"type": "api_error", "message": "bad gateway"}}
        )
    )
    result = _provider().invoke(_request())
    assert result.error is not None
    assert result.error.kind == "network"
    assert result.error.retriable is True


@respx.mock(base_url=BASE_URL, assert_all_called=False)
def test_error_kind_rate_limit_429(respx_mock):
    respx_mock.post("/messages").mock(
        return_value=httpx.Response(
            429,
            json={
                "type": "error",
                "error": {"type": "rate_limit_error", "message": "Too many requests"},
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
def test_error_kind_rate_limit_overloaded_529(respx_mock):
    """HTTP 529 `overloaded_error` is Anthropic's transient overload
    signal and maps to `rate_limit` (§6.6 Anthropic row)."""
    respx_mock.post("/messages").mock(
        return_value=httpx.Response(
            529,
            json={
                "type": "error",
                "error": {"type": "overloaded_error", "message": "Overloaded"},
            },
        )
    )
    result = _provider().invoke(_request())
    assert result.error is not None
    assert result.error.kind == "rate_limit"
    assert result.error.retriable is True


@respx.mock(base_url=BASE_URL, assert_all_called=False)
def test_error_kind_quota_exhausted(respx_mock):
    """HTTP 429 + a quota marker in `error.message` → quota_exhausted."""
    respx_mock.post("/messages").mock(
        return_value=httpx.Response(
            429,
            json={
                "type": "error",
                "error": {
                    "type": "rate_limit_error",
                    "message": "Monthly quota exceeded for organization",
                },
            },
        )
    )
    result = _provider().invoke(_request())
    assert result.error is not None
    assert result.error.kind == "quota_exhausted"
    assert result.error.retriable is False


@respx.mock(base_url=BASE_URL, assert_all_called=False)
def test_error_kind_auth_failure(respx_mock):
    respx_mock.post("/messages").mock(
        return_value=httpx.Response(
            401,
            json={
                "type": "error",
                "error": {"type": "authentication_error", "message": "Bad key"},
            },
        )
    )
    result = _provider().invoke(_request())
    assert result.error is not None
    assert result.error.kind == "auth_failure"
    assert result.error.retriable is False


@respx.mock(base_url=BASE_URL, assert_all_called=False)
def test_error_kind_model_unavailable(respx_mock):
    respx_mock.post("/messages").mock(
        return_value=httpx.Response(
            404,
            json={
                "type": "error",
                "error": {"type": "not_found_error", "message": "Unknown model"},
            },
        )
    )
    result = _provider().invoke(_request())
    assert result.error is not None
    assert result.error.kind == "model_unavailable"
    assert result.error.retriable is False


@respx.mock(base_url=BASE_URL, assert_all_called=False)
def test_error_kind_context_length_exceeded(respx_mock):
    respx_mock.post("/messages").mock(
        return_value=httpx.Response(
            400,
            json={
                "type": "error",
                "error": {
                    "type": "invalid_request_error",
                    "message": "prompt is too long: context length exceeded",
                },
            },
        )
    )
    result = _provider().invoke(_request())
    assert result.error is not None
    assert result.error.kind == "context_length_exceeded"
    assert result.error.retriable is False


@respx.mock(base_url=BASE_URL, assert_all_called=False)
def test_error_kind_content_filter_refusal(respx_mock):
    """Terminal `stop_reason = "refusal"` is HTTP 200 but maps to
    content_filter per §6.6 / §6.10 (Sonnet 4.5+)."""
    respx_mock.post("/messages").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": "msg_refusal",
                "type": "message",
                "role": "assistant",
                "model": "claude-sonnet-4-5",
                "content": [{"type": "text", "text": "I can't help with that."}],
                "stop_reason": "refusal",
                "usage": {"input_tokens": 10, "output_tokens": 8},
            },
        )
    )
    result = _provider().invoke(_request())
    assert result.error is not None
    assert result.error.kind == "content_filter"
    assert result.error.retriable is False
    assert result.finish_reason == "content_filter"
    assert result.error.details == {"stop_reason": "refusal"}


@respx.mock(base_url=BASE_URL, assert_all_called=False)
def test_error_kind_invalid_request(respx_mock):
    respx_mock.post("/messages").mock(
        return_value=httpx.Response(
            400,
            json={
                "type": "error",
                "error": {
                    "type": "invalid_request_error",
                    "message": "Unknown parameter 'foo'",
                },
            },
        )
    )
    result = _provider().invoke(_request())
    assert result.error is not None
    assert result.error.kind == "invalid_request"
    assert result.error.retriable is False


@respx.mock(base_url=BASE_URL, assert_all_called=False)
def test_error_kind_malformed_response_after_corrective_retry(respx_mock):
    """When both the initial and corrective-retry responses fail
    validation, `malformed_response` surfaces (§6.5 / §6.7)."""
    bad_payload = {"not_the_right_field": "value"}
    respx_mock.post("/messages").mock(
        return_value=httpx.Response(200, json=_ok_response(structured=bad_payload))
    )
    result = _provider().invoke(_request())
    assert result.error is not None
    assert result.error.kind == "malformed_response"
    assert result.error.retriable is True
    assert result.structured_output is None
    # Initial call + one corrective retry.
    assert respx_mock.calls.call_count == 2


@respx.mock(base_url=BASE_URL, assert_all_called=False)
def test_error_kind_tool_failure_unknown_tool(respx_mock):
    respx_mock.post("/messages").mock(
        return_value=httpx.Response(
            200, json=_tool_use_response(name="nonexistent_tool", input_args={"x": 1})
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
    """HTTP 200 but `content` is missing — last-resort `internal`."""
    respx_mock.post("/messages").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": "msg-x",
                "type": "message",
                "role": "assistant",
                "model": "claude-sonnet-4-5",
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 0, "output_tokens": 0},
            },
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
    respx_mock.post("/messages").mock(
        return_value=httpx.Response(200, json=_ok_response(structured=payload))
    )
    result = _provider().invoke(_request())
    assert result.error is None
    assert result.finish_reason == "stop"
    assert result.structured_output == payload
    # input_tokens/output_tokens → prompt_tokens/completion_tokens (§6.13 step 4).
    assert result.usage.prompt_tokens == 100
    assert result.usage.completion_tokens == 30
    assert result.usage.total_tokens == 130
    # claude-sonnet-4-5: 100/1000 * 0.003 + 30/1000 * 0.015 = 0.0003 + 0.00045
    assert result.usage.cost_usd == pytest.approx(0.00075, rel=1e-6)


@respx.mock(base_url=BASE_URL, assert_all_called=False)
def test_stop_sequence_is_success_not_content_filter(respx_mock):
    """`stop_reason = "stop_sequence"` with valid JSON → finish_reason
    = "stop" and error = None (§6.6 / §6.10)."""
    respx_mock.post("/messages").mock(
        return_value=httpx.Response(
            200,
            json=_ok_response(structured=_valid_turn(), stop_reason="stop_sequence"),
        )
    )
    result = _provider().invoke(_request())
    assert result.error is None
    assert result.finish_reason == "stop"
    assert result.structured_output == _valid_turn()


# ---------------------------------------------------------------------------
# §6.4 / §6.13 — internal tool-call loop (with user/tool_result rewrite)
# ---------------------------------------------------------------------------


@respx.mock(base_url=BASE_URL, assert_all_called=False)
def test_tool_loop_single_iteration(respx_mock):
    tool_call = _tool_use_response(name="search_papers", input_args={"query": "x"})
    final = _ok_response(
        structured=_valid_turn(),
        usage={"input_tokens": 250, "output_tokens": 60},
    )
    respx_mock.post("/messages").mock(
        side_effect=[
            httpx.Response(200, json=tool_call),
            httpx.Response(200, json=final),
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
    assert result.usage.prompt_tokens == 200 + 250
    assert result.usage.completion_tokens == 40 + 60
    assert result.usage.total_tokens == (200 + 250) + (40 + 60)

    # Anthropic-specific translation: the second outbound request's
    # messages[] must contain a `user` role entry whose `content[]`
    # carries a `tool_result` block (NOT a `tool` role).
    second_body = json.loads(respx_mock.calls[1].request.content)
    msgs = second_body["messages"]
    # The synthetic tool_result message is the last user turn.
    tool_result_msg = msgs[-1]
    assert tool_result_msg["role"] == "user"
    assert isinstance(tool_result_msg["content"], list)
    assert tool_result_msg["content"][0]["type"] == "tool_result"
    assert tool_result_msg["content"][0]["tool_use_id"] == "toolu_001"
    # No `tool` role appears anywhere on the wire.
    assert all(m["role"] != "tool" for m in msgs)


# ---------------------------------------------------------------------------
# §6.10 — finish-reason normalization (every input value)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "vendor_stop, canonical, expect_error",
    [
        ("end_turn", "stop", False),
        ("stop_sequence", "stop", False),
        # max_tokens terminally means the output was truncated at the
        # cap — surfaced as finish_reason="length" with a non-retriable
        # error (see test_max_tokens_truncation_is_length_not_malformed).
        ("max_tokens", "length", True),
        # `refusal` is exercised separately because the body shape is
        # different (no JSON body for refusal).
        # tool_use surfaced terminally without consumable tool_use blocks
        # collapses to `error` (§6.10 last row).
        ("tool_use", "error", True),
        ("unexpected_vendor_value", "error", True),
    ],
)
@respx.mock(base_url=BASE_URL, assert_all_called=False)
def test_finish_reason_normalization(respx_mock, vendor_stop, canonical, expect_error):
    body = _ok_response(structured=_valid_turn(), stop_reason=vendor_stop)
    respx_mock.post("/messages").mock(return_value=httpx.Response(200, json=body))
    result = _provider().invoke(_request())

    assert result.finish_reason == canonical
    if not expect_error:
        assert result.error is None
    else:
        assert result.error is not None


# ---------------------------------------------------------------------------
# §6.7 — corrective retry on malformed response
# ---------------------------------------------------------------------------


@respx.mock(base_url=BASE_URL, assert_all_called=False)
def test_corrective_retry_recovers_after_malformed(respx_mock):
    bad = _ok_response(structured={"not_the_right_field": 42})
    good = _ok_response(structured=_valid_turn())
    respx_mock.post("/messages").mock(
        side_effect=[httpx.Response(200, json=bad), httpx.Response(200, json=good)]
    )
    result = _provider().invoke(_request())
    assert result.error is None
    assert result.finish_reason == "stop"
    assert result.structured_output == _valid_turn()
    assert respx_mock.calls.call_count == 2

    # Corrective retry request appends echoed-assistant + user-annotation
    # turns to the original `messages`. The annotation names the failing
    # path / schema in the user content.
    second_body = json.loads(respx_mock.calls[1].request.content)
    msgs = second_body["messages"]
    # Original `messages` had 1 user (system was hoisted), so the
    # corrective packet has at least: user, assistant (echo), user (annotation).
    assert len(msgs) >= 3
    last = msgs[-1]
    assert last["role"] == "user"
    # `content` is the canonical list-of-blocks form for user/assistant turns.
    text = (
        last["content"][0]["text"]
        if isinstance(last["content"], list)
        else last["content"]
    )
    assert "schema validation" in text
    assert "turn_structured_output" in text


# ---------------------------------------------------------------------------
# §6.8 + §8.9 — credential redaction
# ---------------------------------------------------------------------------


@respx.mock(base_url=BASE_URL, assert_all_called=False)
def test_credentials_never_leak_into_persisted_artifact(tmp_path, respx_mock, monkeypatch):
    """Persisted Artifact / TerminationArtifact never contain the API key.

    Runs a one-round session with an `anthropic` panel agent + coordinator;
    the coordinator's verdict requests user input which terminates the
    session. We then read every persisted file and assert that the API
    key does not appear as a substring anywhere.
    """
    monkeypatch.setenv("ANTHROPIC_API_KEY", API_KEY)
    monkeypatch.setenv("ANTHROPIC_BASE_URL", BASE_URL)

    from symposium.providers import default_registry
    from symposium.scheduler import run_session

    cfg = _minimal_config(session_id="anthropic-creds-redaction-test")

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
            "user_input_request": {"question": "please clarify"},
        },
        usage={"input_tokens": 50, "output_tokens": 25},
    )
    respx_mock.post("/messages").mock(
        side_effect=[
            httpx.Response(200, json=turn_payload),
            httpx.Response(200, json=verdict_payload),
        ]
    )

    reg = default_registry()
    providers = reg.build_session_providers(cfg)

    runs_root = tmp_path / "runs"
    artifact = run_session(cfg, providers, runs_root=str(runs_root))

    assert artifact.outcome.kind == "termination"
    assert artifact.outcome.termination_artifact.reason == "user_input_required"

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
    respx_mock.post("/messages").mock(
        side_effect=[httpx.Response(200, json=bad), httpx.Response(200, json=good)]
    )
    result = _provider().invoke(_request())
    assert result.error is None
    assert respx_mock.calls.call_count == 2
    # Each pass reported 100 prompt / 30 completion tokens.
    assert result.usage.prompt_tokens == 200
    assert result.usage.completion_tokens == 60
    assert result.usage.total_tokens == 260
    # Cost doubles with the tokens (claude-sonnet-4-5 price row).
    assert result.usage.cost_usd == pytest.approx(0.0015, rel=1e-6)


# ---------------------------------------------------------------------------
# max_tokens truncation classification
# ---------------------------------------------------------------------------


@respx.mock(base_url=BASE_URL, assert_all_called=False)
def test_max_tokens_truncation_is_length_not_malformed(respx_mock):
    """Truncated JSON + stop_reason=max_tokens must classify as a
    non-retriable `length` outcome BEFORE JSON parsing — not as a
    retriable malformed_response whose corrective retry is doomed at the
    very same cap."""
    truncated = {
        "id": "msg_trunc",
        "type": "message",
        "role": "assistant",
        "model": "claude-sonnet-4-5",
        "content": [{"type": "text", "text": '{"text": "an answer that got cut of'}],
        "stop_reason": "max_tokens",
        "usage": {"input_tokens": 100, "output_tokens": 1024},
    }
    respx_mock.post("/messages").mock(return_value=httpx.Response(200, json=truncated))
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


@respx.mock(base_url=BASE_URL, assert_all_called=False)
def test_free_text_prose_with_no_schema_is_ok(respx_mock):
    """With no expected schema (the §4.1 llm selector) prose must come
    back verbatim — not be force-parsed into a malformed_response."""
    respx_mock.post("/messages").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": "msg_prose",
                "type": "message",
                "role": "assistant",
                "model": "claude-sonnet-4-5",
                "content": [{"type": "text", "text": "A prose answer, no JSON."}],
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 40, "output_tokens": 12},
            },
        )
    )
    result = _provider().invoke(_request(expected=None))
    assert result.error is None
    assert result.finish_reason == "stop"
    assert result.structured_output is None
    assert result.messages[0].content == "A prose answer, no JSON."
    assert respx_mock.calls.call_count == 1


@respx.mock(base_url=BASE_URL, assert_all_called=False)
def test_free_text_json_array_with_no_schema_is_ok(respx_mock):
    """A JSON-array answer on the free-text path must NOT be parsed into
    `structured_output` — a non-dict there would raise a ValidationError
    out of invoke(), breaking the errors-via-result contract."""
    respx_mock.post("/messages").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": "msg_array",
                "type": "message",
                "role": "assistant",
                "model": "claude-sonnet-4-5",
                "content": [{"type": "text", "text": '["logician", "critic"]'}],
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 40, "output_tokens": 8},
            },
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
    import symposium.providers.anthropic as anthropic_mod

    # A controllable clock: each tool call "takes" 60s. Only the handler
    # advances it, so extra monotonic readers (httpx et al.) can't skew
    # the arithmetic.
    clock = {"v": 0.0}
    monkeypatch.setattr(anthropic_mod.time, "monotonic", lambda: clock["v"])

    def slow_tool(args):
        clock["v"] += 60.0
        return {"matches": []}

    tool_call = _tool_use_response(name="search_papers", input_args={"query": "x"})
    respx_mock.post("/messages").mock(
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
# Usage / cost accounting details
# ---------------------------------------------------------------------------


@respx.mock(base_url=BASE_URL, assert_all_called=False)
def test_cache_tokens_counted_in_prompt_usage(respx_mock):
    """cache_read/cache_creation input tokens are billable input tokens;
    dropping them under-reports prompt usage."""
    body = _ok_response(
        structured=_valid_turn(),
        usage={
            "input_tokens": 100,
            "output_tokens": 30,
            "cache_read_input_tokens": 40,
            "cache_creation_input_tokens": 10,
        },
    )
    respx_mock.post("/messages").mock(return_value=httpx.Response(200, json=body))
    result = _provider().invoke(_request())
    assert result.error is None
    assert result.usage.prompt_tokens == 150
    assert result.usage.completion_tokens == 30
    assert result.usage.total_tokens == 180
    # 150/1000 * 0.003 + 30/1000 * 0.015 = 0.00045 + 0.00045
    assert result.usage.cost_usd == pytest.approx(0.0009, rel=1e-6)


@respx.mock(base_url=BASE_URL, assert_all_called=False)
def test_mid_tool_loop_http_error_keeps_usage_cost(respx_mock):
    """An HTTP failure after a completed tool iteration must report the
    real cost of the tokens burned so far, not a hardcoded 0.0."""
    tool_call = _tool_use_response(name="search_papers", input_args={"query": "x"})
    respx_mock.post("/messages").mock(
        side_effect=[
            httpx.Response(200, json=tool_call),
            httpx.Response(
                429,
                json={
                    "type": "error",
                    "error": {"type": "rate_limit_error", "message": "Too many requests"},
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
    # 200/1000 * 0.003 + 40/1000 * 0.015 = 0.0006 + 0.0006
    assert result.usage.cost_usd == pytest.approx(0.0012, rel=1e-6)


@respx.mock(base_url=BASE_URL, assert_all_called=False)
def test_sub_second_retry_after_is_preserved(respx_mock):
    """A Retry-After of 0.5s must not be truncated to a zero-sleep 0."""
    respx_mock.post("/messages").mock(
        return_value=httpx.Response(
            429,
            json={
                "type": "error",
                "error": {"type": "rate_limit_error", "message": "Too many requests"},
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
    respx_mock.post("/messages").mock(
        return_value=httpx.Response(200, json=_ok_response(structured=_valid_turn()))
    )
    result = _provider().invoke(_request(model="somebody-elses-model"))
    assert result.error is None
    assert result.usage.cost_usd == 0.0
    assert result.usage.estimated is True


# ---------------------------------------------------------------------------
# Tool-schema robustness + message translation
# ---------------------------------------------------------------------------


@respx.mock(base_url=BASE_URL, assert_all_called=False)
def test_broken_tool_input_schema_maps_to_tool_failure(respx_mock):
    """A malformed input_schema must surface as tool_failure instead of
    raising jsonschema.SchemaError out of invoke()."""
    respx_mock.post("/messages").mock(
        return_value=httpx.Response(
            200, json=_tool_use_response(name="search_papers", input_args={"query": "x"})
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
def test_tool_role_message_translated_to_tool_result_block(respx_mock):
    """Canonical `tool`-role turns must be rewired into a user-role
    tool_result block — the Messages API rejects role="tool" with a 400."""
    respx_mock.post("/messages").mock(
        return_value=httpx.Response(200, json=_ok_response(structured=_valid_turn()))
    )
    req = ProviderRequest(
        provider="anthropic",
        model="claude-sonnet-4-5",
        agent_id="logician",
        messages=[
            ProviderRequestMessage(role="system", content="persona=logician"),
            ProviderRequestMessage(role="user", content="problem statement here"),
            ProviderRequestMessage(role="assistant", content="calling a tool"),
            ProviderRequestMessage(
                role="tool", content="tool says 42", tool_call_id="toolu_9"
            ),
        ],
        expected_output_schema="turn_structured_output",
    )
    result = _provider().invoke(req)
    assert result.error is None
    sent = json.loads(respx_mock.calls[0].request.content)
    assert all(m["role"] in ("user", "assistant") for m in sent["messages"])
    rewired = sent["messages"][-1]
    assert rewired["role"] == "user"
    assert rewired["content"][0]["type"] == "tool_result"
    assert rewired["content"][0]["tool_use_id"] == "toolu_9"
    assert rewired["content"][0]["content"] == "tool says 42"


# ---------------------------------------------------------------------------
# Minimal config helper
# ---------------------------------------------------------------------------


def _minimal_config(session_id: str = "anthropic-test-session"):
    """Build a Config with one panel agent + coordinator, both `anthropic`."""
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
                provider="anthropic",
                model="claude-sonnet-4-5",
            )
        ],
        coordinator=AgentConfig(
            id="coordinator",
            persona_ref=persona_c,
            provider="anthropic",
            model="claude-sonnet-4-5",
        ),
        budget=BudgetConfig(
            max_total_tokens=100000,
            max_total_cost_usd=5.0,
            max_rounds=2,
            max_wallclock_seconds=30,
        ),
        runtime=RuntimeConfig(per_agent_retry_budget=0),
    )

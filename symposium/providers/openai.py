"""OpenAI-shaped HTTP provider adapter (§6.12).

Implements the §6.1 `ProviderAdapter` contract against the
`POST /v1/chat/completions` endpoint of an OpenAI-compatible API. The
adapter is transport-only: it has no opinion about prompt-formatting
(the runtime owns `request.messages` per §6.3), and it does not
re-derive anything from a `ContextPacket`.

What this module covers
-----------------------

* **Happy path** — single round-trip, JSON-decoded structured output
  validated against `expected_output_schema` before return.
* **Internal tool-call loop (§6.4)** — up to `max_tool_iterations`
  iterations; `tool_events[]` populated in execution order; unknown
  tool / invalid args / handler exception / iteration cap all map to
  `tool_failure`.
* **Structured-output enforcement (§6.5)** — validation failure
  populates `error.kind = "malformed_response"`.
* **Corrective retry on malformed_response (§6.7)** — one corrective
  packet is constructed and re-sent in-adapter before the failure
  surfaces to the runtime. See the "open clarification" note below.
* **Error mapping (§6.6)** — every CLOSED `error.kind` value has a
  vendor-signal trigger.
* **Finish-reason normalization (§6.10)** — terminal reason mapped
  into the CLOSED 5-value enum.
* **Credentials (§6.8 + §8.9)** — fail-fast at construction if
  `OPENAI_API_KEY` is missing; the secret never enters `raw`,
  `messages`, `tool_events`, or `error.details`.

Open clarification (§6.5 vs M2 done-criteria)
---------------------------------------------

§6.5 reads "On validation failure. The adapter does NOT retry
internally (N10)." — the corrective retry is described as a runtime
concern in §6.7. The M2 prompt's done-criteria list explicitly
require the adapter to construct and send the corrective-retry
packet on `malformed_response` up to its internal cap. The
operational reality is that the current scheduler's
`_invoke_with_retry` re-issues the original request unchanged on a
retriable error, so an internal corrective retry is the only way the
malformed-response → corrective-packet → success path gets exercised
end-to-end. We resolve the ambiguity in favor of the M2 prompt:
the adapter does ONE corrective-retry attempt after a
`malformed_response`. The runtime may further retry per §4.9; this
does not double-count because the second adapter call is just a
single `invoke()` from the runtime's perspective.
"""

from __future__ import annotations

import json
import os
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

import httpx
import jsonschema

from symposium.models import (
    ErrorKind,
    FinishReason,
    ProviderError,
    ProviderRawMessage,
    ProviderRequest,
    ProviderRequestMessage,
    ProviderResult,
    ToolEvent,
    Usage,
)
from symposium.providers._cli_env import effective_timeout
from symposium.providers._http_common import (
    UsageAccum,
    build_corrective_request,
    http_error_result,
    malformed_result,
    merge_usage,
    normalize_tool_result,
    safe_raw,
    usage_from,
    validate_structured_output,
)
from symposium.providers.base import ProviderAdapter
from symposium.providers.registry import MissingCredentialsError

DEFAULT_BASE_URL = "https://api.openai.com/v1"
DEFAULT_TIMEOUT_SECONDS = 60.0

# Minimal per-1k-token price table for cost_usd computation (§6.9).
# Values approximate public OpenAI pricing at M2 drafting; the adapter
# treats this as adapter-internal config, not part of the spec body.
# Keys: model id → (prompt_per_1k_usd, completion_per_1k_usd).
_DEFAULT_PRICES: Dict[str, Tuple[float, float]] = {
    "gpt-4o": (0.005, 0.015),
    "gpt-4o-mini": (0.00015, 0.0006),
    "gpt-4.1": (0.002, 0.008),
    "gpt-4.1-mini": (0.0004, 0.0016),
    "gpt-4-turbo": (0.01, 0.03),
    "gpt-3.5-turbo": (0.0005, 0.0015),
}

ToolHandler = Callable[[Dict[str, Any]], Any]


class OpenAIProvider(ProviderAdapter):
    """OpenAI-shaped Chat Completions adapter (§6.12).

    Constructor parameters
    ----------------------

    api_key:
        Bearer token. Defaults to `os.environ["OPENAI_API_KEY"]`.
        Fail-fast at construction if neither is set (§6.8).
    base_url:
        API root (without trailing slash). Defaults to OpenAI's public
        endpoint. Override for self-hosted OpenAI-compatible servers.
    max_tool_iterations:
        Internal tool-call iteration cap (§6.4). Default 8; the runtime
        passes `Config.runtime.max_tool_iterations` here at construction.
    timeout:
        Per-HTTP-call timeout in seconds.
    tool_handlers:
        Mapping `tool_name -> callable(args_dict) -> result`. M2 ships
        no built-in handlers; callers register handlers explicitly. A
        model emitting a tool_call whose name is not in this map maps
        to `tool_failure` per §6.4.
    price_table:
        Per-model `(prompt_per_1k, completion_per_1k)` USD overrides.
        Unknown models fall back to `(0.0, 0.0)` (zero-cost; the
        runtime's hard-cap is conservative under that flag).
    http_client:
        Optional pre-built `httpx.Client` (tests inject a transport-
        mocked client via `respx`). When omitted, the adapter owns its
        own client and closes it in `shutdown()`.
    """

    name = "openai"

    def __init__(
        self,
        *,
        api_key: Optional[str] = None,
        base_url: str = DEFAULT_BASE_URL,
        max_tool_iterations: int = 8,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        tool_handlers: Optional[Dict[str, ToolHandler]] = None,
        price_table: Optional[Dict[str, Tuple[float, float]]] = None,
        http_client: Optional[httpx.Client] = None,
    ) -> None:
        key = api_key if api_key is not None else os.environ.get("OPENAI_API_KEY")
        if not key:
            raise MissingCredentialsError(
                "OPENAI_API_KEY is not set; OpenAIProvider requires an API key "
                "at construction (§6.8)."
            )
        self._api_key = key
        self._base_url = base_url.rstrip("/")
        self._max_tool_iterations = max_tool_iterations
        self._timeout = timeout
        self._tool_handlers: Dict[str, ToolHandler] = dict(tool_handlers or {})
        self._price_table: Dict[str, Tuple[float, float]] = (
            dict(price_table) if price_table is not None else dict(_DEFAULT_PRICES)
        )
        self._http: httpx.Client = http_client or httpx.Client(timeout=timeout)
        self._owns_http = http_client is None

    # ------------------------------------------------------------------
    # ProviderAdapter contract
    # ------------------------------------------------------------------

    def shutdown(self) -> None:
        if self._owns_http:
            self._http.close()

    def register_tool_handler(self, name: str, handler: ToolHandler) -> None:
        """Register a handler for tool name `name` (§6.4)."""
        self._tool_handlers[name] = handler

    def invoke(self, request: ProviderRequest) -> ProviderResult:
        # The scheduler's deadline-aware path budgets the WHOLE invocation
        # — tool iterations plus the §6.7 corrective retry — via
        # `request.metadata["symposium_timeout_seconds"]`, the same contract
        # the CLI adapters honor. Split the budget across both passes so a
        # single invoke can never overrun the wall-clock the scheduler
        # reserved for the eventual synthesis.
        budget = effective_timeout(request, self._timeout)
        start = time.monotonic()
        first = self._invoke_once(request, budget=budget)
        if first.error is not None and first.error.kind == "malformed_response":
            remaining = budget - (time.monotonic() - start)
            if remaining <= 0:
                return first
            corrective = self._build_corrective_request(request, first)
            second = self._invoke_once(corrective, budget=remaining)
            # The first attempt consumed real tokens and money; fold its
            # usage into the returned result so budget accounting sees
            # both passes, not just the corrective one.
            return second.model_copy(
                update={"usage": merge_usage(first.usage, second.usage)}
            )
        return first

    # ------------------------------------------------------------------
    # Single-pass invocation
    # ------------------------------------------------------------------

    def _invoke_once(self, request: ProviderRequest, *, budget: float) -> ProviderResult:
        body = self._build_request_body(request)
        aggregate = UsageAccum()
        tool_events: List[ToolEvent] = []
        iteration = 0
        current_messages: List[Dict[str, Any]] = list(body["messages"])
        start = time.monotonic()

        while True:
            body["messages"] = current_messages

            # Wall-clock budget check: without it a tool loop of N
            # iterations could run N × timeout, silently overrunning the
            # scheduler's per-turn deadline. The remaining budget doubles
            # as the per-request httpx timeout below.
            remaining = budget - (time.monotonic() - start)
            if remaining <= 0:
                return http_error_result(
                    kind="timeout",
                    message=(
                        f"per-turn budget of {budget:.1f}s exhausted after "
                        f"{iteration} tool iteration(s)"
                    ),
                    retriable=True,
                    usage=self._usage(request.model, aggregate),
                    raw=None,
                    tool_events=tool_events,
                )

            try:
                resp = self._http.post(
                    f"{self._base_url}/chat/completions",
                    json=body,
                    headers=self._auth_headers(),
                    timeout=remaining,
                )
            except httpx.TimeoutException as exc:
                return http_error_result(
                    kind="timeout",
                    message=f"request timed out: {exc}",
                    retriable=True,
                    usage=self._usage(request.model, aggregate),
                    raw=None,
                    tool_events=tool_events,
                )
            except httpx.HTTPError as exc:
                return http_error_result(
                    kind="network",
                    message=f"network error: {exc}",
                    retriable=True,
                    usage=self._usage(request.model, aggregate),
                    raw=None,
                    tool_events=tool_events,
                )

            if resp.status_code != 200:
                return self._classify_http_error(
                    resp, aggregate, tool_events, request.model
                )

            try:
                data = resp.json()
            except Exception as exc:  # noqa: BLE001 — vendor JSON shape opaque
                return http_error_result(
                    kind="internal",
                    message=f"could not parse JSON response body: {exc}",
                    retriable=False,
                    usage=self._usage(request.model, aggregate),
                    raw=None,
                    tool_events=tool_events,
                )

            usage = data.get("usage") or {}
            aggregate.add(usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0))

            try:
                choice = data["choices"][0]
            except (KeyError, IndexError, TypeError):
                return http_error_result(
                    kind="internal",
                    message="response missing choices[0]",
                    retriable=False,
                    usage=self._usage(request.model, aggregate),
                    raw=safe_raw(data),
                    tool_events=tool_events,
                )

            message = choice.get("message") or {}
            vendor_finish = choice.get("finish_reason")

            if vendor_finish == "content_filter":
                return ProviderResult(
                    messages=[
                        ProviderRawMessage(
                            role=message.get("role") or "assistant",
                            content=message.get("content") or "",
                        )
                    ],
                    tool_events=tool_events,
                    usage=self._usage(request.model, aggregate),
                    finish_reason="content_filter",
                    structured_output=None,
                    raw=safe_raw(data),
                    error=ProviderError(
                        kind="content_filter",
                        message="content filtered by provider",
                        retriable=False,
                        details={"vendor_finish_reason": "content_filter"},
                    ),
                )

            tool_calls = message.get("tool_calls") or []
            # Enter the loop whenever tool_calls is populated, regardless
            # of the vendor finish_reason: some OpenAI-compatible servers
            # report `stop` alongside a non-empty tool_calls array, and
            # gating on the finish_reason turned those into a misleading
            # "empty content" internal error.
            if tool_calls:
                if iteration >= self._max_tool_iterations:
                    return http_error_result(
                        kind="tool_failure",
                        message=(
                            f"max_tool_iterations ({self._max_tool_iterations}) exceeded; "
                            "loop cap reached without a terminal response"
                        ),
                        retriable=False,
                        usage=self._usage(request.model, aggregate),
                        raw=safe_raw(data),
                        tool_events=tool_events,
                    )

                assistant_entry: Dict[str, Any] = {
                    "role": "assistant",
                    "content": message.get("content"),
                    "tool_calls": tool_calls,
                }
                next_messages: List[Dict[str, Any]] = list(current_messages) + [assistant_entry]
                tools_by_name: Dict[str, Dict[str, Any]] = {}
                for t in request.tools or []:
                    tname = t.get("name")
                    if isinstance(tname, str):
                        tools_by_name[tname] = t

                # Process every tool_call in this iteration before re-invoking.
                # On any tool error, terminate the loop with tool_failure.
                aborted = self._run_tool_iteration(
                    tool_calls=tool_calls,
                    tools_by_name=tools_by_name,
                    tool_events=tool_events,
                    next_messages=next_messages,
                )
                if aborted is not None:
                    return ProviderResult(
                        messages=[
                            ProviderRawMessage(
                                role=message.get("role") or "assistant",
                                content=message.get("content") or "",
                            )
                        ],
                        tool_events=tool_events,
                        usage=self._usage(request.model, aggregate),
                        finish_reason="error",
                        structured_output=None,
                        raw=safe_raw(data),
                        error=aborted,
                    )

                current_messages = next_messages
                iteration += 1
                continue

            # Terminal — free-text vs structured handling.
            content_str = message.get("content")
            free_text = request.expected_output_schema in (None, "null")

            # Truncation MUST be classified BEFORE any JSON parsing: a
            # response cut off at the token cap is almost never valid
            # JSON, and parsing it first would misfile the failure as a
            # retriable `malformed_response` — dooming the corrective
            # retry (and every runtime retry) at the very same cap.
            if not free_text and vendor_finish == "length":
                return ProviderResult(
                    messages=[
                        ProviderRawMessage(
                            role=message.get("role") or "assistant",
                            content=content_str if isinstance(content_str, str) else "",
                        )
                    ],
                    tool_events=tool_events,
                    usage=self._usage(request.model, aggregate),
                    finish_reason="length",
                    structured_output=None,
                    raw=safe_raw(data),
                    error=ProviderError(
                        kind="context_length_exceeded",
                        message=(
                            "output truncated: finish_reason=length hit the "
                            "completion token cap before the structured output "
                            "completed; retrying at the same cap cannot "
                            "succeed — raise sampling.max_tokens"
                        ),
                        retriable=False,
                        details={"vendor_finish_reason": "length"},
                    ),
                )

            if not isinstance(content_str, str) or content_str == "":
                return ProviderResult(
                    messages=[
                        ProviderRawMessage(
                            role=message.get("role") or "assistant",
                            content=content_str if isinstance(content_str, str) else "",
                        )
                    ],
                    tool_events=tool_events,
                    usage=self._usage(request.model, aggregate),
                    finish_reason="error",
                    structured_output=None,
                    raw=safe_raw(data),
                    error=ProviderError(
                        kind="internal",
                        message="assistant message has empty content and no tool_calls",
                        retriable=False,
                    ),
                )

            # Free-text path (no expected schema — the §4.1 llm selector):
            # the model legitimately answers in prose, so force-parsing it
            # as JSON is wrong twice over (prose → spurious
            # malformed_response; a JSON scalar/array → a non-dict
            # structured_output that would raise out of invoke()). Return
            # the raw text, mirroring the CLI adapters.
            if free_text:
                canonical_finish = _normalize_finish_reason(vendor_finish)
                if canonical_finish == "error":
                    return ProviderResult(
                        messages=[
                            ProviderRawMessage(
                                role=message.get("role") or "assistant",
                                content=content_str,
                            )
                        ],
                        tool_events=tool_events,
                        usage=self._usage(request.model, aggregate),
                        finish_reason="error",
                        structured_output=None,
                        raw=safe_raw(data),
                        error=ProviderError(
                            kind="internal",
                            message=(
                                f"vendor finish_reason {vendor_finish!r} arrived "
                                "terminally with no consumable tool_calls"
                            ),
                            retriable=False,
                            details={"vendor_finish_reason": vendor_finish},
                        ),
                    )
                return ProviderResult(
                    messages=[
                        ProviderRawMessage(
                            role=message.get("role") or "assistant",
                            content=content_str,
                        )
                    ],
                    tool_events=tool_events,
                    usage=self._usage(request.model, aggregate),
                    finish_reason=canonical_finish,
                    structured_output=None,
                    raw=safe_raw(data),
                    error=None,
                )

            try:
                structured = json.loads(content_str)
            except json.JSONDecodeError as exc:
                return malformed_result(
                    request=request,
                    content_str=content_str,
                    usage=self._usage(request.model, aggregate),
                    tool_events=tool_events,
                    raw=safe_raw(data),
                    failing_path="<root>",
                    validator_message=f"content is not valid JSON: {exc}",
                )

            failure = validate_structured_output(structured, request.expected_output_schema)
            if failure is not None:
                return malformed_result(
                    request=request,
                    content_str=content_str,
                    usage=self._usage(request.model, aggregate),
                    tool_events=tool_events,
                    raw=safe_raw(data),
                    failing_path=failure["failing_path"],
                    validator_message=failure["validator_message"],
                    raw_attempt=structured,
                )

            canonical_finish = _normalize_finish_reason(vendor_finish)
            if canonical_finish == "error":
                # Vendor reported a non-terminal reason terminally (e.g.
                # `tool_calls` / `function_call` / null with no actual
                # tool_calls array). §6.10 maps that to `error`; we
                # populate an `internal` error to satisfy the §6.5
                # invariant that `error` finish_reason implies `error != null`.
                return ProviderResult(
                    messages=[
                        ProviderRawMessage(
                            role=message.get("role") or "assistant",
                            content=content_str,
                        )
                    ],
                    tool_events=tool_events,
                    usage=self._usage(request.model, aggregate),
                    finish_reason="error",
                    structured_output=None,
                    raw=safe_raw(data),
                    error=ProviderError(
                        kind="internal",
                        message=(
                            f"vendor finish_reason {vendor_finish!r} arrived "
                            "terminally with no consumable tool_calls"
                        ),
                        retriable=False,
                        details={"vendor_finish_reason": vendor_finish},
                    ),
                )
            return ProviderResult(
                messages=[
                    ProviderRawMessage(
                        role=message.get("role") or "assistant",
                        content=content_str,
                    )
                ],
                tool_events=tool_events,
                usage=self._usage(request.model, aggregate),
                finish_reason=canonical_finish,
                structured_output=structured,
                raw=safe_raw(data),
                error=None,
            )

    # ------------------------------------------------------------------
    # Tool loop
    # ------------------------------------------------------------------

    def _run_tool_iteration(
        self,
        *,
        tool_calls: List[Dict[str, Any]],
        tools_by_name: Dict[str, Dict[str, Any]],
        tool_events: List[ToolEvent],
        next_messages: List[Dict[str, Any]],
    ) -> Optional[ProviderError]:
        """Execute one tool iteration in order. Returns a ProviderError to
        abort the loop, or None to continue."""
        for call in tool_calls:
            call_id = call.get("id") or ""
            fn = call.get("function") or {}
            name = fn.get("name") or ""
            args_raw = fn.get("arguments") or "{}"

            tool_desc = tools_by_name.get(name)
            if tool_desc is None:
                err = ProviderError(
                    kind="tool_failure",
                    message=f"unknown tool: {name!r}",
                    retriable=False,
                    details={"tool_name": name},
                )
                tool_events.append(
                    ToolEvent(name=name or "<unnamed>", arguments={}, result=None, error=err)
                )
                return err

            try:
                args = json.loads(args_raw) if args_raw else {}
            except json.JSONDecodeError as exc:
                err = ProviderError(
                    kind="tool_failure",
                    message=f"tool arguments not valid JSON: {exc}",
                    retriable=False,
                    details={"tool_name": name, "raw_arguments": args_raw},
                )
                tool_events.append(
                    ToolEvent(name=name, arguments={}, result=None, error=err)
                )
                return err

            schema = tool_desc.get("input_schema") or {}
            try:
                jsonschema.Draft202012Validator.check_schema(schema)
                jsonschema.Draft202012Validator(schema).validate(args)
            except jsonschema.SchemaError as exc:
                # The tool's OWN input_schema is broken (config bug) —
                # surface as tool_failure instead of letting SchemaError
                # escape invoke() (errors-via-result contract).
                err = ProviderError(
                    kind="tool_failure",
                    message=f"tool input_schema is not a valid JSON Schema: {exc.message}",
                    retriable=False,
                    details={"tool_name": name, "validator_message": exc.message},
                )
                tool_events.append(
                    ToolEvent(
                        name=name,
                        arguments=args if isinstance(args, dict) else {},
                        result=None,
                        error=err,
                    )
                )
                return err
            except jsonschema.ValidationError as exc:
                err = ProviderError(
                    kind="tool_failure",
                    message=f"tool arguments failed input_schema: {exc.message}",
                    retriable=False,
                    details={
                        "tool_name": name,
                        "validator_message": exc.message,
                        "failing_path": "/".join(str(p) for p in exc.absolute_path) or "<root>",
                    },
                )
                tool_events.append(
                    ToolEvent(
                        name=name,
                        arguments=args if isinstance(args, dict) else {},
                        result=None,
                        error=err,
                    )
                )
                return err

            handler = self._tool_handlers.get(name)
            if handler is None:
                err = ProviderError(
                    kind="tool_failure",
                    message=f"no registered handler for tool {name!r}",
                    retriable=False,
                    details={"tool_name": name},
                )
                tool_events.append(
                    ToolEvent(name=name, arguments=args, result=None, error=err)
                )
                return err

            start = time.monotonic()
            try:
                result = handler(args)
            except Exception as exc:  # noqa: BLE001 — handler-defined errors
                latency_ms = int((time.monotonic() - start) * 1000)
                err = ProviderError(
                    kind="tool_failure",
                    message=f"tool handler raised: {exc}",
                    retriable=False,
                    details={"tool_name": name, "exception": type(exc).__name__},
                )
                tool_events.append(
                    ToolEvent(
                        name=name,
                        arguments=args,
                        result=None,
                        latency_ms=latency_ms,
                        error=err,
                    )
                )
                return err
            latency_ms = int((time.monotonic() - start) * 1000)

            normalized_result = normalize_tool_result(result)
            tool_events.append(
                ToolEvent(
                    name=name,
                    arguments=args,
                    result=normalized_result,
                    latency_ms=latency_ms,
                    error=None,
                )
            )

            tool_content = (
                normalized_result
                if isinstance(normalized_result, str)
                else json.dumps(normalized_result)
            )
            next_messages.append(
                {"role": "tool", "tool_call_id": call_id, "content": tool_content}
            )

        return None

    # ------------------------------------------------------------------
    # Translation: ProviderRequest -> OpenAI request body
    # ------------------------------------------------------------------

    def _build_request_body(self, request: ProviderRequest) -> Dict[str, Any]:
        body: Dict[str, Any] = {
            "model": request.model,
            "messages": [_translate_message(m) for m in request.messages],
        }
        if request.tools:
            body["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": t["name"],
                        "description": t.get("description", ""),
                        "parameters": t.get("input_schema", {}),
                    },
                }
                for t in request.tools
            ]
        if request.expected_output_schema in (
            "turn_structured_output",
            "verdict",
            "synthesis_content",
        ):
            body["response_format"] = {"type": "json_object"}
        reasoning_effort = getattr(request, "reasoning_effort", None)
        if reasoning_effort and _supports_reasoning_effort(request.model):
            # Only reasoning-capable families accept the parameter; the
            # gpt-4o generation 400s on it (a non-retriable failure for
            # what is only a hint). Elsewhere we mirror the Anthropic
            # adapter's posture and silently drop it (§6.2).
            body["reasoning_effort"] = reasoning_effort
        sampling = request.sampling or {}
        for key in ("temperature", "top_p", "seed", "max_tokens", "stop", "stop_sequences"):
            if key in sampling:
                # OpenAI uses `stop` not `stop_sequences`.
                target = "stop" if key == "stop_sequences" else key
                body[target] = sampling[key]
        return body

    def _auth_headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }

    # ------------------------------------------------------------------
    # Error classification (§6.6)
    # ------------------------------------------------------------------

    def _classify_http_error(
        self,
        resp: httpx.Response,
        aggregate: UsageAccum,
        tool_events: List[ToolEvent],
        model: str,
    ) -> ProviderResult:
        status = resp.status_code
        try:
            body = resp.json()
            if not isinstance(body, dict):
                body = {"_non_object_body": body}
        except Exception:  # noqa: BLE001
            body = {"raw_text": resp.text}

        err = body.get("error") if isinstance(body, dict) else None
        if not isinstance(err, dict):
            err = {}
        vendor_code = err.get("code") or err.get("type") or ""
        vendor_type = err.get("type") or ""
        vendor_message = err.get("message") or (resp.text or f"HTTP {status}")

        kind: ErrorKind
        retriable: bool
        if status == 408:
            kind, retriable = "timeout", True
        elif status in (502, 503, 504):
            kind, retriable = "network", True
        elif status == 429:
            if vendor_code == "insufficient_quota" or vendor_type == "insufficient_quota":
                kind, retriable = "quota_exhausted", False
            else:
                kind, retriable = "rate_limit", True
        elif status in (401, 403):
            kind, retriable = "auth_failure", False
        elif status == 404:
            kind, retriable = "model_unavailable", False
        elif status == 400:
            if vendor_code == "context_length_exceeded":
                kind, retriable = "context_length_exceeded", False
            else:
                kind, retriable = "invalid_request", False
        elif 500 <= status < 600:
            kind, retriable = "network", True
        else:
            kind, retriable = "internal", False

        details: Dict[str, Any] = {"status": status}
        if vendor_code:
            details["vendor_code"] = vendor_code
        if vendor_type and vendor_type != vendor_code:
            details["vendor_type"] = vendor_type
        if kind == "rate_limit":
            retry_after = resp.headers.get("retry-after")
            if retry_after:
                try:
                    # Keep the float: truncating a sub-second Retry-After
                    # to int(0) would produce a zero-sleep retry.
                    details["retry_after_seconds"] = float(retry_after)
                except ValueError:
                    pass

        return ProviderResult(
            messages=[],
            tool_events=tool_events,
            usage=self._usage(model, aggregate),
            finish_reason="error",
            structured_output=None,
            raw=safe_raw(body),
            error=ProviderError(
                kind=kind,
                message=f"{vendor_code or 'http_error'}: {vendor_message}".strip(),
                retriable=retriable,
                details=details,
            ),
        )

    # ------------------------------------------------------------------
    # Corrective retry packet construction (§6.7)
    # ------------------------------------------------------------------

    def _build_corrective_request(
        self,
        original: ProviderRequest,
        malformed: ProviderResult,
    ) -> ProviderRequest:
        bad_content = ""
        if malformed.raw:
            try:
                bad_content = (
                    malformed.raw.get("choices", [{}])[0]
                    .get("message", {})
                    .get("content")
                    or ""
                )
            except Exception:  # noqa: BLE001
                bad_content = ""

        failing_path = "<unknown>"
        validator_message = malformed.error.message if malformed.error else "unknown"
        if malformed.error and malformed.error.details:
            failing_path = (
                malformed.error.details.get("failing_path") or "<unknown>"
            )
            validator_message = malformed.error.details.get(
                "validator_message", validator_message
            )
        return build_corrective_request(
            original=original,
            bad_content=bad_content,
            failing_path=failing_path,
            validator_message=validator_message,
        )

    # ------------------------------------------------------------------
    # Cost
    # ------------------------------------------------------------------

    def _prices_for(self, model: str) -> Optional[Tuple[float, float]]:
        prices = self._price_table.get(model)
        if prices is None:
            # Heuristic: try a prefix match (e.g. gpt-4o-mini-2024-07-18 → gpt-4o-mini).
            for known, p in self._price_table.items():
                if model.startswith(known):
                    return p
        return prices

    def _usage(self, model: str, aggregate: UsageAccum) -> Usage:
        prices = self._prices_for(model)
        if prices is None:
            # No price row for this model: 0.0 is a placeholder, not a
            # measurement — flag it estimated (§6.9).
            return usage_from(aggregate, cost=0.0, estimated=True)
        prompt_per_1k, completion_per_1k = prices
        cost = round(
            (aggregate.prompt / 1000.0) * prompt_per_1k
            + (aggregate.completion / 1000.0) * completion_per_1k,
            6,
        )
        return usage_from(aggregate, cost=cost)


# ---------------------------------------------------------------------------
# Helpers — module-level
# ---------------------------------------------------------------------------


def _translate_message(m: ProviderRequestMessage) -> Dict[str, Any]:
    """Translate one canonical message into an OpenAI Chat Completions entry."""
    entry: Dict[str, Any] = {"role": m.role, "content": m.content}
    if m.tool_call_id is not None:
        entry["tool_call_id"] = m.tool_call_id
    return entry


def _supports_reasoning_effort(model: str) -> bool:
    """Whether `model` accepts the `reasoning_effort` parameter.

    Only the reasoning families (o-series, gpt-5 generation) do; sending
    it to a non-reasoning model (gpt-4o and friends) is a 400. Prefix
    matching keeps dated snapshots (`o3-mini-2025-...`) covered.
    """
    m = model.lower()
    return m.startswith(("o1", "o3", "o4", "gpt-5"))


def _normalize_finish_reason(vendor_finish: Optional[str]) -> FinishReason:
    """§6.10 OpenAI terminal mapping."""
    if vendor_finish == "stop":
        return "stop"
    if vendor_finish == "length":
        return "length"
    if vendor_finish == "content_filter":
        return "content_filter"
    # tool_calls / function_call / None / anything unexpected at terminal-time
    # collapses to `error` (the loop is supposed to have consumed those).
    return "error"

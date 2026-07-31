"""Anthropic-shaped HTTP provider adapter (§6.13).

Implements the §6.1 `ProviderAdapter` contract against the
`POST /v1/messages` endpoint of an Anthropic-compatible Messages API.
The adapter is structurally a sibling of `OpenAIProvider`: same
single-pass + tool-loop + corrective-retry + error-classification
scaffolding, different vendor wire format and §6.6 / §6.10 mapping
tables.

What this module covers
-----------------------

* **System-message rewire (§6.13)** — `request.messages[0]` (canonical
  `system` position per §6.3) is hoisted into the top-level `system`
  field of the Messages API request body. It is NOT re-emitted as a
  `role = system` entry inside the body's `messages[]`.
* **Tool loop (§6.4)** — the vendor uses parallel `tool_use` /
  `tool_result` BLOCKS inside `content[]`; the adapter translates one
  vendor response into one iteration regardless of how many tool_use
  blocks it carries. Tool results are sent back as a synthetic
  `user`-role message whose `content[]` is one `{type:"tool_result",
  tool_use_id, content}` block per call. The translation is
  adapter-internal — the outgoing `provider_result.messages[]` still
  reports the canonical `tool` role (§6.3).
* **Structured-output enforcement (§6.5)** — the adapter parses the
  first `text` block of the assistant's `content[]` array as JSON and
  validates against `expected_output_schema`. On failure,
  `error.kind = "malformed_response"`.
* **Corrective retry (§6.7)** — same open-clarification posture as the
  OpenAI adapter: ONE corrective retry attempt happens in-adapter on
  `malformed_response` before surfacing the failure to the runtime.
  The runtime may further retry per §4.9.
* **Error mapping (§6.6)** — every CLOSED `error.kind` value has a
  vendor-signal trigger. Anthropic-specific signals:
    - HTTP 529 (`overloaded_error`) → `rate_limit`.
    - HTTP 429 (`rate_limit_error`) whose body message contains a
      quota marker → `quota_exhausted`; otherwise → `rate_limit`.
    - `stop_reason = "refusal"` (Sonnet 4.5+) → `content_filter`.
    - `stop_reason = "stop_sequence"` → SUCCESS (`finish_reason =
      stop`, NOT a content_filter).
* **Finish-reason normalization (§6.10)** — `end_turn` / `stop_sequence`
  → `stop`; `max_tokens` → `length`; `refusal` → `content_filter`;
  anything else collapses to `error`.
* **Credentials (§6.8 + §8.9)** — fail-fast at construction if
  `ANTHROPIC_API_KEY` is missing. Auth uses the `x-api-key` header.
  The pinned `anthropic-version` header is a module constant; bumping
  it is a code change, not a config change (vendor versioning is
  prose, per N4).
* **Sampling**: `seed` and `reasoning_effort` are silently dropped
  per §6.2 ("adapters MUST silently drop unrecognized keys"). The
  Messages API requires `max_tokens`; we default to 4096 if
  `sampling.max_tokens` is not supplied.

Open clarification (`rate_limit` vs `quota_exhausted`)
------------------------------------------------------

§6.6 distinguishes `rate_limit` from `quota_exhausted` for
"`rate_limit_error` exhausting a daily/monthly hard cap" but Anthropic
does NOT emit a distinct error type for it. Heuristic: if the response
body's `error.message` contains "quota", "monthly_limit_reached", or a
similar vendor-documented marker, classify as `quota_exhausted`;
otherwise default to `rate_limit`. The heuristic is documented here
and flagged as a v1+ refinement candidate.
"""

from __future__ import annotations

import json
import os
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

import httpx
import jsonschema

from symposium.models import (
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

DEFAULT_ANTHROPIC_BASE_URL = "https://api.anthropic.com/v1"
DEFAULT_TIMEOUT_SECONDS = 60.0

# Pinned `anthropic-version` header value. Bumping this is a code
# change, NOT a config change — vendor versioning is N4 prose. Pick a
# value at implementation time that matches the Messages-API contract
# this adapter was tested against.
DEFAULT_ANTHROPIC_VERSION = "2023-06-01"

# Required-but-defaulted `max_tokens`. The Messages API REQUIRES this
# field on every request; if the caller didn't supply it via
# `sampling.max_tokens`, we substitute this default. Surfacing the
# default here keeps callers from having to know vendor-specific
# requirements. 4096 gives a structured deliberation turn (multi-KB
# JSON payloads) enough room — the earlier 1024 truncated real turns
# and every retry was doomed at the same cap.
DEFAULT_MAX_TOKENS = 4096

# Per-1k-token price table for `cost_usd` (§6.9). Values approximate
# public Anthropic pricing at M3 drafting; the adapter treats this as
# adapter-internal config, NOT part of the spec body (N4).
# Keys: model id → (prompt_per_1k_usd, completion_per_1k_usd).
_DEFAULT_PRICES: Dict[str, Tuple[float, float]] = {
    "claude-opus-4-7": (0.015, 0.075),
    "claude-opus-4-6": (0.015, 0.075),
    "claude-opus-4": (0.015, 0.075),
    "claude-sonnet-4-6": (0.003, 0.015),
    "claude-sonnet-4-5": (0.003, 0.015),
    "claude-sonnet-4": (0.003, 0.015),
    "claude-haiku-4-5": (0.001, 0.005),
    "claude-3-5-sonnet": (0.003, 0.015),
    "claude-3-5-haiku": (0.0008, 0.004),
    "claude-3-opus": (0.015, 0.075),
    "claude-3-sonnet": (0.003, 0.015),
    "claude-3-haiku": (0.00025, 0.00125),
}

# Vendor markers we use to disambiguate `rate_limit` from
# `quota_exhausted` on HTTP 429 (`rate_limit_error`). The list is
# intentionally small and case-insensitive; bumping it is a code
# change.
_QUOTA_MARKERS: Tuple[str, ...] = (
    "quota",
    "monthly_limit",
    "credit balance",
    "daily limit",
)

ToolHandler = Callable[[Dict[str, Any]], Any]


class AnthropicProvider(ProviderAdapter):
    """Anthropic-shaped Messages API adapter (§6.13).

    Constructor parameters
    ----------------------

    api_key:
        Bearer token. Defaults to `os.environ["ANTHROPIC_API_KEY"]`.
        Fail-fast at construction if neither is set (§6.8). The secret
        is sent as the `x-api-key` HTTP header and NEVER appears in any
        persisted Artifact field.
    base_url:
        API root (without trailing slash). Defaults to Anthropic's
        public endpoint. Override for self-hosted Anthropic-compatible
        servers.
    anthropic_version:
        Value of the `anthropic-version` header. Defaults to
        `DEFAULT_ANTHROPIC_VERSION`. Bumping this is a code change.
    max_tool_iterations:
        Internal tool-call iteration cap (§6.4). Default 8; the runtime
        passes `Config.runtime.max_tool_iterations` here at construction.
    timeout:
        Per-HTTP-call timeout in seconds.
    tool_handlers:
        Mapping `tool_name -> callable(args_dict) -> result`. M3 ships
        no built-in handlers. A model emitting a `tool_use` block whose
        `name` is not in this map maps to `tool_failure` per §6.4.
    price_table:
        Per-model `(prompt_per_1k, completion_per_1k)` USD overrides.
        Unknown models fall back to `(0.0, 0.0)`.
    http_client:
        Optional pre-built `httpx.Client` (tests inject a transport-
        mocked client via `respx`). When omitted, the adapter owns its
        own client and closes it in `shutdown()`.
    """

    name = "anthropic"

    def __init__(
        self,
        *,
        api_key: Optional[str] = None,
        base_url: str = DEFAULT_ANTHROPIC_BASE_URL,
        anthropic_version: str = DEFAULT_ANTHROPIC_VERSION,
        max_tool_iterations: int = 8,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        tool_handlers: Optional[Dict[str, ToolHandler]] = None,
        price_table: Optional[Dict[str, Tuple[float, float]]] = None,
        http_client: Optional[httpx.Client] = None,
    ) -> None:
        key = api_key if api_key is not None else os.environ.get("ANTHROPIC_API_KEY")
        if not key:
            raise MissingCredentialsError(
                "ANTHROPIC_API_KEY is not set; AnthropicProvider requires an API "
                "key at construction (§6.8)."
            )
        self._api_key = key
        self._base_url = base_url.rstrip("/")
        self._anthropic_version = anthropic_version
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
                    f"{self._base_url}/messages",
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
            # Cache reads/writes are billable input tokens too; dropping
            # them under-reports prompt usage (the CLI adapter counts them).
            aggregate.add(
                (usage.get("input_tokens") or 0)
                + (usage.get("cache_read_input_tokens") or 0)
                + (usage.get("cache_creation_input_tokens") or 0),
                usage.get("output_tokens", 0),
            )

            content_blocks = data.get("content")
            if not isinstance(content_blocks, list):
                return http_error_result(
                    kind="internal",
                    message="response missing content[] array",
                    retriable=False,
                    usage=self._usage(request.model, aggregate),
                    raw=safe_raw(data),
                    tool_events=tool_events,
                )

            vendor_stop = data.get("stop_reason")

            # Refusal: Sonnet 4.5+ safety stop. Maps to content_filter
            # regardless of any content[] blocks present.
            if vendor_stop == "refusal":
                refusal_text = _first_text_block(content_blocks)
                return ProviderResult(
                    messages=[
                        ProviderRawMessage(role="assistant", content=refusal_text)
                    ],
                    tool_events=tool_events,
                    usage=self._usage(request.model, aggregate),
                    finish_reason="content_filter",
                    structured_output=None,
                    raw=safe_raw(data),
                    error=ProviderError(
                        kind="content_filter",
                        message="Model refused on safety grounds",
                        retriable=False,
                        details={"stop_reason": "refusal"},
                    ),
                )

            tool_use_blocks = [
                b for b in content_blocks
                if isinstance(b, dict) and b.get("type") == "tool_use"
            ]

            if tool_use_blocks and vendor_stop == "tool_use":
                if iteration >= self._max_tool_iterations:
                    return http_error_result(
                        kind="tool_failure",
                        message=(
                            f"max_tool_iterations ({self._max_tool_iterations}) "
                            "exceeded; loop cap reached without a terminal response"
                        ),
                        retriable=False,
                        usage=self._usage(request.model, aggregate),
                        raw=safe_raw(data),
                        tool_events=tool_events,
                    )

                # Echo the assistant turn (full content[]) into the
                # outgoing conversation before appending the user
                # tool_result message.
                assistant_entry: Dict[str, Any] = {
                    "role": "assistant",
                    "content": content_blocks,
                }
                next_messages: List[Dict[str, Any]] = list(current_messages) + [assistant_entry]
                tools_by_name = {t.get("name"): t for t in (request.tools or [])}

                aborted, tool_result_blocks = self._run_tool_iteration(
                    tool_use_blocks=tool_use_blocks,
                    tools_by_name=tools_by_name,
                    tool_events=tool_events,
                )
                if aborted is not None:
                    return ProviderResult(
                        messages=[
                            ProviderRawMessage(
                                role="assistant",
                                content=_first_text_block(content_blocks),
                            )
                        ],
                        tool_events=tool_events,
                        usage=self._usage(request.model, aggregate),
                        finish_reason="error",
                        structured_output=None,
                        raw=safe_raw(data),
                        error=aborted,
                    )

                # Anthropic tool_result is delivered as a user-role
                # message with one tool_result block per call.
                next_messages.append({"role": "user", "content": tool_result_blocks})

                current_messages = next_messages
                iteration += 1
                continue

            # Terminal — free-text vs structured handling.
            text = _first_text_block(content_blocks)
            free_text = request.expected_output_schema in (None, "null")

            # Truncation MUST be classified BEFORE any JSON parsing: a
            # response cut off at `max_tokens` is almost never valid JSON,
            # and parsing it first would misfile the failure as a
            # retriable `malformed_response` — dooming the corrective
            # retry (and every runtime retry) at the very same cap.
            if not free_text and vendor_stop == "max_tokens":
                return ProviderResult(
                    messages=[ProviderRawMessage(role="assistant", content=text)],
                    tool_events=tool_events,
                    usage=self._usage(request.model, aggregate),
                    finish_reason="length",
                    structured_output=None,
                    raw=safe_raw(data),
                    error=ProviderError(
                        kind="context_length_exceeded",
                        message=(
                            "output truncated: stop_reason=max_tokens hit the "
                            f"{body.get('max_tokens')}-token cap before the "
                            "structured output completed; retrying at the same "
                            "cap cannot succeed — raise sampling.max_tokens"
                        ),
                        retriable=False,
                        details={
                            "stop_reason": "max_tokens",
                            "max_tokens": body.get("max_tokens"),
                        },
                    ),
                )

            if not text:
                return ProviderResult(
                    messages=[ProviderRawMessage(role="assistant", content="")],
                    tool_events=tool_events,
                    usage=self._usage(request.model, aggregate),
                    finish_reason="error",
                    structured_output=None,
                    raw=safe_raw(data),
                    error=ProviderError(
                        kind="internal",
                        message="terminal response carries no text block",
                        retriable=False,
                        details={"stop_reason": vendor_stop},
                    ),
                )

            # Free-text path (no expected schema — the §4.1 llm selector):
            # the model legitimately answers in prose, so force-parsing it
            # as JSON is wrong twice over (prose → spurious
            # malformed_response; a JSON scalar/array → a non-dict
            # structured_output that would raise out of invoke()). Return
            # the raw text, mirroring the CLI adapters.
            if free_text:
                canonical_finish = _normalize_finish_reason(vendor_stop)
                if canonical_finish == "error":
                    return ProviderResult(
                        messages=[ProviderRawMessage(role="assistant", content=text)],
                        tool_events=tool_events,
                        usage=self._usage(request.model, aggregate),
                        finish_reason="error",
                        structured_output=None,
                        raw=safe_raw(data),
                        error=ProviderError(
                            kind="internal",
                            message=(
                                f"vendor stop_reason {vendor_stop!r} arrived "
                                "terminally with no consumable tool_use blocks"
                            ),
                            retriable=False,
                            details={"stop_reason": vendor_stop},
                        ),
                    )
                return ProviderResult(
                    messages=[ProviderRawMessage(role="assistant", content=text)],
                    tool_events=tool_events,
                    usage=self._usage(request.model, aggregate),
                    finish_reason=canonical_finish,
                    structured_output=None,
                    raw=safe_raw(data),
                    error=None,
                )

            try:
                structured = json.loads(text)
            except json.JSONDecodeError as exc:
                return malformed_result(
                    request=request,
                    content_str=text,
                    usage=self._usage(request.model, aggregate),
                    tool_events=tool_events,
                    raw=safe_raw(data),
                    failing_path="<root>",
                    validator_message=f"content is not valid JSON: {exc}",
                )

            failure = validate_structured_output(
                structured, request.expected_output_schema
            )
            if failure is not None:
                return malformed_result(
                    request=request,
                    content_str=text,
                    usage=self._usage(request.model, aggregate),
                    tool_events=tool_events,
                    raw=safe_raw(data),
                    failing_path=failure["failing_path"],
                    validator_message=failure["validator_message"],
                    raw_attempt=structured,
                )

            canonical_finish = _normalize_finish_reason(vendor_stop)
            if canonical_finish == "error":
                return ProviderResult(
                    messages=[ProviderRawMessage(role="assistant", content=text)],
                    tool_events=tool_events,
                    usage=self._usage(request.model, aggregate),
                    finish_reason="error",
                    structured_output=None,
                    raw=safe_raw(data),
                    error=ProviderError(
                        kind="internal",
                        message=(
                            f"vendor stop_reason {vendor_stop!r} arrived terminally "
                            "with no consumable tool_use blocks"
                        ),
                        retriable=False,
                        details={"stop_reason": vendor_stop},
                    ),
                )

            return ProviderResult(
                messages=[ProviderRawMessage(role="assistant", content=text)],
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
        tool_use_blocks: List[Dict[str, Any]],
        tools_by_name: Dict[str, Dict[str, Any]],
        tool_events: List[ToolEvent],
    ) -> Tuple[Optional[ProviderError], List[Dict[str, Any]]]:
        """Execute every tool_use block in order, building the matching
        tool_result blocks for the next user turn.

        Returns `(error, tool_result_blocks)`. On the first failed call
        the loop aborts: `error` is set, and the partially-built
        `tool_result_blocks` list is returned for forensic completeness
        but the caller discards it (the loop terminates with the
        ProviderError).
        """
        tool_result_blocks: List[Dict[str, Any]] = []
        for block in tool_use_blocks:
            call_id = block.get("id") or ""
            name = block.get("name") or ""
            args_raw = block.get("input")
            args: Dict[str, Any] = args_raw if isinstance(args_raw, dict) else {}

            tool_desc = tools_by_name.get(name)
            if tool_desc is None:
                err = ProviderError(
                    kind="tool_failure",
                    message=f"unknown tool: {name!r}",
                    retriable=False,
                    details={"tool_name": name},
                )
                tool_events.append(
                    ToolEvent(
                        name=name or "<unnamed>",
                        arguments=args,
                        result=None,
                        error=err,
                    )
                )
                return err, tool_result_blocks

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
                    ToolEvent(name=name, arguments=args, result=None, error=err)
                )
                return err, tool_result_blocks
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
                    ToolEvent(name=name, arguments=args, result=None, error=err)
                )
                return err, tool_result_blocks

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
                return err, tool_result_blocks

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
                return err, tool_result_blocks
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
            tool_result_blocks.append(
                {
                    "type": "tool_result",
                    "tool_use_id": call_id,
                    "content": tool_content,
                }
            )

        return None, tool_result_blocks

    # ------------------------------------------------------------------
    # Translation: ProviderRequest -> Anthropic Messages-API request body
    # ------------------------------------------------------------------

    def _build_request_body(self, request: ProviderRequest) -> Dict[str, Any]:
        # §6.13 step 1: hoist the position-0 system message into the
        # top-level `system` field. Anthropic does NOT have a `system`
        # role in the `messages[]` array.
        messages = list(request.messages)
        system_field: Optional[str] = None
        if messages and messages[0].role == "system":
            system = messages[0]
            content = system.content
            if isinstance(content, str):
                system_field = content
            else:
                # Defensive: callers SHOULD pass a string per §6.3, but
                # we accept and stringify other shapes rather than 400
                # the request.
                system_field = json.dumps(content)
            messages = messages[1:]

        body: Dict[str, Any] = {
            "model": request.model,
            "messages": [_translate_message(m) for m in messages],
        }
        if system_field is not None:
            body["system"] = system_field

        if request.tools:
            body["tools"] = [
                {
                    "name": t["name"],
                    "description": t.get("description", ""),
                    "input_schema": t.get("input_schema", {}),
                }
                for t in request.tools
            ]

        sampling = request.sampling or {}
        # The Messages API REQUIRES `max_tokens`. Default if absent.
        body["max_tokens"] = int(sampling.get("max_tokens") or DEFAULT_MAX_TOKENS)
        if "temperature" in sampling:
            body["temperature"] = sampling["temperature"]
        if "top_p" in sampling:
            body["top_p"] = sampling["top_p"]
        if "stop_sequences" in sampling:
            body["stop_sequences"] = sampling["stop_sequences"]
        elif "stop" in sampling:
            # Tolerate the OpenAI key; map to Anthropic's name.
            body["stop_sequences"] = sampling["stop"]
        # Silently drop `seed` and `reasoning_effort` per §6.2.

        return body

    def _auth_headers(self) -> Dict[str, str]:
        return {
            "x-api-key": self._api_key,
            "anthropic-version": self._anthropic_version,
            "Content-Type": "application/json",
        }

    # ------------------------------------------------------------------
    # Error classification (§6.6 — Anthropic table)
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
        vendor_type = err.get("type") or ""
        vendor_message = err.get("message") or (resp.text or f"HTTP {status}")

        if status == 408:
            kind, retriable = "timeout", True
        elif status == 429:
            if _looks_like_quota(vendor_message):
                kind, retriable = "quota_exhausted", False
            else:
                kind, retriable = "rate_limit", True
        elif status == 529:
            # `overloaded_error` — Anthropic's transient overload code.
            kind, retriable = "rate_limit", True
        elif status in (401, 403):
            kind, retriable = "auth_failure", False
        elif status == 404:
            kind, retriable = "model_unavailable", False
        elif status == 400:
            # `invalid_request_error`. Distinguish context-length blow-up
            # via vendor message marker.
            if _looks_like_context_length(vendor_message):
                kind, retriable = "context_length_exceeded", False
            else:
                kind, retriable = "invalid_request", False
        elif 500 <= status < 600:
            kind, retriable = "network", True
        else:
            kind, retriable = "internal", False

        details: Dict[str, Any] = {"status": status}
        if vendor_type:
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
                message=f"{vendor_type or 'http_error'}: {vendor_message}".strip(),
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
                bad_content = _first_text_block(malformed.raw.get("content"))
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
            # Heuristic: try a prefix match (e.g. claude-sonnet-4-5-20251022 → claude-sonnet-4-5).
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
    """Translate one canonical (non-system) message into a Messages-API entry.

    Anthropic accepts either a plain string `content` or an array of
    typed content blocks. For canonical user/assistant turns we emit a
    single-block text array; for adapter-internal turns (e.g. echoed
    assistant content during the tool loop, or a synthetic user
    tool_result) the caller bypasses this helper and constructs the
    `content[]` directly.

    Canonical `tool`-role turns (§6.3) need a rewire: the Messages API
    only accepts `user` / `assistant` roles, so passing `role = "tool"`
    verbatim is a guaranteed 400. The vendor shape for a tool result is
    a user-role message carrying a `tool_result` block — the same
    translation the internal tool loop performs.
    """
    if m.role == "tool":
        return {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": m.tool_call_id or "",
                    "content": _stringify_content(m.content),
                }
            ],
        }
    return {
        "role": m.role,
        "content": [{"type": "text", "text": _stringify_content(m.content)}],
    }


def _stringify_content(content: Any) -> str:
    if isinstance(content, str):
        return content
    try:
        return json.dumps(content)
    except (TypeError, ValueError):
        return str(content)


def _first_text_block(blocks: Any) -> str:
    """Return the `text` of the first `type=text` block in `content[]`.

    Returns an empty string if `blocks` is not a list or contains no
    text block. The Messages API guarantees a `text` block is present
    on terminal responses for text-mode requests; this helper degrades
    gracefully on malformed bodies.
    """
    if not isinstance(blocks, list):
        return ""
    for b in blocks:
        if isinstance(b, dict) and b.get("type") == "text":
            t = b.get("text")
            if isinstance(t, str):
                return t
    return ""


def _normalize_finish_reason(vendor_stop: Optional[str]) -> str:
    """§6.10 Anthropic terminal mapping."""
    if vendor_stop == "end_turn":
        return "stop"
    if vendor_stop == "stop_sequence":
        return "stop"
    if vendor_stop == "max_tokens":
        return "length"
    if vendor_stop == "refusal":
        return "content_filter"
    return "error"


def _looks_like_quota(message: str) -> bool:
    """Heuristic to disambiguate `rate_limit` from `quota_exhausted`.

    Anthropic does not emit a distinct `error.type` for daily/monthly
    hard-cap exhaustion; both surface as `rate_limit_error`. We scan
    the vendor message for known markers. Case-insensitive.
    """
    if not message:
        return False
    lowered = message.lower()
    return any(marker in lowered for marker in _QUOTA_MARKERS)


def _looks_like_context_length(message: str) -> bool:
    """Heuristic for `context_length_exceeded` inside `invalid_request_error`."""
    if not message:
        return False
    lowered = message.lower()
    return (
        "context length" in lowered
        or "context_length_exceeded" in lowered
        or "maximum context" in lowered
        or "too many tokens" in lowered
        or "exceeds the maximum" in lowered
    )

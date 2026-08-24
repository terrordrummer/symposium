"""Vendor-agnostic helpers shared by HTTP-based provider adapters.

This module exists to keep the OpenAI and Anthropic adapters narrowly
focused on their vendor-specific shapes. Logic that does NOT depend on
the vendor's wire format — usage aggregation, structured-output
validation against the canonical schemas, malformed-response packet
construction, tool-result canonicalization, etc. — lives here and is
imported by every HTTP adapter.

Nothing in this module knows about a specific vendor's request body,
response shape, or stop-reason vocabulary. Each adapter calls these
helpers after it has translated the vendor response into vendor-neutral
inputs (the parsed structured object, a raw dict, the failing path, an
echoed bad-content string).
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from pydantic import ValidationError

from symposium.models import (
    ProviderError,
    ProviderRawMessage,
    ProviderRequest,
    ProviderRequestMessage,
    ProviderResult,
    SynthesisContent,
    ToolEvent,
    TurnStructuredOutput,
    Usage,
    Verdict,
)


class UsageAccum:
    """Aggregates per-iteration token counts across an internal tool loop.

    Adapter implementations call `add(prompt, completion)` once per
    vendor round-trip; the accumulator is then converted into a canonical
    `Usage` via `usage_from(...)` at terminal time.
    """

    def __init__(self) -> None:
        self.prompt = 0
        self.completion = 0

    def add(self, prompt_tokens: int, completion_tokens: int) -> None:
        self.prompt += int(prompt_tokens or 0)
        self.completion += int(completion_tokens or 0)

    @property
    def total(self) -> int:
        return self.prompt + self.completion


def usage_from(aggregate: UsageAccum, *, cost: float, estimated: bool = False) -> Usage:
    """Convert an accumulator into a canonical `Usage`.

    `estimated` MUST be True whenever `cost` is a placeholder rather than
    a computed figure — notably the unknown-model fallback where the
    adapter has no price row and reports 0.0 (§6.9). Without the flag a
    zero would read as an exact measurement downstream.
    """
    return Usage(
        prompt_tokens=aggregate.prompt,
        completion_tokens=aggregate.completion,
        total_tokens=aggregate.total,
        cost_usd=cost,
        estimated=estimated,
    )


def merge_usage(first: Usage, second: Usage) -> Usage:
    """Sum the usage of two provider passes into one canonical `Usage`.

    Used by the §6.7 corrective-retry path: the malformed first attempt
    consumed real tokens and real money, so its usage must be folded into
    the usage of the result the adapter ultimately returns — otherwise
    budget accounting only ever sees the second call.
    """
    return Usage(
        prompt_tokens=first.prompt_tokens + second.prompt_tokens,
        completion_tokens=first.completion_tokens + second.completion_tokens,
        total_tokens=first.total_tokens + second.total_tokens,
        cost_usd=round(first.cost_usd + second.cost_usd, 6),
        estimated=bool(first.estimated) or bool(second.estimated),
    )


def safe_raw(value: Any) -> Optional[Dict[str, Any]]:
    """Coerce a parsed vendor response into a `raw`-shaped dict.

    Drops non-dict tops so the persisted `raw` always matches the schema's
    `object | null` shape. API keys never appear in response bodies, so
    no redaction is needed here; the §8.9 invariant is maintained at
    the request-header layer of each adapter.
    """
    if value is None or isinstance(value, dict):
        return value
    return None


def normalize_tool_result(result: Any) -> Any:
    """Best-effort canonicalization of tool handler output.

    Tool results land in `provider_result.tool_events[].result`, which the
    schema constrains to `null | object | string`. We keep dicts/strings
    verbatim; everything else gets JSON-encoded to a string.
    """
    if result is None:
        return None
    if isinstance(result, dict):
        return result
    if isinstance(result, str):
        return result
    try:
        return json.dumps(result)
    except (TypeError, ValueError):
        return str(result)


def validate_structured_output(
    structured: Any, expected: Optional[str]
) -> Optional[Dict[str, Any]]:
    """Validate the model's structured output against the canonical schema.

    Returns None on success, or a dict with `failing_path` /
    `validator_message` keys on failure. The CLOSED enum's `null` value
    is reserved (§6.2) and has no MVP code path; we treat it as "no
    validation required" defensively.
    """
    if expected in (None, "null"):
        return None
    if not isinstance(structured, dict):
        return {
            "failing_path": "<root>",
            "validator_message": "top-level structured_output is not a JSON object",
        }
    if expected == "turn_structured_output":
        model_cls: Any = TurnStructuredOutput
    elif expected == "verdict":
        model_cls = Verdict
    elif expected == "synthesis_content":
        model_cls = SynthesisContent
    else:
        return {
            "failing_path": "<root>",
            "validator_message": f"adapter has no validator for {expected!r}",
        }
    try:
        model_cls.model_validate(structured)
        return None
    except ValidationError as exc:
        errs = exc.errors()
        first: Dict[str, Any] = dict(errs[0]) if errs else {}
        loc = first.get("loc") or ()
        return {
            "failing_path": "/".join(str(p) for p in loc) or "<root>",
            "validator_message": first.get("msg", str(exc)),
        }


def malformed_result(
    *,
    request: ProviderRequest,
    content_str: str,
    usage: Usage,
    tool_events: List[ToolEvent],
    raw: Optional[Dict[str, Any]],
    failing_path: str,
    validator_message: str,
    raw_attempt: Any = None,
) -> ProviderResult:
    details: Dict[str, Any] = {
        "validator": "pydantic",
        "expected_output_schema": request.expected_output_schema,
        "failing_path": failing_path,
        "validator_message": validator_message,
        "raw_attempt": raw_attempt if raw_attempt is not None else content_str,
    }
    return ProviderResult(
        messages=[ProviderRawMessage(role="assistant", content=content_str or "")],
        tool_events=tool_events,
        usage=usage,
        finish_reason="error",
        structured_output=None,
        raw=raw,
        error=ProviderError(
            kind="malformed_response",
            message=f"{failing_path}: {validator_message}",
            retriable=True,
            details=details,
        ),
    )


def http_error_result(
    *,
    kind: str,
    message: str,
    retriable: bool,
    usage: Usage,
    raw: Optional[Dict[str, Any]],
    tool_events: List[ToolEvent],
) -> ProviderResult:
    return ProviderResult(
        messages=[],
        tool_events=tool_events,
        usage=usage,
        finish_reason="error",
        structured_output=None,
        raw=raw,
        error=ProviderError(kind=kind, message=message, retriable=retriable),  # type: ignore[arg-type]
    )


def build_corrective_request(
    *,
    original: ProviderRequest,
    bad_content: str,
    failing_path: str,
    validator_message: str,
) -> ProviderRequest:
    """Construct the §6.7 corrective-retry packet.

    Appends the echoed (malformed) assistant content and a user
    annotation that names the failing path and the validator message
    to the original request's `messages`. Vendor-specific extraction
    of `bad_content` happens at the call site; this function is
    transport-agnostic.
    """
    schema_name = original.expected_output_schema
    annotation = (
        f"The previous response failed schema validation against "
        f"`expected_output_schema = {schema_name}` at path "
        f"`{failing_path}`:\n{validator_message}\n\n"
        "Please re-emit the entire response as a single JSON object "
        "that conforms to the schema. Do not wrap it in markdown. "
        "Do not include explanatory prose outside the JSON."
    )

    # `content` requires minLength=1 only when persisted; for the
    # echoed assistant turn, substitute a placeholder when the
    # provider returned an empty body. The echo is internal-only and
    # never written into the canonical_transcript.
    echoed_content = bad_content if bad_content else "<empty response>"

    new_messages: List[ProviderRequestMessage] = list(original.messages) + [
        ProviderRequestMessage(role="assistant", content=echoed_content),
        ProviderRequestMessage(role="user", content=annotation),
    ]
    return original.model_copy(update={"messages": new_messages}, deep=False)

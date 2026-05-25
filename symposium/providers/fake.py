"""FakeProvider — deterministic test adapter (§6.14, §9.1).

The FakeProvider returns pre-scripted `ProviderResult`s from a
`FakeProviderScript`. Each invocation consumes the next ordinal entry;
an optional per-entry `match` clause asserts that the inbound request
matches expectations (agent_id, expected_output_schema, round,
turn_index). On match failure or script exhaustion the FakeProvider
returns a synthetic `ProviderResult` carrying `error.kind = internal`,
which the runtime routes through §4.9's failure-handling path.

Determinism is unconditional under the §2.7 N3 qualifier: a fixed
script + a fixed sequence of `ProviderRequest`s yields bit-identical
ProviderResults on any host (no LLM is invoked).
"""

from __future__ import annotations

from typing import Optional

from symposium.models import (
    FakeProviderScript,
    ProviderError,
    ProviderRawMessage,
    ProviderRequest,
    ProviderResult,
    Usage,
)
from symposium.providers.base import ProviderAdapter


class FakeProvider(ProviderAdapter):
    """Deterministic adapter driven by a `FakeProviderScript`.

    Implements §6.14 + §9.1. The script binds entries to invocations by
    ordinal position; the per-entry `match` clause can additionally
    assert on the inbound request's `agent_id`, `expected_output_schema`,
    and (when context_packet round / turn_index are derivable) the round
    or turn_index.

    Public attributes:
        name: provider identifier ("fake").
        script: the bound FakeProviderScript.
        invocation_count: number of invocations consumed so far.
        last_request_round / last_request_turn_index: optional hints
            updated by the runtime before each invocation (used by the
            `match.round` / `match.turn_index` assertions).
    """

    name = "fake"

    def __init__(self, script: FakeProviderScript) -> None:
        self.script = script
        self.invocation_count = 0
        # Optional context hints set by the runtime before each invoke.
        self.last_request_round: Optional[int] = None
        self.last_request_turn_index: Optional[int] = None

    # ------------------------------------------------------------------
    # ProviderAdapter contract
    # ------------------------------------------------------------------

    def invoke(self, request: ProviderRequest) -> ProviderResult:
        idx = self.invocation_count
        self.invocation_count += 1

        n = len(self.script.entries)
        if idx >= n:
            if self.script.on_exhaustion == "loop":
                idx = idx % n
            else:
                return _synth_error_result(
                    "internal",
                    "fake_provider_script: entries exhausted",
                )

        entry = self.script.entries[idx]
        if entry.match is not None:
            err = self._check_match(entry.match, request, ordinal=idx + 1)
            if err is not None:
                return err

        # Return a copy of the scripted result so callers cannot mutate
        # the script in place between runs.
        return entry.result.model_copy(deep=True)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _check_match(self, match, request: ProviderRequest, *, ordinal: int):
        if match.agent_id is not None and match.agent_id != request.agent_id:
            return _synth_error_result(
                "internal",
                f"fake_provider_script: match assertion failed at entry {ordinal}: "
                f"agent_id expected={match.agent_id!r} got={request.agent_id!r}",
            )
        if (
            match.expected_output_schema is not None
            and match.expected_output_schema != request.expected_output_schema
        ):
            return _synth_error_result(
                "internal",
                f"fake_provider_script: match assertion failed at entry {ordinal}: "
                f"expected_output_schema expected={match.expected_output_schema!r} "
                f"got={request.expected_output_schema!r}",
            )
        if match.round is not None and self.last_request_round is not None:
            if match.round != self.last_request_round:
                return _synth_error_result(
                    "internal",
                    f"fake_provider_script: match assertion failed at entry {ordinal}: "
                    f"round expected={match.round} got={self.last_request_round}",
                )
        if match.turn_index is not None and self.last_request_turn_index is not None:
            if match.turn_index != self.last_request_turn_index:
                return _synth_error_result(
                    "internal",
                    f"fake_provider_script: match assertion failed at entry {ordinal}: "
                    f"turn_index expected={match.turn_index} got={self.last_request_turn_index}",
                )
        return None


def _synth_error_result(kind: str, message: str) -> ProviderResult:
    return ProviderResult(
        messages=[ProviderRawMessage(role="assistant", content="")],
        tool_events=[],
        usage=Usage(prompt_tokens=0, completion_tokens=0, total_tokens=0, cost_usd=0.0),
        finish_reason="error",
        structured_output=None,
        raw=None,
        error=ProviderError(kind=kind, message=message, retriable=False),
    )

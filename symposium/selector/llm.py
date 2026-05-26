"""LLM selector strategy (§4.1) — one bounded provider invocation.

The `llm` selector makes exactly ONE provider call to choose the panel,
accumulates its `usage` against `selector_budget`, and parses the model's
output into a `SelectorOutput`. Three open clarifications (no spec edits;
the §3 milestone defaults), documented here at the call site:

  * **Invocation shape.** `SelectorOutput` is NOT a member of the §6.2
    `expected_output_schema` closed enum `{turn_structured_output,
    verdict, synthesis_content, null}`, so the adapter cannot validate it.
    We invoke with ``expected_output_schema = None`` — the spec-reserved
    free-text path encoded as JSON ``null`` per §6.2 — and the selector
    parses + validates the model's JSON into a `SelectorOutput` itself.

  * **Which agent drives the call.** The MVP `Config` has no dedicated
    selector-agent field, so we reuse the `coordinator` agent's
    `provider` / `model` (the coordinator is the natural meta-agent). The
    selector stays a *distinct role* (ADR-005): this is a separate phase,
    NOT a `coordination_turn`, and it emits no `canonical_transcript`
    message.

  * **Where the decision lives.** The returned `SelectorOutput` is
    persisted to `selector_output.json`, never the transcript; selector
    `usage` is budgeted here against `selector_budget` and does NOT flow
    into `Artifact.cumulative_usage` or the `transcript_digest`.

Under FakeProvider the single call is deterministic (a fixed script
yields a byte-identical `ProviderResult`), so an `llm` session is just as
replayable as a `fixed` one.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from symposium.models import (
    Config,
    ProviderRequest,
    ProviderRequestMessage,
    SelectorBudget,
    SelectorExclusion,
    SelectorMissingCapability,
    SelectorOutput,
)
from symposium.providers.base import ProviderAdapter
from symposium.selector.base import SelectorBudgetExceeded, SelectorError


def select_llm(
    config: Config, *, providers: Dict[str, ProviderAdapter]
) -> SelectorOutput:
    """Run the single bounded selector invocation and parse its output."""
    coord = config.coordinator
    provider = providers.get(coord.id) or providers.get("default")
    if provider is None:
        raise SelectorError(
            f"selector strategy=llm found no provider for the coordinator agent "
            f"{coord.id!r} (nor a 'default'); cannot invoke the selector"
        )

    request = _build_selector_request(config)

    # If FakeProvider: clear the round / turn hints so a `match.round`
    # clause (if any) is not accidentally satisfied by stale state.
    if hasattr(provider, "last_request_round"):
        provider.last_request_round = None  # type: ignore[attr-defined]
        provider.last_request_turn_index = None  # type: ignore[attr-defined]

    result = provider.invoke(request)

    # --- §4.7 selector-budget cap: accumulate then check BEFORE parsing ----
    budget = _selector_budget(config)
    _enforce_budget(budget, total_tokens=result.usage.total_tokens, cost_usd=result.usage.cost_usd)

    if result.error is not None:
        raise SelectorError(
            f"llm selector invocation failed: {result.error.kind}: {result.error.message}"
        )

    payload = _extract_payload(result)
    return _payload_to_output(config, payload)


# ---------------------------------------------------------------------------
# Request construction
# ---------------------------------------------------------------------------


def _build_selector_request(config: Config) -> ProviderRequest:
    coord = config.coordinator
    candidate_ids = [a.id for a in config.agents]
    messages = [
        ProviderRequestMessage(
            role="system",
            content=(
                "You are the panel selector. Choose which agents should "
                "deliberate on the problem and return a JSON object with keys "
                "selected_agents (array of agent ids), coordinator_agent "
                "(agent id), and optional reasoning."
            ),
        ),
        ProviderRequestMessage(
            role="user",
            content=(
                f"problem_statement: {config.problem_statement}\n"
                f"candidate_agents: {candidate_ids}\n"
                f"coordinator: {coord.id}"
            ),
        ),
    ]
    return ProviderRequest(
        provider=coord.provider,
        model=coord.model,
        agent_id=coord.id,
        # Forward the coordinator's reasoning_effort to the selector call too —
        # the selector hits the same provider/model as the coordinator, so any
        # operator-set hint should apply here as well.
        reasoning_effort=coord.reasoning_effort,
        messages=messages,
        sampling=None,
        tools=[],
        expected_output_schema=None,  # §6.2 free-text path (JSON null per schema)
    )


# ---------------------------------------------------------------------------
# Budget
# ---------------------------------------------------------------------------


def _selector_budget(config: Config) -> Optional[SelectorBudget]:
    """The applicable selector budget: `selector.selector_budget` (required for
    llm) falling back to `budget.selector_budget` (§4.7 / §5.2)."""
    if config.selector.selector_budget is not None:
        return config.selector.selector_budget
    return config.budget.selector_budget


def _enforce_budget(
    budget: Optional[SelectorBudget], *, total_tokens: int, cost_usd: float
) -> None:
    if budget is None:
        return
    if budget.max_tokens is not None and total_tokens > budget.max_tokens:
        raise SelectorBudgetExceeded(
            f"selector usage {total_tokens} tokens exceeds selector_budget.max_tokens "
            f"{budget.max_tokens}",
            total_tokens=total_tokens,
            max_tokens=budget.max_tokens,
        )
    if budget.max_cost_usd is not None and cost_usd > budget.max_cost_usd:
        raise SelectorBudgetExceeded(
            f"selector usage ${cost_usd} exceeds selector_budget.max_cost_usd "
            f"${budget.max_cost_usd}",
            cost_usd=cost_usd,
            max_cost_usd=budget.max_cost_usd,
        )


# ---------------------------------------------------------------------------
# Output parsing
# ---------------------------------------------------------------------------


def _extract_payload(result) -> Dict[str, Any]:
    """Pull the selection JSON from the result.

    Prefers a `structured_output` dict (the easiest authoring form under
    FakeProvider); otherwise parses the last assistant message's text as
    JSON (the true §6.2 `null` free-text path). Either way the selector —
    not the adapter — owns validation.
    """
    if isinstance(result.structured_output, dict):
        return result.structured_output
    for msg in reversed(result.messages or []):
        if isinstance(msg.content, str) and msg.content.strip():
            try:
                parsed = json.loads(msg.content)
            except json.JSONDecodeError as exc:
                raise SelectorError(
                    f"llm selector output is not valid JSON: {exc}"
                ) from exc
            if isinstance(parsed, dict):
                return parsed
            raise SelectorError(
                "llm selector output JSON must be an object with selected_agents"
            )
    raise SelectorError(
        "llm selector invocation returned neither structured_output nor a "
        "parseable free-text message"
    )


def _payload_to_output(config: Config, payload: Dict[str, Any]) -> SelectorOutput:
    selected = payload.get("selected_agents")
    if not isinstance(selected, list) or not selected:
        raise SelectorError(
            "llm selector output missing a non-empty selected_agents array"
        )
    if not all(isinstance(s, str) and s for s in selected):
        raise SelectorError("llm selector selected_agents must be non-empty strings")

    coordinator = payload.get("coordinator_agent") or config.selector.coordinator_agent

    excluded = _parse_exclusions(payload.get("excluded_agents"))
    missing = _parse_missing(payload.get("missing_capabilities"))
    reasoning = payload.get("reasoning")
    if reasoning is not None and not isinstance(reasoning, str):
        raise SelectorError("llm selector reasoning must be a string")

    return SelectorOutput(
        strategy="llm",
        selected_agents=list(selected),
        coordinator_agent=coordinator,
        excluded_agents=excluded or None,
        missing_capabilities=missing or None,
        reasoning=reasoning,
    )


def _parse_exclusions(raw: Any) -> List[SelectorExclusion]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise SelectorError("llm selector excluded_agents must be an array")
    try:
        return [SelectorExclusion.model_validate(item) for item in raw]
    except Exception as exc:  # noqa: BLE001 — surface as a malformed selection
        raise SelectorError(f"llm selector excluded_agents malformed: {exc}") from exc


def _parse_missing(raw: Any) -> List[SelectorMissingCapability]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise SelectorError("llm selector missing_capabilities must be an array")
    try:
        return [SelectorMissingCapability.model_validate(item) for item in raw]
    except Exception as exc:  # noqa: BLE001 — surface as a malformed selection
        raise SelectorError(
            f"llm selector missing_capabilities malformed: {exc}"
        ) from exc

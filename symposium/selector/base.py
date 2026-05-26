"""Selector dispatch + error types (§4.1 selector phase, §5.11).

The selector is the first of the three ADR-005 roles: it chooses *who*
deliberates (the `active_deliberation_panel`) and which agent coordinates,
emitting a schema-valid `SelectorOutput` for replay / audit. It is a
distinct phase — NOT a `coordination_turn` — and the `fixed` / `rules`
strategies make NO provider call at all.

`run_selector(config, *, providers=None)` dispatches on
`config.selector.strategy`:

  * ``fixed``  — degenerate (R3). No provider call; `providers` ignored.
  * ``rules``  — pure, deterministic rule match. No provider call.
  * ``llm``    — one bounded provider invocation. `providers` REQUIRED.

Every path returns a `SelectorOutput`. The dispatcher then enforces the
two cross-config invariants the §4.1 selector exit demands:

  1. ``selected_agents ⊆ {a.id for a in config.agents}`` — the panel may
     only name declared agents.
  2. ``coordinator_agent == config.coordinator.id`` — a selector that
     names a coordinator absent from the config is malformed.

A violation (or an empty selection) raises `SelectorError`, which
`run_session` maps to ``terminate(reason = schema_error)`` before round 1
opens (§4.1). A `SelectorBudgetExceeded` from the `llm` path maps to
``terminate(reason = budget_exceeded)`` (§4.7 selector-budget cap).
"""

from __future__ import annotations

from typing import Dict, Optional

from symposium.models import Config, SelectorOutput
from symposium.providers.base import ProviderAdapter


class SelectorError(ValueError):
    """Empty or malformed selection (§4.1 → ``terminate(schema_error)``).

    Carries the offending detail in its message so the runtime's
    termination path and the test suite can attribute the failure.
    """


class SelectorBudgetExceeded(Exception):
    """An ``llm`` selection's cumulative usage breached `selector_budget`.

    Maps to ``terminate(reason = budget_exceeded)`` (§4.7). Carries the
    breaching tokens / cost and the configured caps for diagnostics.
    """

    def __init__(
        self,
        message: str,
        *,
        total_tokens: Optional[int] = None,
        cost_usd: Optional[float] = None,
        max_tokens: Optional[int] = None,
        max_cost_usd: Optional[float] = None,
    ) -> None:
        super().__init__(message)
        self.total_tokens = total_tokens
        self.cost_usd = cost_usd
        self.max_tokens = max_tokens
        self.max_cost_usd = max_cost_usd


def run_selector(
    config: Config,
    *,
    providers: Optional[Dict[str, ProviderAdapter]] = None,
) -> SelectorOutput:
    """Run the configured §4.1 selector strategy and return a `SelectorOutput`.

    Args:
        config: the session `Config`. `config.selector.strategy` selects
            the strategy; `config.agents` / `config.coordinator` bound the
            invariant checks.
        providers: an ``agent_id -> ProviderAdapter`` map (with optional
            ``"default"`` fallback). Ignored by ``fixed`` / ``rules``;
            REQUIRED by ``llm`` (its absence raises `SelectorError`).

    Returns:
        A `SelectorOutput` whose `selected_agents` is the ordered active
        panel and `coordinator_agent` is the bound coordinator id.

    Raises:
        SelectorError: empty / malformed selection, an unknown selected
            agent, a coordinator mismatch, or a missing `providers` map
            for the ``llm`` strategy.
        SelectorBudgetExceeded: the ``llm`` path breached `selector_budget`.
    """
    # Late imports keep the strategy modules a thin import graph and avoid a
    # cycle (llm.py imports nothing heavyweight; rules.py is pure).
    from symposium.selector.fixed import select_fixed
    from symposium.selector.llm import select_llm
    from symposium.selector.rules import select_rules

    strategy = config.selector.strategy
    if strategy == "fixed":
        output = select_fixed(config)
    elif strategy == "rules":
        output = select_rules(config)
    elif strategy == "llm":
        if not providers:
            raise SelectorError(
                "selector strategy=llm requires a providers map (the selector "
                "invokes the coordinator agent's provider); none was supplied"
            )
        output = select_llm(config, providers=providers)
    else:  # pragma: no cover — SelectorStrategy is a closed enum
        raise SelectorError(f"unknown selector strategy {strategy!r}")

    _enforce_invariants(config, output)
    return output


def _enforce_invariants(config: Config, output: SelectorOutput) -> None:
    """§4.1 selector-exit invariants common to every strategy."""
    declared = {a.id for a in config.agents}
    unknown = [aid for aid in output.selected_agents if aid not in declared]
    if unknown:
        raise SelectorError(
            f"selector selected agent(s) {unknown!r} absent from config.agents "
            f"{sorted(declared)!r}"
        )
    if output.coordinator_agent != config.coordinator.id:
        raise SelectorError(
            f"selector coordinator_agent {output.coordinator_agent!r} does not match "
            f"config.coordinator.id {config.coordinator.id!r}"
        )

"""Fixed selector strategy (§4.1, R3) — degenerate, no provider call.

The MVP `fixed` selector is derivable from `Config` without any
invocation (§5.11): the active panel is the declared
`default_deliberation_panel` and the coordinator is the declared
`coordinator_agent`. This module makes ZERO provider calls — the
`selector_fixed_no_provider_invocation` ADR-005 separation assertion
(§9.7) depends on it: a `fixed` run's FakeProvider observes no
selector-phase `invoke`.
"""

from __future__ import annotations

from symposium.models import Config, SelectorOutput


def select_fixed(config: Config) -> SelectorOutput:
    """Return the degenerate `fixed` SelectorOutput (no provider call)."""
    return SelectorOutput(
        strategy="fixed",
        selected_agents=list(config.selector.default_deliberation_panel),
        coordinator_agent=config.selector.coordinator_agent,
    )

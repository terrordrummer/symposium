"""Selector component (§4.1 selector phase, §5.11 SelectorOutput).

The selector is the first ADR-005 role — it chooses *who* deliberates and
which agent coordinates, producing a schema-valid `SelectorOutput` for
replay / audit. Three strategies sit behind one dispatcher:

  * ``fixed``  — degenerate (R3): the declared panel + coordinator, no
    provider call.
  * ``rules``  — pure, deterministic persona-metadata match, no provider
    call.
  * ``llm``    — one bounded provider invocation (the §6.2 ``null``
    free-text path) parsed into a `SelectorOutput`, budgeted against
    `selector_budget`.

Public entry point: ``run_selector(config, *, providers=None)``.
"""

from symposium.models import SelectorOutput
from symposium.selector.base import (
    SelectorBudgetExceeded,
    SelectorError,
    run_selector,
)

__all__ = [
    "SelectorBudgetExceeded",
    "SelectorError",
    "SelectorOutput",
    "run_selector",
]

"""Built-in default personas (§2.3, R3).

The MVP default panel (R3): `logician`, `visionary`, `researcher`,
`critic`, `engineer`, with `coordinator` distinct from the panel
(ADR-005). All six are horizontal personas (reasoning_scope only;
no domain bounding).
"""

from symposium.personas.defaults import (
    COORDINATOR,
    CRITIC,
    DEFAULT_PANEL,
    ENGINEER,
    LOGICIAN,
    RESEARCHER,
    VISIONARY,
    persona_by_id,
)

__all__ = [
    "COORDINATOR",
    "CRITIC",
    "DEFAULT_PANEL",
    "ENGINEER",
    "LOGICIAN",
    "RESEARCHER",
    "VISIONARY",
    "persona_by_id",
]

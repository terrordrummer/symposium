"""Rules selector strategy (§4.1) — pure, deterministic, no provider call.

`rules` selects a subset / ordering of `config.agents` by matching each
agent's declared `Persona` metadata against the `problem_statement`,
using a transparent, documented, **pure** rule function. "Pure" is a
hard requirement (§3 of the milestone): no clock, no RNG, no network, no
filesystem — the same `(config)` yields a byte-identical `SelectorOutput`
on every host, which is what keeps `execution_replay` (§7.6) meaningful
for `rules` sessions.

The rule is intentionally simple and legible (this is a forward,
spec-"reserved for v1+" implementation of the §5.11 contract, not a
learned policy):

  1. Lower-case the `problem_statement` once.
  2. For each agent in declared order, resolve its inline `Persona` and
     test whether the persona *matches* the problem:
       * a **horizontal** persona matches when any trigger keyword for
         its `reasoning_scope` is a substring of the problem statement;
       * a **domain** persona matches when any of its `domain_scope`
         terms is a substring of the problem statement (in addition to
         the reasoning_scope triggers).
     An agent whose `persona_ref` is an unresolved string id cannot be
     introspected, so it is selected by default (the rules cannot justify
     dropping what they cannot read) and noted as such.
  3. Selected agents keep declared order; dropped agents are recorded in
     `excluded_agents` with a reason.
  4. `missing_capabilities` records each trigger category present in the
     problem statement for which NO available agent declares the matching
     `reasoning_scope` — the gap an offline persona-creation workflow
     (Pass 1 row #124) would fill.
  5. An empty selection raises `SelectorError` (§4.1 → schema_error).

Open clarification (no spec edits): the spec marks `rules` "reserved for
v1+" and does not fix the matching algorithm, so the keyword table below
is this implementation's documented, defensible choice. It is stable and
versioned with the source; changing it changes selection and is a
behaviour change, not a bug fix.
"""

from __future__ import annotations

from typing import List, Tuple

from symposium.models import (
    AgentConfig,
    Config,
    Persona,
    SelectorExclusion,
    SelectorMissingCapability,
    SelectorOutput,
)
from symposium.selector.base import SelectorError

# Transparent keyword triggers per `reasoning_scope` (the R3 default
# panel's scopes plus room for domain personas). Substring match against
# the lower-cased problem statement; order within a list is irrelevant.
_SCOPE_TRIGGERS = {
    "formal-structural": (
        "prove", "proof", "logic", "consisten", "formal", "axiom",
        "theorem", "definition", "valid", "contradiction", "rigor",
    ),
    "lateral-creative": (
        "design", "novel", "creativ", "reframe", "alternativ", "vision",
        "imagine", "brainstorm", "idea", "innovat", "explore",
    ),
    "evidence-based": (
        "evidence", "research", "data", "study", "studies", "cite",
        "citation", "benchmark", "empirical", "literature", "source",
    ),
    "adversarial-scrutiny": (
        "risk", "fail", "flaw", "critique", "weak", "attack", "vulnerab",
        "counterexample", "edge case", "threat", "security",
    ),
    "implementation-feasibility": (
        "implement", "build", "cost", "engineer", "deploy", "feasib",
        "system", "architecture", "performance", "scal", "latency",
    ),
}


def select_rules(config: Config) -> SelectorOutput:
    """Return a deterministic `rules` SelectorOutput (no provider call)."""
    problem = config.problem_statement.lower()

    selected: List[str] = []
    excluded: List[SelectorExclusion] = []
    scopes_present: set[str] = set()

    for agent in config.agents:
        persona = _resolve_persona(agent)
        if persona is None:
            # Unresolved string persona_ref: include by default; the rules
            # cannot introspect it to justify an exclusion.
            selected.append(agent.id)
            continue

        scopes_present.add(persona.reasoning_scope)
        matched, why = _persona_matches(persona, problem)
        if matched:
            selected.append(agent.id)
        else:
            excluded.append(SelectorExclusion(id=agent.id, reason=why))

    if not selected:
        raise SelectorError(
            "rules selector matched no agent's persona metadata against the "
            f"problem_statement (excluded {[e.id for e in excluded]!r}); "
            "a non-empty active panel is required (§4.1)"
        )

    missing = _missing_capabilities(problem, scopes_present)

    return SelectorOutput(
        strategy="rules",
        selected_agents=selected,
        coordinator_agent=config.selector.coordinator_agent,
        excluded_agents=excluded or None,
        missing_capabilities=missing or None,
        reasoning=(
            f"rule-based selection: matched {len(selected)} of "
            f"{len(config.agents)} declared agents against the problem statement "
            "by persona reasoning_scope / domain_scope keyword triggers"
        ),
    )


def _resolve_persona(agent: AgentConfig):
    """Return the inline `Persona` or None when `persona_ref` is a string id."""
    return agent.persona_ref if isinstance(agent.persona_ref, Persona) else None


def _persona_matches(persona: Persona, problem: str) -> Tuple[bool, str]:
    """Return (matched, reason-if-not-matched) for one persona vs the problem."""
    triggers = _SCOPE_TRIGGERS.get(persona.reasoning_scope, ())
    for kw in triggers:
        if kw in problem:
            return True, ""
    # Domain personas additionally match on any domain_scope term.
    if persona.domain_scope:
        for term in persona.domain_scope:
            if term.lower() in problem:
                return True, ""
    return (
        False,
        f"no trigger for reasoning_scope {persona.reasoning_scope!r} "
        "(nor any domain_scope term) appears in the problem statement",
    )


def _missing_capabilities(
    problem: str, scopes_present: set[str]
) -> List[SelectorMissingCapability]:
    """Trigger categories the problem invokes but no available persona covers."""
    missing: List[SelectorMissingCapability] = []
    for scope, triggers in _SCOPE_TRIGGERS.items():
        if scope in scopes_present:
            continue
        hit = next((kw for kw in triggers if kw in problem), None)
        if hit is not None:
            missing.append(
                SelectorMissingCapability(
                    capability=scope,
                    reason=(
                        f"problem invokes {scope!r} (trigger {hit!r}) but no "
                        "available agent declares that reasoning_scope"
                    ),
                )
            )
    return missing

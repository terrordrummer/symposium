"""Per-persona CLI routing with installed-CLI fallback (host layer).

Symposium's runtime already supports a *different provider per agent*
(`run_session` takes an `agent_id -> ProviderAdapter` map, and each
`AgentConfig` carries its own `provider` / `model`). This module is the
host-side policy that *decides* that map for the terminal CLIs:

  * route each agent to its **preferred** CLI by persona — the default
    policy sends the `visionary` (lateral / creative reasoning) to
    `codex-cli` and everyone else (logician / engineer / researcher /
    critic / coordinator — technical / systematic / strict-JSON work) to
    `claude-cli`;
  * **fall back** to whichever CLI is actually installed when the
    preferred one is missing (so a machine with only `codex` runs the
    whole panel on codex, and vice-versa);
  * **rewrite** each agent's `provider` / `model` in a copy of the
    `Config` so the persisted run, `panel_disclosure`, and §7.9 metrics
    reflect the CLI that actually answered.

It is a pure consumer of the public API — no runtime / spec / schema
changes. The `installed` set and the `adapters` cache are injectable so
tests never spawn a real CLI.
"""

from __future__ import annotations

import shutil
from typing import Any, Dict, Optional, Tuple

from symposium.models import Config, Persona
from symposium.providers.base import ProviderAdapter

# Persona id → preferred CLI provider id. Personas not listed use DEFAULT_CLI.
DEFAULT_ROUTING: Dict[str, str] = {
    "visionary": "codex-cli",
}
DEFAULT_CLI = "claude-cli"

# CLI provider id → the executable to probe on PATH.
_CLI_BINARIES: Dict[str, str] = {
    "claude-cli": "claude",
    "codex-cli": "codex",
}
# Deterministic fallback order when a preferred CLI is missing.
_FALLBACK_ORDER = ("claude-cli", "codex-cli")


class NoCliAvailableError(RuntimeError):
    """Neither supported CLI (`claude`, `codex`) is installed on PATH."""


def detect_installed_clis() -> set[str]:
    """Return the set of CLI provider ids whose executable is on PATH."""
    return {cli for cli, binary in _CLI_BINARIES.items() if shutil.which(binary)}


def route_cli_providers(
    config: Config,
    *,
    routing: Optional[Dict[str, str]] = None,
    default_cli: str = DEFAULT_CLI,
    claude_model: str = "opus",
    codex_model: str = "gpt-5.5",
    installed: Optional[set] = None,
    adapters: Optional[Dict[str, ProviderAdapter]] = None,
) -> Tuple[Config, Dict[str, ProviderAdapter]]:
    """Route each agent to a CLI adapter and return (rewritten_config, providers).

    Args:
        config: the session config (agents + coordinator).
        routing: persona_id → preferred CLI provider id, merged over
            `DEFAULT_ROUTING`.
        default_cli: preferred CLI for personas not named in `routing`.
        claude_model / codex_model: model strings stamped onto agents
            routed to each CLI (`codex_model="auto"` lets the codex CLI
            pick its default model).
        installed: override the detected CLI set (tests inject this).
        adapters: a provider-id → adapter cache (tests inject fakes;
            production lazily builds one shared adapter per CLI).

    Returns:
        `(rewritten_config, providers)` where `providers` maps every
        agent id (plus `"default"`) to a `ProviderAdapter`, and the
        config's agents/coordinator have their `provider` / `model`
        rewritten to the chosen CLI.

    Raises:
        NoCliAvailableError: no supported CLI is installed.
    """
    routing = {**DEFAULT_ROUTING, **(routing or {})}
    installed = detect_installed_clis() if installed is None else set(installed)
    if not installed:
        raise NoCliAvailableError(
            "no supported CLI found on PATH; install the `claude` and/or `codex` "
            "CLI (no API key needed — they reuse their own login)"
        )
    cache: Dict[str, ProviderAdapter] = {} if adapters is None else adapters

    def _adapter(cli: str) -> ProviderAdapter:
        if cli not in cache:
            cache[cli] = _build_adapter(cli)
        return cache[cli]

    def _model_for(cli: str) -> str:
        return claude_model if cli == "claude-cli" else codex_model

    providers: Dict[str, ProviderAdapter] = {}
    rewritten = []
    for agent in config.agents:
        cli = _choose(routing.get(_persona_id(agent.persona_ref), default_cli), installed)
        providers[agent.id] = _adapter(cli)
        rewritten.append(agent.model_copy(update={"provider": cli, "model": _model_for(cli)}))

    coord = config.coordinator
    ccli = _choose(routing.get(_persona_id(coord.persona_ref), default_cli), installed)
    providers[coord.id] = _adapter(ccli)
    new_coord = coord.model_copy(update={"provider": ccli, "model": _model_for(ccli)})

    # `default` fallback adapter: the preferred CLI if installed, else any.
    fallback_cli = default_cli if default_cli in installed else _choose(default_cli, installed)
    providers["default"] = _adapter(fallback_cli)

    new_config = config.model_copy(update={"agents": rewritten, "coordinator": new_coord})
    return new_config, providers


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _persona_id(persona_ref: Any) -> str:
    return persona_ref.id if isinstance(persona_ref, Persona) else str(persona_ref)


def _choose(preferred: str, installed: set) -> str:
    """Preferred CLI if installed, else the first installed in fallback order."""
    if preferred in installed:
        return preferred
    for alt in _FALLBACK_ORDER:
        if alt in installed:
            return alt
    raise NoCliAvailableError("no installed CLI to satisfy routing")


def _build_adapter(cli: str) -> ProviderAdapter:
    if cli == "claude-cli":
        from symposium.providers.claude_cli import ClaudeCliProvider

        return ClaudeCliProvider()
    if cli == "codex-cli":
        from symposium.providers.codex_cli import CodexCliProvider

        # `-c model_reasoning_effort=xhigh` matches the operator's
        # documented preference for codex (their `~/.codex/config.toml`
        # uses `xhigh` by default and we pass `--ignore-user-config` to
        # avoid inheriting an interactive setup — so the effort knob
        # has to be reasserted on the argv). `xhigh` is the highest
        # reasoning level supported by codex CLI 0.122+ (the prior
        # `max` value was rejected starting some 0.12x build with
        # "unknown variant `max`, expected one of `none, minimal, low,
        # medium, high, xhigh`" — observed against codex-cli 0.128.0,
        # which terminated the symposium run with
        # `provider_unrecoverable` after retry-budget exhaustion).
        return CodexCliProvider(extra_args=["-c", "model_reasoning_effort=xhigh"])
    raise ValueError(f"unknown CLI provider id: {cli!r}")

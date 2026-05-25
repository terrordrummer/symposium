"""Adapter registration / discovery (§6.11).

In-process registry mapping `provider_id -> AdapterFactory -> ProviderAdapter`.
The factory pattern guarantees the adapter's constructor runs once per
session per provider id; agents sharing a provider id share one adapter
instance.

MVP ships built-in registrations for the OpenAI-shaped HTTP adapter
(`openai`) and the deterministic test adapter (`fake`). Plugin-style
discovery via entry points is a v1 extension (§12 Roadmap;
`repository-strategy.md` §10).

The runtime resolves `Config.agents[].provider` and
`Config.coordinator.provider` through the registry at session init
(§6.11). An unknown provider id raises `UnknownProviderError`, which
the CLI surfaces as a `schema_error`-class failure before any
provider invocation happens.
"""

from __future__ import annotations

from typing import Callable, Dict, Optional

from symposium.models import AgentConfig, Config
from symposium.providers.base import ProviderAdapter

AdapterFactory = Callable[[str, Config], ProviderAdapter]


class UnknownProviderError(Exception):
    """Raised when `Config.agents[].provider` is not registered."""

    def __init__(self, provider_id: str) -> None:
        super().__init__(
            f"unknown provider id: {provider_id!r} (no adapter registered; "
            "register one via AdapterRegistry.register before run_session)"
        )
        self.provider_id = provider_id


class MissingCredentialsError(Exception):
    """Raised by an adapter factory when required credentials are absent (§6.8)."""


class AdapterRegistry:
    """Provider-id → factory map (§6.11).

    A factory is a callable `(provider_id, config) -> ProviderAdapter`.
    The registry caches one instance per provider id per session when
    `build_session_providers` is used so the §6.11 "constructor runs
    once per session per provider id" guarantee holds.
    """

    def __init__(self) -> None:
        self._factories: Dict[str, AdapterFactory] = {}

    def register(self, provider_id: str, factory: AdapterFactory) -> None:
        if not provider_id:
            raise ValueError("provider_id must be a non-empty string")
        self._factories[provider_id] = factory

    def unregister(self, provider_id: str) -> None:
        self._factories.pop(provider_id, None)

    def has(self, provider_id: str) -> bool:
        return provider_id in self._factories

    def create(self, provider_id: str, config: Config) -> ProviderAdapter:
        factory = self._factories.get(provider_id)
        if factory is None:
            raise UnknownProviderError(provider_id)
        return factory(provider_id, config)

    def build_session_providers(self, config: Config) -> Dict[str, ProviderAdapter]:
        """Return an `agent_id -> ProviderAdapter` mapping for the session.

        Adapter instances are cached per `provider_id`: two agents that
        share a provider id share one adapter instance (§6.11). Resolves
        both the panel agents and the coordinator. Raises
        `UnknownProviderError` on the first unresolved provider id.
        """
        per_provider: Dict[str, ProviderAdapter] = {}
        agent_map: Dict[str, ProviderAdapter] = {}
        all_agents: list[AgentConfig] = list(config.agents) + [config.coordinator]
        for ac in all_agents:
            pid = ac.provider
            if pid not in per_provider:
                per_provider[pid] = self.create(pid, config)
            agent_map[ac.id] = per_provider[pid]
        return agent_map


def default_registry() -> AdapterRegistry:
    """Built-in registry with the §6 HTTP adapters.

    `fake` is NOT registered by default: the FakeProvider requires a
    `FakeProviderScript` at construction, which the registry has no
    way to provide. Tests and the CLI's `--script` flag register a
    fake factory ad-hoc when needed.
    """
    r = AdapterRegistry()
    r.register("openai", _openai_factory)
    r.register("anthropic", _anthropic_factory)
    return r


def _openai_factory(provider_id: str, config: Config) -> ProviderAdapter:
    # Late import: keeps `httpx` optional for callers that only use the
    # FakeProvider (e.g. the walking-skeleton tests imported registry).
    import os

    from symposium.providers.openai import DEFAULT_BASE_URL, OpenAIProvider

    return OpenAIProvider(
        base_url=os.environ.get("OPENAI_BASE_URL", DEFAULT_BASE_URL),
        max_tool_iterations=config.runtime.max_tool_iterations,
    )


def _anthropic_factory(provider_id: str, config: Config) -> ProviderAdapter:
    # Late import: same rationale as the OpenAI factory.
    import os

    from symposium.providers.anthropic import (
        DEFAULT_ANTHROPIC_BASE_URL,
        AnthropicProvider,
    )

    return AnthropicProvider(
        base_url=os.environ.get("ANTHROPIC_BASE_URL", DEFAULT_ANTHROPIC_BASE_URL),
        max_tool_iterations=config.runtime.max_tool_iterations,
    )


def make_fake_factory(provider: "ProviderAdapter") -> AdapterFactory:
    """Build a factory that always returns the given FakeProvider instance.

    The CLI uses this when `--script` is supplied so the run binds a
    single FakeProvider against every agent that declares `provider: fake`.
    """

    def _factory(provider_id: str, config: Config) -> ProviderAdapter:
        return provider

    return _factory

"""Per-persona CLI routing + installed-CLI fallback (host layer).

No real CLI is ever spawned: the `installed` set and the `adapters`
cache are injected, so these tests assert pure routing/fallback logic.
"""

from __future__ import annotations

import pytest

from symposium.integrations.cli_routing import (
    NoCliAvailableError,
    route_cli_providers,
)
from symposium.models import (
    AgentConfig,
    BudgetConfig,
    Config,
    SelectorConfig,
)
from symposium.personas import COORDINATOR, persona_by_id


class _FakeAdapter:
    def __init__(self, name):
        self.name = name

    def invoke(self, request):  # pragma: no cover — never invoked in routing tests
        raise NotImplementedError


def _config(panel):
    return Config(
        schema_version="1.0.0", session_id="route", originator="t", problem_statement="p",
        selector=SelectorConfig(strategy="fixed", default_deliberation_panel=panel,
                                coordinator_agent="coordinator"),
        agents=[AgentConfig(id=p, persona_ref=persona_by_id(p), provider="placeholder",
                            model="placeholder") for p in panel],
        coordinator=AgentConfig(id="coordinator", persona_ref=COORDINATOR,
                                provider="placeholder", model="placeholder"),
        budget=BudgetConfig(max_total_tokens=1000, max_total_cost_usd=1.0, max_rounds=2,
                            max_wallclock_seconds=10),
    )


@pytest.fixture
def adapters():
    return {"claude-cli": _FakeAdapter("claude"), "codex-cli": _FakeAdapter("codex")}


def test_codex_adapter_uses_xhigh_not_max_reasoning_effort(monkeypatch):
    """`_build_adapter("codex-cli")` MUST pass
    `-c model_reasoning_effort=xhigh`, NOT `max`. Codex CLI 0.12x
    started rejecting `max` with "unknown variant `max`, expected one
    of `none, minimal, low, medium, high, xhigh`" — a wrong value here
    silently terminates the entire deliberation as
    `provider_unrecoverable` after retry-budget exhaustion.

    Hermetic: monkeypatches `shutil.which` so the test runs even on a
    box without the codex binary installed (Codex review T1 item #6).
    """
    import shutil
    from symposium.integrations.cli_routing import _build_adapter

    # The CodexCliProvider constructor fail-fasts when codex isn't on
    # PATH (check_binary=True default). Fake it so this test asserts
    # pure routing logic, not the operator's local install state.
    monkeypatch.setattr(shutil, "which", lambda binary: f"/fake/bin/{binary}")

    adapter = _build_adapter("codex-cli")
    args = adapter._extra_args
    assert "-c" in args
    idx = args.index("-c")
    assert args[idx + 1] == "model_reasoning_effort=xhigh", (
        f"codex adapter must use xhigh (highest level accepted by current "
        f"codex CLI), got {args[idx + 1]!r}"
    )


def test_codex_registry_factory_matches_router_reasoning_effort(monkeypatch):
    """`_codex_cli_factory` (registry — what `provider="codex-cli"` uses
    when forced explicitly) MUST pass the same effort knob as
    `cli_routing._build_adapter("codex-cli")` (what `provider="cli-auto"`
    routes through). Without this they diverge silently — same provider
    id, different effort level depending on routing path. (Codex review
    T1 item #5.)
    """
    import shutil
    from symposium.integrations.cli_routing import _build_adapter
    from symposium.providers.registry import _codex_cli_factory

    monkeypatch.setattr(shutil, "which", lambda binary: f"/fake/bin/{binary}")

    router_adapter = _build_adapter("codex-cli")
    registry_adapter = _codex_cli_factory("codex-cli", config=None)  # type: ignore[arg-type]

    assert router_adapter._extra_args == registry_adapter._extra_args, (
        f"router={router_adapter._extra_args} vs registry="
        f"{registry_adapter._extra_args}"
    )


def test_per_persona_routing_both_installed(adapters):
    cfg = _config(["logician", "visionary", "critic"])
    new_cfg, providers = route_cli_providers(
        cfg, installed={"claude-cli", "codex-cli"}, adapters=adapters
    )
    routed = {a.id: a.provider for a in new_cfg.agents}
    # default policy: visionary → codex, everyone else → claude
    assert routed == {"logician": "claude-cli", "visionary": "codex-cli", "critic": "claude-cli"}
    assert new_cfg.coordinator.provider == "claude-cli"
    # providers dict maps every agent id + a default fallback
    assert set(providers) == {"logician", "visionary", "critic", "coordinator", "default"}
    assert providers["visionary"] is adapters["codex-cli"]
    assert providers["logician"] is adapters["claude-cli"]


def test_model_stamped_per_cli(adapters):
    cfg = _config(["visionary", "logician"])
    new_cfg, _ = route_cli_providers(
        cfg, installed={"claude-cli", "codex-cli"}, adapters=adapters,
        claude_model="sonnet", codex_model="auto",
    )
    models = {a.id: a.model for a in new_cfg.agents}
    assert models == {"visionary": "auto", "logician": "sonnet"}


def test_fallback_when_preferred_missing(adapters):
    cfg = _config(["logician", "visionary"])
    # only claude installed → visionary falls back to claude
    new_cfg, providers = route_cli_providers(
        cfg, installed={"claude-cli"}, adapters=adapters
    )
    assert {a.id: a.provider for a in new_cfg.agents} == {
        "logician": "claude-cli", "visionary": "claude-cli",
    }
    assert providers["visionary"] is adapters["claude-cli"]


def test_fallback_only_codex(adapters):
    cfg = _config(["logician", "visionary"])
    new_cfg, _ = route_cli_providers(cfg, installed={"codex-cli"}, adapters=adapters)
    # everything routes to codex (the only installed CLI)
    assert {a.provider for a in new_cfg.agents} == {"codex-cli"}
    assert new_cfg.coordinator.provider == "codex-cli"


def test_custom_routing_override(adapters):
    cfg = _config(["logician", "visionary"])
    new_cfg, _ = route_cli_providers(
        cfg, routing={"logician": "codex-cli"}, installed={"claude-cli", "codex-cli"},
        adapters=adapters,
    )
    routed = {a.id: a.provider for a in new_cfg.agents}
    # explicit override + default visionary→codex
    assert routed == {"logician": "codex-cli", "visionary": "codex-cli"}


def test_no_cli_installed_raises(adapters):
    cfg = _config(["logician"])
    with pytest.raises(NoCliAvailableError):
        route_cli_providers(cfg, installed=set(), adapters=adapters)


def test_config_ids_and_panel_preserved(adapters):
    cfg = _config(["logician", "visionary", "critic"])
    new_cfg, _ = route_cli_providers(
        cfg, installed={"claude-cli", "codex-cli"}, adapters=adapters
    )
    # ids unchanged → selector panel still valid
    assert [a.id for a in new_cfg.agents] == ["logician", "visionary", "critic"]
    assert new_cfg.selector.default_deliberation_panel == ["logician", "visionary", "critic"]

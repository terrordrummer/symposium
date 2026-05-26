"""Symposium MCP server (spec §11.4 / §11.5; §12 "HTTP / RPC service host").

A thin Model-Context-Protocol host that wraps the frozen Symposium
reference runtime as MCP tools, so a Claude client (Claude Code / Claude
Desktop / claude.ai) can launch a structured deliberation, then read its
result, replay status, and metrics — all over the stable
`run_session(...)` public API, with **zero** changes to the runtime, the
protocol, or the JSON Schemas.

This module is a pure *consumer*: it re-implements the CLI's
`args → Config → providers → run_session → read-Artifact` flow
(`symposium/cli/main.py`) programmatically from tool arguments. The
deliberation semantics, `transcript_digest`, replay, and metrics are
unchanged — runs are persisted exactly as the CLI persists them and read
back the same way.

Tools exposed:

  * ``deliberate``       — build a Config from arguments, run a session,
    return its outcome + synthesis answer (or termination reason) and a
    compact run summary.
  * ``get_run_summary``  — load a persisted run, recompute §7.9 metrics,
    verify the §7.5 transcript replay, return the summary.
  * ``list_personas``    — the six built-in personas (R3 default panel +
    coordinator).

The `mcp` SDK is an OPTIONAL dependency (the ``[mcp]`` extra). It is
imported at *this module's* import time, NOT at `symposium` package
import time — importing `symposium` and running the CLI work without the
extra. Install with ``pip install "symposium-protocol[mcp]"``.

Entry point: ``symposium-mcp`` → :func:`main` → ``mcp.run(transport="stdio")``.

Real-provider tools read ``ANTHROPIC_API_KEY`` / ``OPENAI_API_KEY`` from
the environment (the server inherits the client's env). The deterministic,
network-free path used by tests and demos is ``provider="fake"`` with a
``fake_script_path`` pointing at a FakeProviderScript JSON.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from mcp.server.fastmcp import FastMCP

from symposium.models import (
    AgentConfig,
    Artifact,
    BudgetConfig,
    Config,
    FakeProviderScript,
    Persona,
    SelectorBudget,
    SelectorConfig,
)
from symposium.observability import compute_metrics
from symposium.personas import COORDINATOR, DEFAULT_PANEL, persona_by_id
from symposium.providers import (
    FakeProvider,
    MissingCredentialsError,
    UnknownProviderError,
    default_registry,
    make_fake_factory,
)
from symposium.replay import replay_transcript
from symposium.scheduler import run_session

# ---------------------------------------------------------------------------
# Defaults (read from the example config when present; constant fallback for
# wheel installs where examples/ is not packaged — see anti-patterns §5).
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parents[2]
_ANTHROPIC_EXAMPLE = _REPO_ROOT / "examples" / "configs" / "anthropic.yaml"
# Constant fallback mirrors the model string in examples/configs/anthropic.yaml.
_FALLBACK_ANTHROPIC_MODEL = "claude-sonnet-4-5"
_FALLBACK_OPENAI_MODEL = "gpt-4o"
_DEFAULT_PANEL_IDS = [p.id for p in DEFAULT_PANEL]

mcp = FastMCP("symposium")


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


@mcp.tool()
def deliberate(
    problem: str,
    *,
    panel: Optional[List[str]] = None,
    coordinator: str = "coordinator",
    provider: str = "anthropic",
    model: Optional[str] = None,
    selector_strategy: str = "fixed",
    max_rounds: int = 4,
    max_total_tokens: int = 100000,
    max_total_cost_usd: float = 5.0,
    max_wallclock_seconds: int = 300,
    fake_script_path: Optional[str] = None,
    selector_fake_script_path: Optional[str] = None,
    output_dir: str = "runs",
) -> Dict[str, Any]:
    """Run a structured Symposium deliberation and return its result.

    Builds a `Config` exactly as the CLI does — resolving each panel id and
    the coordinator id through `persona_by_id` into inline `Persona`
    objects — resolves providers via the built-in adapter registry, calls
    `run_session(...)`, and returns a JSON-able summary.

    Args:
        problem: the problem statement the panel deliberates on.
        panel: built-in persona ids for the deliberation panel. Defaults to
            the R3 default panel (logician, visionary, researcher, critic,
            engineer).
        coordinator: built-in coordinator persona id (default "coordinator").
        provider: adapter id every agent uses — "anthropic" (default),
            "openai", or "fake". Real providers read their API key from the
            environment; "fake" requires `fake_script_path`.
        model: provider model string. Defaults per provider (Anthropic:
            the example-config model; OpenAI: a sane default; fake:
            "fake-deterministic").
        selector_strategy: §4.1 selector — "fixed" (default), "rules"
            (deterministic persona-metadata match, no provider call), or
            "llm" (one bounded provider call; needs `selector_fake_script_path`
            under provider="fake").
        max_rounds, max_total_tokens, max_total_cost_usd,
        max_wallclock_seconds: §4.7 hard caps.
        fake_script_path: FakeProviderScript JSON, required when
            provider="fake".
        selector_fake_script_path: a distinct FakeProviderScript JSON
            driving the §4.1 `llm` selector under provider="fake".
        output_dir: root directory for the persisted run (default "runs").

    Returns:
        On success: ``{outcome, synthesis_answer | termination_reason,
        selected_agents, transcript_digest, cumulative_usage, run_dir,
        rounds}``. A selector or budget termination is a *result*, not an
        error. On a bad argument / missing credential / provider failure:
        ``{"error": "<kind>: <message>"}`` — the transport never crashes.
    """
    try:
        panel_ids = list(panel) if panel else list(_DEFAULT_PANEL_IDS)
        resolved_model = model or _default_model(provider)
        session_id = f"mcp-{uuid.uuid4().hex}"

        config = _build_config(
            problem=problem,
            session_id=session_id,
            panel_ids=panel_ids,
            coordinator_id=coordinator,
            provider=provider,
            model=resolved_model,
            selector_strategy=selector_strategy,
            max_rounds=max_rounds,
            max_total_tokens=max_total_tokens,
            max_total_cost_usd=max_total_cost_usd,
            max_wallclock_seconds=max_wallclock_seconds,
        )

        registry = default_registry()
        if provider == "fake":
            if not fake_script_path:
                raise ValueError(
                    'provider="fake" requires fake_script_path (a FakeProviderScript JSON)'
                )
            fp = FakeProvider(script=_load_script(fake_script_path))
            registry.register("fake", make_fake_factory(fp))

        providers = registry.build_session_providers(config)

        # §4.1 `llm` selector: a distinct provider drives the single selector
        # invocation so it never consumes deliberation-script entries (mirrors
        # the CLI's --selector-script). `fixed` / `rules` make no provider call.
        selector_providers = None
        if selector_strategy == "llm" and provider == "fake":
            if not selector_fake_script_path:
                raise ValueError(
                    'selector_strategy="llm" with provider="fake" requires '
                    "selector_fake_script_path"
                )
            sel_fp = FakeProvider(script=_load_script(selector_fake_script_path))
            selector_providers = {"default": sel_fp}

        artifact = run_session(
            config,
            providers,
            runs_root=output_dir,
            selector_providers=selector_providers,
        )

        run_dir = Path(output_dir) / session_id
        result: Dict[str, Any] = {
            "outcome": artifact.outcome.kind,
            "selected_agents": _read_selected_agents(run_dir, fallback=panel_ids),
            "transcript_digest": artifact.transcript_digest,
            "cumulative_usage": artifact.cumulative_usage.model_dump(mode="json"),
            "run_dir": str(run_dir),
            "rounds": _max_round(artifact),
        }
        if artifact.outcome.kind == "synthesis":
            result["synthesis_answer"] = _synthesis_answer(artifact)
        else:
            result["termination_reason"] = artifact.outcome.termination_artifact.reason
        return result
    except (UnknownProviderError, MissingCredentialsError) as exc:
        return _error(exc)
    except Exception as exc:  # noqa: BLE001 — every failure is a structured result
        return _error(exc)


@mcp.tool()
def get_run_summary(run_dir: str) -> Dict[str, Any]:
    """Summarize a persisted run: outcome, digest, replay check, metrics.

    Loads ``<run_dir>/artifact.json``, recomputes the §7.9 MVP metrics,
    verifies the §7.5 transcript replay, and returns
    ``{outcome, transcript_digest, digest_replay_ok, tokens, cost, rounds,
    selected_agents, termination_reason?}``. On any failure returns
    ``{"error": "<kind>: <message>"}``.
    """
    try:
        rd = Path(run_dir)
        artifact_path = rd / "artifact.json"
        if not artifact_path.exists():
            raise FileNotFoundError(f"no artifact.json under {rd}")
        artifact = Artifact.model_validate(json.loads(artifact_path.read_text()))

        metrics = compute_metrics(artifact)
        replay = replay_transcript(rd)

        result: Dict[str, Any] = {
            "outcome": artifact.outcome.kind,
            "transcript_digest": artifact.transcript_digest,
            "digest_replay_ok": replay.digest_matches,
            "tokens": metrics.tokens_cumulative.total_tokens,
            "cost": metrics.cost_cumulative.cost_usd,
            "rounds": _max_round(artifact),
            "selected_agents": _read_selected_agents(
                rd, fallback=[a.id for a in artifact.config.agents]
            ),
        }
        if artifact.outcome.kind == "termination":
            result["termination_reason"] = artifact.outcome.termination_artifact.reason
        return result
    except Exception as exc:  # noqa: BLE001 — structured result, never crash transport
        return _error(exc)


@mcp.tool()
def list_personas() -> List[Dict[str, str]]:
    """Return the six built-in personas (R3 default panel + coordinator).

    Each entry: ``{id, reasoning_scope, role_summary}``. Use these ids as
    `panel` / `coordinator` arguments to `deliberate`.
    """
    try:
        return [
            {
                "id": p.id,
                "reasoning_scope": p.reasoning_scope,
                "role_summary": p.reasoning_style,
            }
            for p in list(DEFAULT_PANEL) + [COORDINATOR]
        ]
    except Exception as exc:  # noqa: BLE001 — keep the transport alive
        return [_error(exc)]


# ---------------------------------------------------------------------------
# Helpers (mirror the CLI's _load_config flow; no runtime changes)
# ---------------------------------------------------------------------------


def _build_config(
    *,
    problem: str,
    session_id: str,
    panel_ids: List[str],
    coordinator_id: str,
    provider: str,
    model: str,
    selector_strategy: str,
    max_rounds: int,
    max_total_tokens: int,
    max_total_cost_usd: float,
    max_wallclock_seconds: int,
) -> Config:
    """Resolve persona ids into inline `Persona` objects and build a Config.

    Mirrors `symposium/cli/main.py:_load_config`: the MVP requires inline
    `Persona` objects (not string `persona_ref`), so each id is resolved
    through `persona_by_id` before constructing `Config`.
    """
    agents = [
        AgentConfig(
            id=pid,
            persona_ref=_resolve_persona(pid),
            provider=provider,
            model=model,
        )
        for pid in panel_ids
    ]
    coordinator_agent = AgentConfig(
        id=coordinator_id,
        persona_ref=_resolve_persona(coordinator_id),
        provider=provider,
        model=model,
    )

    selector_budget = (
        SelectorBudget(max_tokens=2000, max_cost_usd=0.5)
        if selector_strategy == "llm"
        else None
    )

    return Config(
        schema_version="1.0.0",
        session_id=session_id,
        originator="mcp",
        problem_statement=problem,
        selector=SelectorConfig(
            strategy=selector_strategy,  # type: ignore[arg-type]
            default_deliberation_panel=panel_ids,
            coordinator_agent=coordinator_id,
            selector_budget=selector_budget,
        ),
        agents=agents,
        coordinator=coordinator_agent,
        budget=BudgetConfig(
            max_total_tokens=max_total_tokens,
            max_total_cost_usd=max_total_cost_usd,
            max_rounds=max_rounds,
            max_wallclock_seconds=max_wallclock_seconds,
        ),
    )


def _resolve_persona(persona_id: str) -> Persona:
    try:
        return persona_by_id(persona_id)
    except KeyError as exc:
        # Surface unknown built-in ids as a clean ValueError the tool wraps.
        raise ValueError(
            f"unknown built-in persona id {persona_id!r}; "
            f"call list_personas() for the available ids"
        ) from exc


def _default_model(provider: str) -> str:
    if provider == "fake":
        return "fake-deterministic"
    if provider == "anthropic":
        return _anthropic_example_model()
    if provider == "openai":
        return _FALLBACK_OPENAI_MODEL
    # Unknown provider id: leave a non-empty placeholder; the registry will
    # reject the provider before any model string is used.
    return "default"


def _anthropic_example_model() -> str:
    """Default Anthropic model: read examples/configs/anthropic.yaml when
    present (dev / sdist installs), else the constant fallback."""
    try:
        import yaml

        raw = yaml.safe_load(_ANTHROPIC_EXAMPLE.read_text())
        coord = raw.get("coordinator") or {}
        model = coord.get("model")
        if isinstance(model, str) and model:
            return model
    except Exception:  # noqa: BLE001 — any read/parse failure → fallback
        pass
    return _FALLBACK_ANTHROPIC_MODEL


def _load_script(path: str) -> FakeProviderScript:
    p = Path(path)
    text = p.read_text()
    if p.suffix.lower() in (".yaml", ".yml"):
        import yaml

        data = yaml.safe_load(text)
    else:
        data = json.loads(text)
    return FakeProviderScript.model_validate(data)


def _read_selected_agents(run_dir: Path, *, fallback: List[str]) -> List[str]:
    sel_path = run_dir / "selector_output.json"
    if sel_path.exists():
        try:
            return list(json.loads(sel_path.read_text())["selected_agents"])
        except Exception:  # noqa: BLE001 — fall back to the declared panel
            pass
    return list(fallback)


def _synthesis_answer(artifact: Artifact) -> Optional[str]:
    """Integrated answer from the synthesis message (§5.8)."""
    sid = artifact.outcome.synthesis_message_id  # type: ignore[union-attr]
    for msg in artifact.canonical_transcript:
        if msg.id == sid:
            content = msg.content
            if isinstance(content, dict):
                return content.get("integrated_answer")
            return getattr(content, "integrated_answer", None)
    return None


def _max_round(artifact: Artifact) -> int:
    return max((m.round for m in artifact.canonical_transcript), default=0)


def _error(exc: Exception) -> Dict[str, str]:
    return {"error": f"{type(exc).__name__}: {exc}"}


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    """Run the Symposium MCP server over stdio (the `symposium-mcp` script)."""
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()

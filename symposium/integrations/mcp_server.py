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

import asyncio
import json
import queue
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

from mcp.server.fastmcp import Context, FastMCP

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
from symposium.integrations.persona_factory import (
    PersonaGenerationError,
    generate_persona as _generate_persona,
    make_cli_persona_caller,
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
    provider: str = "cli-auto",
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
        provider: who answers each turn —
            "cli-auto" (default): route per persona across the installed
            terminal CLIs — visionary → `codex-cli`, the rest → `claude-cli`
            — falling back to whichever CLI is installed. No API key.
            "claude-cli" / "codex-cli": force one terminal CLI for all agents.
            "anthropic" / "openai": HTTP API (read their key from the env).
            "fake": deterministic, requires `fake_script_path`.
        model: provider model string. Defaults per provider (claude-cli:
            "sonnet"; codex-cli: the CLI's own default; Anthropic: the
            example-config model; OpenAI: a sane default; fake:
            "fake-deterministic"). Ignored under "cli-auto" (the router
            stamps a model per chosen CLI).
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
        config, providers, selector_providers, run_dir, panel_ids = _prepare(
            problem=problem,
            panel=panel,
            coordinator=coordinator,
            provider=provider,
            model=model,
            selector_strategy=selector_strategy,
            max_rounds=max_rounds,
            max_total_tokens=max_total_tokens,
            max_total_cost_usd=max_total_cost_usd,
            max_wallclock_seconds=max_wallclock_seconds,
            fake_script_path=fake_script_path,
            selector_fake_script_path=selector_fake_script_path,
            output_dir=output_dir,
        )
        artifact = run_session(
            config,
            providers,
            runs_root=output_dir,
            selector_providers=selector_providers,
        )
        return _build_result(artifact, run_dir, panel_ids)
    except (UnknownProviderError, MissingCredentialsError) as exc:
        return _error(exc)
    except Exception as exc:  # noqa: BLE001 — every failure is a structured result
        return _error(exc)


@mcp.tool()
async def deliberate_streaming(
    problem: str,
    *,
    panel: Optional[List[str]] = None,
    coordinator: str = "coordinator",
    provider: str = "cli-auto",
    model: Optional[str] = None,
    selector_strategy: str = "fixed",
    max_rounds: int = 4,
    max_total_tokens: int = 100000,
    max_total_cost_usd: float = 5.0,
    max_wallclock_seconds: int = 300,
    fake_script_path: Optional[str] = None,
    selector_fake_script_path: Optional[str] = None,
    output_dir: str = "runs",
    ctx: Context,
) -> Dict[str, Any]:
    """Like `deliberate`, but stream each turn live as the panel produces it.

    Same arguments and same final return value as `deliberate`. The
    difference is that while the deliberation runs, every transcript
    message (each agent turn, each coordinator verdict, the final
    synthesis) is pushed to the MCP client *as it is appended to the
    run journal* — as a log notification (`ctx.info`, carrying the
    speaker / type / a text preview) plus a numeric progress tick
    (`ctx.report_progress`). This lets a Claude client follow the
    discussion as it evolves instead of waiting for the whole session.

    The streaming is read-only over the persisted journal: it changes
    nothing about the deliberation, `transcript_digest`, replay, or
    metrics. On any failure the streamed events stop and the final
    return value is the structured `{"error": ...}` (the transport
    never crashes).
    """
    events_q: "queue.Queue[Dict[str, Any]]" = queue.Queue()
    _DONE = {"event": "__done__"}

    def _producer() -> None:
        try:
            for ev in stream_deliberation(
                problem,
                panel=panel,
                coordinator=coordinator,
                provider=provider,
                model=model,
                selector_strategy=selector_strategy,
                max_rounds=max_rounds,
                max_total_tokens=max_total_tokens,
                max_total_cost_usd=max_total_cost_usd,
                max_wallclock_seconds=max_wallclock_seconds,
                fake_script_path=fake_script_path,
                selector_fake_script_path=selector_fake_script_path,
                output_dir=output_dir,
            ):
                events_q.put(ev)
        except Exception as exc:  # noqa: BLE001 — defensive; generator already wraps
            events_q.put({"event": "error", "error": _error(exc)["error"]})
        finally:
            events_q.put(_DONE)

    worker = threading.Thread(target=_producer, daemon=True)
    worker.start()

    loop = asyncio.get_running_loop()
    final: Optional[Dict[str, Any]] = None
    while True:
        # Block off the event loop on the thread-safe queue without busy-waiting.
        ev = await loop.run_in_executor(None, events_q.get)
        if ev is _DONE:
            break
        kind = ev.get("event")
        if kind == "message":
            await ctx.info(ev["line"])
            try:
                await ctx.report_progress(progress=float(ev["index"]), total=None)
            except Exception:  # noqa: BLE001 — progress is best-effort
                pass
        elif kind == "result":
            final = ev["result"]
        elif kind == "error":
            final = {"error": ev["error"]}

    worker.join(timeout=5)
    return final if final is not None else {
        "error": "RuntimeError: streaming deliberation produced no result"
    }


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


@mcp.tool()
def generate_persona(
    need: str, *, persona_class: str = "domain", prefer_cli: str = "claude"
) -> Dict[str, Any]:
    """Design a new expert `Persona` for a capability gap and return it.

    Asks an installed terminal CLI (`claude` preferred, `codex` fallback)
    to design ONE persona whose output is constrained to the `Persona`
    JSON Schema, then validates it. Returns
    ``{"persona": <persona dict>}`` or ``{"error": ...}``. The returned
    persona can be used as a `panel` member or fed to `deliberate_adaptive`.
    """
    try:
        caller = make_cli_persona_caller(prefer=prefer_cli)
        persona = _generate_persona(need, caller=caller, persona_class=persona_class)
        return {"persona": persona.model_dump(mode="json", exclude_none=True)}
    except Exception as exc:  # noqa: BLE001 — structured result, never crash
        return _error(exc)


@mcp.tool()
def deliberate_adaptive(
    problem: str,
    *,
    panel: Optional[List[str]] = None,
    coordinator: str = "coordinator",
    provider: str = "cli-auto",
    experts: Optional[List[str]] = None,
    max_expansions: int = 2,
    max_rounds: int = 4,
    max_total_tokens: int = 100000,
    max_total_cost_usd: float = 5.0,
    max_wallclock_seconds: int = 300,
    output_dir: str = "runs",
) -> Dict[str, Any]:
    """Deliberate with dynamic agent generation — early-start AND runtime.

    Two ways a *new* expert agent joins the panel, both host-orchestrated
    over the frozen runtime (no spec / schema changes):

      * **early-start** — for each capability in `experts` (free-text
        needs), a domain `Persona` is generated and added to the panel
        *before* the first session.
      * **runtime** — if a session terminates asking for help
        (`user_input_required` / `external_research_required`), a persona
        is generated for that need and the deliberation **continues** in a
        fresh session with the augmented panel, up to `max_expansions`.

    Returns ``{final, sessions, generated_agents, expansions, panel_final}``
    where `final` is the last session's summary (same shape as
    `deliberate`), `generated_agents` lists who was created and in which
    phase, and `panel_final` is the panel after all expansions. On a setup
    failure returns ``{"error": ...}``.
    """
    try:
        caller = make_cli_persona_caller()
        return _run_adaptive(
            problem=problem, panel=panel, coordinator=coordinator, provider=provider,
            experts=experts, max_expansions=max_expansions, max_rounds=max_rounds,
            max_total_tokens=max_total_tokens, max_total_cost_usd=max_total_cost_usd,
            max_wallclock_seconds=max_wallclock_seconds, output_dir=output_dir,
            persona_caller=caller,
        )
    except Exception as exc:  # noqa: BLE001 — structured result, never crash
        return _error(exc)


# ---------------------------------------------------------------------------
# Adaptive orchestration core (dynamic agent generation; host-side, frozen
# runtime). `persona_caller` and `run_one` are injectable so tests never
# spawn a CLI or a real session.
# ---------------------------------------------------------------------------


def _run_adaptive(
    *,
    problem: str,
    panel: Optional[List[str]],
    coordinator: str,
    provider: str,
    experts: Optional[List[str]],
    max_expansions: int,
    max_rounds: int,
    max_total_tokens: int,
    max_total_cost_usd: float,
    max_wallclock_seconds: int,
    output_dir: str,
    persona_caller,
    run_one=None,
) -> Dict[str, Any]:
    """Core adaptive loop. `run_one(config) -> (artifact, result_dict)`."""
    runner = run_one or (lambda cfg: _default_adaptive_run_one(cfg, provider, output_dir))
    panel_ids = list(panel) if panel else list(_DEFAULT_PANEL_IDS)
    add_provider = "claude-cli" if provider == "cli-auto" else provider
    add_model = "sonnet" if provider in ("cli-auto", "claude-cli") else _default_model(provider)

    config = _build_config(
        problem=problem, session_id="adaptive-seed", panel_ids=panel_ids,
        coordinator_id=coordinator,
        provider="claude-cli" if provider == "cli-auto" else provider,
        model="sonnet" if provider == "cli-auto" else _default_model(provider),
        selector_strategy="fixed", max_rounds=max_rounds,
        max_total_tokens=max_total_tokens, max_total_cost_usd=max_total_cost_usd,
        max_wallclock_seconds=max_wallclock_seconds,
    )
    existing = {a.id for a in config.agents} | {config.coordinator.id}
    generated: List[Dict[str, Any]] = []

    # --- early-start expansion ------------------------------------------------
    for need in (experts or []):
        persona = _generate_persona(need, caller=persona_caller, existing_ids=existing)
        config = _add_persona(config, persona, provider=add_provider, model=add_model)
        existing.add(persona.id)
        generated.append({"id": persona.id, "need": need, "phase": "early_start"})

    # --- run + runtime expansion ---------------------------------------------
    sessions: List[Dict[str, Any]] = []
    expansions = 0
    while True:
        cfg = config.model_copy(update={"session_id": f"mcp-adaptive-{uuid.uuid4().hex}"})
        artifact, result = runner(cfg)
        sessions.append(result)
        if artifact.outcome.kind == "synthesis":
            break
        need = _pending_need(artifact)
        if need is None or expansions >= max_expansions:
            break
        persona = _generate_persona(need, caller=persona_caller, existing_ids=existing)
        config = _add_persona(config, persona, provider=add_provider, model=add_model)
        existing.add(persona.id)
        generated.append({"id": persona.id, "need": need, "phase": "runtime"})
        config = _augment_problem(config, prior=result, need=need)
        expansions += 1

    return {
        "final": sessions[-1],
        "sessions": sessions,
        "generated_agents": generated,
        "expansions": expansions,
        "panel_final": [a.id for a in config.agents],
    }


def _add_persona(config: Config, persona: Persona, *, provider: str, model: str) -> Config:
    """Return a copy of `config` with `persona` added as a panel agent."""
    agent = AgentConfig(id=persona.id, persona_ref=persona, provider=provider, model=model)
    agents = list(config.agents) + [agent]
    panel = list(config.selector.default_deliberation_panel) + [persona.id]
    selector = config.selector.model_copy(update={"default_deliberation_panel": panel})
    return config.model_copy(update={"agents": agents, "selector": selector})


def _pending_need(artifact: Artifact) -> Optional[str]:
    """The 'we need help with X' text from an expansion-triggering termination."""
    outcome = artifact.outcome
    if outcome.kind != "termination":
        return None
    ta = outcome.termination_artifact
    if ta.reason == "user_input_required" and ta.pending_user_input_request is not None:
        return ta.pending_user_input_request.question
    if ta.reason == "external_research_required" and ta.pending_external_research_request is not None:
        return ta.pending_external_research_request.query
    return None


def _augment_problem(config: Config, *, prior: Dict[str, Any], need: str) -> Config:
    """Carry the prior session's outcome forward so the continuation has context."""
    prior_outcome = prior.get("synthesis_answer") or prior.get("termination_reason") or "(none)"
    addition = (
        f"\n\n[CONTINUATION] A new expert has joined the panel to address: {need}. "
        f"Prior deliberation outcome: {prior_outcome} "
        "Continue the deliberation, integrating the new expertise."
    )
    return config.model_copy(update={"problem_statement": config.problem_statement + addition})


def _default_adaptive_run_one(config: Config, provider: str, output_dir: str):
    """Build providers for `config`, run one session, return (artifact, result)."""
    if provider == "cli-auto":
        from symposium.integrations.cli_routing import route_cli_providers

        rc, providers = route_cli_providers(config)
    else:
        rc = config
        providers = default_registry().build_session_providers(config)
    artifact = run_session(rc, providers, runs_root=output_dir)
    result = _build_result(
        artifact, Path(output_dir) / config.session_id, [a.id for a in rc.agents]
    )
    return artifact, result


# ---------------------------------------------------------------------------
# Streaming core (sync generator; the async MCP tool bridges it to a Context)
# ---------------------------------------------------------------------------


def stream_deliberation(
    problem: str,
    *,
    panel: Optional[List[str]] = None,
    coordinator: str = "coordinator",
    provider: str = "cli-auto",
    model: Optional[str] = None,
    selector_strategy: str = "fixed",
    max_rounds: int = 4,
    max_total_tokens: int = 100000,
    max_total_cost_usd: float = 5.0,
    max_wallclock_seconds: int = 300,
    fake_script_path: Optional[str] = None,
    selector_fake_script_path: Optional[str] = None,
    output_dir: str = "runs",
    poll_interval: float = 0.05,
) -> Iterator[Dict[str, Any]]:
    """Run a deliberation and yield live events as turns are produced.

    Synchronous generator (the testable core behind `deliberate_streaming`).
    Runs `run_session(...)` in a worker thread and tails the run's
    append-only `transcript.jsonl` (the runtime writes it line-buffered,
    one JSON message per line), yielding:

      * ``{"event": "message", "index": int, "message": <compact dict>,
        "line": <human preview str>}`` — once per transcript message, in
        order, as it is appended;
      * ``{"event": "result", "result": <same dict deliberate returns>}``
        — once, at the end of a successful run;
      * ``{"event": "error", "error": "<kind>: <message>"}`` — on any
        failure (build error, provider failure). A budget / selector
        *termination* is NOT an error: it surfaces in the final
        ``result`` with a ``termination_reason``.

    Reads only the persisted journal; it never touches the deliberation
    semantics, the digest, replay, or metrics.
    """
    try:
        config, providers, selector_providers, run_dir, panel_ids = _prepare(
            problem=problem,
            panel=panel,
            coordinator=coordinator,
            provider=provider,
            model=model,
            selector_strategy=selector_strategy,
            max_rounds=max_rounds,
            max_total_tokens=max_total_tokens,
            max_total_cost_usd=max_total_cost_usd,
            max_wallclock_seconds=max_wallclock_seconds,
            fake_script_path=fake_script_path,
            selector_fake_script_path=selector_fake_script_path,
            output_dir=output_dir,
        )
    except (UnknownProviderError, MissingCredentialsError) as exc:
        yield {"event": "error", "error": _error(exc)["error"]}
        return
    except Exception as exc:  # noqa: BLE001 — structured error event, no crash
        yield {"event": "error", "error": _error(exc)["error"]}
        return

    journal = run_dir / "transcript.jsonl"
    result_box: Dict[str, Artifact] = {}
    error_box: Dict[str, Exception] = {}

    def _worker() -> None:
        try:
            result_box["artifact"] = run_session(
                config,
                providers,
                runs_root=output_dir,
                selector_providers=selector_providers,
            )
        except Exception as exc:  # noqa: BLE001 — surfaced as an error event below
            error_box["exc"] = exc

    worker = threading.Thread(target=_worker, daemon=True)
    worker.start()

    tail = _JournalTail(journal)
    index = 0
    while worker.is_alive():
        for msg in tail.drain():
            index += 1
            yield _message_event(index, msg)
        time.sleep(poll_interval)
    # Final drain: the thread has finished, so the journal is complete.
    for msg in tail.drain():
        index += 1
        yield _message_event(index, msg)
    worker.join()

    if "exc" in error_box:
        yield {"event": "error", "error": _error(error_box["exc"])["error"]}
        return
    artifact = result_box.get("artifact")
    if artifact is None:  # pragma: no cover — worker always sets one of the boxes
        yield {"event": "error", "error": "RuntimeError: run_session produced no artifact"}
        return
    yield {"event": "result", "result": _build_result(artifact, run_dir, panel_ids)}


class _JournalTail:
    """Incremental reader for a line-delimited `transcript.jsonl`.

    Tracks a seek cookie and a partial trailing line so each `drain()`
    returns only the message dicts appended since the last call.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._offset = 0
        self._pending = ""

    def drain(self) -> List[Dict[str, Any]]:
        if not self._path.exists():
            return []
        with open(self._path, "r", encoding="utf-8") as fp:
            fp.seek(self._offset)
            chunk = fp.read()
            self._offset = fp.tell()
        if not chunk:
            return []
        self._pending += chunk
        lines = self._pending.split("\n")
        self._pending = lines.pop()  # trailing partial (or "") survives to next drain
        out: List[Dict[str, Any]] = []
        for line in lines:
            if not line.strip():
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:  # pragma: no cover — line-buffered writes are atomic
                continue
        return out


def _message_event(index: int, msg: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "event": "message",
        "index": index,
        "message": _compact_message(msg),
        "line": _preview_line(msg),
    }


def _compact_message(msg: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "round": msg.get("round"),
        "turn_index": msg.get("turn_index"),
        "speaker": msg.get("speaker"),
        "type": msg.get("type"),
        "preview": _message_preview(msg),
    }


def _preview_line(msg: Dict[str, Any]) -> str:
    return (
        f"[r{msg.get('round')}/t{msg.get('turn_index')}] "
        f"{msg.get('speaker')} · {msg.get('type')}: {_message_preview(msg)}"
    )


_PREVIEW_MAX = 280


def _message_preview(msg: Dict[str, Any]) -> str:
    """A short, human-readable preview of a transcript message's content."""
    mtype = msg.get("type")
    content = msg.get("content")
    text: str
    if mtype == "problem_statement":
        text = content if isinstance(content, str) else str(content)
    elif mtype in ("primary_turn", "branch_turn"):
        text = content.get("text", "") if isinstance(content, dict) else str(content)
    elif mtype == "coordination_turn":
        if isinstance(content, dict):
            text = f"next_action={content.get('next_action')} — {content.get('rationale', '')}"
        else:
            text = str(content)
    elif mtype == "synthesis":
        text = content.get("integrated_answer", "") if isinstance(content, dict) else str(content)
    elif mtype == "panel_contraction":
        if isinstance(content, dict):
            text = f"{content.get('agent_id')} dropped ({content.get('reason')})"
        else:
            text = str(content)
    else:  # pragma: no cover — MessageType is a closed enum
        text = str(content)
    text = " ".join(text.split())  # collapse whitespace/newlines for a clean one-liner
    if len(text) > _PREVIEW_MAX:
        text = text[: _PREVIEW_MAX - 1].rstrip() + "…"
    return text


# ---------------------------------------------------------------------------
# Helpers (mirror the CLI's _load_config flow; no runtime changes)
# ---------------------------------------------------------------------------


def _prepare(
    *,
    problem: str,
    panel: Optional[List[str]],
    coordinator: str,
    provider: str,
    model: Optional[str],
    selector_strategy: str,
    max_rounds: int,
    max_total_tokens: int,
    max_total_cost_usd: float,
    max_wallclock_seconds: int,
    fake_script_path: Optional[str],
    selector_fake_script_path: Optional[str],
    output_dir: str,
) -> Tuple[Config, Dict[str, Any], Optional[Dict[str, Any]], Path, List[str]]:
    """Build the Config, providers, optional selector providers, run dir, panel.

    Shared by `deliberate` and `stream_deliberation` so both produce a
    byte-identical run. Mirrors the CLI's args→Config→providers flow.
    """
    panel_ids = list(panel) if panel else list(_DEFAULT_PANEL_IDS)
    resolved_model = model or _default_model(provider)
    session_id = f"mcp-{uuid.uuid4().hex}"

    config = _build_config(
        problem=problem,
        session_id=session_id,
        panel_ids=panel_ids,
        coordinator_id=coordinator,
        # "cli-auto" is a host-side routing sentinel, not a registered
        # provider id — build with a concrete placeholder the router then
        # rewrites per agent. Any other value is the real provider id.
        provider="claude-cli" if provider == "cli-auto" else provider,
        model=("sonnet" if provider == "cli-auto" else resolved_model),
        selector_strategy=selector_strategy,
        max_rounds=max_rounds,
        max_total_tokens=max_total_tokens,
        max_total_cost_usd=max_total_cost_usd,
        max_wallclock_seconds=max_wallclock_seconds,
    )

    # cli-auto: route each agent to claude-cli / codex-cli per persona, with
    # installed-CLI fallback (§ per-persona routing). Rewrites the Config so
    # the persisted run + metrics reflect the CLI that actually answered.
    if provider == "cli-auto":
        from symposium.integrations.cli_routing import route_cli_providers

        routed_config, providers = route_cli_providers(config)
        run_dir = Path(output_dir) / session_id
        return routed_config, providers, None, run_dir, panel_ids

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
    selector_providers: Optional[Dict[str, Any]] = None
    if selector_strategy == "llm" and provider == "fake":
        if not selector_fake_script_path:
            raise ValueError(
                'selector_strategy="llm" with provider="fake" requires '
                "selector_fake_script_path"
            )
        sel_fp = FakeProvider(script=_load_script(selector_fake_script_path))
        selector_providers = {"default": sel_fp}

    run_dir = Path(output_dir) / session_id
    return config, providers, selector_providers, run_dir, panel_ids


def _build_result(artifact: Artifact, run_dir: Path, panel_ids: List[str]) -> Dict[str, Any]:
    """The `deliberate` / streaming final result dict (§1 done-criteria shape)."""
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
    if provider in ("fake",):
        return "fake-deterministic"
    if provider in ("claude-cli", "cli-auto"):
        # Model alias understood by the `claude` CLI (no API key needed).
        return "sonnet"
    if provider == "codex-cli":
        # Sentinel → CodexCliProvider omits -m and uses the CLI's default model.
        return "auto"
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

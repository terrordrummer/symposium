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

  * ``deliberate``                    — DEFAULT. Build a Config from
    arguments, run one session, and stream each transcript message live
    (``ctx.info`` / ``ctx.report_progress``) as it is produced; the final
    return carries outcome + synthesis answer (or termination reason) and
    a compact run summary.
  * ``deliberate_muted``              — same as ``deliberate`` but with NO
    live streaming: one synchronous result returned when the session ends.
  * ``deliberate_adaptive``           — DEFAULT adaptive. Adds dynamic agent
    generation (early-start + runtime) over multiple linked sessions, with
    live streaming.
  * ``deliberate_adaptive_muted``     — adaptive without live streaming.
  * ``get_run_summary``               — load a persisted run, recompute
    §7.9 metrics, verify the §7.5 transcript replay, return the summary.
  * ``get_run_status``                — read transcript progressively
    while a run is still active (polling-friendly, v1.10.9+).
  * ``get_version``                   — runtime introspection: package
    version, package_path, CLI versions, cli_auto routing, budget
    defaults (v1.10.5+).
  * ``list_personas``                 — the six built-in personas (R3
    default panel + coordinator).
  * ``generate_persona``              — design ONE new domain expert
    persona from a free-text capability need.

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
    RuntimeConfig,
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
from symposium.storage.writer import _is_stale_lock

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

# Streaming caps: bound producer/consumer queue and producer-side join window
# so a slow / disappeared MCP client cannot blow up server memory or hang the
# tool indefinitely.
_STREAM_QUEUE_MAXSIZE = 64
_STREAM_PUT_TIMEOUT_SECONDS = 30.0
_STREAM_JOIN_TIMEOUT_SECONDS = 10.0
# Consumer-side liveness check: how long to wait before noticing a dead
# producer that never delivered the sentinel.
_STREAM_GET_TIMEOUT_SECONDS = 30.0

# Dynamic-agent generation caps. `max_expansions` already bounds runtime
# expansions; `_MAX_EARLY_START_EXPERTS` bounds the early-start path (and the
# implicit cost of N persona-generation CLI calls before the first session).
# `_MAX_NEED_LENGTH_CHARS` bounds each free-text need passed to the persona
# generator — protects against pathological prompts coming over MCP.
_MAX_EARLY_START_EXPERTS = 8
_MAX_NEED_LENGTH_CHARS = 4096
# Server-side ceiling on runtime expansions, regardless of the caller's
# `max_expansions` value. Protects the MCP host from a misbehaving client
# requesting hundreds of cascading sessions.
_MAX_RUNTIME_EXPANSIONS = 5


def _put_sentinel(q: "queue.Queue[Dict[str, Any]]", sentinel: Dict[str, Any]) -> None:
    """Ensure a sentinel reaches the consumer even if the queue is saturated.

    A short drain-and-retry loop with a hard upper bound: if a slow client
    has filled the queue with un-consumed messages, drop the oldest entries
    to make room for the sentinel rather than wedge the producer forever.
    """
    for _ in range(_STREAM_QUEUE_MAXSIZE + 4):
        try:
            q.put_nowait(sentinel)
            return
        except queue.Full:
            try:
                q.get_nowait()  # drop one stale event to make room
            except queue.Empty:
                pass
    # Last resort: blocking put with a finite timeout. If the consumer is
    # truly dead, the consumer-side timeout (`_STREAM_GET_TIMEOUT_SECONDS`)
    # will notice the dead worker independently.
    try:
        q.put(sentinel, timeout=_STREAM_PUT_TIMEOUT_SECONDS)
    except queue.Full:
        pass

mcp = FastMCP("symposium")


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


@mcp.tool(name="deliberate_muted")
def deliberate(
    problem: str,
    *,
    panel: Optional[List[str]] = None,
    coordinator: str = "coordinator",
    provider: str = "cli-auto",
    model: Optional[str] = None,
    selector_strategy: str = "fixed",
    max_rounds: int = 4,
    max_total_tokens: int = 100_000_000,
    max_total_cost_usd: float = 1000.0,
    max_wallclock_seconds: int = 3600,
    per_agent_token_budget: Optional[Dict[str, int]] = None,
    fake_script_path: Optional[str] = None,
    selector_fake_script_path: Optional[str] = None,
    output_dir: str = "runs",
) -> Dict[str, Any]:
    """Non-streaming Symposium deliberation — one final result, no live events.

    Muted variant, registered as the MCP tool ``deliberate_muted``:
    identical arguments and return value to the streaming default
    ``deliberate``, but it pushes no per-turn events and returns only once
    the whole session is done. Prefer ``deliberate`` for interactive use.

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
            "opus"; codex-cli: the CLI's own default; Anthropic: the
            example-config model; OpenAI: a sane default; fake:
            "fake-deterministic"). Ignored under "cli-auto" (the router
            stamps a model per chosen CLI).
        selector_strategy: §4.1 selector — "fixed" (default), "rules"
            (deterministic persona-metadata match, no provider call), or
            "llm" (one bounded provider call; needs `selector_fake_script_path`
            under provider="fake").
        max_rounds, max_total_tokens, max_total_cost_usd,
        max_wallclock_seconds: §4.7 hard caps. **Under `cli-auto` (the
            default), `max_total_tokens` (100M) and `max_total_cost_usd`
            ($1000) are telemetry canaries, NOT real quota** — codex CLI
            reports cost=0 (subscription, not metered), and Claude's
            `cost_usd` is API-equivalent reference, not a bill. The real
            hard caps under `cli-auto` are `max_wallclock_seconds`
            (default 1800s) and your subscription rate-limit window.
            Lower the token/cost caps explicitly when forcing
            `provider="anthropic"` / `"openai"` where every token is a
            billable charge.
        per_agent_token_budget: optional per-persona token cap, eg.
            `{"logician": 200_000}`. Useful under `cli-auto` as a hard
            per-agent canary even when the global cap is loose.
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
            per_agent_token_budget=per_agent_token_budget,
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


@mcp.tool(name="deliberate")
async def deliberate_streaming(
    problem: str,
    *,
    panel: Optional[List[str]] = None,
    coordinator: str = "coordinator",
    provider: str = "cli-auto",
    model: Optional[str] = None,
    selector_strategy: str = "fixed",
    max_rounds: int = 4,
    max_total_tokens: int = 100_000_000,
    max_total_cost_usd: float = 1000.0,
    max_wallclock_seconds: int = 3600,
    per_agent_token_budget: Optional[Dict[str, int]] = None,
    fake_script_path: Optional[str] = None,
    selector_fake_script_path: Optional[str] = None,
    output_dir: str = "runs",
    ctx: Context,
) -> Dict[str, Any]:
    """Default Symposium deliberation — streams each turn live as it is produced.

    The streaming default, registered as the MCP tool ``deliberate``. Same
    arguments and same final return value as the muted variant
    ``deliberate_muted``. The
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
    # Bounded queue: producers may be faster than the MCP transport;
    # an unbounded queue is a memory growth vector against slow clients.
    # 64 events buffer ≫ peak per-round message count under realistic caps.
    events_q: "queue.Queue[Dict[str, Any]]" = queue.Queue(maxsize=_STREAM_QUEUE_MAXSIZE)
    _DONE = {"event": "__done__"}
    stop_event = threading.Event()

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
                per_agent_token_budget=per_agent_token_budget,
                fake_script_path=fake_script_path,
                selector_fake_script_path=selector_fake_script_path,
                output_dir=output_dir,
            ):
                if stop_event.is_set():
                    # Client cancelled; stop publishing. The deliberation may
                    # still be running in the scheduler — it will bound itself
                    # via its own caps (max_wallclock_seconds, etc.).
                    return
                try:
                    events_q.put(ev, timeout=_STREAM_PUT_TIMEOUT_SECONDS)
                except queue.Full:
                    # Consumer (this async tool) has stalled; treat as cancel.
                    stop_event.set()
                    return
        except Exception as exc:  # noqa: BLE001 — defensive; generator already wraps
            try:
                events_q.put(
                    {"event": "error", "error": _error(exc)["error"]},
                    timeout=_STREAM_PUT_TIMEOUT_SECONDS,
                )
            except queue.Full:
                pass
        finally:
            # The sentinel MUST reach the consumer; otherwise the consumer
            # blocks on `events_q.get` forever. We use an unbounded retry
            # loop (with short drains if the queue is full) so a slow client
            # cannot wedge the tool. The producer is wrapping up anyway.
            _put_sentinel(events_q, _DONE)

    worker = threading.Thread(target=_producer, daemon=True)
    worker.start()

    final: Optional[Dict[str, Any]] = None
    _SENTINEL_EMPTY = object()

    def _blocking_get_with_timeout():
        # The timeout MUST be inside the blocking call. Wrapping
        # `events_q.get` in `asyncio.wait_for` would cancel the awaiting
        # coroutine but leave the underlying blocking getter alive in the
        # executor thread, where it could later consume a real event or
        # the `_DONE` sentinel out of band — orphaned getters accumulate
        # and ordering is lost.
        try:
            return events_q.get(timeout=_STREAM_GET_TIMEOUT_SECONDS)
        except queue.Empty:
            return _SENTINEL_EMPTY

    try:
        while True:
            ev = await asyncio.to_thread(_blocking_get_with_timeout)
            if ev is _SENTINEL_EMPTY:
                if not worker.is_alive():
                    # Producer thread died without delivering `_DONE`.
                    final = final or {"error": "RuntimeError: streaming worker exited without sentinel"}
                    break
                continue
            if ev is _DONE:
                break
            kind = ev.get("event")
            if kind == "message":
                await ctx.info(ev["line"])
                try:
                    # Route the turn preview into the progress `message` too:
                    # some MCP clients (e.g. Claude Code) render the progress
                    # message inline next to the counter but collapse/hide the
                    # `ctx.info` log notifications — so without this the live
                    # text is invisible and only the bare tick ("Processing… N")
                    # shows. `ev["line"]` is already a bounded (~280-char)
                    # preview, safe to send as a one-line progress message.
                    await ctx.report_progress(
                        progress=float(ev["index"]), total=None, message=ev["line"]
                    )
                except Exception:  # noqa: BLE001 — progress is best-effort
                    pass
            elif kind == "result":
                final = ev["result"]
            elif kind == "error":
                final = {"error": ev["error"]}
    except asyncio.CancelledError:
        # Client cancelled the streaming tool invocation. Signal the producer
        # to stop publishing and wait briefly for it to wind down. The
        # underlying scheduler is not currently cancellable mid-call; it will
        # complete or hit its own wallclock cap. This is a documented gap.
        stop_event.set()
        raise
    finally:
        stop_event.set()
        worker.join(timeout=_STREAM_JOIN_TIMEOUT_SECONDS)

    return final if final is not None else {
        "error": "RuntimeError: streaming deliberation produced no result"
    }


@mcp.tool()
def get_run_status(
    run_dir: str, *, since_index: int = 0, limit: int = 20,
) -> Dict[str, Any]:
    """Read transcript messages from a (possibly still-running) deliberation.

    Designed for **polling during a long-running deliberation** so an
    agent can show the panel's dialogue live in chat without depending
    on MCP `notifications/message` rendering (which some clients hide
    or render minimally). Typical loop::

        s = get_run_status(run_dir)
        # display s["messages"]
        while s["run_active"]:
            time.sleep(5)
            s = get_run_status(run_dir, since_index=s["next_index"])
            # display NEW s["messages"]

    Args:
        run_dir: path to the run directory (the `run_dir` field
            returned by `deliberate*` MCP tools).
        since_index: skip the first N transcript entries. Set to the
            previous call's ``next_index`` to fetch only new turns.
        limit: max messages to return in this call (default 20).
            Larger requests are clamped silently to keep responses
            bounded. The remaining-message count is returned so the
            caller knows whether to drain more.

    Returns:
        On success::

            {
              "messages": [
                {"index": int, "speaker": str, "type": str, "round": int,
                 "turn_index": int, "text": str, "timestamp": str},
                ...
              ],
              "next_index": int,        # pass as since_index next call
              "remaining": int,         # entries beyond next_index NOT returned
              "run_active": bool,       # .lock present AND PID still alive
              "lock_stale": bool,       # .lock present but PID is dead
              "total_so_far": int,      # transcript line count at read time
            }

        On any error: ``{"error": "<kind>: <message>"}``.

    Read-only: never blocks the running deliberation, never mutates
    the run directory. Safe to call from any client.
    """
    try:
        rd = Path(run_dir)
        transcript = rd / "transcript.jsonl"
        if not transcript.exists():
            raise FileNotFoundError(f"no transcript.jsonl under {rd}")

        # Clamp limit defensively — clients (especially LLM-driven ones)
        # occasionally pass nonsense like limit=10000. 100 is a generous
        # ceiling that still fits in a typical MCP response budget.
        effective_limit = max(1, min(int(limit), 100))
        effective_since = max(0, int(since_index))

        messages: List[Dict[str, Any]] = []
        total = 0
        with open(transcript, "r", encoding="utf-8") as f:
            for idx, line in enumerate(f):
                total = idx + 1
                if idx < effective_since:
                    continue
                if len(messages) >= effective_limit:
                    # Don't break — keep counting `total` so the caller
                    # sees how many entries exist beyond what we returned.
                    continue
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                c = d.get("content", {})
                if isinstance(c, dict):
                    # primary_turn / branch_turn carry `.text`; coordination_turn
                    # (Verdict) doesn't have `.text` — its readable content is
                    # `rationale` + `focus`; synthesis (SynthesisContent) carries
                    # `integrated_answer`. Pre-T7 fallback to `c.get("text","")`
                    # silently dropped coordinator + synthesis turns. (Codex
                    # review T7 #6.)
                    text = ""
                    for key in ("text", "integrated_answer", "rationale", "focus"):
                        v = c.get(key)
                        if isinstance(v, str) and v:
                            text = v
                            break
                    if not text:
                        # last resort — render the whole content dict so the
                        # caller sees SOMETHING instead of an empty turn
                        text = json.dumps(c, ensure_ascii=False)[:2000]
                elif isinstance(c, str):
                    text = c
                else:
                    text = ""
                messages.append({
                    "index": idx,
                    "speaker": d.get("speaker"),
                    "type": d.get("type"),
                    "round": d.get("round"),
                    "turn_index": d.get("turn_index"),
                    "text": text,
                    "timestamp": d.get("timestamp"),
                })

        # `next_index` derived from the LAST returned message's index +1
        # (Codex review T7 #4). Avoids the buggy `effective_since + len()`
        # which over-advanced past skipped-malformed lines: if a partial
        # write was skipped, the caller re-reading from that position
        # would re-fetch already-seen lines that had been counted but
        # not returned. Anchoring to the physical index of the last
        # returned line is both correct and idempotent.
        next_index = messages[-1]["index"] + 1 if messages else effective_since
        remaining = max(0, total - next_index)
        # `.lock` present is necessary but NOT sufficient for "still
        # running": a crashed RunWriter leaves the lock orphan, and a
        # polling agent would loop forever (Codex T7 #2). Use the
        # writer's own staleness check — same logic the writer applies
        # before reclaiming a lock (PID-alive probe).
        lock_path = rd / ".lock"
        lock_present = lock_path.exists()
        lock_stale = lock_present and _is_stale_lock(lock_path)
        run_active = lock_present and not lock_stale

        return {
            "messages": messages,
            "next_index": next_index,
            "remaining": remaining,
            "run_active": run_active,
            "lock_stale": lock_stale,
            "total_so_far": total,
        }
    except Exception as exc:  # noqa: BLE001 — structured error, never crash
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
            ta = artifact.outcome.termination_artifact
            result["termination_reason"] = ta.reason
            if ta.last_provider_failure is not None:
                result["last_provider_failure"] = ta.last_provider_failure.model_dump(
                    mode="json", exclude_none=True,
                )
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
def get_version() -> Dict[str, Any]:
    """Return the running MCP server's version + provenance + key defaults.

    Diagnostic introspection so an operator (or LLM agent) can verify
    *what code is actually executing right now*, not what `pip show`
    claims is installed on disk. Useful when a respawn is needed to
    pick up a new version, or when investigating why a tool's
    behavior doesn't match the docs.

    Returns::

        {
          "version": "<package __version__>",
          "schema_version": "<protocol SCHEMA_VERSION>",
          "pid": <int>,
          "python": "<sys.executable>",
          "package_path": "<dir containing symposium/__init__.py>",
          "mcp_server_module": "<path to mcp_server.py>",
          "mcp_server_mtime": "<ISO timestamp of mcp_server.py>",
          "git_commit": "<short sha>" | null,
          "clis": {"claude": "<version>"|null, "codex": "<version>"|null},
          "cli_auto_routing": {"<persona_id>": "<cli>", ...},
          "budget_defaults": {
              "max_total_tokens": <int>,
              "max_total_cost_usd": <float>,
              "max_rounds": <int>,
              "max_wallclock_seconds": <int>
          }
        }
    """
    import inspect
    import os
    import shutil
    import subprocess
    import sys
    from pathlib import Path
    from datetime import datetime, timezone

    import symposium as _symposium
    from symposium import SCHEMA_VERSION as _SCHEMA_VERSION

    pkg_init = Path(_symposium.__file__).resolve()
    pkg_dir = pkg_init.parent
    server_module = Path(__file__).resolve()
    server_mtime = datetime.fromtimestamp(
        server_module.stat().st_mtime, tz=timezone.utc
    ).isoformat()

    # Pull budget defaults straight from the canonical signature
    # (`deliberate_adaptive_streaming`) — guarantees what we report
    # here is exactly what every `deliberate*` tool will use when the
    # caller omits a value.
    sig = inspect.signature(deliberate_adaptive_streaming)
    budget_defaults = {
        name: sig.parameters[name].default
        for name in (
            "max_total_tokens",
            "max_total_cost_usd",
            "max_rounds",
            "max_wallclock_seconds",
        )
    }

    # Best-effort git commit. We're a published package — most installs
    # are NOT inside a git repo, so the repo lookup falls back to None
    # without raising. Useful when running against an editable install
    # from the source tree (eg. during development): pinpoints the
    # commit the live server is on, even if the disk version says
    # "1.10.x".
    git_commit: Optional[str] = None
    try:
        # Look for .git starting from the package dir upward.
        candidate = pkg_dir
        for _ in range(6):  # bounded walk
            if (candidate / ".git").exists():
                proc = subprocess.run(
                    ["git", "-C", str(candidate), "rev-parse", "--short=12", "HEAD"],
                    capture_output=True, text=True, timeout=2.0,
                )
                if proc.returncode == 0 and proc.stdout.strip():
                    git_commit = proc.stdout.strip()
                break
            candidate = candidate.parent
            if candidate == candidate.parent:  # filesystem root
                break
    except Exception:  # noqa: BLE001 — diagnostic, never crash get_version
        git_commit = None

    # Which terminal CLIs are actually installed on the server's host,
    # with their --version. This is what tells the operator at a glance
    # that an upcoming `cli-auto` run will (or won't) find the
    # backend(s) it expects to route to. Each probe is bounded to 2s
    # so a hung CLI can't stall the diagnostic.
    def _probe(binary: str) -> Optional[str]:
        path = shutil.which(binary)
        if not path:
            return None
        try:
            proc = subprocess.run(
                [path, "--version"], capture_output=True, text=True, timeout=2.0,
            )
        except Exception:  # noqa: BLE001 — diagnostic, never crash get_version
            return None
        out = (proc.stdout or proc.stderr or "").strip()
        return out.splitlines()[0] if out else None

    clis = {"claude": _probe("claude"), "codex": _probe("codex")}

    # Default `cli-auto` per-persona routing as currently encoded in
    # `cli_routing.DEFAULT_ROUTING` + `DEFAULT_CLI` fallback. Surfacing
    # the matrix here documents the silent policy that v1.10.x bakes in
    # (visionary → codex, everyone else → claude) and helps the
    # operator notice when a code change drifts it without updating
    # the docs.
    try:
        from symposium.integrations.cli_routing import DEFAULT_CLI, DEFAULT_ROUTING
        cli_auto_routing = {
            p.id: DEFAULT_ROUTING.get(p.id, DEFAULT_CLI)
            for p in DEFAULT_PANEL
        }
        cli_auto_routing["coordinator"] = DEFAULT_ROUTING.get("coordinator", DEFAULT_CLI)
    except (ImportError, AttributeError):
        cli_auto_routing = {}

    return {
        "version": _symposium.__version__,
        "schema_version": _SCHEMA_VERSION,
        "pid": os.getpid(),
        "python": sys.executable,
        "package_path": str(pkg_dir),
        "mcp_server_module": str(server_module),
        "mcp_server_mtime": server_mtime,
        "git_commit": git_commit,
        "clis": clis,
        "cli_auto_routing": cli_auto_routing,
        "budget_defaults": budget_defaults,
    }


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


@mcp.tool(name="deliberate_adaptive_muted")
def deliberate_adaptive(
    problem: str,
    *,
    panel: Optional[List[str]] = None,
    coordinator: str = "coordinator",
    provider: str = "cli-auto",
    experts: Optional[List[str]] = None,
    max_expansions: int = 2,
    max_rounds: int = 4,
    max_total_tokens: int = 100_000_000,
    max_total_cost_usd: float = 1000.0,
    max_wallclock_seconds: int = 3600,
    per_agent_token_budget: Optional[Dict[str, int]] = None,
    output_dir: str = "runs",
) -> Dict[str, Any]:
    """Non-streaming adaptive deliberation — dynamic agent generation, no live events.

    Muted variant, registered as the MCP tool ``deliberate_adaptive_muted``:
    same behavior as the streaming default ``deliberate_adaptive`` but it
    returns only the final aggregate result, with no per-turn streaming.

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
        _validate_experts_input(experts)
        # Clamp `max_expansions` to a server-side ceiling. Without this, a
        # client could request hundreds of cascading sessions, each
        # provider-call and persona-generation-call heavy. We apply a hard
        # cap; the caller's value is honored when smaller.
        effective_expansions = min(int(max_expansions), _MAX_RUNTIME_EXPANSIONS)
        if effective_expansions < 0:
            effective_expansions = 0
        caller = make_cli_persona_caller()
        return _run_adaptive(
            problem=problem, panel=panel, coordinator=coordinator, provider=provider,
            experts=experts, max_expansions=effective_expansions, max_rounds=max_rounds,
            max_total_tokens=max_total_tokens, max_total_cost_usd=max_total_cost_usd,
            max_wallclock_seconds=max_wallclock_seconds,
            per_agent_token_budget=per_agent_token_budget,
            output_dir=output_dir,
            persona_caller=caller,
        )
    except Exception as exc:  # noqa: BLE001 — structured result, never crash
        return _error(exc)


@mcp.tool(name="deliberate_adaptive")
async def deliberate_adaptive_streaming(
    problem: str,
    *,
    panel: Optional[List[str]] = None,
    coordinator: str = "coordinator",
    provider: str = "cli-auto",
    experts: Optional[List[str]] = None,
    max_expansions: int = 2,
    max_rounds: int = 4,
    max_total_tokens: int = 100_000_000,
    max_total_cost_usd: float = 1000.0,
    max_wallclock_seconds: int = 3600,
    per_agent_token_budget: Optional[Dict[str, int]] = None,
    output_dir: str = "runs",
    ctx: Context,
) -> Dict[str, Any]:
    """Default adaptive deliberation — dynamic agent generation + live streaming.

    The streaming default, registered as the MCP tool ``deliberate_adaptive``.
    Same arguments / final return shape as the muted variant
    ``deliberate_adaptive_muted``. The
    difference is that each persona generation, session start/end, and
    every per-turn transcript message is pushed live to the MCP client
    via ``ctx.info`` and ``ctx.report_progress`` as the deliberation
    unfolds.

    Streamed events (in order):
      * ``agent_generated`` — one per persona designed (early-start /
        runtime).
      * ``session_start`` — when a new adaptive session begins, with its
        ``session_id`` and the panel composition at that point.
      * per-turn ``message`` — same shape as ``deliberate``.
      * ``session_end`` — when a session completes, with its outcome.

    On any failure the streaming stops and the final return value is the
    structured ``{"error": ...}`` — the transport never crashes.
    """
    events_q: "queue.Queue[Dict[str, Any]]" = queue.Queue(maxsize=_STREAM_QUEUE_MAXSIZE)
    _DONE = {"event": "__done__"}
    stop_event = threading.Event()

    # Clamp max_expansions to the server-side ceiling (same policy as
    # deliberate_adaptive). Negatives → 0.
    effective_expansions = min(int(max_expansions), _MAX_RUNTIME_EXPANSIONS)
    if effective_expansions < 0:
        effective_expansions = 0

    def _producer() -> None:
        try:
            for ev in stream_adaptive(
                problem,
                panel=panel,
                coordinator=coordinator,
                provider=provider,
                experts=experts,
                max_expansions=effective_expansions,
                max_rounds=max_rounds,
                max_total_tokens=max_total_tokens,
                max_total_cost_usd=max_total_cost_usd,
                max_wallclock_seconds=max_wallclock_seconds,
                per_agent_token_budget=per_agent_token_budget,
                output_dir=output_dir,
            ):
                if stop_event.is_set():
                    return
                try:
                    events_q.put(ev, timeout=_STREAM_PUT_TIMEOUT_SECONDS)
                except queue.Full:
                    stop_event.set()
                    return
        except Exception as exc:  # noqa: BLE001 — defensive; generator already wraps
            try:
                events_q.put(
                    {"event": "error", "error": _error(exc)["error"]},
                    timeout=_STREAM_PUT_TIMEOUT_SECONDS,
                )
            except queue.Full:
                pass
        finally:
            _put_sentinel(events_q, _DONE)

    worker = threading.Thread(target=_producer, daemon=True)
    worker.start()

    final: Optional[Dict[str, Any]] = None
    _SENTINEL_EMPTY = object()

    def _blocking_get_with_timeout():
        try:
            return events_q.get(timeout=_STREAM_GET_TIMEOUT_SECONDS)
        except queue.Empty:
            return _SENTINEL_EMPTY

    try:
        while True:
            ev = await asyncio.to_thread(_blocking_get_with_timeout)
            if ev is _SENTINEL_EMPTY:
                if not worker.is_alive():
                    final = final or {"error": "RuntimeError: streaming worker exited without sentinel"}
                    break
                continue
            if ev is _DONE:
                break
            kind = ev.get("event")
            if kind == "message":
                await ctx.info(ev["line"])
                try:
                    # Route the turn preview into the progress `message` too:
                    # some MCP clients (e.g. Claude Code) render the progress
                    # message inline next to the counter but collapse/hide the
                    # `ctx.info` log notifications — so without this the live
                    # text is invisible and only the bare tick ("Processing… N")
                    # shows. `ev["line"]` is already a bounded (~280-char)
                    # preview, safe to send as a one-line progress message.
                    await ctx.report_progress(
                        progress=float(ev["index"]), total=None, message=ev["line"]
                    )
                except Exception:  # noqa: BLE001 — progress is best-effort
                    pass
            elif kind == "agent_generated":
                await ctx.info(
                    f"[+agent] {ev['id']} ({ev['phase']}) — need: {ev['need']}"
                )
            elif kind == "session_start":
                await ctx.info(
                    f"[session #{ev['session_index']} start] panel="
                    f"{', '.join(ev['panel'])}"
                )
            elif kind == "session_end":
                await ctx.info(
                    f"[session #{ev['session_index']} end] outcome={ev['outcome']}"
                )
            elif kind == "result":
                final = ev["result"]
            elif kind == "error":
                final = {"error": ev["error"]}
    except asyncio.CancelledError:
        stop_event.set()
        raise
    finally:
        stop_event.set()
        worker.join(timeout=_STREAM_JOIN_TIMEOUT_SECONDS)

    return final if final is not None else {
        "error": "RuntimeError: adaptive streaming produced no result"
    }


def _validate_experts_input(experts: Optional[List[str]]) -> None:
    """Bound the early-start `experts` array (count + per-need length).

    Without this, an MCP client could request N expert generations before
    the first session: each generation is a CLI invocation with cost and
    latency. Cap both the number of generations and the per-need free-text
    size to make the surface DoS-resistant.
    """
    if experts is None:
        return
    if not isinstance(experts, list):
        raise ValueError("experts must be a list of strings")
    if len(experts) > _MAX_EARLY_START_EXPERTS:
        raise ValueError(
            f"experts: requested {len(experts)} early-start agents, "
            f"max is {_MAX_EARLY_START_EXPERTS}"
        )
    for i, need in enumerate(experts):
        if not isinstance(need, str) or not need.strip():
            raise ValueError(f"experts[{i}] must be a non-empty string")
        if len(need) > _MAX_NEED_LENGTH_CHARS:
            raise ValueError(
                f"experts[{i}] exceeds {_MAX_NEED_LENGTH_CHARS} chars "
                f"(got {len(need)})"
            )


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
    per_agent_token_budget: Optional[Dict[str, int]] = None,
) -> Dict[str, Any]:
    """Core adaptive loop. `run_one(config) -> (artifact, result_dict)`."""
    runner = run_one or (lambda cfg: _default_adaptive_run_one(cfg, provider, output_dir))
    panel_ids = list(panel) if panel else list(_DEFAULT_PANEL_IDS)
    add_provider = "claude-cli" if provider == "cli-auto" else provider
    add_model = "opus" if provider in ("cli-auto", "claude-cli") else _default_model(provider)

    config = _build_config(
        problem=problem, session_id="adaptive-seed", panel_ids=panel_ids,
        coordinator_id=coordinator,
        provider="claude-cli" if provider == "cli-auto" else provider,
        model="opus" if provider == "cli-auto" else _default_model(provider),
        selector_strategy="fixed", max_rounds=max_rounds,
        max_total_tokens=max_total_tokens, max_total_cost_usd=max_total_cost_usd,
        max_wallclock_seconds=max_wallclock_seconds,
        per_agent_token_budget=per_agent_token_budget,
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


def stream_adaptive(
    problem: str,
    *,
    panel: Optional[List[str]] = None,
    coordinator: str = "coordinator",
    provider: str = "cli-auto",
    experts: Optional[List[str]] = None,
    max_expansions: int = 2,
    max_rounds: int = 4,
    max_total_tokens: int = 100_000_000,
    max_total_cost_usd: float = 1000.0,
    max_wallclock_seconds: int = 3600,
    per_agent_token_budget: Optional[Dict[str, int]] = None,
    output_dir: str = "runs",
    persona_caller=None,
    stream_one=None,
) -> Iterator[Dict[str, Any]]:
    """Adaptive deliberation as a live event stream (testable core behind
    `deliberate_adaptive_streaming`).

    Yields, in order:
      * ``{"event": "agent_generated", "id", "need", "phase"}`` once per
        persona generated (early-start or runtime).
      * ``{"event": "session_start", "session_index", "session_id", "panel"}``
        at the start of each adaptive session.
      * ``{"event": "message", ...}`` for each transcript message as
        the session progresses (forwarded from the per-session journal
        tail).
      * ``{"event": "session_end", "session_index", "session_id", "outcome"}``
        when a session completes.
      * ``{"event": "result", "result": <aggregate adaptive dict>}`` once
        at the end of a successful adaptive run.
      * ``{"event": "error", "error": "..."}`` on any failure.

    `persona_caller` and `stream_one` are injectable seams so tests
    never spawn CLIs or real provider calls.
    """
    if persona_caller is None:
        persona_caller = make_cli_persona_caller()
    if stream_one is None:
        stream_one = lambda cfg: _default_adaptive_stream_one(  # noqa: E731
            cfg, provider, output_dir
        )

    try:
        _validate_experts_input(experts)
    except Exception as exc:  # noqa: BLE001
        yield {"event": "error", "error": _error(exc)["error"]}
        return

    panel_ids = list(panel) if panel else list(_DEFAULT_PANEL_IDS)
    add_provider = "claude-cli" if provider == "cli-auto" else provider
    add_model = "opus" if provider in ("cli-auto", "claude-cli") else _default_model(provider)

    try:
        config = _build_config(
            problem=problem, session_id="adaptive-seed", panel_ids=panel_ids,
            coordinator_id=coordinator,
            provider="claude-cli" if provider == "cli-auto" else provider,
            model="opus" if provider == "cli-auto" else _default_model(provider),
            selector_strategy="fixed", max_rounds=max_rounds,
            max_total_tokens=max_total_tokens, max_total_cost_usd=max_total_cost_usd,
            max_wallclock_seconds=max_wallclock_seconds,
            per_agent_token_budget=per_agent_token_budget,
        )
    except Exception as exc:  # noqa: BLE001
        yield {"event": "error", "error": _error(exc)["error"]}
        return

    existing = {a.id for a in config.agents} | {config.coordinator.id}
    generated: List[Dict[str, Any]] = []
    sessions: List[Dict[str, Any]] = []

    # --- early-start expansion ---------------------------------------------
    for need in (experts or []):
        try:
            persona = _generate_persona(need, caller=persona_caller, existing_ids=existing)
        except Exception as exc:  # noqa: BLE001
            yield {"event": "error", "error": _error(exc)["error"]}
            return
        config = _add_persona(config, persona, provider=add_provider, model=add_model)
        existing.add(persona.id)
        generated.append({"id": persona.id, "need": need, "phase": "early_start"})
        yield {
            "event": "agent_generated",
            "id": persona.id, "need": need, "phase": "early_start",
        }

    # --- run + runtime expansion -------------------------------------------
    expansions = 0
    while True:
        cfg = config.model_copy(update={"session_id": f"mcp-adaptive-{uuid.uuid4().hex}"})
        yield {
            "event": "session_start",
            "session_index": len(sessions) + 1,
            "session_id": cfg.session_id,
            "panel": [a.id for a in cfg.agents],
        }

        result_dict: Optional[Dict[str, Any]] = None
        artifact: Optional[Artifact] = None
        for ev in stream_one(cfg):
            kind = ev.get("event")
            if kind == "result":
                result_dict = ev["result"]
            elif kind == "__artifact":  # internal — never surfaced to the client
                artifact = ev["artifact"]
            elif kind == "error":
                yield ev
                return
            else:
                yield ev  # forward message + any other events

        if result_dict is None or artifact is None:
            yield {"event": "error", "error": "RuntimeError: adaptive session produced no artifact"}
            return

        sessions.append(result_dict)
        yield {
            "event": "session_end",
            "session_index": len(sessions),
            "session_id": cfg.session_id,
            "outcome": result_dict.get("outcome"),
        }

        if artifact.outcome.kind == "synthesis":
            break
        need = _pending_need(artifact)
        if need is None or expansions >= max_expansions:
            break
        try:
            persona = _generate_persona(need, caller=persona_caller, existing_ids=existing)
        except Exception as exc:  # noqa: BLE001
            yield {"event": "error", "error": _error(exc)["error"]}
            return
        config = _add_persona(config, persona, provider=add_provider, model=add_model)
        existing.add(persona.id)
        generated.append({"id": persona.id, "need": need, "phase": "runtime"})
        yield {
            "event": "agent_generated",
            "id": persona.id, "need": need, "phase": "runtime",
        }
        config = _augment_problem(config, prior=result_dict, need=need)
        expansions += 1

    yield {
        "event": "result",
        "result": {
            "final": sessions[-1],
            "sessions": sessions,
            "generated_agents": generated,
            "expansions": expansions,
            "panel_final": [a.id for a in config.agents],
        },
    }


def _default_adaptive_stream_one(cfg: Config, provider: str, output_dir: str) -> Iterator[Dict[str, Any]]:
    """Run one adaptive session as a streaming generator (yields message events
    + a final `result` + an internal `__artifact` so the adaptive loop can
    inspect the outcome).
    """
    if provider == "cli-auto":
        from symposium.integrations.cli_routing import route_cli_providers

        rc, providers = route_cli_providers(cfg)
    else:
        rc = cfg
        providers = default_registry().build_session_providers(cfg)

    journal = Path(output_dir) / cfg.session_id / "transcript.jsonl"
    result_box: Dict[str, Artifact] = {}
    error_box: Dict[str, Exception] = {}

    def _worker() -> None:
        try:
            result_box["artifact"] = run_session(rc, providers, runs_root=output_dir)
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
        time.sleep(0.05)
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
    result = _build_result(artifact, Path(output_dir) / cfg.session_id, [a.id for a in rc.agents])
    yield {"event": "__artifact", "artifact": artifact}
    yield {"event": "result", "result": result}


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
    max_total_tokens: int = 100_000_000,
    max_total_cost_usd: float = 1000.0,
    max_wallclock_seconds: int = 3600,
    per_agent_token_budget: Optional[Dict[str, int]] = None,
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
            per_agent_token_budget=per_agent_token_budget,
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


# Single source of truth for tailing the journal lives in the read-only
# viewer package; the streaming path here re-uses it so the two consumers
# (MCP streaming + `symposium watch`) cannot drift on truncated-line handling.
from symposium.viewer.tail import JournalTail as _JournalTail  # noqa: E402


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
    per_agent_token_budget: Optional[Dict[str, int]] = None,
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
        model=("opus" if provider == "cli-auto" else resolved_model),
        selector_strategy=selector_strategy,
        max_rounds=max_rounds,
        max_total_tokens=max_total_tokens,
        max_total_cost_usd=max_total_cost_usd,
        max_wallclock_seconds=max_wallclock_seconds,
        per_agent_token_budget=per_agent_token_budget,
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
        ta = artifact.outcome.termination_artifact
        result["termination_reason"] = ta.reason
        # Surface the provider's actual complaint so the MCP caller sees
        # actionable diagnostics (eg. codex "unknown variant `max`")
        # instead of just `provider_unrecoverable`. Codex review T1 #2.
        if ta.last_provider_failure is not None:
            result["last_provider_failure"] = ta.last_provider_failure.model_dump(
                mode="json", exclude_none=True,
            )
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
    per_agent_token_budget: Optional[Dict[str, int]] = None,
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

    # Best-effort salvage synthesis is enabled by default for CLI-backed runs
    # (cli-auto / claude-cli / codex-cli), where each turn is a slow agentic
    # subprocess and a wall-clock timeout would otherwise discard the entire
    # deliberation with no answer. API/fake providers keep the spec default
    # (off) so existing deterministic behavior is unchanged.
    runtime = RuntimeConfig(
        synthesize_on_terminate=provider in ("cli-auto", "claude-cli", "codex-cli"),
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
        runtime=runtime,
        budget=BudgetConfig(
            max_total_tokens=max_total_tokens,
            max_total_cost_usd=max_total_cost_usd,
            max_rounds=max_rounds,
            max_wallclock_seconds=max_wallclock_seconds,
            per_agent_token_budget=per_agent_token_budget,
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
        # `opus` alias resolves to the latest opus version on the local CLI
        # (currently opus 4.7). Faster than sonnet on long technical prompts
        # in practice — fewer internal iterations to convergence — and the
        # operator's stated preference for deliberation work.
        return "opus"
    if provider == "codex-cli":
        # Matches the operator's documented preference (`~/.codex/config.toml`
        # default model). Explicit because `--ignore-user-config` skips
        # that config; without -m the CLI would fall back to its built-in
        # default rather than `gpt-5.5`.
        return "gpt-5.5"
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

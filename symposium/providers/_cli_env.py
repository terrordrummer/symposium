"""Shared environment scrub for CLI-driven providers.

Both :mod:`symposium.providers.claude_cli` and
:mod:`symposium.providers.codex_cli` spawn vendor CLIs as subprocesses.
The :mod:`symposium.integrations.persona_factory` helper does the same
to design a new persona on demand. None of those callers want the
spawned child to inherit the *parent's* Claude Code session state or
effort overrides — when they do, observed failure modes range from
silent multi-minute hangs (child does a full nested-Claude-Code
bootstrap and reasons at the parent's ``xhigh`` effort level) to
provider-managed-by-host overrides that ignore the user's chosen
model.

This module owns the single source of truth for that scrub. Adapters
import :data:`INHERITED_ENV_BLOCKLIST` and :func:`scrubbed_env`; they
do not reach into :mod:`os.environ` directly.

What is on the blocklist:

* Variables that mark the child as a nested Claude Code instance
  (``CLAUDECODE`` and the ``CLAUDE_CODE_ENTRYPOINT`` / ``EXECPATH`` /
  ``SESSION_ID`` triad). Without them, the child takes its normal
  headless ``-p`` path; with them, it does the heavy interactive
  bootstrap (skills, plugins, MCP servers, hooks, CLAUDE.md scan).
* Effort overrides (``CLAUDE_CODE_EFFORT_LEVEL`` — the documented
  name on the Claude Code env-vars page — and ``CLAUDE_EFFORT``,
  the legacy alias still present in some shells). At ``xhigh`` /
  ``max`` these alone are enough to push a sub-second deliberation
  turn past a 3-minute per-call timeout.
* ``CLAUDE_CODE_PROVIDER_MANAGED_BY_HOST``, which forces the child
  to ignore its user provider settings and accept a host-managed
  routing the runtime never asked for.
* ``AI_AGENT``, used by some shells to advertise the parent agent's
  identity. Cosmetic, but it bleeds parent identity into the child
  unnecessarily.

What the *shared* blocklist deliberately does NOT touch:

* ``PATH``, ``HOME``, locale vars, Windows ``SystemRoot`` /
  ``PATHEXT`` — required to actually spawn the binary and resolve
  its runtime.
* Other documented ``CLAUDE_CODE_*`` knobs the user may have set
  intentionally (``CLAUDE_CODE_MAX_OUTPUT_TOKENS``,
  ``CLAUDE_CODE_DISABLE_THINKING``, proxy / cert vars, etc.). These
  are explicit operator choices — the scrub is a *minimum-necessary*
  filter against accidental inheritance, not an allowlist.

Provider-specific auth handling (v1.10.7+, Codex review T1 #9): the
shared `scrubbed_env()` no longer makes auth decisions — that's the
job of the provider-specific helpers:

* :func:`claude_child_env` preserves ``ANTHROPIC_*`` and
  ``CLAUDE_CODE_OAUTH_TOKEN`` / ``CLAUDE_CODE_OAUTH_REFRESH_TOKEN``
  (Claude needs them) but actively strips ``CODEX_HOME`` and
  ``OPENAI_*`` — codex credentials have no business in a claude spawn.
* :func:`codex_child_env` preserves ``CODEX_HOME`` and ``OPENAI_*``
  (codex needs them) but actively strips ``CLAUDE_CODE_OAUTH_TOKEN``
  / ``CLAUDE_CODE_OAUTH_REFRESH_TOKEN`` / ``ANTHROPIC_*`` — Claude
  credentials in a codex spawn would just widen the credential
  exposure surface inside an agentic CLI child without operational
  benefit (codex never reads those vars).

The legacy :func:`headless_child_env` is an alias for
:func:`claude_child_env` kept for backward compatibility; new call
sites should use the provider-specific variants.
"""

from __future__ import annotations

import os
import signal
import subprocess
from typing import Any, Dict, FrozenSet, Optional

INHERITED_ENV_BLOCKLIST: FrozenSet[str] = frozenset({
    # Nested-Claude-Code markers — set by a parent Claude Code session
    # on every subprocess it spawns.
    "CLAUDECODE",
    "CLAUDE_CODE_ENTRYPOINT",
    "CLAUDE_CODE_EXECPATH",
    "CLAUDE_CODE_SESSION_ID",
    # Effort overrides. The first is the documented var on
    # code.claude.com/docs/en/env-vars; the second is the legacy alias
    # still observed in user shells. Both can silently force the child
    # onto an extended-thinking effort the runtime never asked for.
    "CLAUDE_CODE_EFFORT_LEVEL",
    "CLAUDE_EFFORT",
    # Bare-mode marker. Set automatically when claude is started with
    # `--bare` ("Sets CLAUDE_CODE_SIMPLE=1" per the CLI's own help
    # text). If the parent session was launched bare, this var would
    # be inherited by the child and force it into bare mode even when
    # our adapter's `bare=False` default tries to preserve OAuth /
    # subscription auth — silently turning a subscription user's run
    # into an auth failure. Stripped to enforce our default.
    "CLAUDE_CODE_SIMPLE",
    # Host-managed provider routing — ignores the user's provider
    # settings, which we don't want for a programmatic turn.
    "CLAUDE_CODE_PROVIDER_MANAGED_BY_HOST",
    # Parent agent identity (cosmetic; scrubbed for cleanliness).
    "AI_AGENT",
})


def scrubbed_env() -> Dict[str, str]:
    """Snapshot of ``os.environ`` with :data:`INHERITED_ENV_BLOCKLIST` stripped.

    Returns a *copy* — callers are free to mutate it further before
    handing it to ``subprocess.run(env=...)``.
    """
    return {k: v for k, v in os.environ.items() if k not in INHERITED_ENV_BLOCKLIST}


# Documented Claude Code env knobs that suppress auto-load behaviors the
# child would otherwise perform during startup, none of which a
# non-interactive deliberation turn needs:
#
#   * CLAUDE_CODE_DISABLE_CLAUDE_MDS — skip CLAUDE.md auto-discovery
#     (the child would walk up from cwd and load every CLAUDE.md it
#     finds, including the user's global ``~/.claude/CLAUDE.md`` which
#     in practice is often hundreds of lines).
#   * CLAUDE_CODE_DISABLE_AUTO_MEMORY — skip auto-memory load.
#   * CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC — collapses to
#     DISABLE_AUTOUPDATER / DISABLE_FEEDBACK_COMMAND /
#     DISABLE_ERROR_REPORTING / DISABLE_TELEMETRY.
#   * CLAUDE_CODE_DISABLE_BACKGROUND_TASKS — skip background-task
#     plumbing.
#
# Why this exists *in addition to* INHERITED_ENV_BLOCKLIST: stripping
# the parent's nested-Claude-Code markers is necessary to take the
# child off heavy parent-driven paths (effort overrides,
# session-attached MCP servers), but does NOT prevent the child's own
# default startup behavior. Without these DISABLE_* vars the child
# still spends multi-minutes loading CLAUDE.md/auto-memory before
# producing its first token — observed in the wild against a user's
# Workspace tree with ~10+ CLAUDE.md files. ``--bare`` would also fix
# it but disables OAuth/keychain (incompatible with the "no API key
# needed" promise of the CLI adapters), so the env-knob approach is
# the surgical middle ground: keep OAuth, skip the heavy loads.
_CHILD_AUTO_LOAD_DISABLES: Dict[str, str] = {
    "CLAUDE_CODE_DISABLE_CLAUDE_MDS": "1",
    "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
    "CLAUDE_CODE_DISABLE_BACKGROUND_TASKS": "1",
}


# Cross-vendor credential / state vars that should NOT leak across
# provider spawns. Codex review T1 item #9: today both helpers go
# through `headless_child_env()`, which preserves every credential the
# parent has set. That means a `codex exec` spawn ends up with
# `CLAUDE_CODE_OAUTH_TOKEN` and `ANTHROPIC_*` lying in its
# `/proc/PID/environ` — and vice versa for a `claude -p` spawn that
# inherits `CODEX_HOME` / `OPENAI_API_KEY`. Neither vendor reads the
# other's vars, so the only effect is widening the credential exposure
# surface inside agentic CLI children that themselves run untrusted
# tool calls. Provider-specific helpers scrub the *other* vendor's set.
_CLAUDE_ONLY_ENV: frozenset = frozenset({
    "CLAUDE_CODE_OAUTH_TOKEN",
    "CLAUDE_CODE_OAUTH_REFRESH_TOKEN",
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_BEDROCK_BASE_URL",
    "ANTHROPIC_VERTEX_BASE_URL",
    "ANTHROPIC_CUSTOM_HEADERS",
    "ANTHROPIC_API_BETAS",
})
_CODEX_ONLY_ENV: frozenset = frozenset({
    "CODEX_HOME",
    "OPENAI_API_KEY",
    "OPENAI_BASE_URL",
    "OPENAI_ORG_ID",
})


def headless_child_env() -> Dict[str, str]:
    """``scrubbed_env()`` plus the auto-load-disable knobs the child needs
    to skip its own heavy startup (CLAUDE.md walk, auto-memory, etc.).

    This is what the CLI providers and ``persona_factory`` should pass
    as ``env=`` to ``subprocess.run`` for a non-interactive turn. The
    env-var-only approach (vs ``--bare``) preserves OAuth / keychain
    auth so subscription users keep working without an
    ``ANTHROPIC_API_KEY``.

    The DISABLE_* values are an **unconditional override** — any
    contrary value inherited from the parent env (eg. a stray
    ``CLAUDE_CODE_DISABLE_CLAUDE_MDS=0``) is replaced, not preserved.
    Parent-env inheritance is not how an operator opts back into the
    heavy auto-loads; that's what the provider's ``env=`` constructor
    arg is for. Letting a stray "0" sticky-pass through would reopen
    exactly the path that caused the 9-minute hang this helper exists
    to prevent.

    .. deprecated:: 1.10.7
        Prefer :func:`claude_child_env` / :func:`codex_child_env` —
        provider-specific variants don't leak the other vendor's auth
        surface into the spawn (Codex review T1 #9). This alias keeps
        the original semantics for backward compatibility.
    """
    return claude_child_env()


def claude_child_env() -> Dict[str, str]:
    """Headless child env for a `claude -p` spawn.

    :func:`scrubbed_env` + the four ``CLAUDE_CODE_DISABLE_*`` knobs +
    actively strips ``CODEX_HOME`` / ``OPENAI_*`` (codex auth has no
    business being in a claude spawn).
    """
    env = scrubbed_env()
    for k, v in _CHILD_AUTO_LOAD_DISABLES.items():
        env[k] = v
    for k in _CODEX_ONLY_ENV:
        env.pop(k, None)
    return env


def codex_child_env() -> Dict[str, str]:
    """Headless child env for a `codex exec` spawn.

    :func:`scrubbed_env` + ``CLAUDE_CODE_DISABLE_*`` (no-ops for codex
    itself, but protect any descendant ``claude`` invocation the child
    might fork — same defensive override as the claude path) + actively
    strips ``CLAUDE_CODE_OAUTH_TOKEN`` / ``CLAUDE_CODE_OAUTH_REFRESH_TOKEN``
    / ``ANTHROPIC_*``: claude auth in a codex spawn is a cross-vendor
    credential leak with no operational reason. Codex review T1 item #9.
    """
    env = scrubbed_env()
    for k, v in _CHILD_AUTO_LOAD_DISABLES.items():
        env[k] = v
    for k in _CLAUDE_ONLY_ENV:
        env.pop(k, None)
    return env


def run_in_process_group(
    argv,
    *,
    input: Optional[str] = None,
    capture_output: bool = False,
    text: bool = False,
    timeout: Optional[float] = None,
    env: Optional[Dict[str, str]] = None,
) -> subprocess.CompletedProcess:
    """`subprocess.run`-shaped runner that kills the whole process GROUP on
    timeout — the default runner for the CLI provider adapters.

    Why not plain `subprocess.run`: the vendor CLIs (`claude`, `codex`) spawn
    their own child processes. `subprocess.run`'s timeout only SIGKILLs the
    direct child; orphaned grandchildren can keep doing work (and holding the
    stdout pipe), so a per-turn timeout may not actually bound wall-clock under
    load. We launch the child as a process-group leader (`start_new_session`)
    and, on timeout, `killpg` the entire group so the deadline is enforced for
    the whole subtree. The injectable `runner=` seam means tests never hit this
    (they pass their own fake), so this runs in production only.
    """
    stdout = subprocess.PIPE if capture_output else None
    stderr = subprocess.PIPE if capture_output else None
    stdin = subprocess.PIPE if input is not None else None
    proc = subprocess.Popen(
        argv,
        stdin=stdin,
        stdout=stdout,
        stderr=stderr,
        text=text,
        env=env,
        start_new_session=True,
    )
    try:
        out, err = proc.communicate(input=input, timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            proc.kill()
        # Reap so the pipes close and no zombie/grandchild lingers.
        try:
            proc.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            pass
        raise
    return subprocess.CompletedProcess(argv, proc.returncode, out, err)


def effective_timeout(request: Any, default: float) -> float:
    """Resolve the subprocess timeout for a CLI turn.

    The scheduler's deadline-aware path may pass a per-turn budget via
    ``request.metadata["symposium_timeout_seconds"]``. We honor it but only
    to TIGHTEN the adapter's own ``default`` — a request can never extend a
    turn past the adapter-configured ceiling, only shorten it to fit the
    remaining session wall-clock. A missing/invalid/non-positive hint falls
    back to ``default``.
    """
    meta = getattr(request, "metadata", None)
    if not isinstance(meta, dict):
        return default
    raw = meta.get("symposium_timeout_seconds")
    if raw is None:
        return default
    try:
        requested = float(raw)
    except (TypeError, ValueError):
        return default
    if requested <= 0:
        return default
    return min(default, requested)

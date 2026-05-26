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

What is *deliberately NOT* on the blocklist:

* ``ANTHROPIC_*`` (API key, base URL, betas, custom headers) and
  ``CODEX_HOME`` — the child needs these for auth and endpoint
  routing.
* ``CLAUDE_CODE_OAUTH_TOKEN`` / ``CLAUDE_CODE_OAUTH_REFRESH_TOKEN`` —
  documented OAuth credentials; stripping them would break the
  "reuses CLI login, no API key needed" promise of the CLI adapters.
* Other documented ``CLAUDE_CODE_*`` knobs the user may have set
  intentionally (``CLAUDE_CODE_MAX_OUTPUT_TOKENS``,
  ``CLAUDE_CODE_DISABLE_THINKING``, proxy / cert vars, etc.). These
  are explicit operator choices — the scrub is a *minimum-necessary*
  filter against accidental inheritance, not an allowlist.
* ``PATH``, ``HOME``, locale vars, Windows ``SystemRoot`` /
  ``PATHEXT`` — required to actually spawn the binary and resolve
  its runtime.
"""

from __future__ import annotations

import os
from typing import Dict, FrozenSet

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
    """
    env = scrubbed_env()
    for k, v in _CHILD_AUTO_LOAD_DISABLES.items():
        env[k] = v
    return env

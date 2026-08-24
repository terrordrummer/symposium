"""Claude-CLI provider adapter — drive deliberations through the `claude`
terminal CLI instead of the HTTP API (§6.1 `ProviderAdapter` contract).

Why this exists
---------------

The HTTP adapters (`OpenAIProvider`, `AnthropicProvider`) call a vendor
REST endpoint and need an API key in the environment. This adapter
instead shells out to the locally-installed **`claude` command** in
non-interactive print mode (`claude -p`), reusing whatever auth the CLI
already has (OAuth / keychain). No `ANTHROPIC_API_KEY` is required: if
you can run `claude` in your terminal, Symposium can drive a panel with
it.

How it maps onto the contract
-----------------------------

* **One turn → one `claude -p` invocation.** The complete request is fed on
  **stdin** so persona material never appears in the process list. The CLI is
  started in safe mode, with built-in tools disabled and without session
  persistence: a Symposium turn is a deliberation call, not an agentic-coding
  session over the local repository.
* **Structured output (§6.5) via `--json-schema`.** The expected output
  schema (`turn_structured_output` / `verdict` / `synthesis_content`) is
  emitted from its Pydantic model as a self-contained JSON Schema and
  passed to `--json-schema`; the CLI returns the conforming object in the
  response's top-level `structured_output` field, which the adapter then
  validates with the same `validate_structured_output` helper the HTTP
  adapters use. The `null` schema (the §4.1 `llm`-selector free-text
  path) skips `--json-schema` and returns the free-text `result`.
* **Corrective retry (§6.7).** On a schema-invalid object the adapter
  appends a corrective annotation to the prompt and retries once before
  surfacing `malformed_response`; the runtime may retry further (§4.9).
* **Usage / cost (§5.7).** Tokens come from the CLI's `usage`
  (`input_tokens` + cache tokens → prompt; `output_tokens` →
  completion). `cost_usd` is the CLI's `total_cost_usd`, but it is the
  **API-equivalent reference** cost (what the tokens would cost at API
  rates), recorded with `estimated = True`: under a subscription login
  (Claude Pro/Max) turns draw on the subscription's usage / rate limits,
  NOT metered per-token billing, so this figure is a reference, not a
  charge. (It would be an actual charge only if the CLI were authenticated
  via an API key rather than a subscription.)
* **Errors (§6.6).** A missing `claude` binary, a non-zero exit, a CLI
  `is_error`, a parse failure, or a timeout map to CLOSED `error.kind`
  values, so the runtime's §4.9 failure handling applies unchanged.

Scope notes
-----------

Tool use (§6.4) is not wired through the CLI here; a `request.tools`
payload is ignored (documented limitation of this first version). The
adapter is a pure consumer of the public `ProviderAdapter` contract and
changes nothing in the runtime, spec, or schemas.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import time
from typing import Any, Callable, Dict, List, Optional

from symposium.models import (
    FinishReason,
    ProviderError,
    ProviderRawMessage,
    ProviderRequest,
    ProviderResult,
    SynthesisContent,
    TurnStructuredOutput,
    Usage,
    Verdict,
)
from symposium.providers._cli_env import (
    claude_child_env,
    classify_cli_exit,
    effective_timeout,
    run_in_process_group,
)
from symposium.providers._http_common import merge_usage, validate_structured_output
from symposium.providers.base import ProviderAdapter

DEFAULT_CLAUDE_BINARY = "claude"
DEFAULT_MODEL = "sonnet"
SYMPOSIUM_SYSTEM_PROMPT = (
    "You are one participant in a bounded Symposium deliberation. "
    "Follow the private [SYSTEM] guidance supplied in the input, use only the "
    "information in that input, do not inspect the local machine, and return "
    "the requested structured output."
)
# Per-invocation timeout. Calibrated against real-world deliberation
# turns: a multi-paragraph technical problem statement (~3KB) on the
# sonnet alias takes ~6 minutes per turn at "low" effort because the
# CLI does an internal agentic loop (observed `num_turns` in the
# double-digits for a single structured-output response). 180s — the
# v1.10.2-and-earlier default — timed out mid-turn on prompts of that
# size, exhausted the retry budget, and surfaced as
# `provider_unrecoverable` with zero token usage even with v1.10.2's
# env scrub in place. 600s gives a single turn enough room to finish
# on sonnet; operators expecting a panel of 5+ personas should pre-
# trim the problem statement or route the panel to ``haiku`` (~3.5×
# faster on the same prompt) to keep total panel wallclock sane.
DEFAULT_TIMEOUT_SECONDS = 600.0

# expected_output_schema → Pydantic model whose JSON Schema we hand to
# `--json-schema`. `null` / None take the free-text path (no schema).
_SCHEMA_MODELS: Dict[str, Any] = {
    "turn_structured_output": TurnStructuredOutput,
    "verdict": Verdict,
    "synthesis_content": SynthesisContent,
}

# `subprocess.run`-shaped callable, injected by tests to avoid real calls.
RunnerResult = subprocess.CompletedProcess
Runner = Callable[..., RunnerResult]


class ClaudeCliProvider(ProviderAdapter):
    """ProviderAdapter that invokes the `claude` CLI in print mode.

    Constructor parameters
    ----------------------

    binary:
        Name or path of the CLI. Defaults to ``claude`` (resolved on
        PATH). Fail-fast at construction if it is not found, with a
        message pointing at the install/login step.
    default_model:
        Model used when a request carries no usable model string.
        Defaults to the ``sonnet`` alias.
    timeout:
        Per-invocation wall-clock cap (seconds) for the subprocess.
    extra_args:
        Additional CLI flags appended to every invocation (advanced;
        e.g. ``["--add-dir", "/path"]``). Defaults to none.
    bare:
        When True, append ``--bare`` (Claude Code's headless / minimal
        mode: no hooks, no LSP, no plugin sync, no auto-memory, no
        CLAUDE.md auto-discovery, no keychain reads) to every CLI
        invocation. **Default is False**, on purpose: ``--bare``
        disables OAuth/keychain auth and requires ``ANTHROPIC_API_KEY``
        (or ``apiKeyHelper``), which would break the "no API key
        needed — reuses the CLI's existing login" promise of this
        adapter for users on a Claude Pro/Max subscription. Set
        ``bare=True`` if you authenticate with an API key AND want the
        absolute minimum-bootstrap path. The :data:`scrubbed_env`
        env-var scrub (always on, see ``env`` below) already addresses
        the most common slow-spawn cause (inherited Claude Code state /
        effort overrides), so ``bare=False`` is safe for the failure
        mode this flag was added for. Requires Claude Code >= 2.1.81
        when enabled.
    disable_mcps:
        When True (the **default**), append
        ``--strict-mcp-config --mcp-config '{"mcpServers": {}}'`` to
        every CLI invocation, which forces the spawned ``claude -p`` to
        load **zero MCP servers** — overriding the user's global
        ``~/.claude.json``. Why this is on by default: the user's MCP
        registry typically holds 3–8 servers (context7, firebase,
        gemini-image, vendor MCPs, etc.). Each one auto-spawns at
        ``claude -p`` startup via ``npm exec`` / node, adding 10–60s of
        latency *per deliberation turn* and producing a noisy process
        tree (including, often, a recursive ``symposium-mcp`` child
        when symposium itself is registered). A deliberation turn does
        not need any of those MCPs — it is a single structured-output
        call. The env-var scrub (``CLAUDE_CODE_DISABLE_*``) alone does
        NOT prevent MCP loading; only this flag pair does, and unlike
        ``--bare`` it preserves OAuth / keychain auth. Set
        ``disable_mcps=False`` only if you have a specific reason for
        the spawned child to load MCPs (eg. tool-use over CLI, not yet
        wired through this adapter as of v1.10).
    safe_mode:
        When True (the **default**), append ``--safe-mode``, ``--tools ""``,
        and ``--no-session-persistence``. Safe mode preserves the user's
        normal OAuth/keychain authentication while disabling project/user
        customizations; the empty tool list prevents a deliberation persona
        from reading or modifying the checkout. Structured output remains
        available because it is an output-format facility, not a tool exposed
        to the agent. Set this to False only for compatibility with an older
        Claude Code binary that predates ``--safe-mode``.
    env:
        Environment passed to the subprocess. ``None`` (the default)
        means :func:`~symposium.providers._cli_env.headless_child_env`:
        ``os.environ`` minus
        :data:`~symposium.providers._cli_env.INHERITED_ENV_BLOCKLIST`
        plus a handful of ``CLAUDE_CODE_DISABLE_*`` knobs that suppress
        the child's own auto-loads (``CLAUDE.md`` walk, auto-memory,
        background tasks, non-essential traffic) — needed in addition
        to the inheritance scrub because the child still does a full
        bootstrap on its own otherwise. Pass an explicit dict to
        override entirely (NOTE: a verbatim dict that omits ``PATH`` /
        Windows ``SystemRoot`` will prevent the child from starting; if
        you need a narrow override, layer it over
        ``headless_child_env()`` yourself). Pass ``os.environ`` to opt
        back into raw inheritance.
    runner:
        Injection seam for tests: a ``subprocess.run``-shaped callable.
        Defaults to :func:`subprocess.run`. Production never overrides it.
    """

    name = "claude-cli"

    def __init__(
        self,
        *,
        binary: str = DEFAULT_CLAUDE_BINARY,
        default_model: str = DEFAULT_MODEL,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        extra_args: Optional[List[str]] = None,
        bare: bool = False,
        disable_mcps: bool = True,
        safe_mode: bool = True,
        env: Optional[Dict[str, str]] = None,
        runner: Optional[Runner] = None,
        check_binary: bool = True,
    ) -> None:
        self._binary = binary
        self._default_model = default_model
        self._timeout = timeout
        self._extra_args = list(extra_args or [])
        self._bare = bare
        self._disable_mcps = disable_mcps
        self._safe_mode = safe_mode
        self._env_override = env
        self._run: Runner = runner or run_in_process_group
        if check_binary and runner is None and shutil.which(binary) is None:
            raise FileNotFoundError(
                f"the {binary!r} CLI was not found on PATH; install Claude Code and "
                f"run `{binary}` once to authenticate, or set provider to a different "
                "adapter. The claude-cli provider needs no API key — it reuses the "
                "CLI's existing login."
            )

    # ------------------------------------------------------------------
    # ProviderAdapter contract
    # ------------------------------------------------------------------

    def invoke(self, request: ProviderRequest) -> ProviderResult:
        # The per-request timeout (deadline-aware path) budgets the WHOLE
        # invocation, including the §6.7 corrective retry below. Split it across
        # both subprocess calls so they can't together overrun the deadline the
        # scheduler reserved for the synthesis (Codex PR1 review #2).
        budget = effective_timeout(request, self._timeout)
        start = time.monotonic()
        first = self._invoke_once(request, system_suffix=None, timeout=budget)
        if first.error is not None and first.error.kind == "malformed_response":
            remaining = budget - (time.monotonic() - start)
            if remaining <= 0:
                return first
            # §6.7 single corrective retry: re-state the schema requirement.
            details = first.error.details or {}
            suffix = (
                "\n\nYour previous response did not conform to the required schema "
                f"({details.get('expected_output_schema')}) at "
                f"`{details.get('failing_path')}`: {details.get('validator_message')}. "
                "Re-emit ONLY the structured object that conforms to the schema."
            )
            second = self._invoke_once(request, system_suffix=suffix, timeout=remaining)
            # The first attempt consumed real tokens; fold its usage into
            # the returned result so budget accounting sees both passes.
            return second.model_copy(
                update={"usage": merge_usage(first.usage, second.usage)}
            )
        return first

    # ------------------------------------------------------------------
    # Single-pass invocation
    # ------------------------------------------------------------------

    def _invoke_once(
        self, request: ProviderRequest, *, system_suffix: Optional[str], timeout: float
    ) -> ProviderResult:
        system_prompt, user_prompt = _split_prompt(request)
        if system_suffix:
            user_prompt = f"{user_prompt}{system_suffix}"

        model = request.model or self._default_model
        argv: List[str] = [
            self._binary,
            "-p",
            "--output-format",
            "json",
            "--model",
            model,
            # Replace Claude Code's coding-agent prompt with a small constant.
            # Persona material stays on stdin and therefore out of `ps`.
            "--system-prompt",
            SYMPOSIUM_SYSTEM_PROMPT,
        ]
        if self._bare:
            # `--bare`: headless / minimal mode (no hooks, no LSP, no plugin
            # sync, no auto-memory, no CLAUDE.md auto-discovery, no keychain
            # reads). See the class docstring for the rationale.
            argv.append("--bare")
        if self._disable_mcps:
            # Force the spawned `claude -p` to load zero MCP servers by
            # making the user's MCP registry unreachable: --strict-mcp-config
            # restricts the child to MCPs declared via --mcp-config (only),
            # and we hand it an empty config. Without this the child
            # auto-loads every MCP from ~/.claude.json, each one a multi-
            # second npm-exec at startup; a deliberation turn doesn't need
            # any of them. Unlike --bare, this preserves OAuth/keychain.
            argv += ["--strict-mcp-config", "--mcp-config", '{"mcpServers": {}}']
        if self._safe_mode:
            # A room turn needs the model, not Claude Code's project agent.
            # Safe mode preserves OAuth auth but suppresses CLAUDE.md, hooks,
            # skills, plugins, and memory; the empty tool list prevents local
            # repository access. Session persistence is unnecessary for the
            # immutable Symposium transcript and would retain private persona
            # context outside the run artifact.
            argv += ["--safe-mode", "--tools", "", "--no-session-persistence"]
        # The system block — which carries persona material and may contain
        # confidential prompt fragments — is ALWAYS folded into the stdin
        # payload with a `[SYSTEM]` sentinel; we never put it on the argv
        # (visible to `ps` to any user on the host). This mirrors the
        # codex-cli pattern.
        schema_json = _schema_for(request.expected_output_schema)
        if schema_json is not None:
            argv += ["--json-schema", schema_json]
        argv += self._extra_args

        stdin_payload = user_prompt
        if system_prompt:
            stdin_payload = f"[SYSTEM]\n{system_prompt}\n\n{user_prompt}"

        try:
            proc = self._run(
                argv,
                input=stdin_payload,
                capture_output=True,
                text=True,
                timeout=timeout,
                env=self._env_override if self._env_override is not None else claude_child_env(),
            )
        except subprocess.TimeoutExpired as exc:
            return _error_result(
                "timeout", f"claude CLI timed out after {timeout}s: {exc}", True
            )
        except FileNotFoundError as exc:
            return _error_result(
                "internal", f"claude CLI not found: {exc}", False
            )
        except Exception as exc:  # noqa: BLE001 — any spawn failure is non-retriable internal
            return _error_result("internal", f"failed to run claude CLI: {exc}", False)

        if proc.returncode != 0:
            stderr = (proc.stderr or "").strip()
            kind, retriable = classify_cli_exit(stderr)
            return _error_result(
                kind,
                f"claude CLI exited {proc.returncode}: {stderr[:500] or '<no stderr>'}",
                retriable=retriable,
            )

        try:
            data = json.loads(proc.stdout)
        except (json.JSONDecodeError, TypeError) as exc:
            return _error_result(
                "internal", f"could not parse claude CLI JSON output: {exc}", False
            )

        if not isinstance(data, dict):
            # rc=0 with `null` / an array on stdout: syntactically valid
            # JSON, but not the result envelope — treat as a malformed
            # response rather than crashing on `data.get(...)`.
            return _error_result(
                "malformed_response",
                f"claude CLI emitted non-object JSON output ({type(data).__name__})",
                True,
            )

        usage = _usage_from_cli(data)

        if data.get("is_error") or data.get("subtype") not in (None, "success"):
            return _error_result(
                "internal",
                f"claude CLI reported an error: {data.get('subtype')} "
                f"{data.get('api_error_status') or ''}".strip(),
                retriable=False,
                raw=_safe(data),
                usage=usage,
            )

        structured = data.get("structured_output")
        stop_reason = data.get("stop_reason")
        # Claude Code implements --json-schema through an internal structured-
        # output tool. Current CLI releases may therefore report `tool_use`
        # even on a successful terminal result. The result envelope's success
        # subtype plus a concrete structured_output object is the authoritative
        # success contract. Never accept tool_use on the free-text path or when
        # the validated object is absent.
        structured_tool_stop = (
            stop_reason == "tool_use"
            and schema_json is not None
            and isinstance(structured, dict)
        )
        finish: FinishReason = "stop" if structured_tool_stop else _finish_reason(stop_reason)
        if finish == "error":
            # Unknown stop_reason terminally — align with the anthropic
            # adapter: surface an internal error instead of a clean stop.
            return _error_result(
                "internal",
                f"claude CLI returned unexpected stop_reason: {stop_reason!r}",
                retriable=False,
                raw=_safe(data),
                usage=usage,
            )
        raw_result = data.get("result")
        result_text = raw_result if isinstance(raw_result, str) else ""

        # Free-text path (null schema): no structured validation (§4.1 llm selector).
        if schema_json is None:
            return ProviderResult(
                messages=[ProviderRawMessage(role="assistant", content=result_text)],
                tool_events=[],
                usage=usage,
                finish_reason=finish,
                structured_output=None,
                raw=_safe(data),
                error=None,
            )

        if not isinstance(structured, dict):
            return _malformed(
                request,
                content=result_text,
                usage=usage,
                raw=_safe(data),
                failing_path="<root>",
                message="claude CLI returned no structured_output object for the schema",
            )

        failure = validate_structured_output(structured, request.expected_output_schema)
        if failure is not None:
            return _malformed(
                request,
                content=json.dumps(structured),
                usage=usage,
                raw=_safe(data),
                failing_path=failure["failing_path"],
                message=failure["validator_message"],
                raw_attempt=structured,
            )

        return ProviderResult(
            messages=[ProviderRawMessage(role="assistant", content=json.dumps(structured))],
            tool_events=[],
            usage=usage,
            finish_reason=finish,
            structured_output=structured,
            raw=_safe(data),
            error=None,
        )


# ---------------------------------------------------------------------------
# Helpers — module level
# ---------------------------------------------------------------------------


_SCHEMA_CACHE: Dict[str, str] = {}


def _schema_for(expected: Optional[str]) -> Optional[str]:
    """Self-contained JSON Schema string for `--json-schema`, or None for free text."""
    if expected in (None, "null"):
        return None
    model = _SCHEMA_MODELS.get(expected)
    if model is None:
        return None
    if expected not in _SCHEMA_CACHE:
        _SCHEMA_CACHE[expected] = json.dumps(model.model_json_schema())
    return _SCHEMA_CACHE[expected]


def _split_prompt(request: ProviderRequest) -> tuple[str, str]:
    """Return (system_prompt, user_prompt) from the canonical messages.

    messages[0] (role system, §6.3) drives --system-prompt; every other
    message's content is concatenated into the stdin prompt.
    """
    system_prompt = ""
    body: List[str] = []
    for i, msg in enumerate(request.messages):
        text = msg.content if isinstance(msg.content, str) else json.dumps(msg.content)
        if i == 0 and msg.role == "system":
            system_prompt = text
        else:
            body.append(text)
    if body:
        return system_prompt, "\n\n".join(body)
    # System-only message list: emit the text once as the prompt body
    # instead of duplicating it behind the [SYSTEM] sentinel.
    return "", system_prompt


def _safe_int(v: Any) -> int:
    """Coerce a CLI-reported usage value to int; malformed → 0 (never raises)."""
    if v is None:
        return 0
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


def _usage_from_cli(data: Dict[str, Any]) -> Usage:
    u = data.get("usage") or {}
    prompt = (
        _safe_int(u.get("input_tokens"))
        + _safe_int(u.get("cache_read_input_tokens"))
        + _safe_int(u.get("cache_creation_input_tokens"))
    )
    completion = _safe_int(u.get("output_tokens"))
    cost = data.get("total_cost_usd")
    # `total_cost_usd` is the API-EQUIVALENT cost (what the tokens would cost
    # at API rates). Under a subscription login it is not a metered charge, so
    # mark the usage `estimated` — the figure is a reference, not a bill.
    return Usage(
        prompt_tokens=prompt,
        completion_tokens=completion,
        total_tokens=prompt + completion,
        cost_usd=float(cost) if isinstance(cost, (int, float)) else 0.0,
        estimated=True,
    )


def _finish_reason(stop_reason: Optional[str]) -> FinishReason:
    if stop_reason in ("end_turn", "stop_sequence", None):
        return "stop"
    if stop_reason == "max_tokens":
        return "length"
    if stop_reason == "refusal":
        return "content_filter"
    # Unknown vendor value — align with the anthropic adapter (§6.10):
    # collapse to `error` rather than report a clean finish.
    return "error"


def _safe(data: Any) -> Optional[Dict[str, Any]]:
    return data if isinstance(data, dict) else None


def _error_result(
    kind: str,
    message: str,
    retriable: bool,
    *,
    raw: Optional[Dict[str, Any]] = None,
    usage: Optional[Usage] = None,
) -> ProviderResult:
    return ProviderResult(
        messages=[],
        tool_events=[],
        usage=usage
        or Usage(prompt_tokens=0, completion_tokens=0, total_tokens=0, cost_usd=0.0),
        finish_reason="error",
        structured_output=None,
        raw=raw,
        error=ProviderError(kind=kind, message=message, retriable=retriable),  # type: ignore[arg-type]
    )


def _malformed(
    request: ProviderRequest,
    *,
    content: str,
    usage: Usage,
    raw: Optional[Dict[str, Any]],
    failing_path: str,
    message: str,
    raw_attempt: Any = None,
) -> ProviderResult:
    return ProviderResult(
        messages=[ProviderRawMessage(role="assistant", content=content or "<empty>")],
        tool_events=[],
        usage=usage,
        finish_reason="error",
        structured_output=None,
        raw=raw,
        error=ProviderError(
            kind="malformed_response",
            message=f"{failing_path}: {message}",
            retriable=True,
            details={
                "expected_output_schema": request.expected_output_schema,
                "failing_path": failing_path,
                "validator_message": message,
                "raw_attempt": raw_attempt if raw_attempt is not None else content,
            },
        ),
    )

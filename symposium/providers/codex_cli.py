"""Codex-CLI provider adapter — drive deliberations through the `codex`
terminal CLI (§6.1 `ProviderAdapter` contract).

Sibling of `ClaudeCliProvider`: same "one turn → one non-interactive CLI
invocation, structured output enforced by a JSON Schema" shape, against
OpenAI's `codex exec` instead of `claude -p`. Like the Claude adapter it
needs **no API key** beyond whatever auth the `codex` CLI already has.

Vendor specifics
----------------

* Invocation: ``codex exec --json --output-schema <file> [-m <model>]
  -s read-only --skip-git-repo-check -C <neutral-cwd> <prompt>``. Codex
  has no `--system-prompt`, so the §6.3 system message is folded into the
  prompt text (system block first, then the user content).
* Structured output (§6.5): ``--output-schema`` takes a *file* holding
  the JSON Schema (emitted from the expected output's Pydantic model).
  The conforming object comes back as the text of the final
  ``item.completed`` / ``agent_message`` event in the ``--json`` JSONL
  stream; the adapter parses and validates it with the shared helper.
* Usage (§5.7): from the ``turn.completed`` event's ``usage`` block
  (``input_tokens`` + ``cached_input_tokens`` → prompt;
  ``output_tokens`` + ``reasoning_output_tokens`` → completion). Codex
  reports **no cost**, so ``cost_usd = 0.0`` and ``estimated = True``.
  Under a subscription login (a ChatGPT plan) turns draw on that plan's
  usage / rate limits, not metered per-token API billing.
* Errors (§6.6): a missing binary, a non-zero exit, a ``turn.failed`` /
  ``error`` event, a parse failure, or a timeout map to CLOSED
  ``error.kind`` values; one corrective retry (§6.7) on a schema miss.

The model string is optional: a falsy / sentinel value (``"auto"``,
``"default"``) omits ``-m`` and lets the CLI use its configured default
model. Tool use (§6.4) is not wired (documented limitation). Pure
consumer of the public contract — no runtime / spec / schema changes.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from typing import Any, Callable, Dict, List, Optional

from symposium.models import (
    ProviderError,
    ProviderRawMessage,
    ProviderRequest,
    ProviderResult,
    SynthesisContent,
    TurnStructuredOutput,
    Usage,
    Verdict,
)
from symposium.providers._cli_env import codex_child_env
from symposium.providers._http_common import validate_structured_output
from symposium.providers.base import ProviderAdapter

DEFAULT_CODEX_BINARY = "codex"
# See claude_cli.DEFAULT_TIMEOUT_SECONDS for the rationale (multi-min
# per-turn inference on heavy technical prompts). Codex is empirically
# in the same range as sonnet on similar prompt sizes.
DEFAULT_TIMEOUT_SECONDS = 600.0
# Model strings that mean "let the CLI choose its default" → omit -m.
_AUTO_MODELS = {"", "auto", "default", "codex-default"}

_SCHEMA_MODELS: Dict[str, Any] = {
    "turn_structured_output": TurnStructuredOutput,
    "verdict": Verdict,
    "synthesis_content": SynthesisContent,
}


def _strictify_for_openai(schema: Dict[str, Any]) -> Dict[str, Any]:
    """Patch a Pydantic-generated JSON Schema into OpenAI structured-output
    strict-mode compliance.

    The OpenAI strict mode used by ``codex exec --output-schema`` (and the
    underlying chat-completions `response_format=json_schema, strict=true`)
    rejects any submitted schema that doesn't satisfy:

      1. **Every object type carries** ``additionalProperties: false``.
         Pydantic emits ``false`` at the root but leaves it ``true`` (or
         omits it) inside nested ``anyOf`` branches — notably ``Dict[str,
         Any]`` variants, which Pydantic renders as
         ``{"type": "object", "additionalProperties": true}``.
      2. **Every property is listed in** ``required``. Pydantic omits
         optional fields (``Optional[X] = None``) from ``required`` even
         though their generated type already includes ``null``. Strict
         mode wants them present in ``required`` and the model is
         expected to emit ``null`` for "not provided".

    Failing either rule trips a ``400 invalid_json_schema`` from the
    OpenAI backend — observed in the wild as ``codex exec`` exiting with
    rc=1 and the actual error embedded in the stdout JSONL stream
    (``{"type": "error", "message": "Invalid schema for response_format
    'codex_output_schema': 'additionalProperties' is required to be
    supplied and to be false."}``), not in stderr. This walker is what
    we apply just-in-time before writing ``schema.json`` to the temp dir
    codex reads from.

    Semantic impact:

      * **Optional fields are preserved** — the model can still emit
        ``null`` for them since their generated type already includes a
        ``null`` branch in the original ``anyOf``.
      * **Open object payloads are narrowed.** A union member of the
        form ``{"type": "object", "additionalProperties": true}`` —
        emitted by Pydantic for a ``Dict[str, Any]`` field — becomes
        ``{"type": "object", "additionalProperties": false}`` with no
        ``properties``, i.e. accepts only ``{}``. Today this affects
        ``DirectRequest.content`` (``Union[str, Dict[str, Any]]``):
        through this adapter, a persona that wants to attach structured
        content to a direct_request MUST use the string branch (eg.
        a JSON-encoded envelope). The Pydantic model still accepts the
        object branch — the narrowing is only at the codex submission
        boundary. (Codex review T5 #1/#2 — flagged explicitly to avoid
        the misleading "protocol-equivalent" framing the first draft of
        this docstring used.)

    Pure-functional: returns a deep copy, leaves the input untouched.
    """
    import copy

    out = copy.deepcopy(schema)

    def _walk(node: Any) -> None:
        if isinstance(node, dict):
            # An object schema (explicit `type: object` OR has `properties`)
            # MUST have additionalProperties: false AND every property in
            # required.
            is_object = node.get("type") == "object" or "properties" in node
            if is_object:
                node["additionalProperties"] = False
                props = node.get("properties")
                if isinstance(props, dict):
                    node["required"] = list(props.keys())
            for v in node.values():
                _walk(v)
        elif isinstance(node, list):
            for item in node:
                _walk(item)

    _walk(out)
    return out

Runner = Callable[..., subprocess.CompletedProcess]


class CodexCliProvider(ProviderAdapter):
    """ProviderAdapter that invokes the `codex exec` CLI non-interactively.

    Constructor parameters mirror `ClaudeCliProvider`: `binary`,
    `default_model` (None / a sentinel → omit `-m`), `timeout`,
    `extra_args`, `isolated`, `workdir`, `env`, and a `runner`
    injection seam for tests. `check_binary` fail-fasts at construction
    when the CLI is absent.

    `workdir` (default ``None`` → ``os.getcwd()`` at invocation time)
    is the directory codex sees as its working tree (``-C`` argument).
    The default — the MCP server's cwd, inherited from the parent
    Claude Code session, typically the user's project root — matches
    claude-cli's natural cwd inheritance. Set explicitly for an
    isolated sandbox if needed. Sandbox stays ``-s read-only``, so
    a project-rooted workdir cannot be mutated. (v1.10.9+)

    `isolated` (default ``True``) adds ``--ignore-user-config`` and
    ``--ignore-rules`` to every invocation: those flags skip
    ``~/.codex/config.toml`` and any user/project execpolicy
    ``.rules`` file, so a programmatic non-interactive turn does not
    inherit the operator's interactive customizations. Auth still
    resolves via ``CODEX_HOME``. Note that with ``isolated=True``,
    ``model="auto"`` no longer reads any default model from
    ``~/.codex/config.toml`` — the codex CLI uses its built-in
    default. Requires codex >= 0.122.0 (older versions reject the
    flags); set ``isolated=False`` for older CLIs or to restore
    legacy behavior.

    `env` defaults to ``None``, which means
    :func:`~symposium.providers._cli_env.codex_child_env` (v1.10.7+):
    ``os.environ`` minus the inherited-state blocklist
    (nested-Claude-Code markers, effort overrides) AND minus
    Claude-only auth vars (``CLAUDE_CODE_OAUTH_TOKEN``,
    ``ANTHROPIC_*``) which codex never reads but would otherwise sit
    in the child's ``/proc/PID/environ`` — a cross-vendor credential
    leak with no operational reason (Codex review T1 #9). See
    :mod:`symposium.providers._cli_env`. Pass an explicit dict to
    override entirely (must include ``PATH`` and Windows
    ``SystemRoot`` for the spawn to succeed).
    """

    name = "codex-cli"

    def __init__(
        self,
        *,
        binary: str = DEFAULT_CODEX_BINARY,
        default_model: Optional[str] = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        extra_args: Optional[List[str]] = None,
        isolated: bool = True,
        workdir: Optional[str] = None,
        env: Optional[Dict[str, str]] = None,
        runner: Optional[Runner] = None,
        check_binary: bool = True,
    ) -> None:
        self._binary = binary
        self._default_model = default_model
        self._timeout = timeout
        self._extra_args = list(extra_args or [])
        self._isolated = isolated
        # `workdir` becomes the codex `-C` argument: the directory codex
        # sees as its working tree for read-only file inspection. None
        # (default, v1.10.9+) means `os.getcwd()` at invocation time —
        # the MCP server's cwd, inherited from the parent Claude Code
        # session, which is typically the user's project root. Same
        # semantics as claude-cli (no `-C` flag, picks up cwd
        # naturally). Set explicitly to a path for an isolated sandbox
        # (the pre-v1.10.9 behavior was an empty tmpdir, which made
        # personas blind to the codebase). Sandbox stays `read-only`,
        # so a project-rooted workdir still cannot be mutated.
        self._workdir = workdir
        self._env_override = env
        self._run: Runner = runner or subprocess.run
        if check_binary and runner is None and shutil.which(binary) is None:
            raise FileNotFoundError(
                f"the {binary!r} CLI was not found on PATH; install the Codex CLI and "
                f"run `{binary}` once to authenticate, or route to a different adapter. "
                "The codex-cli provider needs no API key — it reuses the CLI's login."
            )

    # ------------------------------------------------------------------
    # ProviderAdapter contract
    # ------------------------------------------------------------------

    def invoke(self, request: ProviderRequest) -> ProviderResult:
        first = self._invoke_once(request, prompt_suffix=None)
        if first.error is not None and first.error.kind == "malformed_response":
            details = first.error.details or {}
            suffix = (
                "\n\nYour previous response did not conform to the required schema "
                f"({details.get('expected_output_schema')}) at "
                f"`{details.get('failing_path')}`: {details.get('validator_message')}. "
                "Re-emit ONLY the structured object that conforms to the schema."
            )
            return self._invoke_once(request, prompt_suffix=suffix)
        return first

    # ------------------------------------------------------------------
    # Single-pass invocation
    # ------------------------------------------------------------------

    def _invoke_once(
        self, request: ProviderRequest, *, prompt_suffix: Optional[str]
    ) -> ProviderResult:
        prompt = _build_prompt(request)
        if prompt_suffix:
            prompt = f"{prompt}{prompt_suffix}"

        schema_json = _schema_for(request.expected_output_schema)
        # `tmpdir` holds only the --output-schema file (codex's
        # JSON-Schema output enforcement reads from a path, not stdin).
        # The codex working dir (-C) is a SEPARATE concern: v1.10.9+
        # defaults to `os.getcwd()` (= the MCP server's cwd, inherited
        # from the parent Claude Code session, i.e. the user's project
        # root) so personas can READ the project files just like
        # claude-cli does. Sandbox stays `read-only`, so no writes —
        # but visionary's "I can only see schema.json" failure mode
        # from v1.10.8 is closed. Opt back into the old neutral-cwd
        # behavior with `workdir="<isolated_tmp>"` on the constructor.
        schema_dir = tempfile.mkdtemp(prefix="symposium-codex-")
        workdir = self._workdir or os.getcwd()
        schema_path: Optional[str] = None
        try:
            argv: List[str] = [
                self._binary, "exec", "--json",
                "--skip-git-repo-check", "-s", "read-only", "-C", workdir,
            ]
            if self._isolated:
                # Don't pull in the operator's interactive customizations
                # for a programmatic non-interactive turn. Auth still
                # resolves via CODEX_HOME — these flags only skip config /
                # rules loading.
                argv += ["--ignore-user-config", "--ignore-rules"]
            model = request.model or self._default_model
            if model and model not in _AUTO_MODELS:
                argv += ["-m", model]
            if schema_json is not None:
                schema_path = os.path.join(schema_dir, "schema.json")
                with open(schema_path, "w", encoding="utf-8") as fh:
                    fh.write(schema_json)
                argv += ["--output-schema", schema_path]
            argv += self._extra_args
            # Pass the prompt via stdin (positional "-" tells codex exec to
            # read it from stdin). Avoids leaking the prompt — which may
            # contain the full transcript, problem statement, or persona
            # material — to other users via `ps` / process inspection.
            argv.append("-")

            try:
                proc = self._run(
                    argv,
                    input=prompt,
                    capture_output=True,
                    text=True,
                    timeout=self._timeout,
                    env=self._env_override if self._env_override is not None else codex_child_env(),
                )
            except subprocess.TimeoutExpired as exc:
                return _error_result(
                    "timeout", f"codex CLI timed out after {self._timeout}s: {exc}", True
                )
            except FileNotFoundError as exc:
                return _error_result("internal", f"codex CLI not found: {exc}", False)
            except Exception as exc:  # noqa: BLE001
                return _error_result("internal", f"failed to run codex CLI: {exc}", False)
        finally:
            shutil.rmtree(schema_dir, ignore_errors=True)

        if proc.returncode != 0:
            stderr = (proc.stderr or "").strip()
            # Codex emits the actual error in the stdout JSONL stream
            # (`{"type": "error", "message": "..."}`) for backend-side
            # failures (invalid schema, model error, auth), often with
            # an EMPTY stderr. Pre-v1.10.8 the operator just saw
            # `<no stderr>` and had to dig through artifact.json by
            # hand. Now: parse stdout for the JSONL error event and
            # surface its message verbatim.
            _, _, stdout_error = _parse_jsonl(proc.stdout or "")
            stdout_msg = ""
            if stdout_error is not None:
                # The error event's `message` field is sometimes a JSON
                # string itself (the upstream API error body) — try to
                # pretty-extract the human-readable bit if so, AND
                # surface the upstream `code` when present (Codex review
                # T5 #4: "invalid_json_schema" is often more diagnostic
                # than the message prose alone).
                raw_msg = stdout_error.get("message", "")
                if isinstance(raw_msg, str):
                    try:
                        inner = json.loads(raw_msg)
                        if isinstance(inner, dict):
                            err_obj = inner.get("error") or inner
                            if isinstance(err_obj, dict):
                                code = err_obj.get("code")
                                msg = err_obj.get("message", "") or raw_msg
                                stdout_msg = f"[{code}] {msg}" if code else msg
                            else:
                                stdout_msg = raw_msg
                        else:
                            stdout_msg = raw_msg
                    except json.JSONDecodeError:
                        stdout_msg = raw_msg
            detail = stderr or stdout_msg or "<no stderr, no stdout error>"
            kind, retriable = _classify_cli_exit(stderr or stdout_msg)
            return _error_result(
                kind,
                f"codex CLI exited {proc.returncode}: {detail[:500]}",
                retriable=retriable,
            )

        agent_text, usage_event, error_event = _parse_jsonl(proc.stdout or "")
        if error_event is not None:
            return _error_result(
                "internal", f"codex CLI reported an error: {json.dumps(error_event)[:400]}", False
            )

        usage = _usage_from_event(usage_event)
        finish = "stop"

        if schema_json is None:
            return ProviderResult(
                messages=[ProviderRawMessage(role="assistant", content=agent_text or "")],
                tool_events=[], usage=usage, finish_reason=finish,
                structured_output=None, raw=None, error=None,
            )

        if not agent_text:
            return _malformed(
                request, content="", usage=usage,
                failing_path="<root>",
                message="codex CLI returned no agent_message for the output schema",
            )
        try:
            structured = json.loads(agent_text)
        except json.JSONDecodeError as exc:
            return _malformed(
                request, content=agent_text, usage=usage,
                failing_path="<root>", message=f"agent_message is not valid JSON: {exc}",
            )
        if not isinstance(structured, dict):
            return _malformed(
                request, content=agent_text, usage=usage,
                failing_path="<root>", message="structured output is not a JSON object",
            )

        failure = validate_structured_output(structured, request.expected_output_schema)
        if failure is not None:
            return _malformed(
                request, content=agent_text, usage=usage,
                failing_path=failure["failing_path"], message=failure["validator_message"],
                raw_attempt=structured,
            )

        return ProviderResult(
            messages=[ProviderRawMessage(role="assistant", content=json.dumps(structured))],
            tool_events=[], usage=usage, finish_reason=finish,
            structured_output=structured, raw=None, error=None,
        )


# ---------------------------------------------------------------------------
# Helpers — module level
# ---------------------------------------------------------------------------


_SCHEMA_CACHE: Dict[str, str] = {}


def _schema_for(expected: Optional[str]) -> Optional[str]:
    """Pydantic JSON Schema for `expected`, **strictified for OpenAI**
    (v1.10.8+): see :func:`_strictify_for_openai`.

    Without strictify, `codex exec --output-schema` rejects the schema
    upfront with `invalid_json_schema` (the strict-mode backend requires
    `additionalProperties: false` on every object AND every property in
    `required`) — observed as a silent `rc=1` with the error in stdout
    JSONL, not stderr. Strictifying makes the schema submittable. Note
    the open-object narrowing the helper documents: through codex,
    `DirectRequest.content` accepts string or `{}` only — not arbitrary
    object payloads (use the string branch + JSON envelope for
    structured content).
    """
    if expected in (None, "null"):
        return None
    model = _SCHEMA_MODELS.get(expected)
    if model is None:
        return None
    if expected not in _SCHEMA_CACHE:
        strict = _strictify_for_openai(model.model_json_schema())
        _SCHEMA_CACHE[expected] = json.dumps(strict)
    return _SCHEMA_CACHE[expected]


def _build_prompt(request: ProviderRequest) -> str:
    """Fold the canonical messages into a single prompt (codex has no system flag)."""
    parts: List[str] = []
    for i, msg in enumerate(request.messages):
        text = msg.content if isinstance(msg.content, str) else json.dumps(msg.content)
        if i == 0 and msg.role == "system":
            parts.append(f"[SYSTEM]\n{text}")
        else:
            parts.append(text)
    return "\n\n".join(parts)


def _parse_jsonl(stdout: str):
    """Return (last_agent_message_text, usage_event_dict, error_event_dict)."""
    agent_text: Optional[str] = None
    usage_event: Optional[Dict[str, Any]] = None
    error_event: Optional[Dict[str, Any]] = None
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(ev, dict):
            continue
        etype = ev.get("type")
        if etype == "item.completed":
            item = ev.get("item") or {}
            if isinstance(item, dict) and item.get("type") == "agent_message":
                t = item.get("text")
                if isinstance(t, str):
                    agent_text = t  # last agent_message wins
        elif etype == "turn.completed":
            u = ev.get("usage")
            if isinstance(u, dict):
                usage_event = u
        elif etype in ("error", "stream.error"):
            # Prefer the first error/stream.error event WITH a non-empty
            # message — codex sometimes emits a leading `stream.error`
            # with no payload before the actual `error` event that
            # carries the upstream API complaint. Pure "first wins"
            # would lock us onto the empty one. (Codex review T5 #3.)
            # A later `turn.failed` event would otherwise overwrite the
            # actionable event, leaving the operator with a contentless
            # "<no stderr>" diagnostic. (v1.10.8)
            existing_msg = ""
            if isinstance(error_event, dict):
                em = error_event.get("message")
                if isinstance(em, str):
                    existing_msg = em.strip()
            new_msg = ev.get("message") if isinstance(ev.get("message"), str) else ""
            if not existing_msg and new_msg.strip():
                error_event = ev
            elif error_event is None:
                error_event = ev
        elif etype == "turn.failed":
            # turn.failed is a generic "the turn didn't complete" signal
            # — useful only when no richer error event preceded it.
            if error_event is None:
                error_event = ev
    return agent_text, usage_event, error_event


def _safe_int(v: Any) -> int:
    """Coerce a CLI-reported usage value to int; malformed → 0 (never raises)."""
    if v is None:
        return 0
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


def _classify_cli_exit(stderr: str) -> tuple[str, bool]:
    """Map a non-zero codex CLI exit to (error.kind, retriable).

    Looks for well-known phrases in stderr / stdout-tail. Falls back to
    `internal` non-retriable for unknown exits. Keeps the runtime in line
    with the closed §6.6 ErrorKind enum.
    """
    s = (stderr or "").lower()
    if "rate" in s and "limit" in s:
        return ("rate_limit", True)
    if "quota" in s or "exceeded" in s:
        return ("quota_exhausted", False)
    if "auth" in s or "unauthor" in s or "forbidden" in s or "login" in s:
        return ("auth_failure", False)
    if "context" in s and "length" in s:
        return ("context_length_exceeded", False)
    if "timeout" in s or "timed out" in s:
        return ("timeout", True)
    if "network" in s or "connection" in s or "dns" in s:
        return ("network", True)
    return ("internal", False)


def _usage_from_event(usage_event: Optional[Dict[str, Any]]) -> Usage:
    u = usage_event or {}
    prompt = _safe_int(u.get("input_tokens")) + _safe_int(u.get("cached_input_tokens"))
    completion = (
        _safe_int(u.get("output_tokens")) + _safe_int(u.get("reasoning_output_tokens"))
    )
    # Codex exec reports no cost; under a subscription login there is no
    # metered charge anyway → cost 0.0 + estimated so the metrics flag it.
    return Usage(
        prompt_tokens=prompt,
        completion_tokens=completion,
        total_tokens=prompt + completion,
        cost_usd=0.0,
        estimated=True,
    )


def _error_result(kind: str, message: str, retriable: bool) -> ProviderResult:
    return ProviderResult(
        messages=[], tool_events=[],
        usage=Usage(prompt_tokens=0, completion_tokens=0, total_tokens=0, cost_usd=0.0),
        finish_reason="error", structured_output=None, raw=None,
        error=ProviderError(kind=kind, message=message, retriable=retriable),  # type: ignore[arg-type]
    )


def _malformed(
    request: ProviderRequest, *, content: str, usage: Usage,
    failing_path: str, message: str, raw_attempt: Any = None,
) -> ProviderResult:
    return ProviderResult(
        messages=[ProviderRawMessage(role="assistant", content=content or "<empty>")],
        tool_events=[], usage=usage, finish_reason="error",
        structured_output=None, raw=None,
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

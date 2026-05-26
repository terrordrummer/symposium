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
from symposium.providers._http_common import validate_structured_output
from symposium.providers.base import ProviderAdapter

DEFAULT_CODEX_BINARY = "codex"
DEFAULT_TIMEOUT_SECONDS = 240.0
# Model strings that mean "let the CLI choose its default" → omit -m.
_AUTO_MODELS = {"", "auto", "default", "codex-default"}

_SCHEMA_MODELS: Dict[str, Any] = {
    "turn_structured_output": TurnStructuredOutput,
    "verdict": Verdict,
    "synthesis_content": SynthesisContent,
}

Runner = Callable[..., subprocess.CompletedProcess]


class CodexCliProvider(ProviderAdapter):
    """ProviderAdapter that invokes the `codex exec` CLI non-interactively.

    Constructor parameters mirror `ClaudeCliProvider`: `binary`,
    `default_model` (None / a sentinel → omit `-m`), `timeout`,
    `extra_args`, and a `runner` injection seam for tests. `check_binary`
    fail-fasts at construction when the CLI is absent.
    """

    name = "codex-cli"

    def __init__(
        self,
        *,
        binary: str = DEFAULT_CODEX_BINARY,
        default_model: Optional[str] = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        extra_args: Optional[List[str]] = None,
        runner: Optional[Runner] = None,
        check_binary: bool = True,
    ) -> None:
        self._binary = binary
        self._default_model = default_model
        self._timeout = timeout
        self._extra_args = list(extra_args or [])
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
        tmpdir = tempfile.mkdtemp(prefix="symposium-codex-")
        schema_path: Optional[str] = None
        try:
            argv: List[str] = [
                self._binary, "exec", "--json",
                "--skip-git-repo-check", "-s", "read-only", "-C", tmpdir,
            ]
            model = request.model or self._default_model
            if model and model not in _AUTO_MODELS:
                argv += ["-m", model]
            if schema_json is not None:
                schema_path = os.path.join(tmpdir, "schema.json")
                with open(schema_path, "w", encoding="utf-8") as fh:
                    fh.write(schema_json)
                argv += ["--output-schema", schema_path]
            argv += self._extra_args
            argv.append(prompt)

            try:
                proc = self._run(
                    argv, input="", capture_output=True, text=True, timeout=self._timeout
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
            shutil.rmtree(tmpdir, ignore_errors=True)

        if proc.returncode != 0:
            stderr = (proc.stderr or "").strip()
            return _error_result(
                "internal",
                f"codex CLI exited {proc.returncode}: {stderr[:500] or '<no stderr>'}",
                retriable=False,
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
    if expected in (None, "null"):
        return None
    model = _SCHEMA_MODELS.get(expected)
    if model is None:
        return None
    if expected not in _SCHEMA_CACHE:
        _SCHEMA_CACHE[expected] = json.dumps(model.model_json_schema())
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
        elif etype in ("turn.failed", "error", "stream.error"):
            error_event = ev
    return agent_text, usage_event, error_event


def _usage_from_event(usage_event: Optional[Dict[str, Any]]) -> Usage:
    u = usage_event or {}
    prompt = int(u.get("input_tokens", 0) or 0) + int(u.get("cached_input_tokens", 0) or 0)
    completion = (
        int(u.get("output_tokens", 0) or 0) + int(u.get("reasoning_output_tokens", 0) or 0)
    )
    # Codex exec reports no cost → mark estimated so the runtime/metrics know.
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

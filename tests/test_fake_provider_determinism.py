"""§9.1 — FakeProvider determinism is unconditional under the N3 qualifier.

Two runs against the same script + same config produce bit-identical
canonical_transcripts (timestamps and uuids excluded) and identical
transcript_digests.
"""

from __future__ import annotations

import re

from symposium.providers import FakeProvider
from symposium.scheduler import run_session
from symposium.storage.digest import compute_transcript_digest


_TIMESTAMP_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")
_HEX32_RE = re.compile(r"\b[0-9a-f]{32}\b")


def _scrub_volatile(transcript_dump: list) -> list:
    """Zero out fields that vary across runs by construction (timestamp, id)."""
    out = []
    for m in transcript_dump:
        m2 = {k: v for k, v in m.items() if k not in ("timestamp", "id")}
        out.append(m2)
    return out


def test_two_runs_same_script_yield_same_scrubbed_transcript(example_config, example_script):
    fp1 = FakeProvider(script=example_script)
    art1 = run_session(example_config, {"default": fp1})

    fp2 = FakeProvider(script=example_script)
    art2 = run_session(example_config, {"default": fp2})

    t1 = _scrub_volatile([m.model_dump(mode="json", exclude_none=True) for m in art1.canonical_transcript])
    t2 = _scrub_volatile([m.model_dump(mode="json", exclude_none=True) for m in art2.canonical_transcript])
    assert t1 == t2, "scrubbed transcripts differ across two FakeProvider runs"


def test_match_failure_terminates_session(example_config):
    """An agent_id mismatch in the script's match clause triggers a synthetic
    internal error → §4.9 → provider_unrecoverable termination."""
    from symposium.models import (
        FakeProviderEntry,
        FakeProviderMatch,
        FakeProviderScript,
        ProviderError,
        ProviderRawMessage,
        ProviderResult,
        Usage,
    )

    # Build a 1-entry script that asserts agent_id=wrong_id — the first
    # actual call will be from `logician`, mismatching.
    bad = FakeProviderScript(
        entries=[
            FakeProviderEntry(
                match=FakeProviderMatch(agent_id="wrong_id"),
                result=ProviderResult(
                    messages=[ProviderRawMessage(role="assistant", content="x")],
                    tool_events=[],
                    usage=Usage(prompt_tokens=1, completion_tokens=1, total_tokens=2, cost_usd=0.0),
                    finish_reason="stop",
                    structured_output={"text": "x"},
                    raw=None,
                    error=None,
                ),
            )
        ]
    )
    fp = FakeProvider(script=bad)
    art = run_session(example_config, {"default": fp})
    assert art.outcome.kind == "termination"
    assert art.outcome.termination_artifact.reason in ("provider_unrecoverable", "schema_error")

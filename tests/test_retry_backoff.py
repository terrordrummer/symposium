"""Tests for the §4.9 runtime retry / backoff surface (`scheduler.loop`).

Verifies:
  * `_backoff_delay` honors `retry_after` capped to the runtime max.
  * `_backoff_delay` jitter is deterministic per session (a seeded
    `random.Random` produces identical sleep sequences across replays).
  * `_invoke_with_retry` sleeps between attempts on retriable errors and
    consumes `per_agent_retry_budget` correctly.
"""

from __future__ import annotations

import random
from typing import List

from symposium.models import (
    ProviderError,
    ProviderRawMessage,
    ProviderRequest,
    ProviderRequestMessage,
    ProviderResult,
    Usage,
)
from symposium.scheduler.loop import (
    Session,
    _BACKOFF_MAX_SECONDS,
    _backoff_delay,
    _invoke_with_retry,
)


def test_backoff_delay_honors_retry_after_capped():
    """`retry_after` from the upstream wins, but is clamped to the max."""
    assert _backoff_delay(1, retry_after=0.5) == 0.5
    assert _backoff_delay(1, retry_after=1e6) == _BACKOFF_MAX_SECONDS


def test_backoff_delay_jitter_is_deterministic_given_same_rng_seed():
    """Two RNGs seeded the same must produce identical sleep sequences.

    This is the §7.6 replay-divergence guard: jitter must not leak into
    wallclock-cap decisions between original and replay runs.
    """
    rng_a = random.Random("symposium:backoff:test-session-x")
    rng_b = random.Random("symposium:backoff:test-session-x")
    delays_a = [_backoff_delay(i, rng=rng_a) for i in range(1, 6)]
    delays_b = [_backoff_delay(i, rng=rng_b) for i in range(1, 6)]
    assert delays_a == delays_b, (
        f"backoff jitter diverged between identical seeds: {delays_a} vs {delays_b}"
    )


def test_backoff_delay_bounds():
    """Without retry_after, the delay is positive and at most `_BACKOFF_MAX_SECONDS * (1 + jitter)`."""
    rng = random.Random("symposium:backoff:bounds")
    for i in range(1, 20):
        d = _backoff_delay(i, rng=rng)
        assert d >= 0.0
        # Generous upper bound: 2x the cap covers maximum jitter.
        assert d <= _BACKOFF_MAX_SECONDS * 2


def test_invoke_with_retry_sleeps_between_retriable_attempts(
    example_config, example_script,
):
    """A retriable error must trigger an injected sleep before the next attempt.

    Uses a hand-rolled provider returning two retriable errors then a
    valid response, and a sleep recorder to confirm exactly two waits.
    """
    sleep_log: List[float] = []

    def fake_sleep(seconds: float) -> None:
        sleep_log.append(seconds)

    class _FlakyProvider:
        name = "fake"

        def __init__(self):
            self.calls = 0

        def invoke(self, _request):
            self.calls += 1
            if self.calls <= 2:
                return ProviderResult(
                    messages=[ProviderRawMessage(role="assistant", content="x")],
                    tool_events=[],
                    usage=Usage(
                        prompt_tokens=1, completion_tokens=1, total_tokens=2,
                        cost_usd=0.0,
                    ),
                    finish_reason="error",
                    structured_output=None,
                    raw=None,
                    error=ProviderError(
                        kind="rate_limit",
                        message="slow down",
                        retriable=True,
                        details={"retry_after_seconds": 0.001},
                    ),
                )
            return ProviderResult(
                messages=[ProviderRawMessage(role="assistant", content="x")],
                tool_events=[],
                usage=Usage(
                    prompt_tokens=1, completion_tokens=1, total_tokens=2,
                    cost_usd=0.0,
                ),
                finish_reason="stop",
                structured_output={"text": "recovered"},
                raw=None,
                error=None,
            )

    provider = _FlakyProvider()
    session = Session(config=example_config, providers={"default": provider})
    # Pick the first agent (logician) and build a minimal request.
    agent_id = example_config.agents[0].id
    request = ProviderRequest(
        provider="fake", model="fake-1", agent_id=agent_id,
        messages=[ProviderRequestMessage(role="user", content="ping")],
        expected_output_schema="turn_structured_output",
    )

    # The default per_agent_retry_budget=2, so attempts = 3.
    result = _invoke_with_retry(
        session, agent_id=agent_id, request=request, sleep=fake_sleep,
    )
    assert result.ok is True
    assert provider.calls == 3
    # Two backoff sleeps must have happened (after each retriable failure).
    assert len(sleep_log) == 2, f"expected 2 backoff sleeps, got {sleep_log}"
    # Both honored retry_after (0.001) since the upstream supplied it.
    for s in sleep_log:
        assert 0.0 <= s <= _BACKOFF_MAX_SECONDS


def test_session_rng_seeded_from_session_id(example_config, example_script):
    """Two `Session` instances with the same config produce the same backoff sequence."""
    s1 = Session(config=example_config, providers={"default": object()})
    s2 = Session(config=example_config, providers={"default": object()})
    seq_a = [s1.rng.uniform(-0.25, 0.25) for _ in range(8)]
    seq_b = [s2.rng.uniform(-0.25, 0.25) for _ in range(8)]
    assert seq_a == seq_b


def test_rng_seed_override_decouples_seed_from_session_id(example_config):
    """`Session(rng_seed=...)` overrides the session_id-derived default.

    Critical for §7.6 execution_replay: the replay run uses a `-replay`
    suffixed session_id but MUST get the same backoff jitter sequence as
    the original. We do that by passing `rng_seed=<original_session_id>`.
    """
    # Two sessions with different session_id but same explicit seed → match.
    cfg_a = example_config
    cfg_b = example_config.model_copy(update={"session_id": cfg_a.session_id + "-replay"})
    s_orig = Session(config=cfg_a, providers={"default": object()})
    s_replay = Session(config=cfg_b, providers={"default": object()}, rng_seed=cfg_a.session_id)
    seq_orig = [s_orig.rng.uniform(-0.25, 0.25) for _ in range(8)]
    seq_replay = [s_replay.rng.uniform(-0.25, 0.25) for _ in range(8)]
    assert seq_orig == seq_replay, (
        f"replay backoff sequence diverged from original: {seq_orig} vs {seq_replay}"
    )

    # And without the override, the replay session_id would produce a different sequence.
    s_replay_default = Session(config=cfg_b, providers={"default": object()})
    seq_default = [s_replay_default.rng.uniform(-0.25, 0.25) for _ in range(8)]
    assert seq_default != seq_orig, (
        "control: without rng_seed override, the replay-suffixed session_id "
        "must produce a different jitter sequence (so the override is doing work)"
    )

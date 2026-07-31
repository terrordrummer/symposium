"""Runtime identity guards + execution_replay honesty fixes.

  * duplicate agent ids / panel entries are rejected at session start
    (`agent_by_id` returns the first match, so a duplicate silently
    shadows its twin — schema-valid configs must still parse, so this is
    a runtime check, not a model validator);
  * the §7.6 wallclock condition is reported as assumed (cap / deadline
    decisions read the unpinned monotonic clock), never silently checked;
  * a near-limit original session_id no longer pushes the derived
    "<sid>-replay" id past the 64-char §7.1 limit.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import List

import pytest

from symposium.models import (
    AgentConfig,
    BudgetConfig,
    Config,
    Persona,
    SelectorConfig,
)
from symposium.providers import FakeProvider
from symposium.replay import execution_replay
from symposium.scheduler import run_session


# ---------------------------------------------------------------------------
# duplicate-id guards
# ---------------------------------------------------------------------------


def _persona(persona_id: str) -> Persona:
    return Persona(
        persona_class="horizontal",
        id=persona_id,
        reasoning_scope="test",
        reasoning_style="test",
        behavioral_constraints=["x"],
        failure_modes=["y"],
    )


def _config(
    *,
    agent_ids: List[str],
    panel: List[str],
    coordinator_id: str = "coord",
    session_id: str = "dup-guard",
) -> Config:
    agents = [
        AgentConfig(id=aid, persona_ref=_persona(aid), provider="fake", model="fake-1")
        for aid in agent_ids
    ]
    coord = AgentConfig(
        id=coordinator_id,
        persona_ref=_persona(coordinator_id),
        provider="fake",
        model="fake-1",
    )
    return Config(
        schema_version="1.0.0",
        session_id=session_id,
        originator="test",
        problem_statement="P",
        selector=SelectorConfig(
            strategy="fixed",
            default_deliberation_panel=panel,
            coordinator_agent=coordinator_id,
        ),
        agents=agents,
        coordinator=coord,
        budget=BudgetConfig(
            max_total_tokens=1000,
            max_total_cost_usd=1.0,
            max_rounds=1,
            max_wallclock_seconds=60,
        ),
    )


def test_duplicate_agent_ids_rejected_at_session_start(tmp_path, example_script):
    config = _config(agent_ids=["alpha", "alpha"], panel=["alpha"])
    with pytest.raises(ValueError, match="duplicate agent id"):
        run_session(
            config,
            {"default": FakeProvider(script=example_script)},
            runs_root=str(tmp_path),
        )
    # Rejected before any side effect: no run directory was created.
    assert not (tmp_path / config.session_id).exists()


def test_coordinator_id_colliding_with_panel_agent_rejected(example_script):
    config = _config(
        agent_ids=["alpha", "beta"], panel=["alpha", "beta"], coordinator_id="alpha"
    )
    with pytest.raises(ValueError, match="collides"):
        run_session(config, {"default": FakeProvider(script=example_script)})


def test_duplicate_panel_entries_rejected(example_script):
    config = _config(agent_ids=["alpha", "beta"], panel=["alpha", "alpha"])
    with pytest.raises(ValueError, match="duplicate entries"):
        run_session(config, {"default": FakeProvider(script=example_script)})


# ---------------------------------------------------------------------------
# execution_replay: wallclock disposition + replay-sid clamping
# ---------------------------------------------------------------------------


def _make_original_run(tmp_path, config, script):
    run_session(config, {"default": FakeProvider(script=script)}, runs_root=str(tmp_path))
    return tmp_path / config.session_id


def test_wallclock_condition_assumed_by_default(tmp_path, example_config, example_script):
    """Cap/deadline decisions read the unpinned monotonic clock; without a
    caller-supplied fixed_clock the wallclock condition must be reported as
    assumed (with a warning), not checked."""
    run_dir = _make_original_run(tmp_path, example_config, example_script)
    result = execution_replay(
        run_dir, providers={"default": FakeProvider(script=example_script)}
    )
    assert "wallclock" in result.conditions_assumed
    assert "wallclock" not in result.conditions_checked
    assert any(
        "wallclock" in w and "NOT pinned" in w for w in result.warnings
    ), result.warnings


def test_wallclock_budget_half_assumed_even_with_fixed_clock(
    tmp_path, example_config, example_script
):
    """A fixed_clock pins the message-timestamp half (checked) but the
    budget-decision half stays an assumption — both dispositions recorded,
    mirroring the `model` presence/snapshot split."""
    run_dir = _make_original_run(tmp_path, example_config, example_script)
    fixed = datetime(2000, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
    result = execution_replay(
        run_dir,
        providers={"default": FakeProvider(script=example_script)},
        fixed_clock=lambda: fixed,
    )
    assert "wallclock" in result.conditions_checked
    assert "wallclock" in result.conditions_assumed


def test_replay_session_id_clamped_to_64_chars(tmp_path, example_config, example_script):
    """An original session_id at the 64-char limit must not make the derived
    "<sid>-replay" id overflow §7.1 (pre-fix: raw ValueError after all
    conditions passed)."""
    long_sid = "x" * 64
    config = example_config.model_copy(update={"session_id": long_sid})
    run_dir = _make_original_run(tmp_path, config, example_script)

    result = execution_replay(
        run_dir, providers={"default": FakeProvider(script=example_script)}
    )
    fresh_sid = result.fresh_artifact.config.session_id
    assert len(fresh_sid) <= 64
    assert fresh_sid.endswith("-replay")
    assert result.digest_matches


def test_clamped_replay_ids_do_not_collide(tmp_path, example_config, example_script):
    """Two long session ids sharing a 57-char prefix must derive distinct
    replay ids: plain prefix truncation would replay both into the same
    directory, silently mixing two runs' outputs."""
    sids = ["y" * 63 + "a", "y" * 63 + "b"]
    fresh = []
    for sid in sids:
        config = example_config.model_copy(update={"session_id": sid})
        run_dir = _make_original_run(tmp_path / sid[-1], config, example_script)
        result = execution_replay(
            run_dir, providers={"default": FakeProvider(script=example_script)}
        )
        fresh.append(result.fresh_artifact.config.session_id)
    assert fresh[0] != fresh[1]
    assert all(len(s) <= 64 and s.endswith("-replay") for s in fresh)

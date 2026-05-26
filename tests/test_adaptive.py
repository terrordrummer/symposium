"""Adaptive deliberation: dynamic agent generation (early-start + runtime).

The session runner (`run_one`) and the persona `caller` are injected, so
these tests assert the orchestration logic without running a real session
or spawning a CLI.
"""

from __future__ import annotations

import types

import pytest

pytest.importorskip("mcp")

from symposium.integrations.mcp_server import _run_adaptive  # noqa: E402


# --- lightweight stub artifacts (only the fields _run_adaptive reads) -------

def _synth():
    return types.SimpleNamespace(outcome=types.SimpleNamespace(kind="synthesis"))


def _termination(reason="user_input_required", question="need a cryptographer", query=None):
    ta = types.SimpleNamespace(
        reason=reason,
        pending_user_input_request=types.SimpleNamespace(question=question) if question else None,
        pending_external_research_request=types.SimpleNamespace(query=query) if query else None,
    )
    return types.SimpleNamespace(
        outcome=types.SimpleNamespace(kind="termination", termination_artifact=ta)
    )


class _StubRunner:
    """Returns scripted (artifact, result) per call; records the configs seen."""

    def __init__(self, script):
        self._script = list(script)
        self.configs = []
        self._i = 0

    def __call__(self, config):
        self.configs.append(config)
        artifact, result = self._script[min(self._i, len(self._script) - 1)]
        self._i += 1
        return artifact, result


class _PersonaCallerStub:
    def __init__(self):
        self.n = 0

    def __call__(self, prompt, schema):
        self.n += 1
        return {
            "persona_class": "domain", "id": f"expert{self.n}",
            "reasoning_scope": "scope", "reasoning_style": "style",
            "behavioral_constraints": ["c"], "failure_modes": ["f"],
            "domain_scope": ["d"], "forbidden_domains": ["u"],
            "must_delegate": {"x": "y"},
        }


def _adaptive(tmp_path, runner, *, experts=None, max_expansions=2):
    return _run_adaptive(
        problem="Should we ship it?", panel=["logician", "critic"],
        coordinator="coordinator", provider="fake", experts=experts,
        max_expansions=max_expansions, max_rounds=4, max_total_tokens=1000,
        max_total_cost_usd=1.0, max_wallclock_seconds=10, output_dir=str(tmp_path),
        persona_caller=_PersonaCallerStub(), run_one=runner,
    )


def test_early_start_adds_expert_before_first_run(tmp_path):
    runner = _StubRunner([(_synth(), {"outcome": "synthesis", "synthesis_answer": "ok"})])
    out = _adaptive(tmp_path, runner, experts=["a security expert"])

    assert [g["phase"] for g in out["generated_agents"]] == ["early_start"]
    assert out["generated_agents"][0]["id"] == "expert1"
    assert out["expansions"] == 0
    assert out["final"]["outcome"] == "synthesis"
    assert "expert1" in out["panel_final"]
    # the expert was on the panel for the very first (and only) session
    first_cfg = runner.configs[0]
    assert "expert1" in [a.id for a in first_cfg.agents]


def test_runtime_expansion_generates_and_continues(tmp_path):
    runner = _StubRunner([
        (_termination(question="need a cryptographer"),
         {"outcome": "termination", "termination_reason": "user_input_required"}),
        (_synth(), {"outcome": "synthesis", "synthesis_answer": "done"}),
    ])
    out = _adaptive(tmp_path, runner)

    assert [g["phase"] for g in out["generated_agents"]] == ["runtime"]
    assert out["expansions"] == 1
    assert out["final"]["outcome"] == "synthesis"
    assert "expert1" in out["panel_final"]
    # the continuation session saw the new agent + carried-forward context
    assert len(runner.configs) == 2
    assert "expert1" in [a.id for a in runner.configs[1].agents]
    assert "[CONTINUATION]" in runner.configs[1].problem_statement


def test_runtime_expansion_respects_cap(tmp_path):
    # always terminates asking for help → capped at max_expansions
    runner = _StubRunner([
        (_termination(), {"outcome": "termination", "termination_reason": "user_input_required"}),
    ])
    out = _adaptive(tmp_path, runner, max_expansions=2)
    assert out["expansions"] == 2
    assert len(out["generated_agents"]) == 2
    assert len(runner.configs) == 3  # initial + 2 expansions
    assert out["final"]["outcome"] == "termination"


def test_non_expansion_termination_stops_without_generating(tmp_path):
    runner = _StubRunner([
        (_termination(reason="budget_exceeded", question=None),
         {"outcome": "termination", "termination_reason": "budget_exceeded"}),
    ])
    out = _adaptive(tmp_path, runner)
    assert out["generated_agents"] == []
    assert out["expansions"] == 0
    assert len(runner.configs) == 1
    assert out["final"]["termination_reason"] == "budget_exceeded"


def test_external_research_also_triggers_expansion(tmp_path):
    runner = _StubRunner([
        (_termination(reason="external_research_required", question=None, query="benchmark data"),
         {"outcome": "termination", "termination_reason": "external_research_required"}),
        (_synth(), {"outcome": "synthesis", "synthesis_answer": "done"}),
    ])
    out = _adaptive(tmp_path, runner)
    assert out["expansions"] == 1
    assert out["generated_agents"][0]["need"] == "benchmark data"

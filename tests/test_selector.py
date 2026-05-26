"""§4.1 / §5.11 selector tests — fixed / rules / llm strategies.

Covers the M6 done-criteria: the degenerate `fixed` selection and its
ADR-005 zero-invocation guarantee (`selector_fixed_no_provider_invocation`,
§9.7), the behaviour-preservation digest guard, the deterministic `rules`
strategy + its `schema_error` path, the bounded `llm` invocation + its
`budget_exceeded` path (`budget_selector`, §9.7), the dispatcher
invariants, `SelectorOutput` schema round-trip, `selector_output.json`
persistence, and a CLI end-to-end.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner
from jsonschema import Draft202012Validator

from symposium.cli.main import main as cli_group
from symposium.models import (
    AgentConfig,
    BudgetConfig,
    Config,
    FakeProviderScript,
    SelectorBudget,
    SelectorConfig,
    SelectorOutput,
)
from symposium.personas import COORDINATOR, LOGICIAN, persona_by_id
from symposium.providers import FakeProvider
from symposium.replay import pinned_runtime
from symposium.scheduler import run_session
from symposium.selector import (
    SelectorBudgetExceeded,
    SelectorError,
    run_selector,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = REPO_ROOT / "examples"

# Pinned pre-M6 walking-skeleton digest (deterministic ids + fixed clock).
# The `fixed` selector refactor is a pure extraction; this digest MUST NOT
# move (§3 behaviour-preservation hard rule).
PRE_M6_WALKING_SKELETON_DIGEST = (
    "bc6232b80e7c5e3934b3acf33b5808d23f1b6f25b7e2d376aece2e38030044db"
)


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------


def _resolve_persona_refs(raw: dict) -> dict:
    for ac in raw.get("agents", []):
        ref = ac.get("persona_ref")
        if isinstance(ref, str):
            try:
                ac["persona_ref"] = persona_by_id(ref).model_dump(exclude_none=True)
            except KeyError:
                pass
    coord = raw.get("coordinator")
    if isinstance(coord, dict):
        ref = coord.get("persona_ref")
        if isinstance(ref, str):
            try:
                coord["persona_ref"] = persona_by_id(ref).model_dump(exclude_none=True)
            except KeyError:
                pass
    return raw


def _load_example_config(name: str) -> Config:
    raw = yaml.safe_load((EXAMPLES / "configs" / name).read_text())
    return Config.model_validate(_resolve_persona_refs(raw))


@pytest.fixture
def rules_config() -> Config:
    return _load_example_config("rules-selector.yaml")


@pytest.fixture
def llm_config() -> Config:
    return _load_example_config("llm-selector.yaml")


@pytest.fixture
def llm_selector_script() -> FakeProviderScript:
    return FakeProviderScript.model_validate(
        json.loads((EXAMPLES / "scripts" / "llm-selector.json").read_text())
    )


def _selector_output_validator() -> Draft202012Validator:
    schema = json.loads(
        (REPO_ROOT / "docs" / "schemas" / "v1.0.0" / "selector_output.schema.json").read_text()
    )
    return Draft202012Validator(schema)


def _llm_decision_script(payload: dict, *, usage: dict | None = None) -> FakeProviderScript:
    """Build a one-entry selector script whose free-text message is `payload` JSON."""
    return FakeProviderScript.model_validate(
        {
            "schema_version": "1.0.0",
            "on_exhaustion": "error",
            "entries": [
                {
                    "result": {
                        "messages": [{"role": "assistant", "content": json.dumps(payload)}],
                        "tool_events": [],
                        "usage": usage
                        or {
                            "prompt_tokens": 10,
                            "completion_tokens": 10,
                            "total_tokens": 20,
                            "cost_usd": 0.0001,
                        },
                        "finish_reason": "stop",
                        "structured_output": None,
                        "raw": None,
                        "error": None,
                    }
                }
            ],
        }
    )


def _minimal_config(*, strategy: str, problem: str, coordinator_agent: str = "coordinator",
                    selector_budget: SelectorBudget | None = None) -> Config:
    """A two-agent synthetic config for the error / edge paths."""
    return Config(
        schema_version="1.0.0",
        session_id="selector-synth-001",
        originator="user",
        problem_statement=problem,
        selector=SelectorConfig(
            strategy=strategy,  # type: ignore[arg-type]
            default_deliberation_panel=["logician"],
            coordinator_agent=coordinator_agent,
            selector_budget=selector_budget,
        ),
        agents=[AgentConfig(id="logician", persona_ref=LOGICIAN, provider="fake", model="m")],
        coordinator=AgentConfig(
            id="coordinator", persona_ref=COORDINATOR, provider="fake", model="m"
        ),
        budget=BudgetConfig(
            max_total_tokens=100000, max_total_cost_usd=5.0, max_rounds=4, max_wallclock_seconds=60
        ),
    )


# ---------------------------------------------------------------------------
# fixed
# ---------------------------------------------------------------------------


def test_fixed_selection_equals_declared_panel(example_config):
    out = run_selector(example_config)
    assert out.strategy == "fixed"
    assert out.selected_agents == list(example_config.selector.default_deliberation_panel)
    assert out.coordinator_agent == example_config.selector.coordinator_agent
    assert out.coordinator_agent == example_config.coordinator.id


def test_fixed_no_provider_invocation(example_config, example_script):
    """§9.7 selector_fixed_no_provider_invocation: the selector phase makes
    ZERO provider calls, and a full fixed run's invocation count equals the
    deliberation + finalize dispatch count only."""
    sel_fp = FakeProvider(script=example_script)
    run_selector(example_config, providers={"default": sel_fp})
    assert sel_fp.invocation_count == 0  # selector made no provider call

    run_fp = FakeProvider(script=example_script)
    run_session(example_config, {"default": run_fp})
    # 5 primary + 1 coordination per round × 2 rounds + 1 synthesis = 13.
    assert run_fp.invocation_count == 13


def test_fixed_digest_byte_identical_pre_m6(tmp_path, example_config, example_script):
    """Behaviour-preservation guard: the `fixed` refactor must reproduce the
    exact pre-M6 walking-skeleton transcript_digest (§3 hard rule)."""
    fp = FakeProvider(script=example_script)
    with pinned_runtime(clock=lambda: "2025-01-01T00:00:00Z"):
        artifact = run_session(example_config, {"default": fp}, runs_root=str(tmp_path))
    assert artifact.transcript_digest == PRE_M6_WALKING_SKELETON_DIGEST


# ---------------------------------------------------------------------------
# rules
# ---------------------------------------------------------------------------


def test_rules_selection_is_deterministic(rules_config):
    a = run_selector(rules_config)
    b = run_selector(rules_config)
    assert a.strategy == "rules"
    assert a.model_dump() == b.model_dump()
    # the trigger-rich fixture selects all five default agents
    assert a.selected_agents == list(rules_config.selector.default_deliberation_panel)


def test_rules_no_match_raises_selector_error():
    cfg = _minimal_config(strategy="rules", problem="hello there, nothing relevant here at all")
    with pytest.raises(SelectorError):
        run_selector(cfg)


def test_rules_no_match_terminates_schema_error(tmp_path):
    """§4.1: an empty/malformed selection → terminate(schema_error) before round 1."""
    cfg = _minimal_config(strategy="rules", problem="nothing matches any persona scope keyword")
    fp = FakeProvider(script=_llm_decision_script({"selected_agents": ["logician"]}))
    artifact = run_session(cfg, {"default": fp}, runs_root=str(tmp_path))
    assert artifact.outcome.kind == "termination"
    assert artifact.outcome.termination_artifact.reason == "schema_error"
    # seed transcript: only the problem_statement message persisted
    assert len(artifact.canonical_transcript) == 1
    assert artifact.canonical_transcript[0].type == "problem_statement"
    # the selector made no provider call, so the deliberation script is untouched
    assert fp.invocation_count == 0
    # no selector_output.json written on a selector failure
    assert not (tmp_path / cfg.session_id / "selector_output.json").exists()


def test_rules_records_exclusions_when_partial(example_config):
    """The walking-skeleton problem triggers no scopes → all excluded → error;
    a partial-match problem records dropped agents with reasons."""
    cfg = example_config.model_copy(
        update={
            "selector": example_config.selector.model_copy(update={"strategy": "rules"}),
            "problem_statement": "Prove the design is logically consistent.",
        }
    )
    out = run_selector(cfg)
    selected = set(out.selected_agents)
    assert "logician" in selected and "visionary" in selected  # formal + creative
    assert out.excluded_agents is not None
    excluded_ids = {e.id for e in out.excluded_agents}
    assert excluded_ids and excluded_ids.isdisjoint(selected)
    for exc in out.excluded_agents:
        assert exc.reason  # every exclusion carries a rationale


def test_rules_panel_drives_full_deliberation(tmp_path, rules_config, example_script):
    """The rules-selected panel drives a complete deliberation to a valid Artifact."""
    fp = FakeProvider(script=example_script)
    artifact = run_session(rules_config, {"default": fp}, runs_root=str(tmp_path))
    assert artifact.outcome.kind == "synthesis"
    _assert_artifact_schema_valid(tmp_path / rules_config.session_id)


# ---------------------------------------------------------------------------
# llm
# ---------------------------------------------------------------------------


def test_llm_selection_via_fake_script(llm_config, llm_selector_script):
    fp = FakeProvider(script=llm_selector_script)
    out = run_selector(llm_config, providers={"default": fp})
    assert out.strategy == "llm"
    assert out.selected_agents == ["logician", "visionary", "researcher", "critic", "engineer"]
    assert out.coordinator_agent == "coordinator"
    assert out.reasoning
    assert fp.invocation_count == 1  # exactly one bounded invocation
    # schema-valid
    errors = list(_selector_output_validator().iter_errors(out.model_dump(mode="json", exclude_none=True)))
    assert not errors


def test_llm_requires_providers(llm_config):
    with pytest.raises(SelectorError):
        run_selector(llm_config, providers=None)


def test_llm_structured_output_payload_path(llm_config):
    """The selector also accepts a structured_output dict (not only free text)."""
    script = FakeProviderScript.model_validate(
        {
            "schema_version": "1.0.0",
            "on_exhaustion": "error",
            "entries": [
                {
                    "result": {
                        "messages": [{"role": "assistant", "content": "ignored"}],
                        "tool_events": [],
                        "usage": {"prompt_tokens": 5, "completion_tokens": 5, "total_tokens": 10, "cost_usd": 0.0},
                        "finish_reason": "stop",
                        "structured_output": {"selected_agents": ["logician"], "coordinator_agent": "coordinator"},
                        "raw": None,
                        "error": None,
                    }
                }
            ],
        }
    )
    out = run_selector(llm_config, providers={"default": FakeProvider(script=script)})
    assert out.selected_agents == ["logician"]


def test_llm_budget_exceeded_terminates(tmp_path, llm_selector_script):
    """§9.7 budget_selector: a selector whose usage exceeds selector_budget →
    terminate(reason = budget_exceeded)."""
    cfg = _minimal_config(
        strategy="llm",
        problem="choose a panel",
        selector_budget=SelectorBudget(max_tokens=100),
    )
    # selector script reports 300 tokens > the 100-token cap
    script = _llm_decision_script(
        {"selected_agents": ["logician"], "coordinator_agent": "coordinator"},
        usage={"prompt_tokens": 200, "completion_tokens": 100, "total_tokens": 300, "cost_usd": 0.0},
    )
    selector_fp = FakeProvider(script=script)

    # raised directly by the selector
    with pytest.raises(SelectorBudgetExceeded):
        run_selector(cfg, providers={"default": selector_fp})

    # and mapped to terminate(budget_exceeded) by run_session
    delib_fp = FakeProvider(script=llm_selector_script)
    sel_fp2 = FakeProvider(script=script)
    artifact = run_session(
        cfg, {"default": delib_fp}, runs_root=str(tmp_path), selector_providers={"default": sel_fp2}
    )
    assert artifact.outcome.kind == "termination"
    assert artifact.outcome.termination_artifact.reason == "budget_exceeded"
    # selector tokens never entered cumulative_usage
    assert artifact.cumulative_usage.total_tokens == 0


# ---------------------------------------------------------------------------
# dispatcher invariants
# ---------------------------------------------------------------------------


def test_coordinator_absent_from_config_raises(example_config):
    """A selector naming a coordinator absent from the config raises SelectorError."""
    cfg = example_config.model_copy(
        update={"selector": example_config.selector.model_copy(update={"coordinator_agent": "ghost"})}
    )
    with pytest.raises(SelectorError):
        run_selector(cfg)


def test_llm_selects_unknown_agent_raises(llm_config):
    script = _llm_decision_script({"selected_agents": ["nonexistent"], "coordinator_agent": "coordinator"})
    with pytest.raises(SelectorError):
        run_selector(llm_config, providers={"default": FakeProvider(script=script)})


def test_llm_empty_selected_agents_raises(llm_config):
    script = _llm_decision_script({"selected_agents": [], "coordinator_agent": "coordinator"})
    with pytest.raises(SelectorError):
        run_selector(llm_config, providers={"default": FakeProvider(script=script)})


# ---------------------------------------------------------------------------
# SelectorOutput schema + persistence
# ---------------------------------------------------------------------------


def test_selector_output_roundtrips_schema():
    validator = _selector_output_validator()
    for out in (
        SelectorOutput(strategy="fixed", selected_agents=["a"], coordinator_agent="c"),
        SelectorOutput(
            strategy="rules",
            selected_agents=["a", "b"],
            coordinator_agent="c",
            excluded_agents=[{"id": "d", "reason": "no scope match"}],
            missing_capabilities=[{"capability": "legal", "reason": "no domain persona"}],
            reasoning="rule subset",
        ),
        SelectorOutput(strategy="llm", selected_agents=["a"], coordinator_agent="c", reasoning="llm pick"),
    ):
        errors = list(validator.iter_errors(out.model_dump(mode="json", exclude_none=True)))
        assert not errors, [e.message for e in errors]


def test_selector_output_json_written_and_reloads(tmp_path, rules_config, example_script):
    fp = FakeProvider(script=example_script)
    run_session(rules_config, {"default": fp}, runs_root=str(tmp_path))
    sel_path = tmp_path / rules_config.session_id / "selector_output.json"
    assert sel_path.exists()
    reloaded = SelectorOutput.model_validate(json.loads(sel_path.read_text()))
    assert reloaded.strategy == "rules"
    assert reloaded.selected_agents == list(rules_config.selector.default_deliberation_panel)
    # schema-valid on disk too
    errors = list(_selector_output_validator().iter_errors(json.loads(sel_path.read_text())))
    assert not errors


# ---------------------------------------------------------------------------
# CLI end-to-end
# ---------------------------------------------------------------------------


def test_cli_run_rules_writes_selector_output(tmp_path):
    runner = CliRunner()
    result = runner.invoke(
        cli_group,
        [
            "run",
            "--config", str(EXAMPLES / "configs" / "rules-selector.yaml"),
            "--script", str(EXAMPLES / "scripts" / "walking-skeleton.json"),
            "--output", str(tmp_path),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "selector_strategy=rules" in result.output
    assert "selected_agents=" in result.output
    sel_path = tmp_path / "demo-rules-selector-001" / "selector_output.json"
    assert sel_path.exists()
    SelectorOutput.model_validate(json.loads(sel_path.read_text()))


# ---------------------------------------------------------------------------
# shared schema-validation helper
# ---------------------------------------------------------------------------


def _assert_artifact_schema_valid(run_dir: Path) -> None:
    from referencing import Registry, Resource

    schemas_dir = REPO_ROOT / "docs" / "schemas" / "v1.0.0"
    resources = []
    for schema_path in schemas_dir.glob("*.json"):
        data = json.loads(schema_path.read_text())
        if "$id" not in data:
            continue
        resources.append((data["$id"], Resource.from_contents(data)))
        resources.append((schema_path.name, Resource.from_contents(data)))
    registry = Registry().with_resources(resources)
    artifact_schema = json.loads((schemas_dir / "artifact.schema.json").read_text())
    validator = Draft202012Validator(artifact_schema, registry=registry)
    data = json.loads((run_dir / "artifact.json").read_text())
    errors = list(validator.iter_errors(data))
    assert not errors, "artifact failed schema validation: " + "\n".join(
        f"{list(e.absolute_path)}: {e.message}" for e in errors
    )

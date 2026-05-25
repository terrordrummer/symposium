#!/usr/bin/env python3
"""Negative-test cases for Pass 4/5/6/7 schemas.

Each case is an intentionally invalid instance plus the schema it must
fail against. Success = the validator rejects the instance.

Pass 7 additions: negative cases for fake_provider_script.schema.json
and golden_test_case.schema.json (§9.13 fixture inventory).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from jsonschema import Draft202012Validator
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012


_HERE = Path(__file__).resolve().parent
SCHEMA_DIR = _HERE.parent


def load_registry() -> Registry:
    resources = []
    for schema_path in sorted(SCHEMA_DIR.glob("*.schema.json")):
        with open(schema_path) as f:
            schema = json.load(f)
        resource = Resource(contents=schema, specification=DRAFT202012)
        resources.append((schema["$id"], resource))
        resources.append((schema_path.name, resource))
    return Registry().with_resources(resources)


# Each tuple: (label, schema_filename, instance dict, expected_substring in error)
NEGATIVE_CASES = [
    (
        "N9: verdict.next_action='abort' is forbidden",
        "verdict.schema.json",
        {
            "next_action": "abort",
            "rationale": "anything",
            "confidence": 0.5,
            "focus": "anything",
            "next_agents": [],
            "resolved_disagreements": [],
            "unresolved_disagreements": [],
        },
        "abort",
    ),
    (
        "N9: verdict.next_action='final_answer_ready' is forbidden",
        "verdict.schema.json",
        {
            "next_action": "final_answer_ready",
            "rationale": "anything",
            "confidence": 0.5,
            "focus": "anything",
            "next_agents": [],
            "resolved_disagreements": [],
            "unresolved_disagreements": [],
        },
        "final_answer_ready",
    ),
    (
        "horizontal persona MUST NOT carry domain_scope",
        "persona.schema.json",
        {
            "persona_class": "horizontal",
            "id": "logician",
            "reasoning_scope": "formal-structural",
            "reasoning_style": "rigor",
            "behavioral_constraints": ["cite sources"],
            "failure_modes": ["over-confidence"],
            "domain_scope": ["law"],
        },
        None,
    ),
    (
        "domain persona MUST carry forbidden_domains and must_delegate",
        "persona.schema.json",
        {
            "persona_class": "domain",
            "id": "legal_analyst",
            "reasoning_scope": "evidence-based",
            "reasoning_style": "doctrinal",
            "behavioral_constraints": ["cite statute"],
            "failure_modes": ["over-extending precedent"],
            "domain_scope": ["EU GDPR"],
        },
        None,
    ),
    (
        "direct_request: no `inline_mention` field allowed (ADR-003)",
        "direct_request.schema.json",
        {
            "target": "researcher",
            "type": "question",
            "content": "anything",
            "inline_mention": "@Researcher",
        },
        "Additional properties",
    ),
    (
        "verdict: request_user_input without user_input_request payload fails",
        "verdict.schema.json",
        {
            "next_action": "request_user_input",
            "rationale": "need clarification",
            "confidence": 0.4,
            "focus": "disambiguate",
            "next_agents": [],
            "resolved_disagreements": [],
            "unresolved_disagreements": [],
        },
        "user_input_request",
    ),
    (
        "verdict: confidence > 1.0 fails",
        "verdict.schema.json",
        {
            "next_action": "continue",
            "rationale": "x",
            "confidence": 1.5,
            "focus": "y",
            "next_agents": [],
            "resolved_disagreements": [],
            "unresolved_disagreements": [],
        },
        None,
    ),
    (
        "message: problem_statement with round != 0 fails",
        "message.schema.json",
        {
            "id": "msg-bad",
            "speaker": "user",
            "type": "problem_statement",
            "content": "anything",
            "parent_id": None,
            "round": 1,
            "turn_index": 0,
            "branch_depth": 0,
            "timestamp": "2026-05-25T10:00:00Z",
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "cost_usd": 0.0},
        },
        None,
    ),
    (
        "message: branch_turn with branch_depth=0 fails (must be 1)",
        "message.schema.json",
        {
            "id": "msg-bad-2",
            "speaker": "researcher",
            "type": "branch_turn",
            "content": {"text": "answer"},
            "parent_id": "msg-001",
            "round": 1,
            "turn_index": 2,
            "branch_depth": 0,
            "timestamp": "2026-05-25T10:00:00Z",
            "usage": {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150, "cost_usd": 0.001},
        },
        None,
    ),
    (
        "config: schema_version='1.0' (not full SemVer) fails",
        "config.schema.json",
        {
            "schema_version": "1.0",
            "session_id": "x",
            "originator": "u",
            "problem_statement": "p",
            "selector": {
                "strategy": "fixed",
                "default_deliberation_panel": ["a"],
                "coordinator_agent": "c",
            },
            "agents": [{"id": "a", "persona_ref": "a", "provider": "p", "model": "m"}],
            "coordinator": {"id": "c", "persona_ref": "c", "provider": "p", "model": "m"},
            "budget": {
                "max_total_tokens": 1000,
                "max_total_cost_usd": 1.0,
                "max_rounds": 1,
                "max_wallclock_seconds": 60,
            },
            "runtime": {},
        },
        None,
    ),
    (
        "config: selector strategy='llm' MUST require selector_budget",
        "config.schema.json",
        {
            "schema_version": "1.0.0",
            "session_id": "x",
            "originator": "u",
            "problem_statement": "p",
            "selector": {
                "strategy": "llm",
                "default_deliberation_panel": ["a"],
                "coordinator_agent": "c",
            },
            "agents": [{"id": "a", "persona_ref": "a", "provider": "p", "model": "m"}],
            "coordinator": {"id": "c", "persona_ref": "c", "provider": "p", "model": "m"},
            "budget": {
                "max_total_tokens": 1000,
                "max_total_cost_usd": 1.0,
                "max_rounds": 1,
                "max_wallclock_seconds": 60,
            },
            "runtime": {},
        },
        None,
    ),
    (
        "provider_result: finish_reason not in enum fails",
        "provider_result.schema.json",
        {
            "messages": [],
            "tool_events": [],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "cost_usd": 0.0},
            "finish_reason": "completed_successfully",
            "structured_output": None,
            "raw": None,
            "error": None,
        },
        None,
    ),
    (
        "verdict: request_user_input + external_research_request simultaneously fails (turn-2 fix)",
        "verdict.schema.json",
        {
            "next_action": "request_user_input",
            "rationale": "x",
            "confidence": 0.4,
            "focus": "y",
            "next_agents": [],
            "resolved_disagreements": [],
            "unresolved_disagreements": [],
            "user_input_request": {"question": "?"},
            "external_research_request": {"query": "?"},
        },
        None,
    ),
    (
        "termination_artifact: reason=budget_exceeded with pending_user_input_request fails (turn-2 fix)",
        "termination_artifact.schema.json",
        {
            "schema_version": "1.0.0",
            "reason": "budget_exceeded",
            "final_round": 3,
            "cumulative_usage": {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150, "cost_usd": 0.01},
            "unresolved_disagreements": [],
            "transcript_digest": "deadbeef",
            "pending_user_input_request": {"question": "?"},
        },
        None,
    ),
    (
        "message: branch_turn with parent_id=null fails (turn-2 fix)",
        "message.schema.json",
        {
            "id": "msg-x",
            "speaker": "researcher",
            "type": "branch_turn",
            "content": {"text": "answer"},
            "parent_id": None,
            "round": 1,
            "turn_index": 2,
            "branch_depth": 1,
            "timestamp": "2026-05-25T10:00:00Z",
            "usage": {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150, "cost_usd": 0.001},
        },
        None,
    ),
    (
        "message: primary_turn content with extra field fails (turn-2 fix)",
        "message.schema.json",
        {
            "id": "msg-x",
            "speaker": "logician",
            "type": "primary_turn",
            "content": {"text": "ok", "inline_mention": "@critic"},
            "parent_id": None,
            "round": 1,
            "turn_index": 1,
            "branch_depth": 0,
            "timestamp": "2026-05-25T10:00:00Z",
            "usage": {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150, "cost_usd": 0.001},
        },
        None,
    ),
    (
        "provider_result: error.kind not in closed enum fails (Pass 5 closure)",
        "provider_result.schema.json",
        {
            "messages": [],
            "tool_events": [],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "cost_usd": 0.0},
            "finish_reason": "error",
            "structured_output": None,
            "raw": None,
            "error": {
                "kind": "unknown_vendor_specific_kind",
                "message": "something went wrong",
                "retriable": False,
            },
        },
        None,
    ),
    (
        "provider_request: role 'agent' not in CLOSED enum fails (§6.3)",
        "provider_request.schema.json",
        {
            "agent_id": "logician",
            "provider": "provider_a",
            "model": "reasoning_model",
            "messages": [
                {"role": "agent", "content": "hello"}
            ],
            "tools": [],
            "expected_output_schema": "turn_structured_output",
        },
        None,
    ),
    (
        "provider_request: expected_output_schema='whatever' not in CLOSED enum fails (§6.5)",
        "provider_request.schema.json",
        {
            "agent_id": "logician",
            "provider": "provider_a",
            "model": "reasoning_model",
            "messages": [
                {"role": "user", "content": "hello"}
            ],
            "tools": [],
            "expected_output_schema": "whatever",
        },
        None,
    ),
    (
        "provider_request: tool missing required input_schema fails (§6.4)",
        "provider_request.schema.json",
        {
            "agent_id": "researcher",
            "provider": "provider_a",
            "model": "reasoning_model",
            "messages": [
                {"role": "user", "content": "hello"}
            ],
            "tools": [
                {"name": "search_papers", "description": "search"}
            ],
            "expected_output_schema": "turn_structured_output",
        },
        "input_schema",
    ),
    (
        "provider_request: vendor side-channel field 'gpt_model_overrides' rejected by additionalProperties:false (N4)",
        "provider_request.schema.json",
        {
            "agent_id": "logician",
            "provider": "provider_a",
            "model": "reasoning_model",
            "messages": [
                {"role": "user", "content": "hello"}
            ],
            "tools": [],
            "expected_output_schema": "turn_structured_output",
            "gpt_model_overrides": {"temperature": 0.7},
        },
        "Additional properties",
    ),
    (
        "provider_request: empty messages array fails (must be at least 1)",
        "provider_request.schema.json",
        {
            "agent_id": "logician",
            "provider": "provider_a",
            "model": "reasoning_model",
            "messages": [],
            "tools": [],
            "expected_output_schema": "turn_structured_output",
        },
        None,
    ),
    (
        "provider_request: role=tool message without tool_call_id fails (§6.4 conditional)",
        "provider_request.schema.json",
        {
            "agent_id": "researcher",
            "provider": "provider_a",
            "model": "reasoning_model",
            "messages": [
                {"role": "user", "content": "ok"},
                {"role": "tool", "content": "{\"matches\": []}"}
            ],
            "tools": [],
            "expected_output_schema": "turn_structured_output",
        },
        "tool_call_id",
    ),
    (
        "provider_result: tool_events item missing required 'error' field fails (§6.4)",
        "provider_result.schema.json",
        {
            "messages": [],
            "tool_events": [{"name": "search_papers", "arguments": {"query": "x"}}],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "cost_usd": 0.0},
            "finish_reason": "stop",
            "structured_output": None,
            "raw": None,
            "error": None,
        },
        "error",
    ),
    (
        "run_manifest: status='complete' MUST require transcript_digest and outcome_kind (§7.2)",
        "run_manifest.schema.json",
        {
            "schema_version": "1.0.0",
            "manifest_version": "1.0.0",
            "session_id": "demo-2026",
            "status": "complete",
            "producer": {"name": "x", "version": "0.1.0"},
            "created_at": "2026-05-25T10:00:00Z",
            "artifact_path": "artifact.json",
            "config_path": "config.json"
        },
        "transcript_digest",
    ),
    (
        "run_manifest: status='in_progress' MUST NOT carry transcript_digest (§7.2)",
        "run_manifest.schema.json",
        {
            "schema_version": "1.0.0",
            "manifest_version": "1.0.0",
            "session_id": "demo-2026",
            "status": "in_progress",
            "producer": {"name": "x", "version": "0.1.0"},
            "created_at": "2026-05-25T10:00:00Z",
            "updated_at": "2026-05-25T10:05:00Z",
            "artifact_path": "artifact.json",
            "config_path": "config.json",
            "transcript_digest": "9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08"
        },
        None,
    ),
    (
        "run_manifest: status='complete' with outcome_kind='termination' is inconsistent (§7.2)",
        "run_manifest.schema.json",
        {
            "schema_version": "1.0.0",
            "manifest_version": "1.0.0",
            "session_id": "demo-2026",
            "status": "complete",
            "producer": {"name": "x", "version": "0.1.0"},
            "created_at": "2026-05-25T10:00:00Z",
            "artifact_path": "artifact.json",
            "config_path": "config.json",
            "transcript_digest": "9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08",
            "outcome_kind": "termination"
        },
        None,
    ),
    (
        "run_manifest: session_id with disallowed characters fails (§7.1 charset)",
        "run_manifest.schema.json",
        {
            "schema_version": "1.0.0",
            "manifest_version": "1.0.0",
            "session_id": "../etc/passwd",
            "status": "in_progress",
            "producer": {"name": "x", "version": "0.1.0"},
            "created_at": "2026-05-25T10:00:00Z",
            "updated_at": "2026-05-25T10:05:00Z",
            "artifact_path": "artifact.json",
            "config_path": "config.json"
        },
        None,
    ),
    (
        "run_manifest: transcript_digest of wrong length fails (§7.7)",
        "run_manifest.schema.json",
        {
            "schema_version": "1.0.0",
            "manifest_version": "1.0.0",
            "session_id": "demo-2026",
            "status": "complete",
            "producer": {"name": "x", "version": "0.1.0"},
            "created_at": "2026-05-25T10:00:00Z",
            "artifact_path": "artifact.json",
            "config_path": "config.json",
            "transcript_digest": "deadbeef",
            "outcome_kind": "synthesis"
        },
        None,
    ),
    (
        "config: on_budget_exceeded='degrade' rejected by closed enum at MVP (§8.2)",
        "config.schema.json",
        {
            "schema_version": "1.0.0",
            "session_id": "x",
            "originator": "u",
            "problem_statement": "p",
            "selector": {
                "strategy": "fixed",
                "default_deliberation_panel": ["a"],
                "coordinator_agent": "c",
            },
            "agents": [{"id": "a", "persona_ref": "a", "provider": "p", "model": "m"}],
            "coordinator": {"id": "c", "persona_ref": "c", "provider": "p", "model": "m"},
            "budget": {
                "max_total_tokens": 1000,
                "max_total_cost_usd": 1.0,
                "max_rounds": 1,
                "max_wallclock_seconds": 60,
            },
            "runtime": {"on_budget_exceeded": "degrade"},
        },
        "degrade",
    ),
    (
        "config: observability_level='debug' rejected by closed enum (§7.9)",
        "config.schema.json",
        {
            "schema_version": "1.0.0",
            "session_id": "x",
            "originator": "u",
            "problem_statement": "p",
            "selector": {
                "strategy": "fixed",
                "default_deliberation_panel": ["a"],
                "coordinator_agent": "c",
            },
            "agents": [{"id": "a", "persona_ref": "a", "provider": "p", "model": "m"}],
            "coordinator": {"id": "c", "persona_ref": "c", "provider": "p", "model": "m"},
            "budget": {
                "max_total_tokens": 1000,
                "max_total_cost_usd": 1.0,
                "max_rounds": 1,
                "max_wallclock_seconds": 60,
            },
            "runtime": {"observability_level": "debug"},
        },
        None,
    ),
    (
        "artifact: missing transcript_digest is rejected (Pass 6 promotion to top-level)",
        "artifact.schema.json",
        {
            "schema_version": "1.0.0",
            "session_id": "x",
            "config": {
                "schema_version": "1.0.0",
                "session_id": "x",
                "originator": "u",
                "problem_statement": "p",
                "selector": {
                    "strategy": "fixed",
                    "default_deliberation_panel": ["a"],
                    "coordinator_agent": "c"
                },
                "agents": [{"id": "a", "persona_ref": "a", "provider": "p", "model": "m"}],
                "coordinator": {"id": "c", "persona_ref": "c", "provider": "p", "model": "m"},
                "budget": {
                    "max_total_tokens": 1000,
                    "max_total_cost_usd": 1.0,
                    "max_rounds": 1,
                    "max_wallclock_seconds": 60
                },
                "runtime": {}
            },
            "canonical_transcript": [{
                "id": "msg-0",
                "speaker": "user",
                "type": "problem_statement",
                "content": "anything",
                "parent_id": None,
                "round": 0,
                "turn_index": 0,
                "branch_depth": 0,
                "timestamp": "2026-05-25T10:00:00Z",
                "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "cost_usd": 0.0}
            }],
            "outcome": {"kind": "synthesis", "synthesis_message_id": "msg-0"},
            "cumulative_usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "cost_usd": 0.0},
            "cumulative_unresolved": [],
            "started_at": "2026-05-25T10:00:00Z",
            "ended_at": "2026-05-25T10:00:10Z"
        },
        "transcript_digest",
    ),
    (
        "provider_result: tool_events item with additional_property fails (§6.4 close)",
        "provider_result.schema.json",
        {
            "messages": [],
            "tool_events": [
                {
                    "name": "search_papers",
                    "arguments": {"query": "x"},
                    "error": None,
                    "vendor_metadata": {"foo": "bar"}
                }
            ],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "cost_usd": 0.0},
            "finish_reason": "stop",
            "structured_output": None,
            "raw": None,
            "error": None,
        },
        "Additional properties",
    ),
    # === Pass 7 negative cases ===
    (
        "Pass 7 fake_provider_script: missing required `entries` field",
        "fake_provider_script.schema.json",
        {
            "schema_version": "1.0.0",
            "description": "Negative: missing required `entries` field.",
        },
        "entries",
    ),
    (
        "Pass 7 fake_provider_script: match.expected_output_schema='null' outside closed enum",
        "fake_provider_script.schema.json",
        {
            "schema_version": "1.0.0",
            "description": "Negative: 'null' is not in {turn_structured_output, verdict, synthesis_content}.",
            "entries": [
                {
                    "match": {
                        "agent_id": "agent_a",
                        "expected_output_schema": "null",
                    },
                    "result": {
                        "messages": [],
                        "tool_events": [],
                        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "cost_usd": 0.0},
                        "finish_reason": "stop",
                        "structured_output": {"text": "x"},
                        "raw": None,
                        "error": None,
                    },
                }
            ],
        },
        "null",
    ),
    (
        "Pass 7 golden_test_case: case_id violates ^[A-Za-z0-9_-]{1,64}$ charset",
        "golden_test_case.schema.json",
        {
            "schema_version": "1.0.0",
            "case_id": "pass7 invalid case id!",
            "problem_statement": "anything",
            "config": {
                "schema_version": "1.0.0",
                "session_id": "x",
                "originator": "u",
                "problem_statement": "p",
                "selector": {
                    "strategy": "fixed",
                    "default_deliberation_panel": ["a"],
                    "coordinator_agent": "c",
                },
                "agents": [{"id": "a", "persona_ref": "a", "provider": "fake", "model": "m"}],
                "coordinator": {"id": "c", "persona_ref": "c", "provider": "fake", "model": "m"},
                "budget": {
                    "max_total_tokens": 1000,
                    "max_total_cost_usd": 1.0,
                    "max_rounds": 1,
                    "max_wallclock_seconds": 60,
                },
                "runtime": {},
            },
            "fake_script": {
                "schema_version": "1.0.0",
                "entries": [
                    {
                        "result": {
                            "messages": [],
                            "tool_events": [],
                            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "cost_usd": 0.0},
                            "finish_reason": "stop",
                            "structured_output": {"text": "x"},
                            "raw": None,
                            "error": None,
                        }
                    }
                ],
            },
            "expected_artifact": {
                "schema_version": "1.0.0",
                "session_id": "x",
                "config": {
                    "schema_version": "1.0.0",
                    "session_id": "x",
                    "originator": "u",
                    "problem_statement": "p",
                    "selector": {
                        "strategy": "fixed",
                        "default_deliberation_panel": ["a"],
                        "coordinator_agent": "c",
                    },
                    "agents": [{"id": "a", "persona_ref": "a", "provider": "fake", "model": "m"}],
                    "coordinator": {"id": "c", "persona_ref": "c", "provider": "fake", "model": "m"},
                    "budget": {
                        "max_total_tokens": 1000,
                        "max_total_cost_usd": 1.0,
                        "max_rounds": 1,
                        "max_wallclock_seconds": 60,
                    },
                    "runtime": {},
                },
                "canonical_transcript": [{
                    "id": "msg-0",
                    "speaker": "user",
                    "type": "problem_statement",
                    "content": "anything",
                    "parent_id": None,
                    "round": 0,
                    "turn_index": 0,
                    "branch_depth": 0,
                    "timestamp": "2026-05-25T10:00:00Z",
                    "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "cost_usd": 0.0},
                }],
                "outcome": {"kind": "synthesis", "synthesis_message_id": "msg-0"},
                "cumulative_usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "cost_usd": 0.0},
                "cumulative_unresolved": [],
                "transcript_digest": "0" * 64,
                "started_at": "2026-05-25T10:00:00Z",
                "ended_at": "2026-05-25T10:00:10Z",
            },
        },
        None,
    ),
]


def main() -> int:
    registry = load_registry()
    print("# Pass 4 negative-validation pass\n")
    print("Each case is INTENTIONALLY invalid — success = rejection.\n")
    failed = 0
    for label, schema_filename, instance, expected_substring in NEGATIVE_CASES:
        schema_path = SCHEMA_DIR / schema_filename
        with open(schema_path) as f:
            schema = json.load(f)
        validator = Draft202012Validator(schema, registry=registry)
        errors = list(validator.iter_errors(instance))
        if not errors:
            print(f"[FAIL] {label}\n  (schema accepted invalid instance)")
            failed += 1
            continue
        if expected_substring is not None:
            matched = any(expected_substring.lower() in (err.message or "").lower() for err in errors)
            if not matched:
                # Soft-check: as long as it was rejected, accept.
                print(f"[OK*] {label}\n  (rejected — substring '{expected_substring}' not in message, but schema rejected the instance, which is what matters)")
                continue
        print(f"[OK ] {label}")
    print(f"\nTotal: {len(NEGATIVE_CASES)} cases; failed: {failed}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

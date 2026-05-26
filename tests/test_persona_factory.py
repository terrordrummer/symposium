"""Persona generation from a capability need (host layer).

The CLI `caller` / `runner` are injected, so no real `claude`/`codex` is
spawned and nothing hits the network.
"""

from __future__ import annotations

import json
import subprocess

import pytest

from symposium.integrations.persona_factory import (
    PersonaGenerationError,
    generate_persona,
    make_cli_persona_caller,
)
from symposium.models import Persona


def _valid_domain(id="cryptographer"):
    return {
        "persona_class": "domain", "id": id,
        "reasoning_scope": "applied cryptography",
        "reasoning_style": "threat-model-first; primitive-aware",
        "behavioral_constraints": ["name the primitive and its assumptions"],
        "failure_modes": ["over-engineering the threat model"],
        "domain_scope": ["cryptography", "key management"],
        "forbidden_domains": ["ui design"],
        "must_delegate": {"legal compliance": "a legal expert"},
    }


def test_generate_valid_domain_persona():
    persona = generate_persona("need crypto expertise", caller=lambda p, s: _valid_domain())
    assert isinstance(persona, Persona)
    assert persona.persona_class == "domain"
    assert persona.id == "cryptographer"
    assert persona.domain_scope  # domain persona carries its bounding fields


def test_caller_receives_persona_schema():
    captured = {}

    def caller(prompt, schema):
        captured["schema"] = schema
        captured["prompt"] = prompt
        return _valid_domain()

    generate_persona("need X", caller=caller)
    # the schema handed to the CLI is the Persona model's JSON Schema
    assert "persona_class" in captured["schema"]["properties"]
    assert "need X" in captured["prompt"]


def test_invalid_persona_raises():
    # a 'domain' persona missing domain_scope/forbidden_domains/must_delegate
    bad = {"persona_class": "domain", "id": "x", "reasoning_scope": "s",
           "reasoning_style": "y", "behavioral_constraints": ["a"], "failure_modes": ["b"]}
    with pytest.raises(PersonaGenerationError):
        generate_persona("need", caller=lambda p, s: bad)


def test_caller_failure_raises():
    def boom(prompt, schema):
        raise RuntimeError("cli down")

    with pytest.raises(PersonaGenerationError):
        generate_persona("need", caller=boom)


def test_id_deduped_against_existing():
    persona = generate_persona(
        "need", caller=lambda p, s: _valid_domain("critic"), existing_ids={"critic"}
    )
    assert persona.id != "critic"
    assert persona.id.startswith("critic-")


def test_make_cli_caller_claude_path_with_injected_runner():
    def runner(argv, *, input=None, capture_output=None, text=None, timeout=None):
        assert "--json-schema" in argv  # schema enforced
        return subprocess.CompletedProcess(
            argv, 0, stdout=json.dumps({"structured_output": _valid_domain("dba")}), stderr=""
        )

    caller = make_cli_persona_caller(runner=runner)
    obj = caller("design a dba", {"type": "object"})
    assert obj["id"] == "dba"
    # end-to-end through generate_persona
    persona = generate_persona("db tuning", caller=caller)
    assert persona.id == "dba"

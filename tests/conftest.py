"""Shared pytest fixtures for the walking-skeleton tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from symposium.models import Config, FakeProviderScript
from symposium.personas import persona_by_id

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = REPO_ROOT / "examples"


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


@pytest.fixture
def example_config() -> Config:
    raw = yaml.safe_load((EXAMPLES / "configs" / "walking-skeleton.yaml").read_text())
    raw = _resolve_persona_refs(raw)
    return Config.model_validate(raw)


@pytest.fixture
def example_script() -> FakeProviderScript:
    return FakeProviderScript.model_validate(
        json.loads((EXAMPLES / "scripts" / "walking-skeleton.json").read_text())
    )


@pytest.fixture
def repo_root() -> Path:
    return REPO_ROOT

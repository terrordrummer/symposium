"""Symposium — reference implementation of the Symposium 1.0 protocol.

The protocol is specified in `docs/specification.md` of this repository.
This package exposes a runtime conformant with that specification's
[Core MVP] surface (§1–§9) using a deterministic `FakeProvider` adapter
for testing and demonstration.

Public surface:

    from symposium import Session, Config, run_session
    from symposium.providers import FakeProvider, ProviderAdapter

The CLI entry point is `symposium`; see `symposium --help`.
"""

from symposium.models import (
    Artifact,
    Config,
    ContextPacket,
    DirectRequest,
    FakeProviderScript,
    Message,
    Persona,
    ProviderRequest,
    ProviderResult,
    RunManifest,
    SelectorOutput,
    SynthesisContent,
    TerminationArtifact,
    TurnStructuredOutput,
    Verdict,
)

__all__ = [
    "Artifact",
    "Config",
    "ContextPacket",
    "DirectRequest",
    "FakeProviderScript",
    "Message",
    "Persona",
    "ProviderRequest",
    "ProviderResult",
    "RunManifest",
    "SelectorOutput",
    "SynthesisContent",
    "TerminationArtifact",
    "TurnStructuredOutput",
    "Verdict",
]

# Package / runtime release version — kept in sync with pyproject.toml
# at every release. Distinct from SCHEMA_VERSION (the frozen v1.0.0
# protocol + JSON Schemas, which MUST NOT move here) and from
# storage.writer.PRODUCER_VERSION (the §7.6-condition-#1
# reproduction-surface identity).
__version__ = "1.10.2"
SCHEMA_VERSION = "1.0.0"


def __getattr__(name: str):
    # Lazy import of scheduler-side symbols so models can be imported
    # before the scheduler module is built.
    if name in ("Session", "run_session"):
        from symposium.scheduler import Session, run_session  # type: ignore

        return {"Session": Session, "run_session": run_session}[name]
    if name == "run_selector":
        from symposium.selector import run_selector  # type: ignore

        return run_selector
    raise AttributeError(f"module 'symposium' has no attribute {name!r}")

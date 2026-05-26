"""Pydantic models matching the Symposium v1.0.0 JSON Schemas.

Each model mirrors its `docs/schemas/v1.0.0/*.schema.json` counterpart
field-for-field with the same `additionalProperties: false` semantics.
The Pydantic models are the in-runtime representation; the JSON Schemas
are the persisted-artifact validation surface. The package emits both
sides of the boundary (validate against the schema at every persistence
point per §5.15).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated, Any, Dict, List, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# Non-empty string item, used inside arrays whose JSON-Schema items declare
# `minLength: 1` (e.g. Persona.behavioral_constraints, output_requirements,
# domain_scope, forbidden_domains).
_NonEmptyStr = Annotated[str, Field(min_length=1)]

# ---------------------------------------------------------------------------
# Closed enums (§4.13, §6.6, §4.7, §4.9, §6.10)
# ---------------------------------------------------------------------------

MessageType = Literal[
    "problem_statement",
    "primary_turn",
    "coordination_turn",
    "branch_turn",
    "synthesis",
    "panel_contraction",
]

NextAction = Literal[
    "continue",
    "finalize",
    "request_user_input",
    "request_external_research",
]

FinishReason = Literal["stop", "length", "tool_call", "content_filter", "error"]

ErrorKind = Literal[
    "timeout",
    "network",
    "rate_limit",
    "quota_exhausted",
    "auth_failure",
    "model_unavailable",
    "context_length_exceeded",
    "content_filter",
    "invalid_request",
    "malformed_response",
    "tool_failure",
    "internal",
]

TerminationReason = Literal[
    "budget_exceeded",
    "schema_error",
    "provider_unrecoverable",
    "user_cancel",
    "timeout",
    "user_input_required",
    "external_research_required",
]

PersonaClass = Literal["horizontal", "domain"]
SelectorStrategy = Literal["fixed", "rules", "llm"]
OnAgentFailure = Literal["terminate", "continue_without"]
OnBudgetExceeded = Literal["stop"]
ObservabilityLevel = Literal["mvp", "verbose"]
PersonaStatus = Literal["experimental", "stable", "deprecated", "archived"]
# §6.2: `expected_output_schema` is a closed enum of canonical schema names
# OR JSON null (the free-text path). The Python representation uses Optional
# with `None == JSON null`; the prior "null" *string* form drifted from the
# JSON Schema enum (which expects the literal `null`).
ExpectedOutputSchema = Optional[Literal[
    "turn_structured_output",
    "verdict",
    "synthesis_content",
]]
ManifestStatus = Literal["in_progress", "complete", "terminated", "crashed"]

SCHEMA_VERSION = "1.0.0"


def _strict() -> ConfigDict:
    return ConfigDict(extra="forbid")


def now_utc_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# Persona (§5.3)
# ---------------------------------------------------------------------------


class Persona(BaseModel):
    model_config = _strict()

    persona_class: PersonaClass
    id: str = Field(min_length=1)
    reasoning_scope: str = Field(min_length=1)
    reasoning_style: str = Field(min_length=1)
    behavioral_constraints: List[_NonEmptyStr] = Field(min_length=1)
    failure_modes: List[_NonEmptyStr] = Field(min_length=1)
    output_requirements: Optional[List[_NonEmptyStr]] = None
    # domain_scope / forbidden_domains: schema requires minItems=1 when present
    # (the model_validator below enforces presence for domain personas).
    domain_scope: Optional[List[_NonEmptyStr]] = Field(default=None, min_length=1)
    forbidden_domains: Optional[List[_NonEmptyStr]] = Field(default=None, min_length=1)
    must_delegate: Optional[Dict[str, str]] = None
    status: Optional[PersonaStatus] = None

    @model_validator(mode="after")
    def _enforce_class_constraints(self) -> "Persona":
        if self.persona_class == "horizontal":
            for field_name in ("domain_scope", "forbidden_domains", "must_delegate"):
                if getattr(self, field_name) is not None:
                    raise ValueError(
                        f"horizontal persona '{self.id}' must not declare {field_name}"
                    )
        else:  # domain
            for field_name in ("domain_scope", "forbidden_domains", "must_delegate"):
                if getattr(self, field_name) is None:
                    raise ValueError(
                        f"domain persona '{self.id}' must declare {field_name}"
                    )
        return self


# ---------------------------------------------------------------------------
# Config (§5.2)
# ---------------------------------------------------------------------------


class SelectorBudget(BaseModel):
    model_config = _strict()
    max_tokens: Optional[int] = Field(default=None, ge=0)
    max_cost_usd: Optional[float] = Field(default=None, ge=0.0)


class SelectorConfig(BaseModel):
    model_config = _strict()
    strategy: SelectorStrategy
    default_deliberation_panel: List[str] = Field(min_length=1)
    coordinator_agent: str = Field(min_length=1)
    selector_budget: Optional[SelectorBudget] = None

    @model_validator(mode="after")
    def _require_budget_for_llm(self) -> "SelectorConfig":
        if self.strategy == "llm" and self.selector_budget is None:
            raise ValueError("selector strategy=llm requires selector_budget")
        return self


class AgentConfig(BaseModel):
    model_config = _strict()
    id: str = Field(min_length=1)
    persona_ref: Union[str, Persona]
    provider: str = Field(min_length=1)
    model: str = Field(min_length=1)
    reasoning_effort: Optional[str] = None
    tools: Optional[List[Dict[str, Any]]] = None
    output_requirements: Optional[List[str]] = None
    retry_budget: Optional[int] = Field(default=None, ge=0)


class BudgetConfig(BaseModel):
    model_config = _strict()
    max_total_tokens: int = Field(ge=1)
    max_total_cost_usd: float = Field(ge=0.0)
    max_rounds: int = Field(ge=1)
    max_wallclock_seconds: int = Field(ge=1)
    per_agent_token_budget: Optional[Dict[str, int]] = None
    selector_budget: Optional[SelectorBudget] = None


class RuntimeConfig(BaseModel):
    model_config = _strict()
    max_branch_depth: int = Field(default=1, ge=1)
    max_deferred_queue_length: int = Field(default=8, ge=0)
    max_deferred_drains_per_round: int = Field(default=1, ge=0)
    on_agent_failure: OnAgentFailure = "terminate"
    per_agent_retry_budget: int = Field(default=2, ge=0)
    synthesize_on_terminate: bool = False
    on_budget_exceeded: OnBudgetExceeded = "stop"
    observability_level: ObservabilityLevel = "mvp"
    max_tool_iterations: int = Field(default=8, ge=1)


class Config(BaseModel):
    model_config = _strict()
    schema_version: str = Field(pattern=r"^[0-9]+\.[0-9]+\.[0-9]+$")
    session_id: str = Field(min_length=1)
    originator: str = Field(min_length=1)
    problem_statement: str = Field(min_length=1)
    selector: SelectorConfig
    agents: List[AgentConfig] = Field(min_length=1)
    coordinator: AgentConfig
    budget: BudgetConfig
    runtime: RuntimeConfig = Field(default_factory=RuntimeConfig)


# ---------------------------------------------------------------------------
# SelectorOutput (§5.11) — mirrors selector_output.schema.json
# ---------------------------------------------------------------------------


class SelectorExclusion(BaseModel):
    model_config = _strict()
    id: str = Field(min_length=1)
    reason: str = Field(min_length=1)


class SelectorMissingCapability(BaseModel):
    model_config = _strict()
    capability: str = Field(min_length=1)
    reason: str = Field(min_length=1)
    suggested_persona: Optional[str] = None


class SelectorOutput(BaseModel):
    """Structured selector decision (§5.11, Pass 1 row #119).

    Required: `strategy`, `selected_agents`, `coordinator_agent`. The
    `[v1]` optional fields (`excluded_agents`, `missing_capabilities`,
    `reasoning`) are populated by the `rules` / `llm` strategies for
    replay / audit. Persisted to `<run_dir>/selector_output.json`; it is
    NOT part of the frozen Artifact and does not enter the
    `canonical_transcript` or `transcript_digest`.
    """

    model_config = _strict()
    strategy: SelectorStrategy
    selected_agents: List[str] = Field(min_length=1)
    coordinator_agent: str = Field(min_length=1)
    excluded_agents: Optional[List[SelectorExclusion]] = None
    missing_capabilities: Optional[List[SelectorMissingCapability]] = None
    reasoning: Optional[str] = None


# ---------------------------------------------------------------------------
# Verdict (§5.6)
# ---------------------------------------------------------------------------


class ResolvedDisagreement(BaseModel):
    model_config = _strict()
    topic: str = Field(min_length=1)
    resolution: str = Field(min_length=1)
    agents_involved: Optional[List[str]] = None


class DisagreementPosition(BaseModel):
    model_config = _strict()
    agent: str = Field(min_length=1)
    claim: str = Field(min_length=1)


class UnresolvedDisagreement(BaseModel):
    model_config = _strict()
    topic: str = Field(min_length=1)
    positions: List[DisagreementPosition] = Field(min_length=1)
    blocker: Optional[bool] = None


class UserInputRequest(BaseModel):
    model_config = _strict()
    question: str = Field(min_length=1)
    context: Optional[str] = None
    blocking: Optional[bool] = None


class ExternalResearchRequest(BaseModel):
    model_config = _strict()
    query: str = Field(min_length=1)
    rationale: Optional[str] = None
    suggested_sources: Optional[List[str]] = None


class Verdict(BaseModel):
    model_config = _strict()
    next_action: NextAction
    rationale: str = Field(min_length=1)
    confidence: float = Field(ge=0.0, le=1.0)
    focus: str = Field(min_length=1)
    next_agents: List[str]
    resolved_disagreements: List[ResolvedDisagreement]
    unresolved_disagreements: List[UnresolvedDisagreement]
    user_input_request: Optional[UserInputRequest] = None
    external_research_request: Optional[ExternalResearchRequest] = None

    @model_validator(mode="after")
    def _enforce_payload_exclusivity(self) -> "Verdict":
        if self.next_action == "request_user_input":
            if self.user_input_request is None:
                raise ValueError("next_action=request_user_input requires user_input_request")
            if self.external_research_request is not None:
                raise ValueError("user_input and external_research are exclusive")
        elif self.next_action == "request_external_research":
            if self.external_research_request is None:
                raise ValueError(
                    "next_action=request_external_research requires external_research_request"
                )
            if self.user_input_request is not None:
                raise ValueError("user_input and external_research are exclusive")
        else:
            if self.user_input_request is not None or self.external_research_request is not None:
                raise ValueError(
                    "user_input_request / external_research_request only allowed on matching next_action"
                )
        return self


# ---------------------------------------------------------------------------
# DirectRequest + TurnStructuredOutput (§5.5)
# ---------------------------------------------------------------------------


class DirectRequest(BaseModel):
    model_config = _strict()
    target: str = Field(min_length=1)
    type: str = Field(min_length=1)
    content: Union[str, Dict[str, Any]]

    @field_validator("content")
    @classmethod
    def _content_nonempty_string(cls, v):
        # direct_request.schema.json: content oneOf [{string, minLength:1}, {object}].
        if isinstance(v, str) and len(v) == 0:
            raise ValueError("DirectRequest.content string form must be non-empty")
        return v


class TurnStructuredOutput(BaseModel):
    model_config = _strict()
    text: str = Field(min_length=1)
    direct_requests: Optional[List[DirectRequest]] = None


# ---------------------------------------------------------------------------
# Synthesis content (§5.8)
# ---------------------------------------------------------------------------


class SynthesisContent(BaseModel):
    model_config = _strict()
    integrated_answer: str = Field(min_length=1)
    resolved_disagreements: List[ResolvedDisagreement]
    unresolved_disagreements: List[UnresolvedDisagreement]
    confidence: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    open_questions: Optional[List[str]] = None


# ---------------------------------------------------------------------------
# Usage + ProviderResult (§5.7, §6.6)
# ---------------------------------------------------------------------------


class Usage(BaseModel):
    model_config = _strict()
    prompt_tokens: int = Field(ge=0)
    completion_tokens: int = Field(ge=0)
    total_tokens: int = Field(ge=0)
    cost_usd: float = Field(ge=0.0)
    estimated: Optional[bool] = False


class ProviderError(BaseModel):
    model_config = _strict()
    kind: ErrorKind
    message: str = Field(min_length=1)
    retriable: bool
    details: Optional[Dict[str, Any]] = None


class ToolEvent(BaseModel):
    model_config = _strict()
    name: str = Field(min_length=1)
    arguments: Dict[str, Any]
    result: Optional[Union[Dict[str, Any], str]] = None
    latency_ms: Optional[int] = Field(default=None, ge=0)
    # provider_result.schema.json marks `error` as REQUIRED (nullable):
    # the field must be present on every tool_event, even when null.
    # No default → constructors MUST pass `error=` explicitly.
    error: Optional[ProviderError]


class ProviderRawMessage(BaseModel):
    model_config = ConfigDict(extra="allow")
    role: str = Field(min_length=1)
    content: Any


class ProviderResult(BaseModel):
    model_config = _strict()
    messages: List[ProviderRawMessage]
    tool_events: List[ToolEvent]
    usage: Usage
    finish_reason: FinishReason
    structured_output: Optional[Dict[str, Any]]
    raw: Optional[Dict[str, Any]]
    error: Optional[ProviderError]


# ---------------------------------------------------------------------------
# ProviderRequest (§6.2)
# ---------------------------------------------------------------------------


class ProviderRequestMessage(BaseModel):
    # `additionalProperties: false` per provider_request.schema.json §6.2.
    model_config = _strict()
    role: Literal["system", "user", "assistant", "tool"]
    # Schema: `content` is `oneOf [{string, minLength: 1}, {object}]`.
    content: Union[str, Dict[str, Any]]
    name: Optional[str] = Field(default=None, min_length=1)
    tool_call_id: Optional[str] = Field(default=None, min_length=1)

    @field_validator("content")
    @classmethod
    def _content_nonempty_string(cls, v):
        if isinstance(v, str) and len(v) == 0:
            raise ValueError("ProviderRequestMessage.content string form must be non-empty")
        return v

    @model_validator(mode="after")
    def _tool_role_requires_tool_call_id(self) -> "ProviderRequestMessage":
        # Schema invariant: role=tool → tool_call_id required.
        if self.role == "tool" and self.tool_call_id is None:
            raise ValueError("ProviderRequestMessage with role='tool' requires tool_call_id")
        return self


class ProviderTool(BaseModel):
    """Tool descriptor per provider_request.schema.json `$defs.tool` (§6.4)."""
    model_config = _strict()
    name: str = Field(min_length=1)
    description: str = Field(min_length=1)
    input_schema: Dict[str, Any]
    metadata: Optional[Dict[str, Any]] = None


class ProviderRequest(BaseModel):
    model_config = _strict()
    provider: str = Field(min_length=1)
    model: str = Field(min_length=1)
    agent_id: str = Field(min_length=1)
    # `reasoning_effort` is an OPEN string forwarded to the provider (§6.2);
    # adapters ignoring it MUST do so silently.
    reasoning_effort: Optional[str] = None
    messages: List[ProviderRequestMessage] = Field(min_length=1)
    sampling: Optional[Dict[str, Any]] = None
    # `tools` is REQUIRED by the JSON Schema; empty list means "no tools".
    # Loose dict typing kept for back-compat with adapter-built requests that
    # pass vendor-shaped descriptors. The `ProviderTool` model is the
    # canonical shape; runtime-built requests SHOULD validate items against
    # it before constructing the ProviderRequest.
    tools: List[Dict[str, Any]] = Field(default_factory=list)
    expected_output_schema: ExpectedOutputSchema
    metadata: Optional[Dict[str, Any]] = None

    @field_validator("tools", mode="before")
    @classmethod
    def _coerce_none_tools(cls, v):
        # Back-compat: callers passing `tools=None` historically meant
        # "no tools exposed". Coerce to the schema-required empty list.
        return [] if v is None else v

    @field_validator("tools", mode="after")
    @classmethod
    def _validate_tool_shape(cls, v):
        # Each tool dict MUST conform to provider_request.schema.json $defs.tool
        # (closed: name, description, input_schema, optional metadata).
        # Validate via ProviderTool so an out-of-shape entry is rejected at
        # construction rather than slipping through to the provider call.
        for i, t in enumerate(v):
            try:
                ProviderTool.model_validate(t)
            except Exception as exc:  # noqa: BLE001
                raise ValueError(
                    f"tools[{i}] does not match the $defs.tool shape "
                    f"(name, description, input_schema): {exc}"
                ) from exc
        return v

    @field_validator("expected_output_schema", mode="before")
    @classmethod
    def _coerce_null_string(cls, v):
        # Back-compat: prior API used the string "null" to mean the §6.2
        # free-text path. The JSON Schema enum is `null` (literal). Accept
        # both at the input boundary; normalize to `None`.
        return None if v == "null" else v


# ---------------------------------------------------------------------------
# Message (§5.4) — discriminated by `type`, with per-type content shape
# ---------------------------------------------------------------------------


class SchemaFailureRecord(BaseModel):
    model_config = _strict()
    offending_request: Dict[str, Any]
    reason: str = Field(min_length=1)


class PanelContractionContent(BaseModel):
    model_config = _strict()
    agent_id: str = Field(min_length=1)
    reason: Literal["provider_unrecoverable", "schema_error"]


class Message(BaseModel):
    model_config = _strict()

    id: str = Field(min_length=1)
    speaker: str = Field(min_length=1)
    target: Optional[str] = None
    type: MessageType
    content: Any
    parent_id: Optional[str] = None
    round: int = Field(ge=0)
    turn_index: int = Field(ge=0)
    branch_depth: int = Field(ge=0, le=1)
    timestamp: str
    usage: Usage
    suggested_followups: Optional[List[DirectRequest]] = None
    dropped_deferred: Optional[List[DirectRequest]] = None
    schema_failure: Optional[List[SchemaFailureRecord]] = None

    @model_validator(mode="after")
    def _enforce_per_type_shape(self) -> "Message":
        t = self.type
        if t == "problem_statement":
            if not isinstance(self.content, str) or not self.content:
                raise ValueError("problem_statement.content must be non-empty string")
            if self.round != 0 or self.turn_index != 0 or self.branch_depth != 0:
                raise ValueError("problem_statement counters must be 0")
            if self.parent_id is not None:
                raise ValueError("problem_statement.parent_id must be null")
        elif t in ("primary_turn", "branch_turn"):
            if isinstance(self.content, dict):
                TurnStructuredOutput.model_validate(self.content)
            elif not isinstance(self.content, TurnStructuredOutput):
                raise ValueError(f"{t}.content must validate against TurnStructuredOutput")
            if self.round < 1:
                raise ValueError(f"{t}.round must be ≥1")
            if t == "primary_turn":
                if self.branch_depth != 0:
                    raise ValueError("primary_turn.branch_depth must be 0")
                if self.parent_id is not None:
                    raise ValueError("primary_turn.parent_id must be null")
            else:  # branch_turn
                if self.branch_depth != 1:
                    raise ValueError("branch_turn.branch_depth must be 1")
                if not self.parent_id:
                    raise ValueError("branch_turn.parent_id is required")
        elif t == "coordination_turn":
            if isinstance(self.content, dict):
                Verdict.model_validate(self.content)
            elif not isinstance(self.content, Verdict):
                raise ValueError("coordination_turn.content must be a Verdict")
            if self.round < 1 or self.branch_depth != 0 or self.parent_id is not None:
                raise ValueError("coordination_turn counters invalid")
        elif t == "synthesis":
            if isinstance(self.content, dict):
                SynthesisContent.model_validate(self.content)
            elif not isinstance(self.content, SynthesisContent):
                raise ValueError("synthesis.content must be SynthesisContent")
            if self.round < 1 or self.branch_depth != 0 or self.parent_id is not None:
                raise ValueError("synthesis counters invalid")
        elif t == "panel_contraction":
            if self.speaker != "runtime":
                raise ValueError("panel_contraction.speaker must be 'runtime'")
            if isinstance(self.content, dict):
                PanelContractionContent.model_validate(self.content)
            elif not isinstance(self.content, PanelContractionContent):
                raise ValueError("panel_contraction.content must be PanelContractionContent")
            if self.round < 1:
                raise ValueError("panel_contraction.round must be ≥1")
        return self


# ---------------------------------------------------------------------------
# ContextPacket (§5.9)
# ---------------------------------------------------------------------------


class PanelDisclosureEntry(BaseModel):
    model_config = _strict()
    id: str = Field(min_length=1)
    role_summary: str = Field(min_length=1)


class ContextPacket(BaseModel):
    model_config = _strict()
    problem_statement: str = Field(min_length=1)
    round: int = Field(ge=1)
    persona_material: Persona
    panel_disclosure: List[PanelDisclosureEntry] = Field(min_length=1)
    previous_verdict: Optional[Verdict] = None
    current_round_messages: List[Message]
    parent_message: Optional[Message] = None
    originating_direct_request: Optional[DirectRequest] = None


# ---------------------------------------------------------------------------
# TerminationArtifact (§5.8)
# ---------------------------------------------------------------------------


class TerminationArtifact(BaseModel):
    model_config = _strict()
    schema_version: str = Field(default=SCHEMA_VERSION, pattern=r"^[0-9]+\.[0-9]+\.[0-9]+$")
    reason: TerminationReason
    final_round: int = Field(ge=0)
    cumulative_usage: Usage
    most_recent_verdict: Optional[Verdict] = None
    unresolved_disagreements: List[UnresolvedDisagreement] = Field(default_factory=list)
    transcript_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    pending_user_input_request: Optional[UserInputRequest] = None
    pending_external_research_request: Optional[ExternalResearchRequest] = None

    @model_validator(mode="after")
    def _enforce_reason_payload(self) -> "TerminationArtifact":
        if self.reason == "user_input_required":
            if self.pending_user_input_request is None:
                raise ValueError("user_input_required reason requires pending_user_input_request")
            if self.pending_external_research_request is not None:
                raise ValueError("pending_external_research_request not allowed for user_input_required")
        elif self.reason == "external_research_required":
            if self.pending_external_research_request is None:
                raise ValueError(
                    "external_research_required reason requires pending_external_research_request"
                )
            if self.pending_user_input_request is not None:
                raise ValueError("pending_user_input_request not allowed for external_research_required")
        else:
            if (
                self.pending_user_input_request is not None
                or self.pending_external_research_request is not None
            ):
                raise ValueError("pending_* payloads only allowed on matching reason")
        return self


# ---------------------------------------------------------------------------
# Artifact (§5.10)
# ---------------------------------------------------------------------------


class SynthesisOutcome(BaseModel):
    model_config = _strict()
    kind: Literal["synthesis"] = "synthesis"
    synthesis_message_id: str = Field(min_length=1)


class TerminationOutcome(BaseModel):
    model_config = _strict()
    kind: Literal["termination"] = "termination"
    termination_artifact: TerminationArtifact


Outcome = Union[SynthesisOutcome, TerminationOutcome]


class Artifact(BaseModel):
    model_config = _strict()
    schema_version: str = Field(default=SCHEMA_VERSION, pattern=r"^[0-9]+\.[0-9]+\.[0-9]+$")
    session_id: str = Field(min_length=1)
    config: Config
    canonical_transcript: List[Message] = Field(min_length=1)
    outcome: Outcome = Field(discriminator="kind")
    cumulative_usage: Usage
    cumulative_unresolved: List[UnresolvedDisagreement]
    transcript_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    started_at: str
    ended_at: str


# ---------------------------------------------------------------------------
# RunManifest (§7.2)
# ---------------------------------------------------------------------------


class ProducerInfo(BaseModel):
    model_config = _strict()
    name: str = Field(min_length=1)
    version: str = Field(pattern=r"^[0-9]+\.[0-9]+\.[0-9]+(?:[-+][0-9A-Za-z.-]+)?$")


class RunManifest(BaseModel):
    model_config = _strict()
    schema_version: str = Field(default=SCHEMA_VERSION, pattern=r"^[0-9]+\.[0-9]+\.[0-9]+$")
    manifest_version: str = Field(default=SCHEMA_VERSION, pattern=r"^[0-9]+\.[0-9]+\.[0-9]+$")
    session_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    status: ManifestStatus
    producer: ProducerInfo
    created_at: str
    updated_at: Optional[str] = None
    artifact_path: str = Field(min_length=1)
    config_path: str = Field(min_length=1)
    journal_path: Optional[str] = None
    observability_log_path: Optional[str] = None
    transcript_digest: Optional[str] = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    outcome_kind: Optional[Literal["synthesis", "termination"]] = None
    notes: Optional[str] = None

    @model_validator(mode="after")
    def _enforce_status_conditionals(self) -> "RunManifest":
        if self.status in ("complete", "terminated"):
            if self.transcript_digest is None or self.outcome_kind is None:
                raise ValueError(f"status={self.status} requires transcript_digest + outcome_kind")
            if self.status == "complete" and self.outcome_kind != "synthesis":
                raise ValueError("status=complete requires outcome_kind=synthesis")
            if self.status == "terminated" and self.outcome_kind != "termination":
                raise ValueError("status=terminated requires outcome_kind=termination")
        else:  # in_progress / crashed
            if self.transcript_digest is not None or self.outcome_kind is not None:
                raise ValueError(
                    f"status={self.status} must not carry transcript_digest / outcome_kind"
                )
            if self.updated_at is None:
                raise ValueError(f"status={self.status} requires updated_at")
        return self


# ---------------------------------------------------------------------------
# FakeProviderScript (§5 / Pass 7 §9.2)
# ---------------------------------------------------------------------------


class FakeProviderMatch(BaseModel):
    model_config = _strict()
    agent_id: Optional[str] = Field(default=None, min_length=1)
    expected_output_schema: Optional[
        Literal["turn_structured_output", "verdict", "synthesis_content"]
    ] = None
    round: Optional[int] = Field(default=None, ge=0)
    turn_index: Optional[int] = Field(default=None, ge=0)


class FakeProviderEntry(BaseModel):
    model_config = _strict()
    match: Optional[FakeProviderMatch] = None
    result: ProviderResult
    comment: Optional[str] = None


class FakeProviderScript(BaseModel):
    model_config = _strict()
    schema_version: str = Field(default=SCHEMA_VERSION, pattern=r"^[0-9]+\.[0-9]+\.[0-9]+$")
    description: Optional[str] = None
    on_exhaustion: Literal["error", "loop"] = "error"
    entries: List[FakeProviderEntry] = Field(min_length=1)


# Re-export commonly used aliases
Synthesis = SynthesisContent

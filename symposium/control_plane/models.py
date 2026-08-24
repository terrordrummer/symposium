"""Persisted Symposium 2.x workspace and room models.

These product records deliberately live outside the frozen v1 deliberation
schemas. A room can reference many immutable v1 runs without changing their
configs, transcripts, digests, or artifacts.
"""

from __future__ import annotations

from typing import Annotated, Any, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator

ControlId = Annotated[
    str,
    Field(min_length=1, max_length=64, pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$"),
]
RoomStatus = Literal["active", "archived"]
AgentStatus = Literal["active", "archived"]
MembershipRole = Literal["coordinator", "member", "guest", "observer"]
PresenceState = Literal[
    "offline", "joining", "listening", "thinking", "speaking", "leaving"
]
EventType = Literal[
    "workspace_initialized",
    "room_created",
    "room_switched",
    "room_archived",
    "agent_created",
    "agent_invited",
    "agent_dismissed",
    "presence_changed",
    "briefing_requested",
    "briefing_completed",
    "briefing_failed",
]


def _strict() -> ConfigDict:
    return ConfigDict(extra="forbid")


class WorkspaceRecord(BaseModel):
    model_config = _strict()

    id: ControlId
    name: str = Field(min_length=1, max_length=120)
    default_room_id: ControlId
    active_room_id: ControlId
    created_at: str = Field(min_length=1)
    updated_at: str = Field(min_length=1)


class RoomRecord(BaseModel):
    model_config = _strict()

    id: ControlId
    name: str = Field(min_length=1, max_length=120)
    purpose: str = Field(min_length=1, max_length=2000)
    status: RoomStatus = "active"
    created_at: str = Field(min_length=1)
    updated_at: str = Field(min_length=1)
    archived_at: Optional[str] = None


class AgentRecord(BaseModel):
    model_config = _strict()

    id: ControlId
    display_name: str = Field(min_length=1, max_length=120)
    instructions: str = Field(min_length=1, max_length=20_000)
    capabilities: List[str] = Field(default_factory=list)
    status: AgentStatus = "active"
    built_in: bool = False
    avatar_id: Optional[ControlId] = None
    created_at: str = Field(min_length=1)
    updated_at: str = Field(min_length=1)


class RoomMembership(BaseModel):
    model_config = _strict()

    room_id: ControlId
    agent_id: ControlId
    role: MembershipRole
    presence: PresenceState
    onboarding_context: Optional[str] = Field(default=None, max_length=20_000)
    joined_at: str = Field(min_length=1)
    updated_at: str = Field(min_length=1)
    left_at: Optional[str] = None

    @property
    def key(self) -> str:
        return f"{self.room_id}:{self.agent_id}"


class RoomEvent(BaseModel):
    model_config = _strict()

    id: str = Field(pattern=r"^evt-[0-9]{8}$")
    sequence: int = Field(ge=1)
    type: EventType
    timestamp: str = Field(min_length=1)
    actor: str = Field(min_length=1, max_length=120)
    room_id: Optional[ControlId] = None
    agent_id: Optional[ControlId] = None
    details: Dict[str, Any] = Field(default_factory=dict)


class ControlPlaneState(BaseModel):
    """Atomically persisted snapshot plus its append-only logical event trail."""

    model_config = _strict()

    schema_version: Literal["2.0.0-alpha.1"] = "2.0.0-alpha.1"
    workspace: WorkspaceRecord
    rooms: Dict[str, RoomRecord]
    agents: Dict[str, AgentRecord]
    memberships: Dict[str, RoomMembership]
    events: List[RoomEvent]
    next_event_sequence: int = Field(ge=1)

    @model_validator(mode="after")
    def _references_are_closed(self) -> "ControlPlaneState":
        for room_id in (self.workspace.default_room_id, self.workspace.active_room_id):
            if room_id not in self.rooms:
                raise ValueError(f"workspace references missing room {room_id!r}")
        for key, membership in self.memberships.items():
            if key != membership.key:
                raise ValueError(f"membership key {key!r} does not match its record")
            if membership.room_id not in self.rooms:
                raise ValueError(f"membership references missing room {membership.room_id!r}")
            if membership.agent_id not in self.agents:
                raise ValueError(f"membership references missing agent {membership.agent_id!r}")
        if self.events:
            expected = list(range(1, len(self.events) + 1))
            observed = [event.sequence for event in self.events]
            if observed != expected or self.next_event_sequence != expected[-1] + 1:
                raise ValueError("room-event sequence is not contiguous")
        elif self.next_event_sequence != 1:
            raise ValueError("empty event trail must start at sequence 1")
        return self

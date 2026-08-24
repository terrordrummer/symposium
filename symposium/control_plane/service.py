"""Sartori's deterministic room and agent control-plane operations."""

from __future__ import annotations

import re
import secrets
import unicodedata
from pathlib import Path
from typing import Any, Callable, Optional, Sequence

from symposium.avatars import avatar_by_id, avatar_for, avatar_for_agent, avatar_pool
from symposium.control_plane.models import (
    AgentRecord,
    ControlPlaneState,
    MembershipRole,
    PresenceState,
    RoomEvent,
    RoomMembership,
    RoomRecord,
    WorkspaceRecord,
)
from symposium.control_plane.store import ControlPlaneStore
from symposium.models import now_utc_iso
from symposium.personas import COORDINATOR, DEFAULT_PANEL


class ControlPlaneError(ValueError):
    """User-correctable room, agent, or membership command failure."""


def slugify(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode()
    slug = re.sub(r"[^a-z0-9]+", "-", normalized.lower()).strip("-")
    if not slug:
        raise ControlPlaneError("name must contain at least one ASCII letter or digit")
    if slug[0].isdigit():
        slug = f"room-{slug}"
    return slug[:64].rstrip("-")


class ControlPlane:
    """High-level local API used by Sartori, the CLI, MCP, and the viewer."""

    def __init__(
        self,
        state_dir: Path,
        *,
        clock: Callable[[], str] = now_utc_iso,
        avatar_chooser: Callable[[Sequence[str]], str] = secrets.choice,
    ) -> None:
        self.store = ControlPlaneStore(Path(state_dir))
        self.clock = clock
        self.avatar_chooser = avatar_chooser

    def ensure_initialized(self, *, name: str = "Symposium Workspace") -> ControlPlaneState:
        if self.store.exists:
            state = self.store.load()
            if any(agent.avatar_id is None for agent in state.agents.values()):
                state, _ = self.store.update(self._assign_missing_avatars)
            return state
        now = self.clock()
        room = RoomRecord(
            id="symposium",
            name="Symposium",
            purpose="Default reflection room",
            created_at=now,
            updated_at=now,
        )
        agents: dict[str, AgentRecord] = {}
        memberships: dict[str, RoomMembership] = {}
        for persona in [*DEFAULT_PANEL, COORDINATOR]:
            profile = avatar_for(persona.id)
            agent = AgentRecord(
                id=persona.id,
                display_name=profile.display_name,
                instructions=(
                    f"Scope: {persona.reasoning_scope}. "
                    f"Style: {persona.reasoning_style}."
                ),
                capabilities=[persona.reasoning_scope],
                built_in=True,
                avatar_id=persona.id,
                created_at=now,
                updated_at=now,
            )
            agents[agent.id] = agent
            membership = RoomMembership(
                room_id=room.id,
                agent_id=agent.id,
                role="coordinator" if agent.id == "coordinator" else "member",
                presence="listening",
                joined_at=now,
                updated_at=now,
            )
            memberships[membership.key] = membership
        workspace = WorkspaceRecord(
            id="local",
            name=name,
            default_room_id=room.id,
            active_room_id=room.id,
            created_at=now,
            updated_at=now,
        )
        event = RoomEvent(
            id="evt-00000001",
            sequence=1,
            type="workspace_initialized",
            timestamp=now,
            actor="sartori",
            room_id=room.id,
            details={"built_in_agents": list(agents)},
        )
        return self.store.create(ControlPlaneState(
            workspace=workspace,
            rooms={room.id: room},
            agents=agents,
            memberships=memberships,
            events=[event],
            next_event_sequence=2,
        ))

    def create_room(
        self, name: str, purpose: str, *, room_id: Optional[str] = None, actor: str = "sartori"
    ) -> RoomRecord:
        rid = room_id or slugify(name)

        def mutation(state: ControlPlaneState) -> RoomRecord:
            if rid in state.rooms:
                raise ControlPlaneError(f"room {rid!r} already exists")
            if any(room.name.casefold() == name.casefold() for room in state.rooms.values()):
                raise ControlPlaneError(f"a room named {name!r} already exists")
            now = self.clock()
            room = RoomRecord(
                id=rid, name=name, purpose=purpose, created_at=now, updated_at=now
            )
            state.rooms[rid] = room
            coordinator = RoomMembership(
                room_id=rid,
                agent_id="coordinator",
                role="coordinator",
                presence="listening",
                joined_at=now,
                updated_at=now,
            )
            state.memberships[coordinator.key] = coordinator
            self._event(state, "room_created", actor, room_id=rid, details={"name": name})
            self._touch(state, now)
            return room

        return self._update(mutation)

    def switch_room(self, room: str, *, actor: str = "sartori") -> RoomRecord:
        def mutation(state: ControlPlaneState) -> RoomRecord:
            record = self._room(state, room)
            if record.status == "archived":
                raise ControlPlaneError(f"room {record.id!r} is archived")
            previous = state.workspace.active_room_id
            now = self.clock()
            state.workspace.active_room_id = record.id
            self._event(
                state,
                "room_switched",
                actor,
                room_id=record.id,
                details={"previous_room_id": previous},
            )
            self._touch(state, now)
            return record

        return self._update(mutation)

    def archive_room(self, room: str, *, actor: str = "sartori") -> RoomRecord:
        def mutation(state: ControlPlaneState) -> RoomRecord:
            record = self._room(state, room)
            if record.id == state.workspace.default_room_id:
                raise ControlPlaneError("the default Symposium room cannot be archived")
            if record.id == state.workspace.active_room_id:
                raise ControlPlaneError("switch to another room before archiving the active room")
            if record.status == "archived":
                return record
            now = self.clock()
            record.status = "archived"
            record.archived_at = now
            record.updated_at = now
            for membership in state.memberships.values():
                if membership.room_id == record.id and membership.presence != "offline":
                    membership.presence = "offline"
                    membership.left_at = now
                    membership.updated_at = now
            self._event(state, "room_archived", actor, room_id=record.id)
            self._touch(state, now)
            return record

        return self._update(mutation)

    def create_agent(
        self,
        agent_id: str,
        display_name: str,
        instructions: str,
        *,
        capabilities: Optional[list[str]] = None,
        avatar_id: Optional[str] = None,
        actor: str = "sartori",
    ) -> AgentRecord:
        def mutation(state: ControlPlaneState) -> AgentRecord:
            if agent_id in state.agents:
                raise ControlPlaneError(f"agent {agent_id!r} already exists")
            if any(
                agent.display_name.casefold() == display_name.casefold()
                for agent in state.agents.values()
            ):
                raise ControlPlaneError(
                    f"an agent named {display_name!r} already exists"
                )
            now = self.clock()
            selected_avatar = self._select_avatar(state, avatar_id)
            agent = AgentRecord(
                id=agent_id,
                display_name=display_name,
                instructions=instructions,
                capabilities=list(capabilities or []),
                avatar_id=selected_avatar,
                created_at=now,
                updated_at=now,
            )
            state.agents[agent.id] = agent
            self._event(
                state,
                "agent_created",
                actor,
                agent_id=agent.id,
                details={"display_name": display_name, "avatar_id": selected_avatar},
            )
            self._touch(state, now)
            return agent

        return self._update(mutation)

    def invite_agent(
        self,
        agent: str,
        *,
        room: Optional[str] = None,
        role: MembershipRole = "guest",
        onboarding_context: Optional[str] = None,
        actor: str = "sartori",
    ) -> RoomMembership:
        def mutation(state: ControlPlaneState) -> RoomMembership:
            agent_record = self._agent(state, agent)
            room_record = self._room(state, room or state.workspace.active_room_id)
            if room_record.status == "archived":
                raise ControlPlaneError(f"room {room_record.id!r} is archived")
            key = f"{room_record.id}:{agent_record.id}"
            existing = state.memberships.get(key)
            if existing is not None and existing.presence != "offline":
                raise ControlPlaneError(
                    f"agent {agent_record.id!r} is already in room {room_record.id!r}"
                )
            now = self.clock()
            membership = RoomMembership(
                room_id=room_record.id,
                agent_id=agent_record.id,
                role=role,
                presence="listening",
                onboarding_context=onboarding_context,
                joined_at=now,
                updated_at=now,
            )
            state.memberships[key] = membership
            self._event(
                state,
                "agent_invited",
                actor,
                room_id=room_record.id,
                agent_id=agent_record.id,
                details={"role": role, "has_onboarding_context": bool(onboarding_context)},
            )
            self._touch(state, now)
            return membership

        return self._update(mutation)

    def dismiss_agent(
        self, agent: str, *, room: Optional[str] = None, actor: str = "sartori"
    ) -> RoomMembership:
        def mutation(state: ControlPlaneState) -> RoomMembership:
            agent_record = self._agent(state, agent)
            if agent_record.id == "coordinator":
                raise ControlPlaneError("Sartori cannot be dismissed from a room")
            room_record = self._room(state, room or state.workspace.active_room_id)
            key = f"{room_record.id}:{agent_record.id}"
            membership = state.memberships.get(key)
            if membership is None or membership.presence == "offline":
                raise ControlPlaneError(
                    f"agent {agent_record.id!r} is not present in room {room_record.id!r}"
                )
            now = self.clock()
            membership.presence = "offline"
            membership.left_at = now
            membership.updated_at = now
            self._event(
                state,
                "agent_dismissed",
                actor,
                room_id=room_record.id,
                agent_id=agent_record.id,
            )
            self._touch(state, now)
            return membership

        return self._update(mutation)

    def set_presence(
        self,
        agent: str,
        presence: PresenceState,
        *,
        room: Optional[str] = None,
        actor: str = "runtime",
    ) -> RoomMembership:
        def mutation(state: ControlPlaneState) -> RoomMembership:
            agent_record = self._agent(state, agent)
            room_record = self._room(state, room or state.workspace.active_room_id)
            membership = state.memberships.get(f"{room_record.id}:{agent_record.id}")
            if membership is None or membership.presence == "offline":
                raise ControlPlaneError(
                    f"agent {agent_record.id!r} is not present in room {room_record.id!r}"
                )
            now = self.clock()
            membership.presence = presence
            membership.updated_at = now
            self._event(
                state,
                "presence_changed",
                actor,
                room_id=room_record.id,
                agent_id=agent_record.id,
                details={"presence": presence},
            )
            self._touch(state, now)
            return membership

        return self._update(mutation)

    def request_briefing(
        self,
        problem: str,
        *,
        session_id: str,
        room: Optional[str] = None,
        participant_ids: Optional[list[str]] = None,
        actor: str = "user:browser",
    ) -> RoomEvent:
        """Audit the room-to-run link before background execution begins."""
        def mutation(state: ControlPlaneState) -> RoomEvent:
            room_record = self._room(state, room or state.workspace.active_room_id)
            if room_record.status == "archived":
                raise ControlPlaneError(f"room {room_record.id!r} is archived")
            now = self.clock()
            event = self._event(
                state,
                "briefing_requested",
                actor,
                room_id=room_record.id,
                details={
                    "session_id": session_id,
                    "problem": problem,
                    "participant_ids": list(participant_ids or []),
                },
            )
            self._touch(state, now)
            return event

        return self._update(mutation)

    def finish_briefing(
        self,
        *,
        session_id: str,
        room: str,
        outcome: Optional[str] = None,
        termination_reason: Optional[str] = None,
        error: Optional[str] = None,
        actor: str = "runtime",
    ) -> RoomEvent:
        """Record the terminal result while leaving the v1 artifact untouched."""
        def mutation(state: ControlPlaneState) -> RoomEvent:
            room_record = self._room(state, room)
            now = self.clock()
            details = {"session_id": session_id}
            if outcome is not None:
                details["outcome"] = outcome
            if termination_reason is not None:
                details["termination_reason"] = termination_reason
            if error is not None:
                details["error"] = error[:2000]
            event = self._event(
                state,
                "briefing_failed" if error else "briefing_completed",
                actor,
                room_id=room_record.id,
                details=details,
            )
            self._touch(state, now)
            return event

        return self._update(mutation)

    def snapshot(self) -> ControlPlaneState:
        return self.store.load()

    def public_snapshot(self) -> dict[str, Any]:
        state = self.snapshot()
        active = state.rooms[state.workspace.active_room_id]
        counts: dict[str, int] = {room_id: 0 for room_id in state.rooms}
        for membership in state.memberships.values():
            if membership.presence != "offline":
                counts[membership.room_id] += 1
        rooms = [
            {
                "id": room.id,
                "name": room.name,
                "purpose": room.purpose,
                "status": room.status,
                "active": room.id == active.id,
                "participant_count": counts[room.id],
            }
            for room in state.rooms.values()
        ]
        participants = []
        for membership in state.memberships.values():
            if membership.room_id != active.id or membership.presence == "offline":
                continue
            agent = state.agents[membership.agent_id]
            profile = avatar_for_agent(agent.id, agent.display_name, agent.avatar_id)
            participants.append({
                "id": agent.id,
                "label": agent.display_name,
                "reasoning_scope": " · ".join(agent.capabilities) or membership.role,
                "persona_class": "control-plane",
                "avatar": profile.viewer_payload(),
                "role": membership.role,
                "presence": membership.presence,
                "is_coordinator": membership.role == "coordinator",
            })
        return {
            "initialized": True,
            # Event sequences advance on every effective mutation, unlike the
            # human-readable timestamp which can legitimately repeat within
            # one second. The browser uses this as its refresh token.
            "revision": state.next_event_sequence - 1,
            "workspace": state.workspace.model_dump(mode="json"),
            "active_room": {
                "id": active.id,
                "name": active.name,
                "purpose": active.purpose,
            },
            "rooms": rooms,
            "agents": [
                {
                    "id": agent.id,
                    "display_name": agent.display_name,
                    "capabilities": list(agent.capabilities),
                    "built_in": agent.built_in,
                    "avatar": avatar_for_agent(
                        agent.id, agent.display_name, agent.avatar_id
                    ).viewer_payload(),
                    "status": agent.status,
                }
                for agent in state.agents.values()
            ],
            "available_avatars": [
                profile.viewer_payload()
                for profile in avatar_pool()
                if profile.persona_id not in {
                    agent.avatar_id for agent in state.agents.values()
                }
            ],
            "participants": participants,
            "recent_events": [
                event.model_dump(mode="json") for event in state.events[-50:]
            ],
        }

    def _select_avatar(
        self,
        state: ControlPlaneState,
        requested: Optional[str] = None,
    ) -> str:
        used = {agent.avatar_id for agent in state.agents.values() if agent.avatar_id}
        available = [
            profile.persona_id
            for profile in avatar_pool()
            if profile.persona_id not in used
        ]
        if requested:
            profile = avatar_by_id(requested)
            if profile is None or requested not in {item.persona_id for item in avatar_pool()}:
                raise ControlPlaneError(f"avatar {requested!r} is not in the reusable pool")
            if requested in used:
                raise ControlPlaneError(f"avatar {requested!r} is already assigned")
            return requested
        if not available:
            raise ControlPlaneError("the reusable avatar pool is exhausted")
        return self.avatar_chooser(available)

    def _assign_missing_avatars(self, state: ControlPlaneState) -> None:
        """Migrate pre-pool workspaces without changing their event history."""
        used = {agent.avatar_id for agent in state.agents.values() if agent.avatar_id}
        for agent in state.agents.values():
            if agent.avatar_id is not None:
                continue
            if agent.built_in and avatar_by_id(agent.id) is not None:
                agent.avatar_id = agent.id
                used.add(agent.id)
                continue
            available = [
                profile.persona_id
                for profile in avatar_pool()
                if profile.persona_id not in used
            ]
            if not available:
                continue
            agent.avatar_id = self.avatar_chooser(available)
            used.add(agent.avatar_id)

    def _update(self, mutation):
        self.ensure_initialized()
        _state, result = self.store.update(mutation)
        return result

    def _event(
        self,
        state: ControlPlaneState,
        event_type,
        actor: str,
        *,
        room_id: Optional[str] = None,
        agent_id: Optional[str] = None,
        details: Optional[dict[str, Any]] = None,
    ) -> RoomEvent:
        sequence = state.next_event_sequence
        event = RoomEvent(
            id=f"evt-{sequence:08d}",
            sequence=sequence,
            type=event_type,
            timestamp=self.clock(),
            actor=actor,
            room_id=room_id,
            agent_id=agent_id,
            details=details or {},
        )
        state.events.append(event)
        state.next_event_sequence += 1
        return event

    @staticmethod
    def _touch(state: ControlPlaneState, now: str) -> None:
        state.workspace.updated_at = now

    @staticmethod
    def _room(state: ControlPlaneState, reference: str) -> RoomRecord:
        if reference in state.rooms:
            return state.rooms[reference]
        matches = [r for r in state.rooms.values() if r.name.casefold() == reference.casefold()]
        if len(matches) == 1:
            return matches[0]
        raise ControlPlaneError(f"unknown room {reference!r}")

    @staticmethod
    def _agent(state: ControlPlaneState, reference: str) -> AgentRecord:
        if reference in state.agents:
            return state.agents[reference]
        matches = [
            agent for agent in state.agents.values()
            if agent.display_name.casefold() == reference.casefold()
        ]
        if len(matches) == 1:
            return matches[0]
        raise ControlPlaneError(f"unknown agent {reference!r}")

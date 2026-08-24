"""Symposium 2.x room control-plane tests (separate from frozen v1 runs)."""

from __future__ import annotations

import json
import os
import time

import pytest

from symposium.control_plane import (
    ControlPlane,
    ControlPlaneBusy,
    ControlPlaneError,
    ControlPlaneStore,
    execute_sartori_command,
    parse_sartori_command,
)


class _Clock:
    def __init__(self) -> None:
        self.tick = 0

    def __call__(self) -> str:
        self.tick += 1
        return f"2026-08-12T00:00:{self.tick:02d}Z"


@pytest.fixture
def control_plane(tmp_path):
    return ControlPlane(tmp_path / ".symposium", clock=_Clock())


def test_initialize_creates_default_symposium_room_panel_and_sartori(control_plane):
    state = control_plane.ensure_initialized()

    assert state.workspace.default_room_id == "symposium"
    assert state.workspace.active_room_id == "symposium"
    assert list(state.rooms) == ["symposium"]
    assert list(state.agents) == [
        "logician", "visionary", "researcher", "critic", "engineer", "coordinator"
    ]
    assert len(state.memberships) == 6
    sartori = state.memberships["symposium:coordinator"]
    assert sartori.role == "coordinator"
    assert sartori.presence == "listening"
    assert state.events[0].type == "workspace_initialized"

    # Initialization is idempotent and never duplicates the audit event.
    assert len(control_plane.ensure_initialized().events) == 1


def test_room_creation_starts_with_sartori_and_can_be_switched(control_plane):
    control_plane.ensure_initialized()
    room = control_plane.create_room("Product Strategy", "Decide product direction")
    assert room.id == "product-strategy"

    state = control_plane.snapshot()
    assert state.memberships["product-strategy:coordinator"].presence == "listening"
    assert state.workspace.active_room_id == "symposium"

    switched = control_plane.switch_room("Product Strategy")
    assert switched.id == "product-strategy"
    assert control_plane.snapshot().workspace.active_room_id == "product-strategy"
    assert [event.type for event in control_plane.snapshot().events[-2:]] == [
        "room_created", "room_switched"
    ]


def test_zeus_guest_can_be_onboarded_invited_briefed_and_dismissed(control_plane):
    control_plane.ensure_initialized()
    control_plane.create_room("Zeus Focus Talking", "Project status and decisions")
    control_plane.create_agent(
        "zeus-lead",
        "Responsabile Zeus",
        "Report facts, risks, blockers, and the next decision.",
        capabilities=["zeus-project-status", "delivery-risk"],
    )
    membership = control_plane.invite_agent(
        "zeus-lead",
        room="zeus-focus-talking",
        onboarding_context="Disclose only current milestones and open blockers.",
    )
    assert membership.role == "guest"
    assert membership.presence == "listening"
    assert membership.onboarding_context.startswith("Disclose only")

    control_plane.switch_room("zeus-focus-talking")
    snapshot = control_plane.public_snapshot()
    assert snapshot["active_room"]["name"] == "Zeus Focus Talking"
    assert [p["id"] for p in snapshot["participants"]] == ["coordinator", "zeus-lead"]
    zeus = snapshot["participants"][1]
    assert zeus["avatar"]["portrait_url"].startswith("/static/avatars/pool-")
    assert zeus["avatar"]["voice"]["presentation"] in {"feminine", "masculine"}
    assert zeus["role"] == "guest"

    control_plane.set_presence("zeus-lead", "speaking")
    assert control_plane.public_snapshot()["participants"][1]["presence"] == "speaking"
    dismissed = control_plane.dismiss_agent("zeus-lead")
    assert dismissed.presence == "offline"
    assert [p["id"] for p in control_plane.public_snapshot()["participants"]] == [
        "coordinator"
    ]
    assert [event.type for event in control_plane.snapshot().events[-4:]] == [
        "agent_invited", "room_switched", "presence_changed", "agent_dismissed"
    ]


def test_failed_mutation_does_not_rewrite_valid_state(control_plane):
    control_plane.ensure_initialized()
    before = control_plane.store.path.read_bytes()
    with pytest.raises(ControlPlaneError, match="already exists"):
        control_plane.create_room("Symposium", "duplicate")
    assert control_plane.store.path.read_bytes() == before
    json.loads(before)


def test_public_revision_advances_even_when_timestamps_are_identical(tmp_path):
    control = ControlPlane(tmp_path / ".symposium", clock=lambda: "2026-08-12T00:00:00Z")
    control.ensure_initialized()
    assert control.public_snapshot()["revision"] == 1

    control.create_room("Product", "Roadmap")
    assert control.public_snapshot()["revision"] == 2
    control.create_agent("product-lead", "Product Lead", "Report status")
    assert control.public_snapshot()["revision"] == 3


def test_agent_display_names_are_unambiguous(control_plane):
    control_plane.ensure_initialized()
    control_plane.create_agent("zeus-lead", "Responsabile Zeus", "Report status")

    with pytest.raises(ControlPlaneError, match="named.*already exists"):
        control_plane.create_agent("zeus-backup", "responsabile zeus", "Backup")


def test_custom_agents_receive_unique_persisted_pool_avatars(tmp_path):
    control = ControlPlane(
        tmp_path / ".symposium",
        clock=_Clock(),
        avatar_chooser=lambda choices: choices[-1],
    )
    control.ensure_initialized()
    first = control.create_agent("one", "One", "First")
    second = control.create_agent("two", "Two", "Second")
    assert first.avatar_id == "pool-050"
    assert second.avatar_id == "pool-049"
    assert control.ensure_initialized().agents["one"].avatar_id == "pool-050"


def test_requested_avatar_cannot_be_reused(control_plane):
    control_plane.ensure_initialized()
    first = control_plane.create_agent(
        "one", "One", "First", avatar_id="pool-001"
    )
    assert first.avatar_id == "pool-001"
    with pytest.raises(ControlPlaneError, match="already assigned"):
        control_plane.create_agent("two", "Two", "Second", avatar_id="pool-001")


def test_default_active_and_sartori_lifecycle_guards(control_plane):
    control_plane.ensure_initialized()
    with pytest.raises(ControlPlaneError, match="default.*cannot be archived"):
        control_plane.archive_room("symposium")
    with pytest.raises(ControlPlaneError, match="Sartori cannot be dismissed"):
        control_plane.dismiss_agent("coordinator")

    control_plane.create_room("Temporary", "Short-lived work")
    control_plane.switch_room("temporary")
    with pytest.raises(ControlPlaneError, match="switch to another room"):
        control_plane.archive_room("temporary")
    control_plane.switch_room("symposium")
    archived = control_plane.archive_room("temporary")
    assert archived.status == "archived"
    with pytest.raises(ControlPlaneError, match="archived"):
        control_plane.switch_room("temporary")


def test_room_events_are_contiguous_after_reload(control_plane):
    control_plane.ensure_initialized()
    control_plane.create_room("Research", "Evidence gathering")
    control_plane.switch_room("research")
    state = control_plane.snapshot()
    assert [event.sequence for event in state.events] == [1, 2, 3]
    assert [event.id for event in state.events] == [
        "evt-00000001", "evt-00000002", "evt-00000003"
    ]
    assert state.next_event_sequence == 4


@pytest.mark.parametrize(
    ("text", "action", "expected"),
    [
        (
            "crea una stanza Prodotto per decidere la roadmap",
            "create_room",
            {"name": "Prodotto", "purpose": "decidere la roadmap", "activate": True},
        ),
        ("vai nella stanza Prodotto", "switch_room", {"room": "Prodotto"}),
        ("invita il Responsabile Zeus", "invite_agent", {"agent": "Responsabile Zeus"}),
        ("congeda Responsabile Zeus", "dismiss_agent", {"agent": "Responsabile Zeus"}),
        ("archivia la stanza Prodotto", "archive_room", {"room": "Prodotto"}),
    ],
)
def test_sartori_italian_command_parser(text, action, expected):
    parsed = parse_sartori_command(text)
    assert parsed.action == action
    assert parsed.arguments == expected


def test_sartori_commands_execute_without_model_or_network(control_plane):
    control_plane.ensure_initialized()
    message = execute_sartori_command(
        control_plane, "crea una stanza Prodotto per decidere la roadmap"
    )
    assert message == "Ho creato e aperto la stanza Prodotto."
    assert control_plane.snapshot().workspace.active_room_id == "prodotto"

    control_plane.create_agent("product-lead", "Responsabile Prodotto", "Give status")
    assert execute_sartori_command(
        control_plane, "invita il Responsabile Prodotto"
    ) == "Ho invitato Responsabile Prodotto nella stanza."
    assert execute_sartori_command(
        control_plane, "congeda Responsabile Prodotto"
    ) == "Ho congedato Responsabile Prodotto."

    with pytest.raises(ControlPlaneError, match="comando non riconosciuto"):
        execute_sartori_command(control_plane, "fai qualcosa di indefinito")


# ---------------------------------------------------------------------------
# Workspace lock recovery (store)
# ---------------------------------------------------------------------------


def _init_store(tmp_path):
    store = ControlPlaneStore(tmp_path / ".symposium")
    control = ControlPlane(tmp_path / ".symposium")
    control.ensure_initialized()
    return store


def test_unparseable_lock_younger_than_grace_is_busy(tmp_path):
    """An empty lockfile could be a live mid-acquisition holder: refuse."""
    store = _init_store(tmp_path)
    lock = tmp_path / ".symposium" / ".control-plane.lock"
    lock.write_text("", encoding="utf-8")  # crash between O_EXCL and PID write
    with pytest.raises(ControlPlaneBusy, match="workspace is busy"):
        store.update(lambda state: state)


def test_unparseable_lock_older_than_grace_is_broken(tmp_path):
    """A stale unparseable lock must not wedge the workspace forever.

    Pre-fix: `_stale_lock` returned False for any unparseable content, so a
    crash between lock creation and the PID write permanently raised
    ControlPlaneBusy until manual deletion.
    """
    store = _init_store(tmp_path)
    lock = tmp_path / ".symposium" / ".control-plane.lock"
    lock.write_text("", encoding="utf-8")
    old = time.time() - 120.0
    os.utime(lock, (old, old))
    validated, _result = store.update(lambda state: state)
    assert validated.workspace.default_room_id == "symposium"
    assert not lock.exists()  # released by the successful acquisition


def test_dead_pid_lock_is_stale_and_reacquired(tmp_path):
    """Parseable locks keep the existing dead-PID staleness semantics."""
    store = _init_store(tmp_path)
    lock = tmp_path / ".symposium" / ".control-plane.lock"
    lock.write_text("999999999\n", encoding="utf-8")  # pid assumed absent
    store.update(lambda state: state)
    assert not lock.exists()


def test_live_pid_lock_stays_busy(tmp_path):
    """A lock naming THIS process is definitionally live — never broken."""
    store = _init_store(tmp_path)
    lock = tmp_path / ".symposium" / ".control-plane.lock"
    lock.write_text(f"{os.getpid()}\n", encoding="utf-8")
    with pytest.raises(ControlPlaneBusy):
        store.update(lambda state: state)

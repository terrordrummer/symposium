"""Small, deterministic Italian command surface for Sartori.

This is intentionally not an LLM parser: it is local, predictable, free, and
limited to control-plane mutations. Structured browser controls remain the
fallback whenever a sentence is ambiguous.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from symposium.control_plane.service import ControlPlane, ControlPlaneError


@dataclass(frozen=True, slots=True)
class SartoriCommand:
    action: str
    arguments: dict[str, Any] = field(default_factory=dict)


def _clean_reference(value: str) -> str:
    return value.strip().strip(" .!?\"'“”")


def parse_sartori_command(text: str) -> SartoriCommand:
    """Parse the deliberately small no-cost Italian command vocabulary."""
    command = " ".join(text.strip().split())
    if not command:
        raise ControlPlaneError("scrivi un comando per Sartori")

    if re.fullmatch(r"(?:stato|situazione|dove siamo|mostra stato)[.!?]?", command, re.I):
        return SartoriCommand("status")

    match = re.fullmatch(
        r"crea(?:mi)? (?:una )?stanza(?: chiamata)? (.+?)"
        r"(?: (?:con (?:lo )?scopo(?: di)?|per) (.+))?[.!?]?",
        command,
        re.I,
    )
    if match:
        name = _clean_reference(match.group(1))
        purpose = _clean_reference(match.group(2) or "Stanza di lavoro coordinata da Sartori")
        return SartoriCommand(
            "create_room", {"name": name, "purpose": purpose, "activate": True}
        )

    match = re.fullmatch(
        r"(?:vai|passa|spostati|cambia)(?: nella| alla| a)?(?: stanza)? (.+?)[.!?]?",
        command,
        re.I,
    )
    if match:
        return SartoriCommand("switch_room", {"room": _clean_reference(match.group(1))})

    match = re.fullmatch(
        r"invita (?:l['’]agente |l['’]|il |la )?(.+?)"
        r"(?: nella stanza (.+?))?[.!?]?",
        command,
        re.I,
    )
    if match:
        arguments = {"agent": _clean_reference(match.group(1))}
        if match.group(2):
            arguments["room"] = _clean_reference(match.group(2))
        return SartoriCommand("invite_agent", arguments)

    match = re.fullmatch(
        r"(?:congeda|rimuovi|manda via) (?:l['’]agente |l['’]|il |la )?(.+?)"
        r"(?: dalla stanza (.+?))?[.!?]?",
        command,
        re.I,
    )
    if match:
        arguments = {"agent": _clean_reference(match.group(1))}
        if match.group(2):
            arguments["room"] = _clean_reference(match.group(2))
        return SartoriCommand("dismiss_agent", arguments)

    match = re.fullmatch(
        r"(?:archivia|chiudi|distruggi) (?:la )?(?:stanza )?(.+?)[.!?]?",
        command,
        re.I,
    )
    if match:
        return SartoriCommand("archive_room", {"room": _clean_reference(match.group(1))})

    raise ControlPlaneError(
        "comando non riconosciuto; prova «crea una stanza …», «vai nella "
        "stanza …», «invita …», «congeda …» oppure usa i controlli del pannello"
    )


def execute_sartori_command(control: ControlPlane, text: str) -> str:
    """Execute one parsed command and return a concise Italian confirmation."""
    parsed = parse_sartori_command(text)
    args = parsed.arguments
    if parsed.action == "status":
        room = control.public_snapshot()["active_room"]
        return f"Sei nella stanza {room['name']}."
    if parsed.action == "create_room":
        room = control.create_room(args["name"], args["purpose"])
        if args.get("activate"):
            control.switch_room(room.id)
        return f"Ho creato e aperto la stanza {room.name}."
    if parsed.action == "switch_room":
        room = control.switch_room(args["room"])
        return f"Siamo entrati nella stanza {room.name}."
    if parsed.action == "invite_agent":
        membership = control.invite_agent(args["agent"], room=args.get("room"))
        agent = control.snapshot().agents[membership.agent_id]
        return f"Ho invitato {agent.display_name} nella stanza."
    if parsed.action == "dismiss_agent":
        membership = control.dismiss_agent(args["agent"], room=args.get("room"))
        agent = control.snapshot().agents[membership.agent_id]
        return f"Ho congedato {agent.display_name}."
    if parsed.action == "archive_room":
        room = control.archive_room(args["room"])
        return f"Ho archiviato la stanza {room.name}."
    raise AssertionError(f"unhandled Sartori action {parsed.action!r}")

"""Map persisted transcript messages to the viewer's SSE event payloads.

Pure, read-only transforms over the run directory's ``config.json`` and
``transcript.jsonl``. Two responsibilities:

1. :func:`config_event` — derive the meeting-grid inputs (panel personas
   + coordinator + the problem statement) from ``config.json``.
2. :class:`EdgeResolver` — turn each ``branch_turn`` into a directed-arrow
   edge by looking back at the message it answers. A ``branch_turn``'s
   ``parent_id`` points at the requesting message (a primary or
   coordination turn that emitted ``content.direct_requests``); the asker
   is that parent's ``speaker`` and the answerer is the branch turn's own
   ``speaker``. The matching ``direct_requests`` entry (``target ==
   answerer``) supplies the edge ``type`` and ``content``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from symposium.avatars import avatar_for

# Per-type readable text, in priority order. primary/branch turns carry
# `.text`; synthesis carries `integrated_answer`; coordination (Verdict)
# carries `rationale`/`focus`. Mirrors get_run_status's extraction so the
# two consumers render the same string.
_TEXT_KEYS = ("text", "integrated_answer", "rationale", "focus", "summary")
_TEXT_FALLBACK_MAX = 2000


def extract_text(msg: Dict[str, Any]) -> str:
    content = msg.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, dict):
        for key in _TEXT_KEYS:
            v = content.get(key)
            if isinstance(v, str) and v:
                return v
        return json.dumps(content, ensure_ascii=False)[:_TEXT_FALLBACK_MAX]
    return ""


def _direct_requests(msg: Dict[str, Any]) -> List[Dict[str, Any]]:
    content = msg.get("content")
    if isinstance(content, dict):
        drs = content.get("direct_requests")
        if isinstance(drs, list):
            return [d for d in drs if isinstance(d, dict)]
    return []


def _persona_entry(persona_ref: Any, agent_id: str) -> Dict[str, Any]:
    """Normalize an agent's persona_ref (str id or embedded Persona dict)."""
    agent_id = _safe_id(agent_id, "?")
    if isinstance(persona_ref, dict):
        pid = _safe_id(persona_ref.get("id"), agent_id)
        return {
            "id": agent_id,
            "label": _label(pid),
            "persona_id": pid,
            "persona_class": persona_ref.get("persona_class"),
            "reasoning_scope": persona_ref.get("reasoning_scope"),
            "avatar": avatar_for(pid).viewer_payload(),
        }
    # bare string id (unresolved ref)
    pid = _safe_id(persona_ref, agent_id)
    return {
        "id": agent_id,
        "label": _label(pid),
        "persona_id": pid,
        "persona_class": None,
        "reasoning_scope": None,
        "avatar": avatar_for(pid).viewer_payload(),
    }


def _label(pid: str) -> str:
    return pid.replace("_", " ").replace("-", " ").strip().title() or pid


def _safe_id(value: Any, fallback: str) -> str:
    return value.strip() if isinstance(value, str) and value.strip() else fallback


def config_event(run_dir: Path) -> Dict[str, Any]:
    """Build the ``config`` SSE payload from ``<run_dir>/config.json``.

    Degrades gracefully: if config.json is missing/partial, returns
    whatever can be derived (the message stream alone still drives the
    chat; the meeting grid simply starts empty).
    """
    run_dir = Path(run_dir)
    cfg_path = run_dir / "config.json"
    payload: Dict[str, Any] = {
        "session_id": run_dir.name,
        "personas": [],
        "coordinator": None,
        "coordinator_profile": None,
        "problem_statement": "",
    }
    if not cfg_path.exists():
        return payload
    try:
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return payload

    payload["session_id"] = cfg.get("session_id") or run_dir.name
    payload["problem_statement"] = cfg.get("problem_statement") or ""

    personas: List[Dict[str, Any]] = []
    for ac in cfg.get("agents", []) or []:
        if not isinstance(ac, dict):
            continue
        personas.append(_persona_entry(ac.get("persona_ref"), ac.get("id", "?")))
    payload["personas"] = personas

    coord = cfg.get("coordinator")
    if isinstance(coord, dict):
        payload["coordinator"] = _safe_id(coord.get("id"), "coordinator")
        payload["coordinator_profile"] = _persona_entry(
            coord.get("persona_ref"), payload["coordinator"]
        )
    selector = cfg.get("selector")
    if not payload["coordinator"] and isinstance(selector, dict):
        payload["coordinator"] = _safe_id(
            selector.get("coordinator_agent"), "coordinator"
        )
    if payload["coordinator"] and payload["coordinator_profile"] is None:
        coordinator_id = payload["coordinator"]
        payload["coordinator_profile"] = _persona_entry(
            coordinator_id, coordinator_id
        )
    return payload


class EdgeResolver:
    """Stateful: remembers messages by id so branch turns resolve to arrows.

    Feed every message through :meth:`register` in stream order, then call
    :meth:`edge_for` on the same message to get its directed-request edge
    (or ``None`` for a plain turn). Registration is idempotent and order-
    only-forward: a branch turn always arrives after its parent in the
    journal, so by the time we resolve it the parent is already known.
    """

    def __init__(self) -> None:
        self._by_id: Dict[str, Dict[str, Any]] = {}

    def register(self, msg: Dict[str, Any]) -> None:
        mid = msg.get("id")
        if isinstance(mid, str):
            self._by_id[mid] = msg

    def edge_for(self, msg: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        if msg.get("type") != "branch_turn":
            return None
        parent_id = msg.get("parent_id")
        answerer = msg.get("speaker")
        if not parent_id or not answerer:
            return None
        parent = self._by_id.get(parent_id)
        if parent is None:
            # Parent not seen (shouldn't happen in a well-formed journal);
            # still emit a minimal edge so the arrow renders.
            return {
                "from": None,
                "to": answerer,
                "type": "direct-request",
                "content": "",
                "parent_id": parent_id,
            }
        asker = parent.get("speaker")
        dr = self._match_request(parent, answerer)
        return {
            "from": asker,
            "to": answerer,
            "type": (dr or {}).get("type") or "direct-request",
            "content": _dr_content_text(dr) if dr else "",
            "parent_id": parent_id,
        }

    @staticmethod
    def _match_request(parent: Dict[str, Any], answerer: str) -> Optional[Dict[str, Any]]:
        reqs = _direct_requests(parent)
        for dr in reqs:
            if dr.get("target") == answerer:
                return dr
        # Fall back to the sole request if there's exactly one (target may
        # differ from the resolved branch agent in odd configs).
        return reqs[0] if len(reqs) == 1 else None


def _dr_content_text(dr: Dict[str, Any]) -> str:
    c = dr.get("content")
    if isinstance(c, str):
        return c
    if isinstance(c, dict):
        return json.dumps(c, ensure_ascii=False)[:_TEXT_FALLBACK_MAX]
    return ""


def message_event(index: int, msg: Dict[str, Any], resolver: EdgeResolver) -> Dict[str, Any]:
    """Build the ``message`` SSE payload for one transcript line.

    ``resolver`` must already have :meth:`~EdgeResolver.register`-ed this
    message (and all prior ones) so ``edge`` can be derived.
    """
    return {
        "index": index,
        "id": msg.get("id"),
        "speaker": msg.get("speaker"),
        "type": msg.get("type"),
        "round": msg.get("round"),
        "turn_index": msg.get("turn_index"),
        "branch_depth": msg.get("branch_depth"),
        "parent_id": msg.get("parent_id"),
        "timestamp": msg.get("timestamp"),
        "text": extract_text(msg),
        "direct_requests": _direct_requests(msg),
        "edge": resolver.edge_for(msg),
    }

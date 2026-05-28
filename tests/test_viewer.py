"""Tests for the read-only live viewer (`symposium watch`).

Cover the parts with real logic: truncation-safe tailing, newest-run
discovery, config-event layout derivation, and the branch_turn → arrow
edge resolution. No HTTP server is spun up here (the handler is thin glue
over these helpers).
"""

from __future__ import annotations

import json
from pathlib import Path

from symposium.viewer.discovery import list_runs, newest_run
from symposium.viewer.streamer import EdgeResolver, config_event, extract_text, message_event
from symposium.viewer.tail import JournalTail


def _write_lines(path: Path, objs):
    with open(path, "a", encoding="utf-8") as f:
        for o in objs:
            f.write(json.dumps(o) + "\n")


def test_tail_incremental_and_history(tmp_path):
    j = tmp_path / "transcript.jsonl"
    tail = JournalTail(j)
    assert tail.drain() == []  # no file yet

    _write_lines(j, [{"id": "a"}, {"id": "b"}])
    first = tail.drain()
    assert [m["id"] for m in first] == ["a", "b"]
    assert tail.drain() == []  # nothing new

    _write_lines(j, [{"id": "c"}])
    assert [m["id"] for m in tail.drain()] == ["c"]


def test_tail_holds_partial_trailing_line(tmp_path):
    j = tmp_path / "transcript.jsonl"
    tail = JournalTail(j)
    # write a complete line plus a half-flushed one (no trailing newline)
    j.write_text(json.dumps({"id": "a"}) + "\n" + '{"id": "b", "spea', encoding="utf-8")
    drained = tail.drain()
    assert [m["id"] for m in drained] == ["a"]  # partial line withheld

    # the rest of line b arrives
    with open(j, "a", encoding="utf-8") as f:
        f.write('ker": "x"}\n')
    drained = tail.drain()
    assert [m["id"] for m in drained] == ["b"]
    assert drained[0]["speaker"] == "x"


def test_discovery_newest_by_mtime(tmp_path):
    older = tmp_path / "run-old"
    newer = tmp_path / "run-new"
    for d in (older, newer):
        d.mkdir()
        (d / "transcript.jsonl").write_text("{}\n", encoding="utf-8")
    import os

    os.utime(older / "transcript.jsonl", (1000, 1000))
    os.utime(newer / "transcript.jsonl", (2000, 2000))

    runs = list_runs(tmp_path)
    assert [r.name for r in runs] == ["run-new", "run-old"]
    assert newest_run(tmp_path).name == "run-new"


def test_discovery_ignores_non_run_dirs(tmp_path):
    (tmp_path / "not-a-run").mkdir()
    (tmp_path / "real").mkdir()
    (tmp_path / "real" / "config.json").write_text("{}", encoding="utf-8")
    names = [r.name for r in list_runs(tmp_path)]
    assert names == ["real"]


def test_config_event_layout(tmp_path):
    cfg = {
        "session_id": "sess1",
        "problem_statement": "what is X?",
        "agents": [
            {"id": "logician", "persona_ref": {"id": "logician", "persona_class": "horizontal",
                                               "reasoning_scope": "formal reasoning"}},
            {"id": "researcher", "persona_ref": "researcher"},
        ],
        "coordinator": {"id": "coordinator"},
        "selector": {"coordinator_agent": "coordinator"},
    }
    (tmp_path / "config.json").write_text(json.dumps(cfg), encoding="utf-8")
    ev = config_event(tmp_path)
    assert ev["session_id"] == "sess1"
    assert ev["problem_statement"] == "what is X?"
    assert ev["coordinator"] == "coordinator"
    ids = [p["id"] for p in ev["personas"]]
    assert ids == ["logician", "researcher"]
    assert ev["personas"][0]["persona_class"] == "horizontal"
    assert ev["personas"][0]["label"] == "Logician"
    # bare-string persona_ref still yields a usable entry
    assert ev["personas"][1]["persona_id"] == "researcher"


def test_config_event_missing_file(tmp_path):
    ev = config_event(tmp_path)
    assert ev["personas"] == []
    assert ev["coordinator"] is None


def test_edge_resolution_branch_turn():
    resolver = EdgeResolver()
    parent = {
        "id": "p1", "speaker": "critic", "type": "primary_turn",
        "content": {"text": "...", "direct_requests": [
            {"target": "logician", "type": "clarification-request", "content": "pin it down"},
        ]},
    }
    branch = {
        "id": "b1", "speaker": "logician", "type": "branch_turn",
        "parent_id": "p1", "branch_depth": 1, "content": {"text": "here is the pin"},
    }
    resolver.register(parent)
    resolver.register(branch)
    assert resolver.edge_for(parent) is None  # plain turn → no arrow
    edge = resolver.edge_for(branch)
    assert edge == {
        "from": "critic", "to": "logician", "type": "clarification-request",
        "content": "pin it down", "parent_id": "p1",
    }


def test_edge_missing_parent_falls_back():
    resolver = EdgeResolver()
    branch = {"id": "b1", "speaker": "logician", "type": "branch_turn", "parent_id": "ghost"}
    resolver.register(branch)
    edge = resolver.edge_for(branch)
    assert edge["to"] == "logician"
    assert edge["from"] is None
    assert edge["type"] == "direct-request"


def test_extract_text_per_type():
    assert extract_text({"content": "raw string"}) == "raw string"
    assert extract_text({"content": {"text": "turn text"}}) == "turn text"
    assert extract_text({"content": {"integrated_answer": "synth"}}) == "synth"
    assert extract_text({"content": {"rationale": "because"}}) == "because"
    # unknown dict → JSON fallback, not empty
    out = extract_text({"content": {"weird": 1}})
    assert "weird" in out


def test_message_event_shape():
    resolver = EdgeResolver()
    msg = {
        "id": "m1", "speaker": "critic", "type": "primary_turn", "round": 1,
        "turn_index": 4, "branch_depth": 0, "timestamp": "2026-01-01T00:00:00Z",
        "content": {"text": "hello", "direct_requests": [
            {"target": "logician", "type": "challenge", "content": "prove it"}]},
    }
    resolver.register(msg)
    ev = message_event(7, msg, resolver)
    assert ev["index"] == 7
    assert ev["speaker"] == "critic"
    assert ev["text"] == "hello"
    assert ev["direct_requests"][0]["type"] == "challenge"
    assert ev["edge"] is None

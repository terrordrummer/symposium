"""Tests for the viewer HTTP/SSE server (`symposium.viewer.server`).

Exercises the real request handler over an in-process ThreadingHTTPServer
on an ephemeral loopback port: run listing, SSE stream replay, Host-header
gating, static whitelisting, and path confinement. Offline and fast — the
SSE pacing constants are patched down so a finished-run stream completes
in a few hundredths of a second.
"""

from __future__ import annotations

import http.client
import json
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote

import pytest

from symposium.viewer import server as viewer_server


def _write_run(root: Path, name: str, messages) -> Path:
    run_dir = root / name
    run_dir.mkdir()
    cfg = {
        "session_id": name,
        "problem_statement": "what is X?",
        "agents": [{"id": "logician", "persona_ref": "logician"}],
        "coordinator": {"id": "coordinator"},
        "selector": {"coordinator_agent": "coordinator"},
    }
    (run_dir / "config.json").write_text(json.dumps(cfg), encoding="utf-8")
    with open(run_dir / "transcript.jsonl", "w", encoding="utf-8") as f:
        for m in messages:
            f.write(json.dumps(m) + "\n")
    return run_dir


@pytest.fixture
def viewer(tmp_path, monkeypatch):
    # Fast-forward the SSE pacing so a finished-run stream ends immediately.
    monkeypatch.setattr(viewer_server, "_POLL_INTERVAL", 0.01)
    monkeypatch.setattr(viewer_server, "_POST_FINISH_GRACE", 0.02)

    _write_run(
        tmp_path,
        "run-a",
        [
            {
                "id": "m0", "speaker": "user:cli", "type": "problem_statement",
                "round": 0, "turn_index": 0, "branch_depth": 0,
                "timestamp": "2026-01-01T00:00:00Z", "content": "what is X?",
            },
            {
                "id": "m1", "speaker": "logician", "type": "primary_turn",
                "round": 1, "turn_index": 0, "branch_depth": 0,
                "timestamp": "2026-01-01T00:00:05Z", "content": {"text": "X is Y"},
            },
        ],
    )
    _write_run(tmp_path, "run-b", [])

    handler = type(
        "_TestHandler",
        (viewer_server._Handler,),
        {"runs_root": tmp_path, "bind_host": "127.0.0.1"},
    )
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    # A tight poll interval keeps per-test shutdown() near-instant.
    thread = threading.Thread(
        target=lambda: httpd.serve_forever(poll_interval=0.02), daemon=True
    )
    thread.start()
    try:
        yield httpd.server_address[1], tmp_path
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def _get(port: int, path: str, host_header: str | None = None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        headers = {}
        if host_header is not None:
            headers["Host"] = host_header  # suppresses the automatic Host
        conn.request("GET", path, headers=headers)
        resp = conn.getresponse()
        return resp.status, resp.read()
    finally:
        conn.close()


def _get_stream(port: int, path: str, max_lines: int = 200):
    """Read an SSE response line-by-line up to (and including) the `end`
    event. The stream keeps the connection alive, so reading to EOF would
    block — the client closes once the terminal event has arrived."""
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        conn.request("GET", path)
        resp = conn.getresponse()
        if resp.status != 200:
            return resp.status, resp.read().decode("utf-8")
        lines = []
        ended = False
        for _ in range(max_lines):
            raw = resp.fp.readline()
            if not raw:
                break
            line = raw.decode("utf-8").rstrip("\n")
            lines.append(line)
            if line == "event: end":
                ended = True
            elif ended and line.startswith("data: "):
                break
        return resp.status, "\n".join(lines)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# /api/runs
# ---------------------------------------------------------------------------


def test_api_runs_lists_names_not_paths(viewer):
    port, root = viewer
    status, body = _get(port, "/api/runs")
    assert status == 200
    data = json.loads(body)
    names = [r["name"] for r in data["runs"]]
    assert set(names) == {"run-a", "run-b"}
    # No absolute filesystem paths leak into the payload.
    assert str(root) not in body.decode("utf-8")
    assert all("path" not in r for r in data["runs"])


# ---------------------------------------------------------------------------
# /api/stream
# ---------------------------------------------------------------------------


def test_stream_replays_history_and_ends(viewer):
    port, _root = viewer
    status, text = _get_stream(port, "/api/stream?run=run-a")
    assert status == 200
    events = [ln.split(": ", 1)[1] for ln in text.splitlines() if ln.startswith("event: ")]
    assert events[0] == "config"
    assert events.count("message") == 2
    assert events[-1] == "end"

    datas = [json.loads(ln[len("data: "):]) for ln in text.splitlines() if ln.startswith("data: ")]
    speakers = [d["speaker"] for d in datas if "speaker" in d]
    assert speakers == ["user:cli", "logician"]
    # No lock on disk → the run reads as finished, not active.
    statuses = [d for d in datas if "run_active" in d]
    assert statuses and all(s["run_active"] is False for s in statuses)


def test_stream_missing_run_param_404(viewer):
    port, _root = viewer
    status, _ = _get(port, "/api/stream")
    assert status == 404


def test_stream_unknown_run_404(viewer):
    port, _root = viewer
    status, _ = _get(port, "/api/stream?run=no-such-run")
    assert status == 404


def test_stream_traversal_confined_404(viewer):
    port, _root = viewer
    status, _ = _get(port, "/api/stream?run=" + quote("../../etc"))
    assert status == 404


def test_stream_absolute_path_outside_root_404(viewer, tmp_path_factory):
    """A real run dir OUTSIDE runs_root must not be streamable by path."""
    port, _root = viewer
    outside = tmp_path_factory.mktemp("outside")
    _write_run(outside, "leak-me", [])
    status, _ = _get(port, "/api/stream?run=" + quote(str(outside / "leak-me")))
    assert status == 404


# ---------------------------------------------------------------------------
# Host-header gate
# ---------------------------------------------------------------------------


def test_host_header_rebinding_rejected(viewer):
    port, _root = viewer
    for host in ("evil.example.com", "evil.example.com:8000", "10.0.0.7:80", ""):
        status, body = _get(port, "/api/runs", host_header=host)
        assert status == 403, host
        assert body == b""  # no body for a rejected origin


def test_host_header_loopback_variants_allowed(viewer):
    port, _root = viewer
    for host in ("localhost", "localhost:9999", f"127.0.0.1:{port}", "[::1]:8000", "[::1]"):
        status, _ = _get(port, "/api/runs", host_header=host)
        assert status == 200, host


def test_host_header_gate_covers_stream(viewer):
    port, _root = viewer
    status, body = _get(port, "/api/stream?run=run-a", host_header="evil.example.com")
    assert status == 403
    assert body == b""


# ---------------------------------------------------------------------------
# Static serving
# ---------------------------------------------------------------------------


def test_index_served(viewer):
    port, _root = viewer
    status, body = _get(port, "/")
    assert status == 200
    assert b"<" in body  # the viewer page, not an error


def test_static_whitelist_blocks_other_files(viewer):
    port, _root = viewer
    status, _ = _get(port, "/static/" + quote("../server.py"))
    assert status == 404
    status, _ = _get(port, "/static/secrets.txt")
    assert status == 404

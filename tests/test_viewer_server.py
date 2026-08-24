"""Tests for the viewer HTTP/SSE server (`symposium.viewer.server`).

Exercises the real request handler over an in-process ThreadingHTTPServer
on an ephemeral loopback port: run listing, SSE replay delivery, Host-header
gating, static whitelisting, and path confinement. Replay timing belongs to
the browser so it can wait for local speech or explicit user advancement.
"""

from __future__ import annotations

import http.client
import json
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote

import pytest

from symposium.control_plane import ControlPlane
from symposium.viewer import server as viewer_server


class _BrowserExecutionStub:
    def __init__(self):
        self.jobs = []

    def start(self, problem, *, room=None):
        class Job:
            id = "job-browser-test"
            room_id = room or "symposium"
            room_name = "Symposium"

            @staticmethod
            def public_payload():
                return {
                    "id": "job-browser-test",
                    "room_id": room or "symposium",
                    "room_name": "Symposium",
                    "run_name": "room-session-browser-test",
                    "problem": problem,
                    "participant_ids": ["logician"],
                    "status": "preparing",
                    "created_at": "2026-08-12T00:00:00Z",
                    "updated_at": "2026-08-12T00:00:00Z",
                    "outcome": None,
                    "termination_reason": None,
                    "error": None,
                }

        job = Job()
        self.jobs = [job.public_payload()]
        return job

    def public_jobs(self):
        return list(self.jobs)

    def has_active_job(self):
        return any(job["status"] in {"preparing", "running"} for job in self.jobs)


class _TTSStub:
    def __init__(self, root: Path):
        self.audio = root / "test-voice.wav"
        self.audio.write_bytes(b"RIFF" + b"\x00" * 64)

    def public_status(self):
        return {
            "state": "ready",
            "local_only": True,
            "api_key_required": False,
        }

    def install(self):
        return self.public_status()

    def synthesize(self, text, voice_description):
        assert text
        assert "Richard" in voice_description
        return self.audio


@pytest.mark.parametrize("disconnect", [BrokenPipeError, ConnectionResetError])
def test_handler_suppresses_disconnect_before_request_is_parsed(disconnect):
    """Browser speculative connections can reset before ``do_GET`` runs."""

    class DisconnectingHandler(viewer_server._Handler):
        def __init__(self):
            pass

        def handle_one_request(self):
            raise disconnect("client went away")

    DisconnectingHandler().handle()


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
    workspace_root = tmp_path / ".symposium"
    ControlPlane(workspace_root).ensure_initialized()
    execution_manager = _BrowserExecutionStub()

    handler = type(
        "_TestHandler",
        (viewer_server._Handler,),
        {
            "runs_root": tmp_path,
            "workspace_root": workspace_root,
            "execution_manager": execution_manager,
            "tts_manager": _TTSStub(tmp_path),
            "bind_host": "127.0.0.1",
            "launcher_mode": True,
            "shutdown_callback": staticmethod(lambda: None),
        },
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


def _post_control(
    port: int,
    payload: dict,
    *,
    marker: bool = True,
    origin: str | None = None,
    path: str = "/api/control",
):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        headers = {"Content-Type": "application/json"}
        if marker:
            headers["X-Symposium-Request"] = "1"
        if origin is not None:
            headers["Origin"] = origin
        conn.request("POST", path, body=json.dumps(payload), headers=headers)
        response = conn.getresponse()
        return response.status, json.loads(response.read())
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


def test_api_workspace_exposes_active_room_without_private_agent_instructions(viewer):
    port, root = viewer
    control = ControlPlane(root / ".symposium")
    control.create_room("Zeus Focus", "Project briefing")
    control.create_agent("zeus-lead", "Responsabile Zeus", "Private instructions")
    control.invite_agent("zeus-lead", room="zeus-focus")
    control.switch_room("zeus-focus")

    status, body = _get(port, "/api/workspace")
    assert status == 200
    payload = json.loads(body)
    assert payload["active_room"]["id"] == "zeus-focus"
    assert [p["id"] for p in payload["participants"]] == [
        "coordinator", "zeus-lead"
    ]
    assert b"Private instructions" not in body


def test_api_workspace_initializes_itself_without_a_terminal_command(viewer):
    port, root = viewer
    state_file = root / ".symposium" / "control-plane.json"
    state_file.unlink()

    status, body = _get(port, "/api/workspace")

    assert status == 200
    payload = json.loads(body)
    assert payload["initialized"] is True
    assert payload["active_room"]["id"] == "symposium"
    assert payload["revision"] == 1
    assert state_file.is_file()


def test_local_tts_status_setup_and_gender_bound_synthesis(viewer):
    port, _root = viewer
    status, body = _get(port, "/api/tts/status")
    assert status == 200
    assert json.loads(body)["state"] == "ready"

    status, setup = _post_control(port, {}, path="/api/tts/setup")
    assert status == 202
    assert setup["api_key_required"] is False

    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        conn.request(
            "POST",
            "/api/tts/synthesize",
            body=json.dumps({"agent_id": "logician", "text": "Ciao."}),
            headers={
                "Content-Type": "application/json",
                "X-Symposium-Request": "1",
            },
        )
        response = conn.getresponse()
        audio = response.read()
        assert response.status == 200
        assert response.getheader("Content-Type") == "audio/wav"
        assert audio.startswith(b"RIFF")
    finally:
        conn.close()


def test_control_api_drives_room_and_agent_lifecycle_without_terminal(viewer):
    port, _root = viewer
    status, created_room = _post_control(port, {
        "action": "command",
        "command": "crea una stanza Zeus Focus per il briefing di progetto",
    }, origin=f"http://127.0.0.1:{port}")
    assert status == 200
    assert created_room["ok"] is True
    assert created_room["snapshot"]["active_room"]["id"] == "zeus-focus"

    status, created_agent = _post_control(port, {
        "action": "create_agent",
        "agent_id": "zeus-lead",
        "display_name": "Responsabile Zeus",
        "instructions": "Private project instructions",
        "capabilities": ["project-status"],
    })
    assert status == 200
    assert b"Private project instructions" not in json.dumps(created_agent).encode()

    status, invited = _post_control(port, {
        "action": "command",
        "command": "invita il Responsabile Zeus",
    })
    assert status == 200
    assert [p["id"] for p in invited["snapshot"]["participants"]] == [
        "coordinator", "zeus-lead"
    ]

    status, dismissed = _post_control(port, {
        "action": "dismiss_agent",
        "agent": "zeus-lead",
    })
    assert status == 200
    assert [p["id"] for p in dismissed["snapshot"]["participants"]] == [
        "coordinator"
    ]


def test_control_api_starts_room_session_and_exposes_job_status(viewer):
    port, _root = viewer
    status, started = _post_control(port, {
        "action": "start_session",
        "problem": "Qual è la decisione da prendere?",
        "room": "symposium",
    })

    assert status == 200
    assert started["ok"] is True
    assert started["job"]["id"] == "job-browser-test"
    assert started["job"]["run_name"] == "room-session-browser-test"

    status, body = _get(port, "/api/executions")
    assert status == 200
    jobs = json.loads(body)["jobs"]
    assert jobs[0]["problem"] == "Qual è la decisione da prendere?"


def test_launcher_system_api_can_stop_without_a_terminal(viewer):
    port, _root = viewer
    status, body = _get(port, "/api/system")
    assert status == 200
    system = json.loads(body)
    assert system == {
        "name": "Symposium",
        "launcher_mode": True,
        "can_shutdown": True,
    }

    status, result = _post_control(
        port,
        {},
        path="/api/system/shutdown",
        origin=f"http://127.0.0.1:{port}",
    )
    assert status == 200
    assert result["ok"] is True


def test_launcher_refuses_shutdown_during_a_room_run(viewer):
    port, _root = viewer
    status, _started = _post_control(port, {
        "action": "start_session",
        "problem": "Discussione ancora in corso",
        "room": "symposium",
    })
    assert status == 200

    status, result = _post_control(port, {}, path="/api/system/shutdown")
    assert status == 409
    assert "attendi la fine" in result["error"]


def test_control_api_rejects_cross_site_or_unmarked_mutations(viewer):
    port, _root = viewer
    payload = {"action": "create_room", "name": "Forbidden", "purpose": "CSRF"}
    status, result = _post_control(port, payload, marker=False)
    assert status == 403
    assert result["ok"] is False

    status, result = _post_control(
        port, payload, origin="https://hostile.example"
    )
    assert status == 403
    assert result["ok"] is False

    status, workspace = _get(port, "/api/workspace")
    assert status == 200
    assert "forbidden" not in {room["id"] for room in json.loads(workspace)["rooms"]}


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


def test_stream_marks_finished_history_for_client_controlled_playback(viewer):
    port, _root = viewer
    status, text = _get_stream(port, "/api/stream?run=run-a&replay=manual")
    assert status == 200
    events = [ln.split(": ", 1)[1] for ln in text.splitlines() if ln.startswith("event: ")]
    assert events[:2] == ["config", "playback"]
    assert events.count("message") == 2
    assert events.count("playback") == 2
    payloads = [
        json.loads(ln[len("data: "):])
        for ln in text.splitlines()
        if ln.startswith("data: ")
    ]
    playback = [payload for payload in payloads if "mode" in payload]
    assert playback == [
        {"active": True, "mode": "manual", "speed": None},
        {"active": False, "mode": "manual", "speed": None},
    ]


def test_outcome_exposes_actionable_provider_termination(tmp_path):
    run_dir = tmp_path / "failed-run"
    run_dir.mkdir()
    (run_dir / "artifact.json").write_text(json.dumps({
        "outcome": {
            "kind": "termination",
            "termination_artifact": {
                "reason": "provider_unrecoverable",
                "last_provider_failure": {
                    "agent_id": "critic",
                    "provider": "claude-cli",
                    "message": "unexpected structured result",
                },
            },
        },
    }), encoding="utf-8")

    handler = object.__new__(viewer_server._Handler)
    payload = handler._outcome(run_dir)

    assert payload["outcome"] == "termination"
    assert payload["termination_reason"] == "provider_unrecoverable"
    assert "critic" in payload["error"]
    assert "unexpected structured result" in payload["error"]


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ({}, None),
        ({"replay": ["manual"]}, {"mode": "manual", "speed": None}),
        ({"replay": ["invalid"]}, {"mode": "manual", "speed": None}),
        ({"replay": ["0.5"]}, {"mode": "auto", "speed": 0.5}),
        ({"replay": ["1"]}, {"mode": "auto", "speed": 1.0}),
        ({"replay": ["2"]}, {"mode": "auto", "speed": 2.0}),
        ({"replay": ["850"]}, {"mode": "auto", "speed": 1.0}),
        ({"replay": ["99999"]}, {"mode": "auto", "speed": 2.0}),
    ],
)
def test_replay_settings_are_bounded_and_finished_only(query, expected):
    assert viewer_server._replay_settings(query, run_finished=True) == expected
    assert viewer_server._replay_settings(query, run_finished=False) is None


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
    # Retired remote-renderer bundles cannot be served accidentally.
    status, _ = _get(port, "/static/simli-client.js")
    assert status == 404


def test_static_avatar_is_served_with_webp_content(viewer):
    port, _root = viewer
    status, body = _get(port, "/static/avatars/logician.webp")
    assert status == 200
    assert body.startswith(b"RIFF")

    status, body = _get(port, "/static/avatars/pool-050.webp")
    assert status == 200
    assert body.startswith(b"RIFF")


# ---------------------------------------------------------------------------
# Bearer-token gate
# ---------------------------------------------------------------------------


_TOKEN = "test-bearer-token-1234"


@pytest.fixture
def token_viewer(tmp_path, monkeypatch):
    """Same in-process server as `viewer`, minimal surface, token required."""
    monkeypatch.setattr(viewer_server, "_POLL_INTERVAL", 0.01)
    monkeypatch.setattr(viewer_server, "_POST_FINISH_GRACE", 0.02)
    _write_run(tmp_path, "run-a", [])
    handler = type(
        "_TokenHandler",
        (viewer_server._Handler,),
        {
            "runs_root": tmp_path,
            "workspace_root": None,
            "execution_manager": None,
            "tts_manager": None,
            "bind_host": "127.0.0.1",
            "launcher_mode": False,
            "auth_token": _TOKEN,
        },
    )
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
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


def test_token_gate_rejects_missing_and_wrong_credentials(token_viewer):
    port, _root = token_viewer
    # No credential at all.
    status, _ = _get(port, "/api/runs")
    assert status == 403
    status, body = _post_control(port, {"action": "switch_room", "room": "x"})
    assert status == 403
    # Wrong credential — header and query form.
    status, _ = _get(port, "/api/runs?token=wrong")
    assert status == 403
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        conn.request(
            "GET", "/api/runs", headers={"X-Symposium-Token": "wrong"}
        )
        assert conn.getresponse().status == 403
    finally:
        conn.close()


def test_token_gate_accepts_header_and_query_credential(token_viewer):
    port, _root = token_viewer
    # Query form (EventSource path).
    status, _ = _get(port, f"/api/runs?token={_TOKEN}")
    assert status == 200
    # Header form (fetch path).
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        conn.request(
            "GET", "/api/runs", headers={"X-Symposium-Token": _TOKEN}
        )
        resp = conn.getresponse()
        assert resp.status == 200
        assert b"run-a" in resp.read()
    finally:
        conn.close()


def test_post_control_requires_marker_even_with_valid_token(token_viewer):
    """The token augments the CSRF marker; it does not replace it."""
    port, _root = token_viewer
    status, body = _post_control(
        port,
        {"action": "switch_room", "room": "x"},
        marker=False,
        path=f"/api/control?token={_TOKEN}",
    )
    assert status == 403
    assert body["ok"] is False

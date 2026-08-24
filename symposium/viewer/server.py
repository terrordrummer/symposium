"""HTTP + SSE server for the live deliberation workspace.

No web-framework dependency (``http.server`` / ``socketserver``); binds to
loopback by default. The viewer exposes these deliberately small surfaces:

* ``GET /``                serve the single-page viewer (static HTML/JS/CSS)
* ``GET /api/runs``        JSON list of run dirs under ``runs_root`` (newest first)
* ``GET /api/workspace``   active 2.x room and membership projection, when present
* ``GET /api/executions``  recent room-run jobs owned by this viewer process
* ``GET /api/system``      launcher lifecycle capabilities
* ``GET /api/tts/status``  local speech-engine state
* ``GET /api/stream?run=...`` SSE: replay history, then tail it live
* ``POST /api/control``    same-origin Sartori room/agent mutations
* ``POST /api/tts/setup``  install the optional local speech engine
* ``POST /api/tts/synthesize`` synthesize one cached local WAV clip
* ``POST /api/system/shutdown`` stop a launcher-owned local server

The stream is a pure consumer of ``transcript.jsonl`` + ``config.json`` and
never writes to a run directory. The separate control endpoint mutates the
local 2.x workspace and may start a new immutable v1 run; it never rewrites an
existing run.
"""

from __future__ import annotations

import hmac
import json
import secrets
import threading
import time
import webbrowser
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable, Optional, Type, cast
from urllib.parse import parse_qs, urlparse

from symposium.avatars import avatar_asset_paths, avatar_for, avatar_for_agent
from symposium.control_plane.models import MembershipRole
from symposium.viewer.discovery import _is_stale_lock_safe, list_runs
from symposium.viewer.streamer import EdgeResolver, config_event, message_event
from symposium.viewer.tail import JournalTail

_STATIC_DIR = Path(__file__).resolve().parent / "static"
_STATIC_WHITELIST = {
    "index.html",
    "app.js",
    "style.css",
    *avatar_asset_paths(),
}

# SSE streaming.
_POLL_INTERVAL = 0.4          # seconds between journal drains while live
_HEARTBEAT_INTERVAL = 15.0    # seconds between keepalive comments
_POST_FINISH_GRACE = 1.0      # final drain window after the run looks done
_MAX_CONTROL_BODY_BYTES = 65_536

# Host-header gate: loopback names are always fine; anything else must match
# the explicitly configured bind host. Blocks DNS rebinding (a hostile page
# resolving its own domain to 127.0.0.1 to read transcripts cross-origin).
_LOOPBACK_HOSTNAMES = {"localhost", "127.0.0.1", "::1"}


def _is_loopback_host(host: str) -> bool:
    return _hostname_of(host) in _LOOPBACK_HOSTNAMES


def _auth_token_ok(supplied: Optional[str], expected: Optional[str]) -> bool:
    """Constant-time token comparison; a disabled gate always passes."""
    if expected is None:
        return True
    if supplied is None:
        return False
    return hmac.compare_digest(supplied.encode("utf-8"), expected.encode("utf-8"))


def _hostname_of(host_header: str) -> str:
    """Extract the bare lowercase hostname from a Host header value."""
    h = host_header.strip().lower()
    if h.startswith("["):
        # bracketed IPv6, optional port: [::1] or [::1]:8000
        return h[1:].split("]", 1)[0]
    if h.count(":") == 1:
        # host:port (a bare IPv6 address has more than one colon)
        return h.split(":", 1)[0]
    return h


def _run_finished(run_dir: Path) -> bool:
    """A run is done when the writer's lock is gone (or stale) — the artifact
    may or may not exist yet (terminations still write one)."""
    lock = run_dir / ".lock"
    if not lock.exists():
        return True
    return _is_stale_lock_safe(lock)


def _replay_settings(qs, *, run_finished: bool) -> Optional[dict]:
    """Return the safe client-side replay mode for a finished run.

    The browser owns presentation timing because it can observe when locally
    generated audio actually finishes and can support manual advancement.
    The server only marks the bounded mode in its SSE playback events.
    """
    vals = qs.get("replay") or []
    if not run_finished or not vals:
        return None
    raw = str(vals[0]).strip().lower()
    if raw in {"", "manual", "true", "yes"}:
        return {"mode": "manual", "speed": None}

    # Accept the three UI values plus the millisecond values emitted by the
    # earlier viewer, so a cached page cannot accidentally request 850x speed.
    legacy = {"1500": 0.5, "850": 1.0, "400": 2.0}
    try:
        speed = legacy[raw] if raw in legacy else float(raw)
    except ValueError:
        return {"mode": "manual", "speed": None}
    speed = max(0.5, min(2.0, speed))
    return {"mode": "auto", "speed": speed}


class _Handler(BaseHTTPRequestHandler):
    # set per-server (see serve())
    runs_root: Path = Path("runs")
    workspace_root: Optional[Path] = None
    execution_manager = None
    tts_manager = None
    launcher_mode: bool = False
    shutdown_callback: Optional[Callable[[], None]] = None
    bind_host: str = "127.0.0.1"
    # When set, every request must present the token via the
    # `X-Symposium-Token` header or the `?token=` query parameter (the
    # query form exists for EventSource, which cannot send headers).
    auth_token: Optional[str] = None

    # Quieter logging — one line per request is noise for a live viewer.
    def log_message(self, *args) -> None:  # noqa: D401
        pass

    def handle(self) -> None:
        """Treat client disconnects during request parsing as normal.

        Browsers routinely open speculative loopback connections and close
        them before sending a complete request.  That reset happens inside
        ``BaseHTTPRequestHandler.handle_one_request`` — before ``do_GET`` is
        reached — so the routing-level guard below cannot silence it.
        """
        try:
            super().handle()
        except (BrokenPipeError, ConnectionResetError):
            pass

    # ---- host + token gates ---------------------------------------------
    def _host_allowed(self) -> bool:
        hostname = _hostname_of(self.headers.get("Host") or "")
        if hostname in _LOOPBACK_HOSTNAMES:
            return True
        return hostname == _hostname_of(self.bind_host)

    def _token_allowed(self, qs: Optional[dict] = None) -> bool:
        """Require the shared token when one is configured.

        Accepts the `X-Symposium-Token` header or, for EventSource (which
        cannot set headers), the first `token=` query parameter. Compared
        in constant time.
        """
        if self.auth_token is None:
            return True
        supplied = self.headers.get("X-Symposium-Token")
        if not supplied and qs is not None:
            vals = qs.get("token") or []
            supplied = str(vals[0]) if vals else None
        return _auth_token_ok(supplied, self.auth_token)

    # ---- routing -------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802 (stdlib signature)
        parsed = urlparse(self.path)
        qs = parse_qs(parsed.query)
        if not self._host_allowed() or not self._token_allowed(qs):
            # 403 with no body: an unexpected Host (or a missing token) means
            # the request did not come through a name/credential we serve —
            # give it nothing to read.
            self.send_response(403)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        route = parsed.path
        try:
            if route in ("/", "/index.html"):
                self._serve_static("index.html")
            elif route.startswith("/static/"):
                self._serve_static(route[len("/static/"):])
            elif route == "/api/runs":
                self._serve_runs()
            elif route == "/api/workspace":
                self._serve_workspace()
            elif route == "/api/executions":
                self._serve_executions()
            elif route == "/api/system":
                self._serve_system()
            elif route == "/api/tts/status":
                self._serve_tts_status()
            elif route == "/api/stream":
                self._serve_stream(qs)
            else:
                self.send_error(404, "not found")
        except (BrokenPipeError, ConnectionResetError):
            pass  # client navigated away mid-stream — normal

    def do_POST(self) -> None:  # noqa: N802 (stdlib signature)
        parsed = urlparse(self.path)
        if (
            not self._host_allowed()
            or not self._token_allowed(parse_qs(parsed.query))
            or not self._control_request_allowed()
        ):
            self._send_json(403, {"ok": False, "error": "request not allowed"})
            return
        route = parsed.path
        try:
            if route == "/api/control":
                self._serve_control()
            elif route == "/api/tts/setup":
                self._serve_tts_setup()
            elif route == "/api/tts/synthesize":
                self._serve_tts_synthesize()
            elif route == "/api/system/shutdown":
                self._serve_shutdown()
            else:
                self._send_json(404, {"ok": False, "error": "not found"})
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _control_request_allowed(self) -> bool:
        """Block cross-site form/CSRF mutations against the loopback server."""
        if self.headers.get("X-Symposium-Request") != "1":
            return False
        origin = self.headers.get("Origin")
        if not origin:
            return True
        parsed = urlparse(origin)
        return parsed.scheme in {"http", "https"} and parsed.netloc.casefold() == (
            self.headers.get("Host") or ""
        ).casefold()

    def _send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    # ---- static --------------------------------------------------------
    def _serve_static(self, name: str) -> None:
        # Exact allow-listing (including relative subdirectories) makes the
        # generated portraits serveable without widening the static root.
        # Normalize separators so Windows-style traversal is rejected too.
        name = name.replace("\\", "/").lstrip("/")
        if name not in _STATIC_WHITELIST:
            self.send_error(404, "not found")
            return
        path = _STATIC_DIR / name
        if not path.exists():
            self.send_error(404, "not found")
            return
        body = path.read_bytes()
        ctype = {
            ".html": "text/html; charset=utf-8",
            ".js": "application/javascript; charset=utf-8",
            ".css": "text/css; charset=utf-8",
            ".webp": "image/webp",
        }.get(path.suffix, "application/octet-stream")
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    # ---- /api/runs -----------------------------------------------------
    def _serve_runs(self) -> None:
        # Run entries carry the directory NAME as their id — never the
        # absolute filesystem path. The frontend round-trips it opaquely
        # into ?run= and _resolve_run anchors it under runs_root.
        runs = []
        for r in list_runs(self.runs_root):
            entry = asdict(r)
            entry.pop("path", None)
            runs.append(entry)
        body = json.dumps({"runs": runs}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    # ---- /api/workspace ------------------------------------------------
    def _serve_workspace(self) -> None:
        """Expose a read-only public projection of the active product room."""
        payload: dict
        if self.workspace_root is None:
            payload = {"initialized": False}
        else:
            from symposium.control_plane import ControlPlane

            control = ControlPlane(self.workspace_root)
            try:
                control.ensure_initialized()
                payload = control.public_snapshot()
            except (OSError, ValueError):
                self.send_error(500, "invalid workspace state")
                return
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    # ---- /api/control --------------------------------------------------
    def _serve_control(self) -> None:
        if self.workspace_root is None:
            self._send_json(404, {"ok": False, "error": "workspace unavailable"})
            return
        try:
            length = int(self.headers.get("Content-Length") or "0")
        except ValueError:
            length = 0
        if length <= 0 or length > _MAX_CONTROL_BODY_BYTES:
            self._send_json(413, {"ok": False, "error": "invalid request size"})
            return
        try:
            payload = json.loads(self.rfile.read(length))
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._send_json(400, {"ok": False, "error": "invalid JSON"})
            return
        if not isinstance(payload, dict):
            self._send_json(400, {"ok": False, "error": "JSON object required"})
            return

        from symposium.control_plane import ControlPlane, execute_sartori_command

        control = ControlPlane(self.workspace_root)
        try:
            control.ensure_initialized()
            action = payload.get("action")
            response_extra: dict = {}
            if action == "command":
                message = execute_sartori_command(control, str(payload.get("command") or ""))
            elif action == "create_room":
                room = control.create_room(
                    str(payload.get("name") or ""),
                    str(payload.get("purpose") or ""),
                )
                if payload.get("activate", True):
                    control.switch_room(room.id)
                message = f"Ho creato e aperto la stanza {room.name}."
            elif action == "switch_room":
                room = control.switch_room(str(payload.get("room") or ""))
                message = f"Siamo entrati nella stanza {room.name}."
            elif action == "archive_room":
                room = control.archive_room(str(payload.get("room") or ""))
                message = f"Ho archiviato la stanza {room.name}."
            elif action == "create_agent":
                capabilities = payload.get("capabilities") or []
                if not isinstance(capabilities, list):
                    raise ValueError("capabilities must be an array")
                agent = control.create_agent(
                    str(payload.get("agent_id") or ""),
                    str(payload.get("display_name") or ""),
                    str(payload.get("instructions") or ""),
                    capabilities=[str(item) for item in capabilities],
                    avatar_id=(
                        str(payload["avatar_id"])
                        if payload.get("avatar_id") else None
                    ),
                )
                message = f"Ho creato l'agente {agent.display_name}."
            elif action == "invite_agent":
                # Pydantic re-validates the role against the closed enum;
                # the cast only satisfies the static boundary here.
                raw_role = str(payload.get("role") or "guest")
                membership = control.invite_agent(
                    str(payload.get("agent") or ""),
                    room=str(payload["room"]) if payload.get("room") else None,
                    role=cast(MembershipRole, raw_role),
                    onboarding_context=(
                        str(payload["onboarding_context"])
                        if payload.get("onboarding_context") else None
                    ),
                )
                agent = control.snapshot().agents[membership.agent_id]
                message = f"Ho invitato {agent.display_name} nella stanza."
            elif action == "dismiss_agent":
                membership = control.dismiss_agent(
                    str(payload.get("agent") or ""),
                    room=str(payload["room"]) if payload.get("room") else None,
                )
                agent = control.snapshot().agents[membership.agent_id]
                message = f"Ho congedato {agent.display_name}."
            elif action == "start_session":
                if self.execution_manager is None:
                    self._send_json(
                        503,
                        {"ok": False, "error": "room execution is unavailable"},
                    )
                    return
                job = self.execution_manager.start(
                    str(payload.get("problem") or ""),
                    room=str(payload["room"]) if payload.get("room") else None,
                )
                message = (
                    f"Ho avviato la discussione nella stanza {job.room_name}."
                )
                response_extra["job"] = job.public_payload()
            else:
                raise ValueError(f"unknown control action {action!r}")
            self._send_json(200, {
                "ok": True,
                "message": message,
                "snapshot": control.public_snapshot(),
                **response_extra,
            })
        except (RuntimeError, ValueError) as exc:
            self._send_json(400, {"ok": False, "error": str(exc)})

    # ---- /api/executions ----------------------------------------------
    def _serve_executions(self) -> None:
        jobs: list = [] if self.execution_manager is None else self.execution_manager.public_jobs()
        self._send_json(200, {"jobs": jobs})

    # ---- /api/system ---------------------------------------------------
    def _serve_system(self) -> None:
        self._send_json(200, {
            "name": "Symposium",
            "launcher_mode": self.launcher_mode,
            "can_shutdown": self.launcher_mode and self.shutdown_callback is not None,
        })

    # ---- /api/tts ------------------------------------------------------
    def _serve_tts_status(self) -> None:
        if self.tts_manager is None:
            self._send_json(200, {
                "state": "unavailable",
                "message": "la voce locale richiede un workspace Symposium",
                "local_only": True,
                "api_key_required": False,
            })
            return
        self._send_json(200, self.tts_manager.public_status())

    def _serve_tts_setup(self) -> None:
        if self.tts_manager is None:
            self._send_json(404, {"ok": False, "error": "voce locale non disponibile"})
            return
        self._send_json(202, {"ok": True, **self.tts_manager.install()})

    def _serve_tts_synthesize(self) -> None:
        if self.tts_manager is None or self.workspace_root is None:
            self._send_json(404, {"ok": False, "error": "voce locale non disponibile"})
            return
        try:
            length = int(self.headers.get("Content-Length") or "0")
        except ValueError:
            length = 0
        if length <= 0 or length > _MAX_CONTROL_BODY_BYTES:
            self._send_json(413, {"ok": False, "error": "invalid request size"})
            return
        try:
            payload = json.loads(self.rfile.read(length))
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._send_json(400, {"ok": False, "error": "invalid JSON"})
            return
        if not isinstance(payload, dict):
            self._send_json(400, {"ok": False, "error": "JSON object required"})
            return
        agent_id = str(payload.get("agent_id") or "").strip()
        text = str(payload.get("text") or "")

        from symposium.control_plane import ControlPlane
        from symposium.tts import LocalTTSUnavailable

        control = ControlPlane(self.workspace_root)
        state = control.ensure_initialized()
        agent = state.agents.get(agent_id)
        profile = (
            avatar_for_agent(agent.id, agent.display_name, agent.avatar_id)
            if agent is not None else avatar_for(agent_id)
        )
        if profile.voice_description is None:
            self._send_json(400, {
                "ok": False,
                "error": f"nessun profilo vocale registrato per {agent_id!r}",
            })
            return
        try:
            audio = self.tts_manager.synthesize(text, profile.voice_description)
        except (LocalTTSUnavailable, OSError, ValueError) as exc:
            self._send_json(503, {"ok": False, "error": str(exc)})
            return
        body = audio.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "audio/wav")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "private, max-age=31536000, immutable")
        self.end_headers()
        self.wfile.write(body)

    def _serve_shutdown(self) -> None:
        if not self.launcher_mode or self.shutdown_callback is None:
            self._send_json(403, {"ok": False, "error": "launcher shutdown unavailable"})
            return
        if self.execution_manager is not None and self.execution_manager.has_active_job():
            self._send_json(409, {
                "ok": False,
                "error": "attendi la fine della discussione prima di chiudere Symposium",
            })
            return
        self._send_json(200, {"ok": True, "message": "Symposium si sta chiudendo."})
        callback = self.shutdown_callback

        def _delayed_shutdown() -> None:
            time.sleep(0.1)
            callback()

        threading.Thread(
            target=_delayed_shutdown,
            name="symposium-shutdown",
            daemon=True,
        ).start()

    # ---- /api/stream ---------------------------------------------------
    def _resolve_run(self, qs) -> Optional[Path]:
        """Resolve the `run` query param to a dir confined under runs_root."""
        vals = qs.get("run") or []
        if not vals:
            return None
        candidate = Path(vals[0])
        if not candidate.is_absolute():
            candidate = self.runs_root / candidate
        try:
            resolved = candidate.resolve()
            root = self.runs_root.resolve()
        except OSError:
            return None
        # Confinement: the run dir must live under runs_root.
        if resolved != root and root not in resolved.parents:
            return None
        return resolved if resolved.is_dir() else None

    def _sse_write(self, event: str, data: dict) -> None:
        chunk = f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
        self.wfile.write(chunk.encode("utf-8"))
        self.wfile.flush()

    def _serve_stream(self, qs) -> None:
        run_dir = self._resolve_run(qs)
        if run_dir is None:
            self.send_error(404, "run not found or outside runs_root")
            return

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()

        finished_at_connect = _run_finished(run_dir)
        replay = _replay_settings(
            qs, run_finished=finished_at_connect
        )

        # config first (drives the participant grid)
        self._sse_write("config", config_event(run_dir))
        if replay:
            self._sse_write(
                "playback",
                {"active": True, **replay},
            )

        resolver = EdgeResolver()
        tail = JournalTail(run_dir / "transcript.jsonl")
        index = 0
        last_heartbeat = time.monotonic()

        def emit_drained() -> None:
            nonlocal index
            messages = tail.drain()
            for msg in messages:
                resolver.register(msg)
                self._sse_write("message", message_event(index, msg, resolver))
                index += 1

        # 1) history replay (everything already on disk)
        emit_drained()
        if replay:
            self._sse_write(
                "playback",
                {"active": False, **replay},
            )
        self._sse_write("status", self._status(run_dir, index))

        # 2) live tail until the run finishes, then one final drain
        finishing = False
        finish_deadline = 0.0
        while True:
            emit_drained()
            now = time.monotonic()
            if now - last_heartbeat >= _HEARTBEAT_INTERVAL:
                # SSE comment line — keeps proxies/clients from idling out.
                self.wfile.write(b": keepalive\n\n")
                self.wfile.flush()
                self._sse_write("status", self._status(run_dir, index))
                last_heartbeat = now

            if not finishing and _run_finished(run_dir):
                # Grace window: the scheduler may flush a last line right as
                # it releases the lock. Drain once more, then end.
                finishing = True
                finish_deadline = now + _POST_FINISH_GRACE
            if finishing and now >= finish_deadline:
                emit_drained()
                self._sse_write("status", self._status(run_dir, index))
                self._sse_write("end", {"finished": True, **self._outcome(run_dir)})
                return

            time.sleep(_POLL_INTERVAL)

    def _status(self, run_dir: Path, total: int) -> dict:
        lock = run_dir / ".lock"
        present = lock.exists()
        stale = present and _is_stale_lock_safe(lock)
        return {
            "run_active": present and not stale,
            "lock_stale": stale,
            "total": total,
        }

    def _outcome(self, run_dir: Path) -> dict:
        artifact = run_dir / "artifact.json"
        if not artifact.exists():
            return {"outcome": None}
        try:
            data = json.loads(artifact.read_text(encoding="utf-8"))
            outcome = data.get("outcome") or {}
            payload = {"outcome": outcome.get("kind")}
            if outcome.get("kind") == "termination":
                termination = outcome.get("termination_artifact") or {}
                payload["termination_reason"] = termination.get("reason")
                failure = termination.get("last_provider_failure")
                if isinstance(failure, dict):
                    payload["error"] = (
                        f"{failure.get('agent_id') or 'Un agente'} non ha potuto "
                        f"completare l'intervento tramite "
                        f"{failure.get('provider') or 'il provider'}: "
                        f"{failure.get('message') or 'errore non specificato'}"
                    )[:2000]
            return payload
        except (OSError, json.JSONDecodeError):
            return {"outcome": None}


def serve(
    runs_root: Path,
    *,
    workspace_root: Optional[Path] = None,
    host: str = "127.0.0.1",
    port: int = 0,
    open_browser: bool = True,
    run: Optional[str] = None,
    launcher_mode: bool = False,
    ready_callback: Optional[Callable[[str], None]] = None,
    token: Optional[str] = None,
) -> None:
    """Start the viewer server (blocking) and optionally open a browser.

    ``port=0`` lets the OS pick a free port (printed on startup). ``run``
    pre-selects a specific run dir in the UI; otherwise the page follows
    the newest run under ``runs_root``.

    ``token`` turns on bearer authentication: every request must present
    it (header or ``?token=`` query parameter, which EventSource needs).
    When omitted on a NON-loopback bind a random token is generated — an
    unauthenticated network-exposed viewer would otherwise let any peer
    read transcripts and mutate the workspace. The generated value is
    printed once and embedded in the served URL.
    """
    runs_root = Path(runs_root)

    if token is None and not _is_loopback_host(host):
        # Never expose the API unauthenticated beyond loopback.
        token = secrets.token_urlsafe(16)

    execution_manager = None
    tts_manager = None
    if workspace_root is not None:
        from symposium.control_plane import ControlPlane, RoomExecutionManager
        from symposium.tts import LocalTTSManager

        execution_manager = RoomExecutionManager(
            ControlPlane(Path(workspace_root)),
            runs_root,
        )
        tts_manager = LocalTTSManager(Path(workspace_root))

    handler_cls: Type[_Handler] = type(
        "_BoundHandler",
        (_Handler,),
        {
            "runs_root": runs_root,
            "workspace_root": workspace_root,
            "execution_manager": execution_manager,
            "tts_manager": tts_manager,
            "bind_host": host,
            "launcher_mode": launcher_mode,
            "auth_token": token,
        },
    )
    httpd = ThreadingHTTPServer((host, port), handler_cls)
    if launcher_mode:
        handler_cls.shutdown_callback = staticmethod(httpd.shutdown)
    actual_port = httpd.server_address[1]
    url = f"http://{host}:{actual_port}/"
    if run:
        # The page reads ?run= and pins it (skips newest-follow). Pin by
        # directory name — _resolve_run anchors names under runs_root.
        from urllib.parse import quote

        url += f"?run={quote(Path(run).name)}"
    if token is not None:
        # Embed the credential so the browser flow (page + EventSource +
        # POST fetches) works without manual steps; the page relays it to
        # every call from its own location.search.
        url += f"{'&' if '?' in url else '?'}token={token}"

    print(f"symposium watch — serving {runs_root.resolve()}")
    print(f"  open: {url}")
    if token is not None:
        print("  auth: bearer token ACTIVE (requests need X-Symposium-Token or ?token=)")
        if open_browser is False:
            print(f"  token: {token}")
    print("  (Ctrl-C to stop)")

    try:
        if ready_callback is not None:
            ready_callback(url)

        def _open_browser() -> None:
            time.sleep(0.4)
            webbrowser.open(url)

        if open_browser:
            threading.Thread(
                target=_open_browser,
                name="symposium-browser-open",
                daemon=True,
            ).start()
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nsymposium watch — stopped")
    finally:
        if tts_manager is not None:
            tts_manager.close()
        httpd.shutdown()
        httpd.server_close()

"""Read-only HTTP + SSE server for the live deliberation viewer.

Stdlib only (``http.server`` / ``socketserver``) — no third-party web
dependency, consistent with the project's HTTP-only core. Binds to
loopback by default. Three concerns:

* ``GET /``                serve the single-page viewer (static HTML/JS/CSS)
* ``GET /api/runs``        JSON list of run dirs under ``runs_root`` (newest first)
* ``GET /api/stream?run="" SSE: replay the run's history, then tail it live

The stream is a pure consumer of ``transcript.jsonl`` + ``config.json``;
it holds the file only for short reads and never writes to the run dir.
"""

from __future__ import annotations

import json
import os
import threading
import time
import webbrowser
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs, urlparse

from symposium.viewer.discovery import _is_stale_lock_safe, list_runs
from symposium.viewer.streamer import EdgeResolver, config_event, message_event
from symposium.viewer.tail import JournalTail

_STATIC_DIR = Path(__file__).resolve().parent / "static"
_STATIC_WHITELIST = {"index.html", "app.js", "style.css"}

# SSE pacing.
_POLL_INTERVAL = 0.4          # seconds between journal drains while live
_HEARTBEAT_INTERVAL = 15.0    # seconds between keepalive comments
_POST_FINISH_GRACE = 1.0      # final drain window after the run looks done

# Host-header gate: loopback names are always fine; anything else must match
# the explicitly configured bind host. Blocks DNS rebinding (a hostile page
# resolving its own domain to 127.0.0.1 to read transcripts cross-origin).
_LOOPBACK_HOSTNAMES = {"localhost", "127.0.0.1", "::1"}


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


class _Handler(BaseHTTPRequestHandler):
    # set per-server (see serve())
    runs_root: Path = Path("runs")
    bind_host: str = "127.0.0.1"

    # Quieter logging — one line per request is noise for a live viewer.
    def log_message(self, *args) -> None:  # noqa: D401
        pass

    # ---- host gate -----------------------------------------------------
    def _host_allowed(self) -> bool:
        hostname = _hostname_of(self.headers.get("Host") or "")
        if hostname in _LOOPBACK_HOSTNAMES:
            return True
        return hostname == _hostname_of(self.bind_host)

    # ---- routing -------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802 (stdlib signature)
        if not self._host_allowed():
            # 403 with no body: an unexpected Host means the request did not
            # come through a name we serve — give it nothing to read.
            self.send_response(403)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        parsed = urlparse(self.path)
        route = parsed.path
        try:
            if route in ("/", "/index.html"):
                self._serve_static("index.html")
            elif route.startswith("/static/"):
                self._serve_static(route[len("/static/"):])
            elif route == "/api/runs":
                self._serve_runs()
            elif route == "/api/stream":
                self._serve_stream(parse_qs(parsed.query))
            else:
                self.send_error(404, "not found")
        except (BrokenPipeError, ConnectionResetError):
            pass  # client navigated away mid-stream — normal

    # ---- static --------------------------------------------------------
    def _serve_static(self, name: str) -> None:
        name = os.path.basename(name)
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

        # config first (drives the circle layout)
        self._sse_write("config", config_event(run_dir))

        resolver = EdgeResolver()
        tail = JournalTail(run_dir / "transcript.jsonl")
        index = 0
        last_heartbeat = time.monotonic()

        def emit_drained() -> None:
            nonlocal index
            for msg in tail.drain():
                resolver.register(msg)
                self._sse_write("message", message_event(index, msg, resolver))
                index += 1

        # 1) history replay (everything already on disk)
        emit_drained()
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
            return {"outcome": (data.get("outcome") or {}).get("kind")}
        except (OSError, json.JSONDecodeError):
            return {"outcome": None}


def serve(
    runs_root: Path,
    *,
    host: str = "127.0.0.1",
    port: int = 0,
    open_browser: bool = True,
    run: Optional[str] = None,
) -> None:
    """Start the viewer server (blocking) and optionally open a browser.

    ``port=0`` lets the OS pick a free port (printed on startup). ``run``
    pre-selects a specific run dir in the UI; otherwise the page follows
    the newest run under ``runs_root``.
    """
    runs_root = Path(runs_root)

    handler = type(
        "_BoundHandler", (_Handler,), {"runs_root": runs_root, "bind_host": host}
    )
    httpd = ThreadingHTTPServer((host, port), handler)
    actual_port = httpd.server_address[1]
    url = f"http://{host}:{actual_port}/"
    if run:
        # The page reads ?run= and pins it (skips newest-follow). Pin by
        # directory name — _resolve_run anchors names under runs_root.
        from urllib.parse import quote

        url += f"?run={quote(Path(run).name)}"

    print(f"symposium watch — serving {runs_root.resolve()}")
    print(f"  open: {url}")
    print("  (Ctrl-C to stop)")

    if open_browser:
        threading.Thread(target=lambda: (time.sleep(0.4), webbrowser.open(url)), daemon=True).start()

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nsymposium watch — stopped")
    finally:
        httpd.shutdown()
        httpd.server_close()

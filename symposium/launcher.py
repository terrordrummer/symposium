"""Terminal-free local launcher for the Symposium browser workspace.

The generated macOS ``.app`` is intentionally tiny: it starts this module
with the Python environment that installed Symposium.  This process owns the
loopback viewer server, opens the browser, and exits when the user chooses
``Chiudi Symposium`` in the web interface.
"""

from __future__ import annotations

import argparse
import json
import os
import plistlib
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import webbrowser
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import urlparse

_LOCK_FILE = ".launcher.lock"
_STATE_FILE = "launcher.json"


def _pid_is_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _read_pid(path: Path) -> Optional[int]:
    try:
        return int(path.read_text(encoding="utf-8").strip().split()[0])
    except (FileNotFoundError, IndexError, OSError, ValueError):
        return None


class LauncherLock:
    """PID lock held for the lifetime of one launcher-owned server."""

    def __init__(self, state_dir: Path) -> None:
        self.state_dir = Path(state_dir)
        self.path = self.state_dir / _LOCK_FILE
        self.owned = False

    def acquire(self) -> bool:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        for _ in range(2):
            try:
                fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                observed = _read_pid(self.path)
                if observed is not None and _pid_is_alive(observed):
                    return False
                if _read_pid(self.path) != observed:
                    continue
                try:
                    self.path.unlink()
                except FileNotFoundError:
                    pass
                continue
            with os.fdopen(fd, "w", encoding="ascii") as handle:
                handle.write(f"{os.getpid()}\n")
                handle.flush()
                os.fsync(handle.fileno())
            self.owned = True
            return True
        return False

    def release(self) -> None:
        if not self.owned:
            return
        if _read_pid(self.path) == os.getpid():
            try:
                self.path.unlink()
            except FileNotFoundError:
                pass
        self.owned = False


def _atomic_write(path: Path, payload: bytes, *, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}-", dir=path.parent)
    temporary_path = Path(temporary)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary_path, mode)
        os.replace(temporary_path, path)
    finally:
        try:
            temporary_path.unlink()
        except FileNotFoundError:
            pass


def _write_launcher_state(path: Path, url: str) -> None:
    payload = json.dumps(
        {"pid": os.getpid(), "url": url, "started_at": time.time()},
        indent=2,
        sort_keys=True,
    ).encode("utf-8") + b"\n"
    _atomic_write(path, payload)


def _loopback_url(value: object) -> Optional[str]:
    if not isinstance(value, str):
        return None
    parsed = urlparse(value)
    if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        return None
    return value


def _existing_url(state_path: Path, *, timeout: float = 3.0) -> Optional[str]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            payload = json.loads(state_path.read_text(encoding="utf-8"))
            url = _loopback_url(payload.get("url"))
            if url:
                return url
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            pass
        time.sleep(0.1)
    return None


def _show_macos_error(message: str) -> None:
    """Show launch failures without requiring a terminal window."""
    print(f"Symposium launcher: {message}", file=sys.stderr)
    if sys.platform != "darwin":
        return
    escaped = message.replace("\\", "\\\\").replace('"', '\\"')
    try:
        subprocess.run(
            [
                "/usr/bin/osascript",
                "-e",
                f'display alert "Symposium" message "{escaped}" as critical',
            ],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError:
        pass


def launch(
    project_root: Path,
    *,
    port: int = 0,
    opener: Callable[[str], object] = webbrowser.open,
    error_reporter: Callable[[str], None] = _show_macos_error,
    serve_fn=None,
) -> int:
    """Start the local app or reopen the existing singleton in a browser."""
    root = Path(project_root).expanduser().resolve()
    state_dir = root / ".symposium"
    state_path = state_dir / _STATE_FILE
    lock = LauncherLock(state_dir)
    if not lock.acquire():
        url = _existing_url(state_path)
        if url is None:
            error_reporter("Symposium risulta in avvio, ma il suo indirizzo non è disponibile.")
            return 1
        opener(url)
        return 0

    if serve_fn is None:
        from symposium.viewer.server import serve as serve_fn

    previous_cwd = Path.cwd()
    try:
        os.chdir(root)

        def ready(url: str) -> None:
            _write_launcher_state(state_path, url)

        serve_fn(
            root / "runs",
            workspace_root=state_dir,
            host="127.0.0.1",
            port=port,
            open_browser=True,
            launcher_mode=True,
            ready_callback=ready,
        )
        return 0
    except Exception as exc:  # noqa: BLE001 — the .app must surface every startup failure
        error_reporter(f"Avvio non riuscito: {type(exc).__name__}: {exc}")
        return 1
    finally:
        os.chdir(previous_cwd)
        try:
            state_path.unlink()
        except FileNotFoundError:
            pass
        lock.release()


def install_macos_app(
    destination: Path,
    *,
    project_root: Path,
    python_executable: Path = Path(sys.executable),
    path_environment: Optional[str] = None,
) -> Path:
    """Create/update a double-clickable, terminal-free macOS app bundle."""
    app = Path(destination).expanduser().resolve()
    if app.suffix != ".app":
        raise ValueError("launcher destination must end in .app")
    root = Path(project_root).expanduser().resolve()
    python = Path(python_executable).expanduser().resolve()

    contents = app / "Contents"
    executable = contents / "MacOS" / "Symposium"
    has_icon = _install_macos_icon(contents)
    info = {
        "CFBundleDevelopmentRegion": "it",
        "CFBundleDisplayName": "Symposium",
        "CFBundleExecutable": "Symposium",
        "CFBundleIdentifier": "org.symposium.local-launcher",
        "CFBundleInfoDictionaryVersion": "6.0",
        "CFBundleName": "Symposium",
        "CFBundlePackageType": "APPL",
        "CFBundleShortVersionString": "2.0",
        "CFBundleVersion": "1",
        "LSMinimumSystemVersion": "12.0",
        "LSUIElement": True,
        "NSHighResolutionCapable": True,
    }
    if has_icon:
        info["CFBundleIconFile"] = "Symposium.icns"
    _atomic_write(contents / "Info.plist", plistlib.dumps(info))
    _atomic_write(contents / "PkgInfo", b"APPL????", mode=0o644)

    current_path = path_environment if path_environment is not None else os.environ.get("PATH", "")
    path_entries = [
        str(Path.home() / ".local" / "bin"),
        "/opt/homebrew/bin",
        "/usr/local/bin",
        *current_path.split(os.pathsep),
        "/usr/bin",
        "/bin",
    ]
    launcher_path = os.pathsep.join(dict.fromkeys(entry for entry in path_entries if entry))
    resources = contents / "Resources"
    resources.mkdir(parents=True, exist_ok=True)
    swift_source = _native_launcher_source(root, python, launcher_path)
    swift_path = resources / "Launcher.swift"
    _atomic_write(swift_path, swift_source.encode("utf-8"), mode=0o644)

    if not _compile_native_macos_launcher(swift_path, executable):
        # Portable development fallback. Distributed macOS launchers use the
        # native AppKit supervisor above; this keeps bundle generation usable
        # on hosts without Xcode while retaining terminal-free startup.
        bootstrap = (
            "#!/bin/zsh\n"
            f"export PATH={shlex.quote(launcher_path)}\n"
            f"cd {shlex.quote(str(root))}\n"
            f"exec {shlex.quote(str(python))} -m symposium.launcher "
            f"--project-root {shlex.quote(str(root))}\n"
        ).encode("utf-8")
        _atomic_write(executable, bootstrap, mode=0o755)

    if sys.platform == "darwin" and shutil.which("codesign"):
        subprocess.run(
            ["codesign", "--force", "--deep", "--sign", "-", str(app)],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    return app


def _swift_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _native_launcher_source(root: Path, python: Path, launcher_path: str) -> str:
    """Return a tiny AppKit supervisor with native macOS reopen semantics."""
    project = _swift_string(str(root))
    interpreter = _swift_string(str(python))
    path_value = _swift_string(launcher_path)
    return f'''import AppKit
import Foundation

let projectRoot = {project}
let pythonExecutable = {interpreter}
let launcherPath = {path_value}

func openExistingWorkspace() {{
    let stateURL = URL(fileURLWithPath: projectRoot)
        .appendingPathComponent(".symposium/launcher.json")
    guard let data = try? Data(contentsOf: stateURL),
          let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
          let raw = object["url"] as? String,
          let url = URL(string: raw),
          url.scheme == "http",
          ["127.0.0.1", "localhost", "::1"].contains(url.host ?? "") else {{
        return
    }}
    NSWorkspace.shared.open(url)
}}

final class AppDelegate: NSObject, NSApplicationDelegate {{
    private var backend: Process?
    private var logHandle: FileHandle?

    func applicationDidFinishLaunching(_ notification: Notification) {{
        let stateDirectory = URL(fileURLWithPath: projectRoot)
            .appendingPathComponent(".symposium")
        try? FileManager.default.createDirectory(
            at: stateDirectory, withIntermediateDirectories: true
        )
        let logURL = stateDirectory.appendingPathComponent("launcher.log")
        if !FileManager.default.fileExists(atPath: logURL.path) {{
            FileManager.default.createFile(atPath: logURL.path, contents: nil)
        }}
        let handle = try? FileHandle(forWritingTo: logURL)
        try? handle?.seekToEnd()
        logHandle = handle

        let process = Process()
        process.executableURL = URL(fileURLWithPath: pythonExecutable)
        process.arguments = [
            "-m", "symposium.launcher", "--project-root", projectRoot
        ]
        process.currentDirectoryURL = URL(fileURLWithPath: projectRoot)
        var environment = ProcessInfo.processInfo.environment
        environment["PATH"] = launcherPath
        process.environment = environment
        process.standardInput = FileHandle.nullDevice
        process.standardOutput = handle ?? FileHandle.nullDevice
        process.standardError = handle ?? FileHandle.nullDevice
        process.terminationHandler = {{ _ in
            DispatchQueue.main.async {{ NSApp.terminate(nil) }}
        }}
        backend = process

        do {{
            try process.run()
        }} catch {{
            let alert = NSAlert()
            alert.alertStyle = .critical
            alert.messageText = "Symposium"
            alert.informativeText = "Avvio non riuscito: \\(error.localizedDescription)"
            alert.runModal()
            NSApp.terminate(nil)
        }}
    }}

    func applicationShouldHandleReopen(
        _ sender: NSApplication, hasVisibleWindows flag: Bool
    ) -> Bool {{
        openExistingWorkspace()
        return true
    }}

    func applicationWillTerminate(_ notification: Notification) {{
        if let process = backend, process.isRunning {{
            process.terminate()
        }}
        try? logHandle?.close()
    }}
}}

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.setActivationPolicy(.accessory)
app.run()
'''


def _compile_native_macos_launcher(source: Path, executable: Path) -> bool:
    if sys.platform != "darwin":
        return False
    swiftc = shutil.which("swiftc")
    if not swiftc:
        try:
            located = subprocess.run(
                ["xcrun", "--find", "swiftc"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            swiftc = located or None
        except (OSError, subprocess.CalledProcessError):
            return False
    if not swiftc:
        return False
    executable.parent.mkdir(parents=True, exist_ok=True)
    try:
        subprocess.run(
            [swiftc, str(source), "-framework", "AppKit", "-o", str(executable)],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        os.chmod(executable, 0o755)
        return True
    except (OSError, subprocess.CalledProcessError):
        return False


def _install_macos_icon(contents: Path) -> bool:
    """Convert the packaged static Sartori portrait into a local app icon."""
    if sys.platform != "darwin" or not shutil.which("sips") or not shutil.which("iconutil"):
        return False
    source = Path(__file__).parent / "viewer" / "static" / "avatars" / "coordinator.webp"
    if not source.is_file():
        return False
    resources = contents / "Resources"
    resources.mkdir(parents=True, exist_ok=True)
    sizes = {
        "icon_16x16.png": 16,
        "icon_16x16@2x.png": 32,
        "icon_32x32.png": 32,
        "icon_32x32@2x.png": 64,
        "icon_128x128.png": 128,
        "icon_128x128@2x.png": 256,
        "icon_256x256.png": 256,
        "icon_256x256@2x.png": 512,
        "icon_512x512.png": 512,
        "icon_512x512@2x.png": 1024,
    }
    try:
        with tempfile.TemporaryDirectory(prefix="symposium-icon-") as temporary:
            iconset = Path(temporary) / "Symposium.iconset"
            iconset.mkdir()
            for filename, size in sizes.items():
                subprocess.run(
                    [
                        "sips", "-s", "format", "png",
                        "-z", str(size), str(size), str(source),
                        "--out", str(iconset / filename),
                    ],
                    check=True,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            subprocess.run(
                [
                    "iconutil", "-c", "icns", str(iconset),
                    "-o", str(resources / "Symposium.icns"),
                ],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        return True
    except (OSError, subprocess.CalledProcessError):
        return False


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Launch the local Symposium workspace")
    parser.add_argument("--project-root", type=Path, default=Path.cwd())
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument(
        "--install-app",
        action="store_true",
        help="Create Symposium.app instead of starting the server",
    )
    parser.add_argument("--destination", type=Path, default=None)
    args = parser.parse_args(argv)
    if args.install_app:
        destination = args.destination or (args.project_root / "Symposium.app")
        app = install_macos_app(destination, project_root=args.project_root)
        print(app)
        return 0
    return launch(args.project_root, port=args.port)


if __name__ == "__main__":
    raise SystemExit(main())

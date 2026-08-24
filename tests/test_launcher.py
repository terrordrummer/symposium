"""Terminal-free macOS launcher and singleton lifecycle tests."""

from __future__ import annotations

import json
import os
import plistlib

from symposium.launcher import LauncherLock, install_macos_app, launch


def test_macos_app_bundle_is_double_clickable_and_terminal_free(tmp_path):
    project = tmp_path / "My Symposium Project"
    project.mkdir()
    python = tmp_path / "Python Runtime" / "python3"
    python.parent.mkdir()
    python.write_text("", encoding="utf-8")

    app = install_macos_app(
        tmp_path / "Symposium.app",
        project_root=project,
        python_executable=python,
        path_environment="/custom/claude:/custom/codex",
    )

    info = plistlib.loads((app / "Contents" / "Info.plist").read_bytes())
    assert info["CFBundlePackageType"] == "APPL"
    assert info["CFBundleExecutable"] == "Symposium"
    assert info["LSUIElement"] is True
    if "CFBundleIconFile" in info:
        assert (app / "Contents" / "Resources" / info["CFBundleIconFile"]).is_file()

    executable = app / "Contents" / "MacOS" / "Symposium"
    assert os.access(executable, os.X_OK)
    source = (app / "Contents" / "Resources" / "Launcher.swift").read_text(
        encoding="utf-8"
    )
    assert str(project) in source
    assert str(python) in source
    assert '"-m", "symposium.launcher"' in source
    assert "/custom/claude" in source
    assert "applicationShouldHandleReopen" in source
    assert "Terminal.app" not in source


def test_launcher_owns_state_for_server_lifetime_and_cleans_up(tmp_path):
    observed = {}

    def fake_serve(runs_root, **kwargs):
        observed["runs_root"] = runs_root
        observed.update(kwargs)
        kwargs["ready_callback"]("http://127.0.0.1:61234/")
        state = json.loads(
            (tmp_path / ".symposium" / "launcher.json").read_text(encoding="utf-8")
        )
        assert state["pid"] == os.getpid()
        assert state["url"] == "http://127.0.0.1:61234/"

    result = launch(tmp_path, serve_fn=fake_serve, opener=lambda _url: None)

    assert result == 0
    assert observed["runs_root"] == tmp_path / "runs"
    assert observed["workspace_root"] == tmp_path / ".symposium"
    assert observed["launcher_mode"] is True
    assert observed["open_browser"] is True
    assert not (tmp_path / ".symposium" / "launcher.json").exists()
    assert not (tmp_path / ".symposium" / ".launcher.lock").exists()


def test_second_launcher_reopens_the_existing_workspace(tmp_path):
    state_dir = tmp_path / ".symposium"
    state_dir.mkdir()
    (state_dir / ".launcher.lock").write_text(f"{os.getpid()}\n", encoding="utf-8")
    (state_dir / "launcher.json").write_text(
        json.dumps({"pid": os.getpid(), "url": "http://127.0.0.1:60001/"}),
        encoding="utf-8",
    )
    opened = []

    result = launch(
        tmp_path,
        opener=opened.append,
        serve_fn=lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("a second server must not start")
        ),
    )

    assert result == 0
    assert opened == ["http://127.0.0.1:60001/"]


def test_launcher_lock_recovers_a_stale_pid(tmp_path):
    state_dir = tmp_path / ".symposium"
    state_dir.mkdir()
    lock_path = state_dir / ".launcher.lock"
    lock_path.write_text("999999999\n", encoding="utf-8")

    lock = LauncherLock(state_dir)
    assert lock.acquire() is True
    assert lock_path.read_text(encoding="utf-8").strip() == str(os.getpid())
    lock.release()
    assert not lock_path.exists()

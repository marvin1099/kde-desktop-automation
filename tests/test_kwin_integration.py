import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from kde_interaction.backends.kwin_virtual import KWinVirtualBackend
from kde_interaction.paths import RuntimePaths
from kde_interaction.process import process_start_ticks, processes_with_environment_marker
from kde_interaction.sessions import SessionManager

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("KDE_AI_RUN_KWIN_INTEGRATION") != "1",
        reason="set KDE_AI_RUN_KWIN_INTEGRATION=1 to start a private KWin compositor",
    ),
]


def test_private_kwin_output_and_screenshot_round_trip(tmp_path, monkeypatch) -> None:
    host_runtime = os.environ.get("XDG_RUNTIME_DIR")
    if not host_runtime:
        pytest.skip("an XDG_RUNTIME_DIR is required")
    paths = RuntimePaths.from_environment(
        {
            "XDG_RUNTIME_DIR": host_runtime,
            "XDG_STATE_HOME": str(tmp_path / "state"),
        }
    )
    backend = KWinVirtualBackend(paths)
    manager = SessionManager(paths, backend)
    process_markers: list[tuple[tuple[str, ...], str]] = []
    real_popen = subprocess.Popen

    def record_process_marker(command, *args, **kwargs):
        environment = kwargs.get("env")
        if environment is not None and "KDE_AI_PROCESS_MARKER" in environment:
            process_markers.append(
                (tuple(str(part) for part in command), environment["KDE_AI_PROCESS_MARKER"])
            )
        return real_popen(command, *args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", record_process_marker)
    session = manager.create_agent_session("integration", 1024, 768)
    screenshot = tmp_path / "private-output.png"
    marked_helper: subprocess.Popen[bytes] | None = None
    try:
        for identity in (session.kwin_process, session.dbus_process):
            assert any(
                command == identity.command and marker == session.session_id
                for command, marker in process_markers
            )

        marked_helper = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            env=os.environ | {"KDE_AI_PROCESS_MARKER": session.session_id},
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        assert any(
            process.pid == marked_helper.pid
            for process in processes_with_environment_marker(session.session_id)
        )

        displays = manager.displays(session.session_id)
        assert len(displays) == 1
        assert displays[0]["width"] == 1024
        assert displays[0]["height"] == 768

        if session.fake_input_version is not None and session.fake_input_version >= 6:
            receipt = manager.send_key(session.session_id, "CTRL+N")
            assert receipt["server_roundtrip_completed"] is True
            assert receipt["effect_verified"] is False
            text_receipt = manager.send_text(session.session_id, "integration text")
            assert text_receipt["server_roundtrip_completed"] is True
            assert text_receipt["effect_verified"] is False

        if session.fake_input_version is not None and session.fake_input_version >= 3:
            click_receipt = manager.click(session.session_id, 512, 384)
            assert click_receipt["server_roundtrip_completed"] is True
            assert click_receipt["effect_verified"] is False

        app = manager.launch_application(
            session.session_id,
            (
                sys.executable,
                "-c",
                "import subprocess, sys, time; "
                "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']); "
                "time.sleep(60)",
            ),
        )
        assert app["process_started"] is True
        assert manager.get_session(session.session_id)["launched_processes"]

        capture = manager.screenshot(session.session_id, screenshot)
        assert capture["width"] == 1024
        assert capture["height"] == 768
        assert Path(capture["path"]).is_file()
    finally:
        _, warnings = manager.stop(session.session_id)
        assert warnings in (
            [],
            [f"Private runtime directory needs manual cleanup: {session.runtime_dir}"],
        )
        assert process_start_ticks(session.kwin_process.pid) != session.kwin_process.start_ticks
        assert process_start_ticks(session.dbus_process.pid) != session.dbus_process.start_ticks
        assert processes_with_environment_marker(session.session_id) == []
        if marked_helper is not None:
            marked_helper.wait(timeout=2.0)
        if warnings:
            assert Path(session.runtime_dir).is_dir()
        else:
            assert not Path(session.runtime_dir).exists()


def test_private_window_list_activation_and_input_are_session_scoped(tmp_path) -> None:
    if shutil.which("kate") is None:
        pytest.skip("Kate is required for the private window-management integration test")
    host_runtime = os.environ.get("XDG_RUNTIME_DIR")
    if not host_runtime:
        pytest.skip("an XDG_RUNTIME_DIR is required")
    paths = RuntimePaths.from_environment(
        {
            "XDG_RUNTIME_DIR": host_runtime,
            "XDG_STATE_HOME": str(tmp_path / "state"),
        }
    )
    backend = KWinVirtualBackend(paths)
    manager = SessionManager(paths, backend)
    session = manager.create_agent_session("private-window-management", 1024, 768)
    scratch_file = tmp_path / "private-window-scratch.txt"
    scratch_file.write_text("isolated scratch document\n", encoding="utf-8")
    try:
        assert session.window_management_version is not None
        assert session.window_management_version >= 13
        manager.launch_application(session.session_id, ("kate", "--new", str(scratch_file)))

        deadline = time.monotonic() + 8.0
        window: dict[str, object] | None = None
        while time.monotonic() < deadline:
            listing = manager.list_windows(session.session_id)
            matches = [
                item for item in listing["windows"] if "private-window-scratch.txt" in item["title"]
            ]
            if matches:
                window = matches[0]
                break
            time.sleep(0.1)
        assert window is not None, "Kate did not appear in private KWin window management"

        window_id = str(window["window_id"])
        inspected = manager.inspect_window(session.session_id, window_id)["window"]
        assert inspected["app_id"] == "org.kde.kate"
        assert inspected["geometry"]["coordinate_space"] == "logical-desktop-pixels"
        activated = manager.activate_window(session.session_id, window_id)
        assert activated["active_window_verified"] is True

        key_receipt = manager.send_key_to_window(session.session_id, window_id, "CTRL+END")
        assert key_receipt["focus_still_matches_target"] is True
        manager.send_text_to_window(session.session_id, window_id, "TODO")
        manager.send_key_to_window(session.session_id, window_id, "CTRL+S")
        assert "TODO" in scratch_file.read_text(encoding="utf-8")
    finally:
        _, warnings = manager.stop(session.session_id)
        assert warnings in (
            [],
            [f"Private runtime directory needs manual cleanup: {session.runtime_dir}"],
        )

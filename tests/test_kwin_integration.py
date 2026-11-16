import os
import shutil
import struct
import subprocess
import sys
import time
from pathlib import Path

import pytest

from kde_desktop_automation.backends.kwin_virtual import KWinVirtualBackend
from kde_desktop_automation.operations import DesktopOperations
from kde_desktop_automation.paths import RuntimePaths
from kde_desktop_automation.process import process_start_ticks, processes_with_environment_marker
from kde_desktop_automation.sessions import SessionManager

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("KDE_DESKTOP_RUN_KWIN_INTEGRATION") != "1",
        reason="set KDE_DESKTOP_RUN_KWIN_INTEGRATION=1 to start a private KWin compositor",
    ),
]


def test_private_kwin_output_and_process_round_trip(tmp_path, monkeypatch) -> None:
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
        if environment is not None and "KDE_DESKTOP_AUTOMATION_PROCESS_MARKER" in environment:
            process_markers.append(
                (
                    tuple(str(part) for part in command),
                    environment["KDE_DESKTOP_AUTOMATION_PROCESS_MARKER"],
                )
            )
        return real_popen(command, *args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", record_process_marker)
    session = manager.create_agent_session("integration", 1024, 768)
    marked_helper: subprocess.Popen[bytes] | None = None
    try:
        for identity in (session.kwin_process, session.dbus_process):
            assert any(
                command == identity.command and marker == session.session_id
                for command, marker in process_markers
            )

        marked_helper = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            env=os.environ | {"KDE_DESKTOP_AUTOMATION_PROCESS_MARKER": session.session_id},
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


@pytest.mark.skipif(
    os.environ.get("KDE_DESKTOP_RUN_KWIN_SCREENSHOT_INTEGRATION") != "1",
    reason=(
        "set KDE_DESKTOP_RUN_KWIN_SCREENSHOT_INTEGRATION=1 to test authorized "
        "KWin Screenshot2 capture"
    ),
)
def test_private_kwin_captures_inactive_window_without_changing_focus(
    tmp_path,
    monkeypatch,
) -> None:
    helper = os.environ.get("KDE_DESKTOP_SCREENSHOT_HELPER") or shutil.which(
        "kde-desktop-screenshot"
    )
    test_window = os.environ.get("KDE_DESKTOP_SCREENSHOT_TEST_WINDOW")
    if test_window is None:
        test_window = str(
            Path(__file__).parents[1]
            / "build"
            / "screenshot-helper"
            / "kde-desktop-screenshot-test-window"
        )
    if not helper or not Path(helper).is_file() or not os.access(helper, os.X_OK):
        pytest.skip("build kde-desktop-screenshot or set KDE_DESKTOP_SCREENSHOT_HELPER")
    if not Path(test_window).is_file() or not os.access(test_window, os.X_OK):
        pytest.skip("build kde-desktop-screenshot-test-window or set its path in the environment")
    if shutil.which("kbuildsycoca6") is None:
        pytest.skip("kbuildsycoca6 is required to register the private test desktop entry")
    host_runtime = os.environ.get("XDG_RUNTIME_DIR")
    if not host_runtime:
        pytest.skip("an XDG_RUNTIME_DIR is required")

    helper = str(Path(helper).resolve())
    monkeypatch.setenv("KDE_DESKTOP_SCREENSHOT_HELPER", helper)
    registration_data_home = tmp_path / "host-data"
    host_applications = registration_data_home / "applications"
    host_applications.mkdir(parents=True)
    (host_applications / "kde-desktop-screenshot.desktop").write_text(
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Name=KWin Screenshot2 Integration Helper\n"
        f'Exec="{helper}"\n'
        "NoDisplay=true\n"
        "Terminal=false\n"
        "X-KDE-DBUS-Restricted-Interfaces=org.kde.KWin.ScreenShot2;\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("XDG_DATA_HOME", str(registration_data_home))
    paths = RuntimePaths.from_environment(
        {
            "XDG_RUNTIME_DIR": host_runtime,
            "XDG_STATE_HOME": str(tmp_path / "state"),
        }
    )
    backend = KWinVirtualBackend(paths)
    manager = SessionManager(paths, backend)
    session = manager.create_agent_session("screenshot2-integration", 1280, 800)
    try:
        private_registration = (
            Path(session.home_dir) / "data" / "applications" / "kde-desktop-screenshot.desktop"
        )
        assert private_registration.is_file()

        operations = DesktopOperations(session.session_id, manager=manager)
        workspace_path = tmp_path / "private-workspace.png"
        workspace = operations.screenshot(workspace_path)
        workspace_image = workspace_path.read_bytes()
        assert workspace_image.startswith(b"\x89PNG\r\n\x1a\n")
        assert struct.unpack(">II", workspace_image[16:24]) == (1280, 800)
        assert workspace["source"] == "kwin-screenshot2"
        assert workspace["api"] == "org.kde.KWin.ScreenShot2"

        target_title = "Screenshot2 inactive target"
        focus_title = "Screenshot2 focus window"
        manager.launch_application(session.session_id, (test_window, target_title))
        manager.launch_application(session.session_id, (test_window, focus_title))

        def wait_for_title(title: str) -> dict[str, object]:
            deadline = time.monotonic() + 15.0
            latest: list[dict[str, object]] = []
            while time.monotonic() < deadline:
                latest = manager.list_windows(session.session_id)["windows"]
                match = next((window for window in latest if window["title"] == title), None)
                if match is not None:
                    return match
                time.sleep(0.1)
            raise AssertionError(f"Timed out waiting for {title!r}; last windows: {latest!r}")

        target = wait_for_title(target_title)
        focus = wait_for_title(focus_title)
        target_id = str(target["window_id"])
        focus_id = str(focus["window_id"])
        manager.activate_window(session.session_id, focus_id)
        assert manager.inspect_window(session.session_id, target_id)["window"]["active"] is False
        assert manager.inspect_window(session.session_id, focus_id)["window"]["active"] is True

        screenshot = tmp_path / "inactive-window.png"
        capture = operations.screenshot_window(target_id, screenshot)

        image = screenshot.read_bytes()
        assert image.startswith(b"\x89PNG\r\n\x1a\n")
        assert struct.unpack(">II", image[16:24]) == (capture["width"], capture["height"])
        assert capture["source"] == "kwin-screenshot2"
        assert capture["api"] == "org.kde.KWin.ScreenShot2"
        assert capture["window_id"] == target_id
        assert capture["focus_changed"] is False
        assert capture["window"]["active"] is False
        assert manager.inspect_window(session.session_id, focus_id)["window"]["active"] is True
    finally:
        _, warnings = manager.stop(session.session_id)
        assert warnings in (
            [],
            [f"Private runtime directory needs manual cleanup: {session.runtime_dir}"],
        )


@pytest.mark.skipif(
    os.environ.get("KDE_DESKTOP_RUN_KWIN_DND_INTEGRATION") != "1",
    reason=(
        "set KDE_DESKTOP_RUN_KWIN_DND_INTEGRATION=1 to test private Dolphin-to-Kate drag-and-drop"
    ),
)
def test_private_dolphin_drag_and_drop_opens_file_in_kate(tmp_path) -> None:
    required_executables = ("dolphin", "kate", "kdotool", "wl-paste")
    missing = [name for name in required_executables if shutil.which(name) is None]
    if missing:
        pytest.skip(f"Dolphin-to-Kate integration test requires: {', '.join(missing)}")
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
    session = manager.create_agent_session("private-dolphin-kate-dnd", 1280, 800)
    try:
        source_folder = Path(session.home_dir) / "Downloads" / "dragtest"
        source_folder.mkdir(parents=True)
        source_file = source_folder / "dolphin-kate-drop-test.txt"
        expected_contents = "Private Dolphin-to-Kate drop regression.\nVerified from Kate.\n"
        source_file.write_text(expected_contents, encoding="utf-8")
        kate_start_file = Path(session.home_dir) / "kate-start.txt"
        kate_start_file.write_text("Initial Kate document.\n", encoding="utf-8")

        if session.window_management_version is None or session.window_management_version < 13:
            pytest.skip("private KWin window management v13 or newer is required")
        if session.fake_input_version is None or session.fake_input_version < 6:
            pytest.skip("private KWin fake input v6 or newer is required")

        manager.launch_application(
            session.session_id,
            ("dolphin", "--new-window", source_folder.as_uri()),
        )
        manager.launch_application(
            session.session_id,
            ("kate", "--new", str(kate_start_file)),
        )
        # Let both Qt applications finish their first private-session activation
        # before opening KWin protocol clients to enumerate their windows.
        time.sleep(2.0)

        def wait_for_window(app_id: str, title_part: str | None = None) -> dict[str, object]:
            deadline = time.monotonic() + 15.0
            latest: list[dict[str, object]] = []
            while time.monotonic() < deadline:
                latest = manager.list_windows(session.session_id)["windows"]
                matches = [
                    window
                    for window in latest
                    if window["app_id"] == app_id
                    and (title_part is None or title_part in str(window["title"]))
                ]
                if matches:
                    return matches[0]
                time.sleep(0.1)
            raise AssertionError(
                f"Timed out waiting for {app_id} title {title_part!r}; last windows: {latest!r}"
            )

        dolphin = wait_for_window("org.kde.dolphin", "dragtest")
        kate = wait_for_window("org.kde.kate")
        dolphin_id = str(dolphin["window_id"])
        kate_id = str(kate["window_id"])

        dolphin_placement = manager.set_window_geometry(
            session.session_id, dolphin_id, 0, 0, 640, 800
        )
        kate_placement = manager.set_window_geometry(session.session_id, kate_id, 640, 0, 640, 800)
        assert dolphin_placement["verified"] is True
        assert kate_placement["verified"] is True

        manager.activate_window(session.session_id, kate_id)
        manager.send_key_to_window(session.session_id, kate_id, "CTRL+N")
        wait_for_window("org.kde.kate", "Untitled")

        manager.activate_window(session.session_id, dolphin_id)
        # In a fresh Dolphin profile, the first icon is centered at this point
        # after arranging the 1280x800 private output into two equal windows.
        manager.click(session.session_id, 230, 125)
        time.sleep(0.2)
        receipt = manager.drag(
            session.session_id,
            230,
            125,
            950,
            350,
            steps=64,
            duration_ms=2_000,
            hold_ms=300,
            end_pause_ms=1_000,
        )
        assert receipt["effect_verified"] is False

        opened_document = wait_for_window("org.kde.kate", source_file.name)
        opened_document_id = str(opened_document["window_id"])
        manager.activate_window(session.session_id, opened_document_id)
        manager.send_key_to_window(session.session_id, opened_document_id, "CTRL+A")
        manager.send_key_to_window(session.session_id, opened_document_id, "CTRL+C")

        paste = subprocess.run(
            [shutil.which("wl-paste") or "wl-paste", "--no-newline"],
            env=manager.runtime_environment(session.session_id),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            check=False,
            text=True,
            timeout=5.0,
        )
        assert paste.returncode == 0, paste.stderr
        assert paste.stdout == expected_contents
    finally:
        stopped, warnings = manager.stop(session.session_id)
        assert stopped["status"] == "stopped"
        assert warnings in (
            [],
            [f"Private runtime directory needs manual cleanup: {session.runtime_dir}"],
        )
        assert processes_with_environment_marker(session.session_id) == []


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
                item
                for item in listing["windows"]
                if "private-window-scratch.txt" in item["title"]
                and isinstance(item.get("geometry"), dict)
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

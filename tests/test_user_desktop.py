from __future__ import annotations

import socket
from pathlib import Path
from typing import Any

import pytest

from kde_desktop_automation.backends import kwin_controls, user_desktop
from kde_desktop_automation.backends.kwin_input import InputReceipt
from kde_desktop_automation.errors import InteractionError
from kde_desktop_automation.process import CommandResult

WINDOW_ID = "8a415edd-10b8-4f26-a074-b341938128d7"
OTHER_WINDOW_ID = "772c92c6-7f71-4ae2-b9bb-a946b62f00c1"


def _window_info(
    window_id: str = WINDOW_ID,
    *,
    caption: str = "Kate — notes.txt",
    app_id: str = "org.kde.kate",
    fullscreen: bool = False,
) -> dict[str, Any]:
    return {
        "uuid": "{" + window_id + "}",
        "caption": caption,
        "desktopFile": app_id,
        "resourceName": "kate",
        "resourceClass": app_id,
        "role": "main",
        "pid": 1234,
        "x": 100.0,
        "y": 50.0,
        "width": 200.0,
        "height": 100.0,
        "fullscreen": fullscreen,
        "minimized": False,
        "hasTransientParent": False,
        "desktops": ["desktop-1"],
    }


def _desktop_environment(tmp_path: Path) -> dict[str, str]:
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o700)
    runtime.chmod(0o700)
    socket_path = runtime / "wayland-test"
    wayland_socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    wayland_socket.bind(str(socket_path))
    wayland_socket.close()
    return {
        "XDG_CURRENT_DESKTOP": "KDE",
        "XDG_SESSION_TYPE": "wayland",
        "XDG_RUNTIME_DIR": str(runtime),
        "WAYLAND_DISPLAY": "wayland-test",
        "DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/user/1000/bus",
        "KDE_SESSION_VERSION": "6",
        "PATH": "/usr/bin",
    }


def _backend(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    active_ids: list[str] | None = None,
    records: dict[str, dict[str, Any]] | None = None,
) -> tuple[user_desktop.UserDesktopBackend, list[tuple[str, ...]]]:
    environment = _desktop_environment(tmp_path)
    monkeypatch.setattr(user_desktop, "require_executable", lambda _name: "/usr/bin/kdotool")
    monkeypatch.setattr(kwin_controls, "require_executable", lambda _name: "/usr/bin/kdotool")
    commands: list[tuple[str, ...]] = []
    active_values = list(active_ids or [WINDOW_ID])
    current_active = [active_values[-1]]
    available_records = records or {WINDOW_ID: _window_info()}

    def run(argv, *, env, timeout=5.0):
        command = tuple(str(part) for part in argv)
        commands.append(command)
        if command[1] == "search":
            output = "\n".join(available_records)
        elif command[1] == "getactivewindow":
            output = current_active[0]
            if active_values:
                output = active_values.pop(0)
                current_active[0] = output
        elif command[1] == "windowactivate":
            current_active[0] = command[2].strip("{}")
            output = ""
        elif command[1] == "windowstate":
            window_id = command[-1].strip("{}")
            if command[2] == "--add":
                available_records[window_id]["minimized"] = True
            elif command[2] == "--remove":
                available_records[window_id]["minimized"] = False
            else:
                raise AssertionError(f"unexpected window state command: {command}")
            output = ""
        elif command[1] == "getmouselocation":
            output = f"x:25 y:40 screen:0 window:{{{WINDOW_ID}}}"
        elif command[1] == "getwindowgeometry":
            record = available_records[command[2].strip("{}").lower()]
            output = (
                f"Window {{{command[2].strip('{}')}}}\n"
                f"  Position: {record['x']},{record['y']}\n"
                f"  Geometry: {record['width']}x{record['height']}\n"
            )
        elif command[1] == "windowsize":
            record = available_records[command[2].strip("{}").lower()]
            record["width"], record["height"] = float(command[3]), float(command[4])
            output = ""
        elif command[1] == "windowmove":
            record = available_records[command[2].strip("{}").lower()]
            record["x"], record["y"] = float(command[3]), float(command[4])
            output = ""
        else:
            raise AssertionError(f"unexpected kdotool command: {command}")
        return CommandResult(command, 0, output, "")

    monkeypatch.setattr(user_desktop, "run_command", run)
    monkeypatch.setattr(kwin_controls, "run_command", run)
    backend = user_desktop.UserDesktopBackend(
        environment,
        window_info_reader=lambda ids: {
            window_id: available_records[window_id]
            for window_id in ids
            if window_id in available_records
        },
    )
    return backend, commands


def test_live_desktop_environment_rejects_private_sessions(tmp_path: Path) -> None:
    environment = _desktop_environment(tmp_path)

    assert user_desktop.user_desktop_environment_issue(environment) is None
    assert (
        user_desktop.user_desktop_environment_issue(
            environment | {"XDG_CURRENT_DESKTOP": "KDE Plasma"}
        )
        is None
    )
    for marker in user_desktop._PRIVATE_PROCESS_MARKERS:
        assert (
            user_desktop.user_desktop_environment_issue(
                environment | {marker: "agent-123456789abc"}
            )
            == "This process belongs to a managed private session, not the user desktop."
        )
    assert (
        user_desktop.user_desktop_environment_issue(environment | {"XDG_SESSION_TYPE": "x11"})
        == "Live user-desktop interaction currently requires a KDE Wayland session."
    )


def test_live_desktop_environment_rejects_private_session_runtime_path(
    tmp_path: Path,
) -> None:
    environment = _desktop_environment(tmp_path)
    private_runtime = "/run/user/1000/kde-desktop-automation/sessions/agent-123456789abc"

    assert (
        user_desktop.user_desktop_environment_issue(
            environment
            | {
                "XDG_RUNTIME_DIR": private_runtime,
                "DBUS_SESSION_BUS_ADDRESS": f"unix:path={private_runtime}/bus",
            }
        )
        == "The current environment points to a managed private session, not the user desktop."
    )


def test_window_id_parser_normalizes_braces_and_rejects_arbitrary_values() -> None:
    assert user_desktop._parse_window_ids(f"{{{WINDOW_ID.upper()}}}\n{WINDOW_ID}\n") == [WINDOW_ID]
    with pytest.raises(InteractionError, match="UUIDs"):
        user_desktop._normalize_window_id("not-a-window")


def test_list_windows_returns_live_metadata_and_fullscreen_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    records = {
        WINDOW_ID: _window_info(fullscreen=True),
        OTHER_WINDOW_ID: _window_info(
            OTHER_WINDOW_ID,
            caption="Other window",
            app_id="org.example.App",
        ),
    }
    backend, commands = _backend(tmp_path, monkeypatch, records=records)

    result = backend.list_windows()

    assert result["target"] == {"kind": "user-desktop"}
    assert result["count"] == 2
    assert result["windows"][0]["fullscreen"] is True
    assert result["windows"][0]["active"] is True
    assert result["windows"][0]["geometry"]["coordinate_space"] == "logical-desktop-pixels"
    assert commands[0] == ("/usr/bin/kdotool", "search", "--title", ".*")


def test_find_window_uses_exact_app_id_and_returns_ambiguous_matches(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    records = {
        WINDOW_ID: _window_info(),
        OTHER_WINDOW_ID: _window_info(
            OTHER_WINDOW_ID,
            caption="Kate — other.txt",
        ),
    }
    backend, _commands = _backend(tmp_path, monkeypatch, records=records)

    with pytest.raises(InteractionError) as error:
        backend.find_window(app_id="org.kde.kate")

    assert error.value.code == "ambiguous_target"
    assert len(error.value.details["matches"]) == 2


def test_find_window_returns_only_a_unique_match(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend, _commands = _backend(tmp_path, monkeypatch)

    result = backend.find_window(app_id="org.kde.kate", title="NOTES")

    assert result["window"]["window_id"] == WINDOW_ID
    assert result["window"]["title"] == "Kate — notes.txt"


def test_activate_window_passes_an_explicit_window_id_and_verifies_focus(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend, commands = _backend(tmp_path, monkeypatch, active_ids=[OTHER_WINDOW_ID, WINDOW_ID])

    result = backend.activate_window(WINDOW_ID)

    assert result["active_window_verified"] is True
    assert result["window"]["active"] is True
    assert commands[-3] == ("/usr/bin/kdotool", "windowactivate", f"{{{WINDOW_ID}}}")
    assert commands[-2:] == [
        ("/usr/bin/kdotool", "getactivewindow"),
        ("/usr/bin/kdotool", "getactivewindow"),
    ]


def test_key_input_refuses_to_act_when_named_window_is_not_active(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend, _commands = _backend(tmp_path, monkeypatch, active_ids=[OTHER_WINDOW_ID])
    input_calls: list[str] = []

    class FakeInput:
        def __init__(self, *_args, **_kwargs):
            input_calls.append("connected")

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            pass

        def send_key(self, _chord: str) -> InputReceipt:
            input_calls.append("sent")
            return InputReceipt("key CTRL+END", 4, True)

    monkeypatch.setattr(user_desktop, "KWinFakeInput", FakeInput)

    with pytest.raises(InteractionError, match="already be active"):
        backend.send_key(WINDOW_ID, "CTRL+END")

    assert input_calls == []


def test_type_input_checks_focus_again_after_connecting_and_reports_submission(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend, _commands = _backend(
        tmp_path,
        monkeypatch,
        active_ids=[WINDOW_ID, WINDOW_ID, WINDOW_ID],
    )
    input_calls: list[str] = []

    class FakeInput:
        def __init__(self, *_args, **_kwargs):
            input_calls.append("connected")

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            pass

        def send_text(self, text: str) -> InputReceipt:
            input_calls.append(text)
            return InputReceipt(f"type text ({len(text)} Unicode characters)", 8, True)

    monkeypatch.setattr(user_desktop, "KWinFakeInput", FakeInput)

    result = backend.send_text(WINDOW_ID, "\nTODO")

    assert input_calls == ["connected", "\nTODO"]
    assert result["target"]["kind"] == "user-desktop"
    assert result["target"]["window_id"] == WINDOW_ID
    assert result["focus_still_matches_target"] is True
    assert result["effect_verified"] is False


def test_click_maps_normalized_window_coordinates_to_live_desktop_pixels(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend, _commands = _backend(
        tmp_path,
        monkeypatch,
        active_ids=[WINDOW_ID, WINDOW_ID, WINDOW_ID],
    )
    clicks: list[tuple[int, int]] = []

    class FakeInput:
        def __init__(self, *_args, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            pass

        def click(self, x: int, y: int, button: str = "left") -> InputReceipt:
            clicks.append((x, y))
            return InputReceipt(f"click {button} at ({x}, {y})", 3, True)

    monkeypatch.setattr(user_desktop, "KWinFakeInput", FakeInput)

    result = backend.click_window(WINDOW_ID, 0.5, 0.5)

    assert clicks == [(200, 100)]
    assert result["requested_point"] == {"x": 0.5, "y": 0.5, "space": "normalized-window"}
    assert result["resolved_point"] == {
        "x": 200,
        "y": 100,
        "space": "logical-desktop-pixels",
        "geometry_basis": "kwin-dbus-reported",
    }


def test_click_refuses_input_if_focus_changes_during_connection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend, _commands = _backend(
        tmp_path,
        monkeypatch,
        active_ids=[WINDOW_ID, OTHER_WINDOW_ID],
    )
    sent: list[bool] = []

    class FakeInput:
        def __init__(self, *_args, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            pass

        def click(self, _x: int, _y: int) -> InputReceipt:
            sent.append(True)
            return InputReceipt("click", 3, True)

    monkeypatch.setattr(user_desktop, "KWinFakeInput", FakeInput)

    with pytest.raises(InteractionError, match="active window changed"):
        backend.click_window(WINDOW_ID, 0.5, 0.5)

    assert sent == []


def test_global_click_uses_logical_desktop_coordinates_without_window_guard(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend, _commands = _backend(tmp_path, monkeypatch, active_ids=[OTHER_WINDOW_ID] * 2)
    monkeypatch.setattr(
        backend,
        "list_displays",
        lambda: [
            {
                "name": "DP-1",
                "x": -1920,
                "y": 0,
                "width": 1920,
                "height": 1080,
                "coordinate_space": "logical-desktop-pixels",
            }
        ],
    )
    clicks: list[tuple[int, int, str]] = []

    class FakeInput:
        def __init__(self, *_args, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            pass

        def click(self, x: int, y: int, button: str = "left") -> InputReceipt:
            clicks.append((x, y, button))
            return InputReceipt("click left at (-1800, 400)", 3, True)

    monkeypatch.setattr(user_desktop, "KWinFakeInput", FakeInput)

    result = backend.click_at(-1800, 400)

    assert clicks == [(-1800, 400, "left")]
    assert result["requested_point"] == {
        "x": -1800,
        "y": 400,
        "space": "logical-desktop-pixels",
    }
    assert result["active_window_before"] == OTHER_WINDOW_ID
    assert result["active_window_after"] == OTHER_WINDOW_ID
    assert result["focus_change_observed"] is False
    assert result["input_semantics"]["targeting"] == "global-coordinate"
    assert result["effect_verified"] is False


def test_global_drag_validates_every_sampled_point_and_submits_bounded_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend, _commands = _backend(tmp_path, monkeypatch)
    monkeypatch.setattr(
        backend,
        "list_displays",
        lambda: [
            {
                "name": "DP-1",
                "x": 0,
                "y": 0,
                "width": 1920,
                "height": 1080,
                "coordinate_space": "logical-desktop-pixels",
            }
        ],
    )
    drags: list[tuple[int, int, int, int, str, int, int, int, int]] = []

    class FakeInput:
        def __init__(self, *_args, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            pass

        def drag(
            self,
            start_x,
            start_y,
            end_x,
            end_y,
            *,
            button,
            steps,
            duration_ms,
            hold_ms,
            end_pause_ms,
        ):
            drags.append(
                (start_x, start_y, end_x, end_y, button, steps, duration_ms, hold_ms, end_pause_ms)
            )
            return InputReceipt("drag left from (10, 20) to (50, 40)", steps + 3, True)

    monkeypatch.setattr(user_desktop, "KWinFakeInput", FakeInput)

    result = backend.drag_at(10, 20, 50, 40, steps=2)

    assert drags == [(10, 20, 50, 40, "left", 2, 750, 100, 250)]
    assert result["requested_path"] == {
        "start": {"x": 10, "y": 20},
        "end": {"x": 50, "y": 40},
        "steps": 2,
        "duration_ms": 750,
        "hold_ms": 100,
        "end_pause_ms": 250,
        "space": "logical-desktop-pixels",
    }
    assert result["effect_verified"] is False


def test_global_pointer_input_rejects_points_outside_displays_before_connecting(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend, _commands = _backend(tmp_path, monkeypatch)
    monkeypatch.setattr(
        backend,
        "list_displays",
        lambda: [
            {
                "name": "DP-1",
                "x": 0,
                "y": 0,
                "width": 100,
                "height": 100,
                "coordinate_space": "logical-desktop-pixels",
            }
        ],
    )
    connections: list[bool] = []

    class FakeInput:
        def __init__(self, *_args, **_kwargs):
            connections.append(True)

    monkeypatch.setattr(user_desktop, "KWinFakeInput", FakeInput)

    with pytest.raises(InteractionError) as error:
        backend.click_at(100, 50)

    assert error.value.code == "coordinates_out_of_bounds"
    assert connections == []


def test_window_minimize_is_explicit_and_verified_by_readback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend, commands = _backend(tmp_path, monkeypatch)

    result = backend.set_window_minimized(WINDOW_ID, True)

    assert commands[-2] == (
        "/usr/bin/kdotool",
        "windowstate",
        "--add",
        "MINIMIZED",
        f"{{{WINDOW_ID}}}",
    )
    assert result["window"]["minimized"] is True
    assert result["verified"] is True


def test_window_geometry_uses_kdotool_and_verifies_frame_readback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend, commands = _backend(tmp_path, monkeypatch)

    result = backend.set_window_geometry(WINDOW_ID, 20, 30, 800, 600)

    assert result["verified"] is True
    assert result["changed"] is True
    assert result["requested_geometry"] == {
        "x": 20,
        "y": 30,
        "width": 800,
        "height": 600,
        "coordinate_space": "logical-desktop-pixels",
        "basis": "kwin-frame",
    }
    assert [command[1] for command in commands if command[1] in {"windowsize", "windowmove"}] == [
        "windowsize",
        "windowmove",
    ]
    assert result["window"]["geometry"]["x"] == 20


def test_pointer_position_is_a_read_only_kdotool_query(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend, commands = _backend(tmp_path, monkeypatch)

    result = backend.pointer_position()

    assert result == {
        "x": 25,
        "y": 40,
        "screen_index": 0,
        "window_id": WINDOW_ID,
        "coordinate_space": "logical-desktop-pixels",
    }
    assert commands == [("/usr/bin/kdotool", "getmouselocation")]


def test_live_capabilities_probe_window_service_and_report_focus_routing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend, _commands = _backend(tmp_path, monkeypatch)
    monkeypatch.setattr(user_desktop, "fake_input_version", lambda _socket: 6)
    monkeypatch.setattr(user_desktop.shutil, "which", lambda name: f"/usr/bin/{name}")

    result = backend.capabilities()

    assert result["capabilities"]["window.find"] is True
    assert result["capabilities"]["window.wait"] is True
    assert result["capabilities"]["input.key"] is True
    assert result["capabilities"]["input.focus_guard"] is True
    assert result["capabilities"]["input.active_window_guard"] is True
    assert result["capabilities"]["input.global_click"] is True
    assert result["capabilities"]["input.global_drag"] is True
    assert result["capabilities"]["window.set_minimized"] is True
    assert result["capabilities"]["window.set_geometry"] is True
    assert result["capabilities"]["input.pointer_position"] is True
    assert result["capabilities"]["desktop.observe"] is True
    assert result["capabilities"]["media.players"] is True
    assert result["capabilities"]["media.control"] is True
    assert result["capabilities"]["media.seek_to"] is True
    assert result["capabilities"]["audio.devices"] is True
    assert result["capabilities"]["audio.route_stream"] is True
    assert result["input_semantics"]["atomic_window_routing"] is False
    assert result["window_reason"] is None


def test_live_capabilities_report_unavailable_window_metadata_service(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend, _commands = _backend(tmp_path, monkeypatch)
    monkeypatch.setattr(user_desktop, "fake_input_version", lambda _socket: 6)

    def fail_window_info(_window_ids):
        raise InteractionError("kwin_dbus_unavailable", "KWin D-Bus is unavailable.")

    backend._window_info_reader = fail_window_info
    result = backend.capabilities()

    assert result["capabilities"]["window.list"] is False
    assert result["capabilities"]["input.key"] is False
    assert result["window_reason"] == "KWin D-Bus is unavailable."


def test_live_screenshot_reports_target_and_common_pixel_coordinate_space(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend, _commands = _backend(tmp_path, monkeypatch)
    monkeypatch.setattr(
        user_desktop,
        "capture_fullscreen",
        lambda output, *, env, overwrite: {
            "path": str(output),
            "width": 1920,
            "height": 1080,
            "coordinate_space": "screenshot-pixels",
            "source": "spectacle",
        },
    )

    result = backend.screenshot(tmp_path / "live.png")

    assert result["target"] == {"kind": "user-desktop"}
    assert result["coordinate_space"] == "screenshot-pixels"

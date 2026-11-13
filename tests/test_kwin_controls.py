from __future__ import annotations

from typing import Any

import pytest

from kde_desktop_automation.backends import kwin_controls
from kde_desktop_automation.errors import InteractionError
from kde_desktop_automation.process import CommandResult

WINDOW_ID = "b5ff2329-3976-4c31-bf31-1538cb97c8f0"


def test_pointer_position_parses_read_only_kdotool_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(kwin_controls, "require_executable", lambda _name: "/usr/bin/kdotool")
    monkeypatch.setattr(
        kwin_controls,
        "run_command",
        lambda command, **_kwargs: CommandResult(
            tuple(command), 0, f"x:-12 y:34 screen:1 window:{{{WINDOW_ID}}}\n", ""
        ),
    )

    result = kwin_controls.pointer_position(env={})

    assert result == {
        "x": -12,
        "y": 34,
        "screen_index": 1,
        "window_id": WINDOW_ID,
        "coordinate_space": "logical-desktop-pixels",
    }


def test_set_window_geometry_moves_resizes_and_verifies_readback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(kwin_controls, "require_executable", lambda _name: "/usr/bin/kdotool")
    geometry = {"x": 10, "y": 20, "width": 300, "height": 200}
    commands: list[tuple[str, ...]] = []

    def run(command, *, env, timeout):
        parts = tuple(str(part) for part in command)
        commands.append(parts)
        if parts[1] == "getwindowgeometry":
            stdout = (
                f"Window {{{WINDOW_ID}}}\n"
                f"  Position: {geometry['x']},{geometry['y']}\n"
                f"  Geometry: {geometry['width']}x{geometry['height']}\n"
            )
        elif parts[1] == "windowsize":
            geometry["width"], geometry["height"] = int(parts[3]), int(parts[4])
            stdout = ""
        elif parts[1] == "windowmove":
            geometry["x"], geometry["y"] = int(parts[3]), int(parts[4])
            stdout = ""
        else:
            raise AssertionError(parts)
        return CommandResult(parts, 0, stdout, "")

    monkeypatch.setattr(kwin_controls, "run_command", run)

    result = kwin_controls.set_window_frame_geometry(WINDOW_ID, 40, 50, 640, 480, env={})

    assert result["verified"] is True
    assert result["changed"] is True
    assert result["effective_geometry"] == {
        "x": 40,
        "y": 50,
        "width": 640,
        "height": 480,
        "coordinate_space": "logical-desktop-pixels",
    }
    assert [command[1] for command in commands] == [
        "getwindowgeometry",
        "windowsize",
        "getwindowgeometry",
        "windowmove",
        "getwindowgeometry",
    ]


def test_set_window_geometry_is_idempotent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(kwin_controls, "require_executable", lambda _name: "/usr/bin/kdotool")
    geometry = "Position: 40,50\nGeometry: 640x480"
    commands: list[tuple[str, ...]] = []

    def run(command, **_kwargs):
        parts = tuple(str(part) for part in command)
        commands.append(parts)
        return CommandResult(parts, 0, geometry, "")

    monkeypatch.setattr(kwin_controls, "run_command", run)

    result = kwin_controls.set_window_frame_geometry(WINDOW_ID, 40, 50, 640, 480, env={})

    assert result["changed"] is False
    assert result["verified"] is True
    assert [command[1] for command in commands] == ["getwindowgeometry", "getwindowgeometry"]


def test_move_failure_after_resize_reports_effective_partial_geometry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(kwin_controls, "require_executable", lambda _name: "/usr/bin/kdotool")
    geometry = {"x": 10, "y": 20, "width": 300, "height": 200}

    def run(command, **_kwargs):
        parts = tuple(str(part) for part in command)
        if parts[1] == "getwindowgeometry":
            stdout = (
                f"Position: {geometry['x']},{geometry['y']}\n"
                f"Geometry: {geometry['width']}x{geometry['height']}"
            )
            return CommandResult(parts, 0, stdout, "")
        if parts[1] == "windowsize":
            geometry["width"], geometry["height"] = int(parts[3]), int(parts[4])
            return CommandResult(parts, 0, "", "")
        if parts[1] == "windowmove":
            return CommandResult(parts, 1, "", "move denied")
        raise AssertionError(parts)

    monkeypatch.setattr(kwin_controls, "run_command", run)

    with pytest.raises(InteractionError) as error:
        kwin_controls.set_window_frame_geometry(WINDOW_ID, 40, 50, 640, 480, env={})

    assert error.value.code == "window_geometry_partial_failure"
    assert error.value.details["partial_action"] == "move_failed_after_resize"
    assert error.value.details["effective_geometry"]["width"] == 640
    assert error.value.details["effective_geometry"]["x"] == 10


@pytest.mark.parametrize(
    ("geometry", "code"),
    [
        ((True, 0, 100, 100), "invalid_window_geometry"),
        ((0, 0, 0, 100), "invalid_window_geometry"),
        ((0, 0, 100, 32_769), "invalid_window_geometry"),
        ((32_769, 0, 100, 100), "invalid_window_geometry"),
    ],
)
def test_set_window_geometry_rejects_invalid_values(
    monkeypatch: pytest.MonkeyPatch,
    geometry: tuple[Any, int, int, int],
    code: str,
) -> None:
    monkeypatch.setattr(kwin_controls, "require_executable", lambda _name: "/usr/bin/kdotool")

    with pytest.raises(InteractionError) as error:
        kwin_controls.set_window_frame_geometry(WINDOW_ID, *geometry, env={})

    assert error.value.code == code


def test_pointer_position_rejects_unrecognized_output(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(kwin_controls, "require_executable", lambda _name: "/usr/bin/kdotool")
    monkeypatch.setattr(
        kwin_controls,
        "run_command",
        lambda command, **_kwargs: CommandResult(tuple(command), 0, "cursor is somewhere", ""),
    )

    with pytest.raises(InteractionError, match="unrecognized pointer"):
        kwin_controls.pointer_position(env={})

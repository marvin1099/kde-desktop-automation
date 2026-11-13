from __future__ import annotations

import re
import time
from collections.abc import Mapping
from typing import Any

from kde_desktop_automation.errors import InteractionError
from kde_desktop_automation.process import require_executable, require_success, run_command

_POINTER_LOCATION = re.compile(r"x:(-?\d+)\s+y:(-?\d+)\s+screen:(-?\d+)\s+window:(\S+)")
_WINDOW_GEOMETRY = re.compile(
    r"Position:\s*(-?\d+(?:\.\d+)?),\s*(-?\d+(?:\.\d+)?)\s+"
    r"Geometry:\s*(\d+(?:\.\d+)?)x(\d+(?:\.\d+)?)"
)
_WINDOW_UUID = re.compile(r"^\{?([0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12})\}?$")
_GEOMETRY_LIMIT = 32_768


def pointer_position(*, env: Mapping[str, str]) -> dict[str, Any]:
    """Read KWin's current pointer location without moving or clicking it."""
    executable = require_executable("kdotool")
    result = require_success(run_command([executable, "getmouselocation"], env=env, timeout=5.0))
    match = _POINTER_LOCATION.search(result.stdout)
    if match is None:
        raise InteractionError(
            "invalid_backend_data",
            "kdotool returned an unrecognized pointer location.",
            details={"output": result.stdout.strip()},
        )
    window_value = match.group(4)
    window_match = _WINDOW_UUID.fullmatch(window_value)
    return {
        "x": int(match.group(1)),
        "y": int(match.group(2)),
        "screen_index": int(match.group(3)),
        "window_id": None if window_match is None else window_match.group(1).lower(),
        "coordinate_space": "logical-desktop-pixels",
    }


def read_window_frame_geometry(
    window_id: str,
    *,
    env: Mapping[str, str],
) -> dict[str, int | float | str]:
    """Read the KWin frame rectangle used by kdotool move/resize operations."""
    executable = require_executable("kdotool")
    result = require_success(
        run_command(
            [executable, "getwindowgeometry", _window_argument(window_id)],
            env=env,
            timeout=5.0,
        )
    )
    match = _WINDOW_GEOMETRY.search(result.stdout)
    if match is None:
        raise InteractionError(
            "invalid_backend_data",
            "kdotool returned unrecognized window frame geometry.",
            details={"window_id": window_id, "output": result.stdout.strip()},
        )
    return {
        "x": _number(match.group(1)),
        "y": _number(match.group(2)),
        "width": _number(match.group(3)),
        "height": _number(match.group(4)),
        "coordinate_space": "logical-desktop-pixels",
    }


def set_window_frame_geometry(
    window_id: str,
    x: int,
    y: int,
    width: int,
    height: int,
    *,
    env: Mapping[str, str],
) -> dict[str, Any]:
    """Set a KWin window's frame rectangle and verify the compositor readback."""
    _validate_geometry(x, y, width, height)
    executable = require_executable("kdotool")
    window_argument = _window_argument(window_id)
    before = read_window_frame_geometry(window_id, env=env)

    before_move = before
    if not _same_size(before, width, height):
        require_success(
            run_command(
                [executable, "windowsize", window_argument, str(width), str(height)],
                env=env,
                timeout=5.0,
            )
        )
        size_deadline = time.monotonic() + 1.0
        before_move = read_window_frame_geometry(window_id, env=env)
        while not _same_size(before_move, width, height) and time.monotonic() < size_deadline:
            time.sleep(0.05)
            before_move = read_window_frame_geometry(window_id, env=env)
        if not _same_size(before_move, width, height):
            raise InteractionError(
                "window_geometry_unverified",
                "KWin did not apply the requested frame size; position was not changed.",
                details={
                    "window_id": window_id,
                    "requested_geometry": _geometry(x, y, width, height),
                    "before_geometry": before,
                    "effective_geometry": before_move,
                    "partial_action": "resize",
                },
            )
    if not _same_position(before_move, x, y):
        try:
            require_success(
                run_command(
                    [executable, "windowmove", window_argument, str(x), str(y)],
                    env=env,
                    timeout=5.0,
                )
            )
        except InteractionError as exc:
            try:
                effective: dict[str, Any] = read_window_frame_geometry(window_id, env=env)
            except InteractionError as read_error:
                effective = {
                    "available": False,
                    "error": read_error.as_dict(),
                }
            raise InteractionError(
                "window_geometry_partial_failure",
                "KWin could not move the window after resizing; geometry may be partially changed.",
                details={
                    "window_id": window_id,
                    "requested_geometry": _geometry(x, y, width, height),
                    "before_geometry": before,
                    "effective_geometry": effective,
                    "partial_action": "move_failed_after_resize"
                    if not _same_size(before, width, height)
                    else "move_unverified",
                    "move_error": exc.as_dict(),
                },
            ) from exc

    deadline = time.monotonic() + 1.0
    after = read_window_frame_geometry(window_id, env=env)
    while not _matches_geometry(after, x, y, width, height) and time.monotonic() < deadline:
        time.sleep(0.05)
        after = read_window_frame_geometry(window_id, env=env)

    if not _matches_geometry(after, x, y, width, height):
        raise InteractionError(
            "window_geometry_unverified",
            "KWin did not report the requested frame geometry after the change.",
            details={
                "window_id": window_id,
                "requested_geometry": _geometry(x, y, width, height),
                "before_geometry": before,
                "effective_geometry": after,
            },
        )

    return {
        "before_geometry": before,
        "requested_geometry": _geometry(x, y, width, height),
        "effective_geometry": after,
        "changed": not _matches_geometry(before, x, y, width, height),
        "verified": True,
    }


def _window_argument(window_id: str) -> str:
    match = _WINDOW_UUID.fullmatch(window_id.strip())
    if match is None:
        raise InteractionError(
            "invalid_window_id",
            "Window IDs must be UUIDs returned by the selected session.",
            details={"window_id": window_id},
        )
    return "{" + match.group(1).lower() + "}"


def _number(value: str) -> int | float:
    number = float(value)
    return int(number) if number.is_integer() else number


def _validate_geometry(x: int, y: int, width: int, height: int) -> None:
    values = {"x": x, "y": y, "width": width, "height": height}
    if any(isinstance(value, bool) or not isinstance(value, int) for value in values.values()):
        raise InteractionError(
            "invalid_window_geometry",
            "Window geometry must use integer logical desktop pixels.",
            details=values,
        )
    if not (-_GEOMETRY_LIMIT <= x <= _GEOMETRY_LIMIT) or not (
        -_GEOMETRY_LIMIT <= y <= _GEOMETRY_LIMIT
    ):
        raise InteractionError(
            "invalid_window_geometry",
            f"Window position must be within ±{_GEOMETRY_LIMIT} logical pixels.",
            details=values,
        )
    if not (1 <= width <= _GEOMETRY_LIMIT and 1 <= height <= _GEOMETRY_LIMIT):
        raise InteractionError(
            "invalid_window_geometry",
            f"Window dimensions must be from 1 through {_GEOMETRY_LIMIT} logical pixels.",
            details=values,
        )


def _geometry(x: int, y: int, width: int, height: int) -> dict[str, Any]:
    return {
        "x": x,
        "y": y,
        "width": width,
        "height": height,
        "coordinate_space": "logical-desktop-pixels",
        "basis": "kwin-frame",
    }


def _same_position(geometry: Mapping[str, Any], x: int, y: int) -> bool:
    return abs(float(geometry["x"]) - x) <= 1.0 and abs(float(geometry["y"]) - y) <= 1.0


def _same_size(geometry: Mapping[str, Any], width: int, height: int) -> bool:
    return (
        abs(float(geometry["width"]) - width) <= 1.0
        and abs(float(geometry["height"]) - height) <= 1.0
    )


def _matches_geometry(geometry: Mapping[str, Any], x: int, y: int, width: int, height: int) -> bool:
    return _same_position(geometry, x, y) and _same_size(geometry, width, height)

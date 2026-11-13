from __future__ import annotations

import asyncio
import math
import os
import re
import shutil
import stat
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import Any

from dbus_next.aio import MessageBus  # type: ignore[attr-defined]

from kde_desktop_automation.backends.displays import list_displays
from kde_desktop_automation.backends.kwin_controls import (
    pointer_position as read_pointer_position,
)
from kde_desktop_automation.backends.kwin_controls import (
    set_window_frame_geometry,
)
from kde_desktop_automation.backends.kwin_input import (
    DEFAULT_DRAG_DURATION_MS,
    DEFAULT_DRAG_END_PAUSE_MS,
    DEFAULT_DRAG_HOLD_MS,
    InputReceipt,
    KWinFakeInput,
    fake_input_version,
    validate_drag_steps,
    validate_drag_timing,
)
from kde_desktop_automation.backends.screenshots import capture_fullscreen
from kde_desktop_automation.errors import InteractionError
from kde_desktop_automation.process import require_executable, require_success, run_command
from kde_desktop_automation.window_selection import find_one_window

_WINDOW_ID = re.compile(r"^\{?([0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12})\}?$")
_PRIVATE_RUNTIME_MARKER = "/kde-desktop-automation/sessions/"
_PRIVATE_PROCESS_MARKERS = ("KDE_DESKTOP_AUTOMATION_PROCESS_MARKER",)
_KWINDOW_SERVICE = "org.kde.KWin"
_KWINDOW_PATH = "/KWin"
_KWINDOW_INTERFACE = "org.kde.KWin"


@dataclass(frozen=True, slots=True)
class DesktopWindow:
    window_id: str
    title: str
    app_id: str
    resource_name: str
    role: str
    pid: int | None
    x: float
    y: float
    width: float
    height: float
    fullscreen: bool | None
    minimized: bool
    maximized: bool
    active: bool
    has_transient_parent: bool
    desktops: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "window_id": self.window_id,
            "title": self.title,
            "app_id": self.app_id,
            "resource_name": self.resource_name,
            "role": self.role,
            "pid": self.pid,
            "fullscreen": self.fullscreen,
            "minimized": self.minimized,
            "maximized": self.maximized,
            "active": self.active,
            "has_transient_parent": self.has_transient_parent,
            "desktops": list(self.desktops),
            "geometry": {
                "x": self.x,
                "y": self.y,
                "width": self.width,
                "height": self.height,
                "coordinate_space": "logical-desktop-pixels",
            },
            "frame_geometry": None,
            "content_geometry": None,
            "geometry_basis": "kwin-dbus-reported",
        }


def user_desktop_environment_issue(environ: Mapping[str, str] | None = None) -> str | None:
    """Return why this process cannot safely address the live KDE Wayland session."""
    values = os.environ if environ is None else environ
    if any(values.get(marker) for marker in _PRIVATE_PROCESS_MARKERS):
        return "This process belongs to a managed private session, not the user desktop."

    desktop_names = values.get("XDG_CURRENT_DESKTOP", "").lower().split(":")
    if not any("kde" in name or "plasma" in name for name in desktop_names):
        return "The current desktop is not identified as KDE Plasma."
    if values.get("XDG_SESSION_TYPE", "").lower() != "wayland":
        return "Live user-desktop interaction currently requires a KDE Wayland session."

    runtime_value = values.get("XDG_RUNTIME_DIR", "")
    display_value = values.get("WAYLAND_DISPLAY", "")
    bus_address = values.get("DBUS_SESSION_BUS_ADDRESS", "")
    if not runtime_value or not display_value or not bus_address:
        return "XDG_RUNTIME_DIR, WAYLAND_DISPLAY, and DBUS_SESSION_BUS_ADDRESS are required."
    if _PRIVATE_RUNTIME_MARKER in runtime_value or _PRIVATE_RUNTIME_MARKER in bus_address:
        return "The current environment points to a managed private session, not the user desktop."

    runtime_dir = Path(runtime_value)
    if not runtime_dir.is_absolute():
        return "XDG_RUNTIME_DIR must be an absolute path."
    try:
        runtime_stat = runtime_dir.stat()
        if not stat.S_ISDIR(runtime_stat.st_mode) or runtime_stat.st_uid != os.getuid():
            return "XDG_RUNTIME_DIR must be a directory owned by the current user."
        if stat.S_IMODE(runtime_stat.st_mode) & 0o077:
            return "XDG_RUNTIME_DIR must not be accessible to group or other users."
        socket_path = _wayland_socket_path(runtime_dir, display_value)
        socket_stat = socket_path.lstat()
    except (InteractionError, OSError):
        return "The selected user Wayland socket is unavailable."
    if not stat.S_ISSOCK(socket_stat.st_mode) or socket_stat.st_uid != os.getuid():
        return "The selected Wayland endpoint is not a user-owned socket."
    return None


class UserDesktopBackend:
    """Explicit, best-effort access to the current user's live KDE Wayland session."""

    def __init__(
        self,
        environ: Mapping[str, str] | None = None,
        *,
        window_info_reader: Callable[[Sequence[str]], dict[str, dict[str, Any]]] | None = None,
    ) -> None:
        self.environ = dict(os.environ if environ is None else environ)
        issue = user_desktop_environment_issue(self.environ)
        if issue is not None:
            raise InteractionError(
                "user_desktop_unavailable",
                issue,
                details={"target": "user-desktop"},
            )
        self.kdotool = require_executable("kdotool")
        self.environ["LC_ALL"] = "C.UTF-8"
        self._window_info_reader = window_info_reader or partial(
            _query_window_info,
            bus_address=self.environ["DBUS_SESSION_BUS_ADDRESS"],
        )
        self.wayland_display = self.environ["WAYLAND_DISPLAY"]
        self.socket_path = _wayland_socket_path(
            Path(self.environ["XDG_RUNTIME_DIR"]), self.wayland_display
        )

    def list_windows(self) -> dict[str, Any]:
        result = run_command(
            [self.kdotool, "search", "--title", ".*"],
            env=self.environ,
            timeout=5.0,
        )
        require_success(result)
        ids = _parse_window_ids(result.stdout)

        records = self._window_info_reader(ids) if ids else {}
        active_id = self._active_window_id()
        windows = [
            _parse_window_info(window_id, records[window_id], active_id=active_id).to_dict()
            for window_id in ids
            if window_id in records
        ]
        return {
            "target": {"kind": "user-desktop"},
            "observed_at": datetime.now(UTC).isoformat(),
            "count": len(windows),
            "windows": windows,
            "note": "Window metadata is live; IDs can become stale when windows close.",
        }

    def inspect_window(self, window_id: str) -> dict[str, Any]:
        normalized_id = _normalize_window_id(window_id)
        records = self._window_info_reader([normalized_id])
        info = records.get(normalized_id)
        if info is None:
            raise InteractionError(
                "stale_target",
                "The selected window no longer exists in the live KDE session.",
                details={"window_id": normalized_id},
            )
        window = _parse_window_info(
            normalized_id,
            info,
            active_id=self._active_window_id(),
        )
        return {"target": {"kind": "user-desktop"}, "window": window.to_dict()}

    def find_window(
        self,
        *,
        app_id: str | None = None,
        title: str | None = None,
    ) -> dict[str, Any]:
        listing = self.list_windows()
        return {
            "target": {"kind": "user-desktop"},
            "observed_at": listing["observed_at"],
            "window": find_one_window(listing["windows"], app_id=app_id, title=title),
        }

    def activate_window(self, window_id: str) -> dict[str, Any]:
        normalized_id = _normalize_window_id(window_id)
        self.inspect_window(normalized_id)
        result = run_command(
            [self.kdotool, "windowactivate", _dbus_window_id(normalized_id)],
            env=self.environ,
            timeout=5.0,
        )
        require_success(result)
        active_id = self._active_window_id()
        if active_id != normalized_id:
            raise InteractionError(
                "window_activation_unverified",
                "KWin did not report the requested window as active after activation.",
                details={"requested_window_id": normalized_id, "active_window_id": active_id},
            )
        activated_window = self.inspect_window(normalized_id)["window"]
        if not activated_window["active"]:
            raise InteractionError(
                "window_activation_unverified",
                "KWin focus changed before the activated window state could be re-read.",
                details={"requested_window_id": normalized_id},
            )
        return {
            "target": {"kind": "user-desktop", "window_id": normalized_id},
            "action": "window activated",
            "active_window_verified": True,
            "window": activated_window,
            "note": "Activation may switch virtual desktops and changes live user focus.",
        }

    def send_key(self, window_id: str, chord: str) -> dict[str, Any]:
        window, _active_id = self._require_active_window(window_id)
        with KWinFakeInput(
            self.socket_path,
            application_name="kde-desktop user-desktop",
            reason=f"Explicit user-desktop key input to window {window.window_id}.",
        ) as client:
            active_before = self._verify_window_still_active(window.window_id)
            receipt = client.send_key(chord)
        return self._input_result(receipt, window, active_before)

    def send_text(self, window_id: str, text: str) -> dict[str, Any]:
        window, _active_id = self._require_active_window(window_id)
        with KWinFakeInput(
            self.socket_path,
            application_name="kde-desktop user-desktop",
            reason=f"Explicit user-desktop text input to window {window.window_id}.",
        ) as client:
            active_before = self._verify_window_still_active(window.window_id)
            receipt = client.send_text(text)
        return self._input_result(receipt, window, active_before)

    def click_window(
        self,
        window_id: str,
        x: float,
        y: float,
        button: str = "left",
    ) -> dict[str, Any]:
        window, _active_id = self._require_active_window(window_id)
        if not (math.isfinite(x) and math.isfinite(y) and 0.0 <= x <= 1.0 and 0.0 <= y <= 1.0):
            raise InteractionError(
                "coordinates_out_of_bounds",
                "Window-relative click coordinates must be normalized values from 0 to 1.",
                details={"x": x, "y": y},
            )
        with KWinFakeInput(
            self.socket_path,
            application_name="kde-desktop user-desktop",
            reason=f"Explicit user-desktop click to window {window.window_id}.",
        ) as client:
            active_before = self._verify_window_still_active(window.window_id)
            fresh_window = self.inspect_window(window.window_id)["window"]
            if not fresh_window["active"]:
                raise InteractionError(
                    "window_focus_changed",
                    "The selected window was no longer active after its geometry was refreshed.",
                    details={
                        "window_id": window.window_id,
                        "active_window_id": self._active_window_id(),
                    },
                )
            absolute_x = round(
                fresh_window["geometry"]["x"] + (fresh_window["geometry"]["width"] - 1) * x
            )
            absolute_y = round(
                fresh_window["geometry"]["y"] + (fresh_window["geometry"]["height"] - 1) * y
            )
            receipt = client.click(absolute_x, absolute_y, button=button)
        result = self._input_result(receipt, window, active_before)
        result["requested_point"] = {"x": x, "y": y, "space": "normalized-window"}
        result["resolved_point"] = {
            "x": absolute_x,
            "y": absolute_y,
            "space": "logical-desktop-pixels",
            "geometry_basis": fresh_window["geometry_basis"],
        }
        return result

    def click_at(self, x: int, y: int, button: str = "left") -> dict[str, Any]:
        """Submit a pointer click at an explicit logical desktop coordinate."""
        _validate_integer_point(x, y)
        displays = self.list_displays()
        if not _point_is_on_display(x, y, displays):
            raise InteractionError(
                "coordinates_out_of_bounds",
                "The global pointer coordinate is outside every listed display.",
                details={"x": x, "y": y, "displays": displays},
            )
        button_name = button.lower()
        if button_name not in {"left", "middle", "right"}:
            raise InteractionError(
                "invalid_button",
                "Mouse button must be left, middle, or right.",
                details={"button": button},
            )
        active_before = self._observe_active_window_id()
        with KWinFakeInput(
            self.socket_path,
            application_name="kde-desktop user-desktop",
            reason="Explicit user-desktop global pointer click at logical desktop coordinates.",
        ) as client:
            receipt = client.click(x, y, button=button_name)
        active_after = self._observe_active_window_id()
        data = self._global_pointer_result(receipt, active_before, active_after)
        data["requested_point"] = {
            "x": x,
            "y": y,
            "space": "logical-desktop-pixels",
        }
        return data

    def drag_at(
        self,
        start_x: int,
        start_y: int,
        end_x: int,
        end_y: int,
        *,
        button: str = "left",
        steps: int = 8,
        duration_ms: int = DEFAULT_DRAG_DURATION_MS,
        hold_ms: int = DEFAULT_DRAG_HOLD_MS,
        end_pause_ms: int = DEFAULT_DRAG_END_PAUSE_MS,
    ) -> dict[str, Any]:
        """Drag between explicit logical desktop coordinates."""
        validate_drag_steps(steps)
        validate_drag_timing(duration_ms, hold_ms, end_pause_ms)
        _validate_integer_point(start_x, start_y)
        _validate_integer_point(end_x, end_y)
        displays = self.list_displays()
        points = [
            (
                round(start_x + (end_x - start_x) * index / steps),
                round(start_y + (end_y - start_y) * index / steps),
            )
            for index in range(steps + 1)
        ]
        outside = [point for point in points if not _point_is_on_display(*point, displays)]
        if outside:
            raise InteractionError(
                "coordinates_out_of_bounds",
                "Every sampled point in a global drag must be inside a listed display.",
                details={"outside_points": outside, "displays": displays},
            )
        button_name = button.lower()
        if button_name not in {"left", "middle", "right"}:
            raise InteractionError(
                "invalid_button",
                "Mouse button must be left, middle, or right.",
                details={"button": button},
            )
        active_before = self._observe_active_window_id()
        with KWinFakeInput(
            self.socket_path,
            application_name="kde-desktop user-desktop",
            reason="Explicit user-desktop global pointer drag in logical desktop coordinates.",
        ) as client:
            receipt = client.drag(
                start_x,
                start_y,
                end_x,
                end_y,
                button=button_name,
                steps=steps,
                duration_ms=duration_ms,
                hold_ms=hold_ms,
                end_pause_ms=end_pause_ms,
            )
        active_after = self._observe_active_window_id()
        data = self._global_pointer_result(receipt, active_before, active_after)
        data["requested_path"] = {
            "start": {"x": start_x, "y": start_y},
            "end": {"x": end_x, "y": end_y},
            "steps": steps,
            "duration_ms": duration_ms,
            "hold_ms": hold_ms,
            "end_pause_ms": end_pause_ms,
            "space": "logical-desktop-pixels",
        }
        return data

    def set_window_minimized(self, window_id: str, minimized: bool) -> dict[str, Any]:
        """Set and verify a live window's minimized state; this may change focus."""
        if not isinstance(minimized, bool):
            raise InteractionError(
                "invalid_window_state",
                "Minimized state must be explicitly true or false.",
                details={"minimized": minimized},
            )
        normalized_id = _normalize_window_id(window_id)
        before = self.inspect_window(normalized_id)["window"]
        if before["minimized"] != minimized:
            option = "--add" if minimized else "--remove"
            result = run_command(
                [
                    self.kdotool,
                    "windowstate",
                    option,
                    "MINIMIZED",
                    _dbus_window_id(normalized_id),
                ],
                env=self.environ,
                timeout=5.0,
            )
            require_success(result)
        after = self.inspect_window(normalized_id)["window"]
        if after["minimized"] is not minimized:
            raise InteractionError(
                "window_state_unverified",
                "KWin did not report the requested minimized state after the action.",
                details={
                    "window_id": normalized_id,
                    "requested_minimized": minimized,
                    "effective_minimized": after["minimized"],
                },
            )
        return {
            "target": {"kind": "user-desktop", "window_id": normalized_id},
            "action": "set-minimized",
            "minimized": minimized,
            "changed": before["minimized"] != after["minimized"],
            "verified": True,
            "window": after,
            "note": "Changing minimized state may change live user focus.",
        }

    def set_window_geometry(
        self,
        window_id: str,
        x: int,
        y: int,
        width: int,
        height: int,
    ) -> dict[str, Any]:
        """Set and verify the live KWin frame rectangle without activating the window."""
        normalized_id = _normalize_window_id(window_id)
        before = self.inspect_window(normalized_id)["window"]
        if before["fullscreen"] or before["maximized"]:
            raise InteractionError(
                "window_geometry_state_conflict",
                "Restore a maximized or fullscreen window before setting its geometry.",
                details={
                    "window_id": normalized_id,
                    "fullscreen": before["fullscreen"],
                    "maximized": before["maximized"],
                },
            )
        result = set_window_frame_geometry(
            normalized_id,
            x,
            y,
            width,
            height,
            env=self.environ,
        )
        after = self.inspect_window(normalized_id)["window"]
        return result | {
            "target": {"kind": "user-desktop", "window_id": normalized_id},
            "action": "set-geometry",
            "window": after,
            "note": (
                "Geometry is the KWin frame rectangle in logical desktop pixels; "
                "this does not intentionally change focus."
            ),
        }

    def pointer_position(self) -> dict[str, Any]:
        """Read the actual pointer location in the live desktop without moving it."""
        return read_pointer_position(env=self.environ)

    def screenshot(self, output: Path, *, overwrite: bool = False) -> dict[str, Any]:
        capture = capture_fullscreen(output, env=self.environ, overwrite=overwrite)
        return capture | {
            "target": {"kind": "user-desktop"},
            "coordinate_space": "screenshot-pixels",
            "note": (
                "Compare screenshot pixels with live window geometry before coordinate actions."
            ),
        }

    def capabilities(self) -> dict[str, Any]:
        version: int | None
        input_reason: str | None = None
        window_reason: str | None = None
        try:
            self.list_windows()
            window_available = True
        except InteractionError as exc:
            window_available = False
            window_reason = exc.message
        try:
            version = fake_input_version(self.socket_path)
            if version is None:
                input_reason = "The live KWin session does not advertise fake input."
        except InteractionError as exc:
            version = None
            input_reason = exc.message
        key_available = window_available and version is not None and version >= 6
        click_available = window_available and version is not None and version >= 3
        global_pointer_available = version is not None and version >= 3
        return {
            "target": {"kind": "user-desktop"},
            "capabilities": {
                "window.list": window_available,
                "window.find": window_available,
                "window.wait": window_available,
                "window.inspect": window_available,
                "window.activate": window_available,
                "window.set_minimized": window_available,
                "window.set_geometry": window_available and shutil.which("kdotool") is not None,
                "desktop.observe": window_available and shutil.which("wlpdisplays") is not None,
                "input.pointer_position": shutil.which("kdotool") is not None,
                "display.inspect": shutil.which("wlpdisplays") is not None,
                "screenshot.capture": shutil.which("spectacle") is not None,
                "input.key": key_available,
                "input.text": key_available,
                "input.click": click_available,
                "input.inject": version is not None and version >= 3,
                "input.focus_guard": click_available,
                "input.active_window_guard": window_available
                and (key_available or click_available),
                "input.global_click": global_pointer_available
                and shutil.which("wlpdisplays") is not None,
                "input.global_drag": global_pointer_available
                and shutil.which("wlpdisplays") is not None,
                "input.effect_ack": False,
                "media.players": shutil.which("playerctl") is not None,
                "media.inspect": shutil.which("playerctl") is not None,
                "media.control": shutil.which("playerctl") is not None,
                "media.seek_to": shutil.which("playerctl") is not None,
                "audio.devices": shutil.which("wpctl") is not None
                and shutil.which("pactl") is not None,
                "audio.default": shutil.which("wpctl") is not None
                and shutil.which("pactl") is not None,
                "audio.volume": shutil.which("wpctl") is not None
                and shutil.which("pactl") is not None,
                "audio.mute": shutil.which("wpctl") is not None
                and shutil.which("pactl") is not None,
                "audio.route_stream": shutil.which("wpctl") is not None
                and shutil.which("pactl") is not None,
            },
            "input_protocol_version": version,
            "input_semantics": {
                "routing": "KWin fake input through the live compositor's active focus",
                "window_targeting": "active-window-guard; focus can still change during submission",
                "global_pointer": (
                    "logical desktop coordinates; not window-bound; may move the pointer "
                    "or change the active window"
                ),
                "atomic_window_routing": False,
                "delivery_acknowledged": False,
            },
            "capability_probe": {
                "window_management": "read-only window listing and KWin D-Bus metadata query",
                "display": "wlpdisplays executable presence only",
                "screenshot": "Spectacle executable presence only",
            },
            "window_reason": window_reason,
            "limitations": [
                "KWin fake input may be ignored; submission is not proof of application delivery",
                "fake input is routed through live compositor focus and is not atomically "
                "bound to a window",
                "window clicks use calibrated window-relative coordinates, not semantic controls",
            ],
            "input_reason": input_reason,
        }

    def list_displays(self) -> list[dict[str, Any]]:
        return [display.to_dict() for display in list_displays(env=self.environ)]

    def _require_active_window(self, window_id: str) -> tuple[DesktopWindow, str]:
        normalized_id = _normalize_window_id(window_id)
        records = self._window_info_reader([normalized_id])
        info = records.get(normalized_id)
        if info is None:
            raise InteractionError(
                "stale_target",
                "The selected window no longer exists in the live KDE session.",
                details={"window_id": normalized_id},
            )
        window = _parse_window_info(normalized_id, info, active_id=None)
        active_id = self._active_window_id()
        if active_id != normalized_id:
            raise InteractionError(
                "window_not_active",
                "Input requires the selected window to already be active. "
                "Activate it explicitly first.",
                details={"window_id": normalized_id, "active_window_id": active_id},
            )
        return window, active_id

    def _verify_window_still_active(self, window_id: str) -> str:
        active_id = self._active_window_id()
        if active_id != window_id:
            raise InteractionError(
                "window_focus_changed",
                "The active window changed before input submission; no input was sent.",
                details={"window_id": window_id, "active_window_id": active_id},
            )
        return active_id

    def _input_result(
        self,
        receipt: InputReceipt,
        window: DesktopWindow,
        active_before: str,
    ) -> dict[str, Any]:
        active_after = self._active_window_id()
        data = receipt.to_dict(
            session_id=None,
            wayland_display=self.wayland_display,
            target_kind="user-desktop",
        )
        data["target"]["window_id"] = window.window_id
        data["target"]["keyboard_focus"] = (
            "active KWin window verified immediately before submission"
        )
        data["target"]["focus_before"] = active_before
        data["focus_after"] = active_after
        data["focus_still_matches_target"] = active_after == window.window_id
        data["input_semantics"] = {
            "targeting": "active-window-guard",
            "focus_verified_before_submission": True,
            "atomic_window_routing": False,
            "delivery_acknowledged": False,
        }
        if active_after != window.window_id:
            data["note"] += (
                " KWin focus changed during the action; delivery to the selected window is unknown."
            )
        return data

    def _global_pointer_result(
        self,
        receipt: InputReceipt,
        active_before: str | None,
        active_after: str | None,
    ) -> dict[str, Any]:
        data = receipt.to_dict(
            session_id=None,
            wayland_display=self.wayland_display,
            target_kind="user-desktop",
        )
        data["target"]["keyboard_focus"] = "not targeted by this global pointer action"
        data["active_window_before"] = active_before
        data["active_window_after"] = active_after
        data["focus_change_observed"] = (
            None if active_before is None or active_after is None else active_before != active_after
        )
        data["input_routing"] = "global pointer coordinates; not directly window-bound"
        data["input_semantics"] = {
            "targeting": "global-coordinate",
            "atomic_window_routing": False,
            "delivery_acknowledged": False,
        }
        data["note"] += (
            " This global pointer action may move the pointer or change the active window; "
            "inspect the desktop to verify its effect."
        )
        return data

    def _active_window_id(self) -> str:
        result = run_command(
            [self.kdotool, "getactivewindow"],
            env=self.environ,
            timeout=5.0,
        )
        require_success(result)
        output = result.stdout.strip().splitlines()
        if not output:
            raise InteractionError(
                "active_window_unavailable",
                "KWin did not return the current active window ID.",
            )
        return _normalize_window_id(output[-1])

    def _observe_active_window_id(self) -> str | None:
        try:
            return self._active_window_id()
        except InteractionError:
            return None


def _query_window_info(
    window_ids: Sequence[str],
    *,
    bus_address: str,
) -> dict[str, dict[str, Any]]:
    return asyncio.run(_query_window_info_async(window_ids, bus_address=bus_address))


async def _query_window_info_async(
    window_ids: Sequence[str],
    *,
    bus_address: str,
) -> dict[str, dict[str, Any]]:
    try:
        bus = await MessageBus(bus_address=bus_address).connect()
    except Exception as exc:
        raise InteractionError(
            "kwin_dbus_unavailable",
            "Could not connect to the current user's KWin D-Bus service.",
            details={"reason": str(exc)},
        ) from exc

    records: dict[str, dict[str, Any]] = {}
    try:
        introspection = await bus.introspect(_KWINDOW_SERVICE, _KWINDOW_PATH)
        proxy = bus.get_proxy_object(_KWINDOW_SERVICE, _KWINDOW_PATH, introspection)
        interface = proxy.get_interface(_KWINDOW_INTERFACE)
        for window_id in window_ids:
            raw = await interface.call_get_window_info(  # type: ignore[attr-defined]
                _dbus_window_id(window_id)
            )
            if raw:
                records[window_id] = {
                    str(key): getattr(value, "value", value) for key, value in raw.items()
                }
    except Exception as exc:
        raise InteractionError(
            "kwin_dbus_query_failed",
            "KWin could not provide live window metadata.",
            details={"reason": str(exc)},
        ) from exc
    finally:
        bus.disconnect()  # type: ignore[no-untyped-call]
    return records


def _parse_window_info(
    window_id: str,
    info: Mapping[str, Any],
    *,
    active_id: str | None,
) -> DesktopWindow:
    returned_id = info.get("uuid")
    if returned_id is not None and _normalize_window_id(str(returned_id)) != window_id:
        raise InteractionError(
            "invalid_backend_data",
            "KWin returned window metadata for a different window ID.",
            details={"requested_window_id": window_id, "returned_window_id": str(returned_id)},
        )
    try:
        x, y = float(info["x"]), float(info["y"])
        width, height = float(info["width"]), float(info["height"])
    except (KeyError, TypeError, ValueError) as exc:
        raise InteractionError(
            "invalid_backend_data",
            "KWin returned a window without valid geometry.",
            details={"window_id": window_id},
        ) from exc
    if (
        not all(math.isfinite(value) for value in (x, y, width, height))
        or width <= 0
        or height <= 0
    ):
        raise InteractionError(
            "invalid_backend_data",
            "KWin returned invalid window geometry.",
            details={"window_id": window_id},
        )
    desktop_ids = info.get("desktops", [])
    if not isinstance(desktop_ids, (list, tuple)):
        desktop_ids = []
    raw_pid = info.get("pid")
    try:
        pid = None if raw_pid is None else int(raw_pid)
    except (TypeError, ValueError):
        pid = None
    raw_fullscreen = info.get("fullscreen")
    fullscreen = None if raw_fullscreen is None else bool(raw_fullscreen)
    try:
        maximized = (
            int(info.get("maximizeHorizontal", 0)) == 1
            and int(info.get("maximizeVertical", 0)) == 1
        )
    except (TypeError, ValueError):
        maximized = False
    return DesktopWindow(
        window_id=window_id,
        title=str(info.get("caption", "")),
        app_id=str(info.get("desktopFile") or info.get("resourceClass") or ""),
        resource_name=str(info.get("resourceName", "")),
        role=str(info.get("role", "")),
        pid=pid,
        x=x,
        y=y,
        width=width,
        height=height,
        fullscreen=fullscreen,
        minimized=bool(info.get("minimized", False)),
        maximized=maximized,
        active=active_id == window_id,
        has_transient_parent=bool(info.get("hasTransientParent", False)),
        desktops=tuple(str(item) for item in desktop_ids),
    )


def _parse_window_ids(output: str) -> list[str]:
    ids: list[str] = []
    seen: set[str] = set()
    for line in output.splitlines():
        candidate = line.strip()
        if not candidate:
            continue
        window_id = _normalize_window_id(candidate)
        if window_id not in seen:
            ids.append(window_id)
            seen.add(window_id)
    return ids


def _normalize_window_id(window_id: str) -> str:
    match = _WINDOW_ID.fullmatch(window_id.strip())
    if match is None:
        raise InteractionError(
            "invalid_window_id",
            "Window IDs must be UUIDs returned by the current user-desktop window list.",
            details={"window_id": window_id},
        )
    return match.group(1).lower()


def _dbus_window_id(window_id: str) -> str:
    return "{" + _normalize_window_id(window_id) + "}"


def _validate_integer_point(x: int, y: int) -> None:
    if (
        isinstance(x, bool)
        or not isinstance(x, int)
        or isinstance(y, bool)
        or not isinstance(y, int)
    ):
        raise InteractionError(
            "invalid_coordinates",
            "Global pointer coordinates must be integer logical desktop pixels.",
            details={"x": x, "y": y},
        )


def _point_is_on_display(x: int, y: int, displays: Sequence[Mapping[str, Any]]) -> bool:
    for display in displays:
        if display.get("coordinate_space") != "logical-desktop-pixels":
            continue
        try:
            display_x = display["x"]
            display_y = display["y"]
            width = display["width"]
            height = display["height"]
        except KeyError:
            continue
        if not all(
            isinstance(value, (int, float)) and not isinstance(value, bool)
            for value in (display_x, display_y, width, height)
        ):
            continue
        if (
            width > 0
            and height > 0
            and display_x <= x < display_x + width
            and display_y <= y < display_y + height
        ):
            return True
    return False


def _wayland_socket_path(runtime_dir: Path, display: str) -> Path:
    display_path = Path(display)
    if display_path.is_absolute():
        return display_path
    if display_path.name != display or display in {".", ".."}:
        raise InteractionError(
            "unsafe_wayland_socket",
            "WAYLAND_DISPLAY must be a socket name or an absolute socket path.",
        )
    return runtime_dir / display_path

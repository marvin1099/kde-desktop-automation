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

from kde_interaction.backends.displays import list_displays
from kde_interaction.backends.kwin_input import InputReceipt, KWinFakeInput, fake_input_version
from kde_interaction.backends.screenshots import capture_fullscreen
from kde_interaction.errors import InteractionError
from kde_interaction.process import require_executable, require_success, run_command
from kde_interaction.window_selection import find_one_window

_WINDOW_ID = re.compile(r"^\{?([0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12})\}?$")
_PRIVATE_RUNTIME_MARKER = "/kde-interaction/sessions/"
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
    if values.get("KDE_AI_PROCESS_MARKER"):
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
            application_name="kde-ai user-desktop",
            reason=f"Explicit user-desktop key input to window {window.window_id}.",
        ) as client:
            active_before = self._verify_window_still_active(window.window_id)
            receipt = client.send_key(chord)
        return self._input_result(receipt, window, active_before)

    def send_text(self, window_id: str, text: str) -> dict[str, Any]:
        window, _active_id = self._require_active_window(window_id)
        with KWinFakeInput(
            self.socket_path,
            application_name="kde-ai user-desktop",
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
            application_name="kde-ai user-desktop",
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
        return {
            "target": {"kind": "user-desktop"},
            "capabilities": {
                "window.list": window_available,
                "window.find": window_available,
                "window.wait": window_available,
                "window.inspect": window_available,
                "window.activate": window_available,
                "display.inspect": shutil.which("wlpdisplays") is not None,
                "screenshot.capture": shutil.which("spectacle") is not None,
                "input.key": key_available,
                "input.text": key_available,
                "input.click": click_available,
                "input.inject": version is not None and version >= 3,
                "input.focus_guard": click_available,
                "input.window_targeted": click_available,
                "input.effect_ack": False,
                "media.players": shutil.which("playerctl") is not None,
                "media.inspect": shutil.which("playerctl") is not None,
                "media.control": shutil.which("playerctl") is not None,
            },
            "input_protocol_version": version,
            "input_semantics": {
                "routing": "KWin fake input through the live compositor's active focus",
                "window_targeting": "active-window-guard; focus can still change during submission",
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

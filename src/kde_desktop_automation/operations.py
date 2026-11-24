from __future__ import annotations

import os
import time
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from math import isfinite
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

from kde_desktop_automation.backends.kwin_input import (
    DEFAULT_DRAG_DURATION_MS,
    DEFAULT_DRAG_END_PAUSE_MS,
    DEFAULT_DRAG_HOLD_MS,
)
from kde_desktop_automation.backends.media_players import MprisBackend
from kde_desktop_automation.backends.pipewire_audio import PipeWireAudioBackend
from kde_desktop_automation.backends.power_inhibitors import (
    DEFAULT_POWER_INHIBITOR_SECONDS,
    PowerInhibitorManager,
    power_inhibitor_capabilities,
)
from kde_desktop_automation.backends.user_desktop import UserDesktopBackend
from kde_desktop_automation.errors import InteractionError
from kde_desktop_automation.paths import RuntimePaths
from kde_desktop_automation.process import find_executable
from kde_desktop_automation.sessions import SessionManager

USER_DESKTOP = "user-desktop"
_DISPLAY_COVERAGE_TOLERANCE = 2.0


class DesktopOperations:
    """Shared target-aware desktop operations used by CLI and MCP transports."""

    def __init__(
        self,
        session: str | None = None,
        *,
        manager: SessionManager | None = None,
        manager_factory: Callable[[], SessionManager] | None = None,
        user_desktop: UserDesktopBackend | None = None,
        user_desktop_factory: Callable[[], UserDesktopBackend] | None = None,
        power_inhibitor_factory: (
            Callable[[Path, Mapping[str, str]], PowerInhibitorManager] | None
        ) = None,
    ) -> None:
        self._power_inhibitor_factory = power_inhibitor_factory or PowerInhibitorManager
        self._session_id: str | None
        self._manager: SessionManager | None
        self._user_desktop: UserDesktopBackend | None
        if session == USER_DESKTOP:
            self._session_id = None
            self._manager = None
            self._user_desktop = user_desktop or (user_desktop_factory or UserDesktopBackend)()
            return

        self._manager = manager or (manager_factory or self._default_manager)()
        selected = session
        if selected is None:
            configured_session = os.environ.get("KDE_DESKTOP_SESSION")
            if configured_session == USER_DESKTOP:
                raise InteractionError(
                    "user_desktop_requires_explicit_selection",
                    "KDE_DESKTOP_SESSION may select only a managed private session. "
                    "Select the live desktop explicitly with --session user-desktop "
                    "or session='user-desktop' in MCP.",
                )
            selected = (
                configured_session
                if configured_session is not None
                else self._manager.default_session_id()
            )
            if selected is None:
                raise InteractionError(
                    "private_default_session_unset",
                    "No private session is selected. Set KDE_DESKTOP_SESSION, configure a "
                    "private default, or pass a session ID (--session in CLI); live "
                    "desktop access always requires explicit selection.",
                )
        if not isinstance(selected, str) or not selected:
            raise InteractionError(
                "invalid_session_selector",
                "The session selector must be a managed session ID or 'user-desktop'.",
            )
        self._session_id = selected
        self._user_desktop = None

    @staticmethod
    def _default_manager() -> SessionManager:
        return SessionManager(RuntimePaths.from_environment())

    @property
    def session_id(self) -> str | None:
        return self._session_id

    def window_list(self) -> dict[str, Any]:
        if self._user_desktop is not None:
            return self._user_desktop.list_windows()
        return self._private_manager().list_windows(self._private_session_id())

    def window_find(self, *, app_id: str | None, title: str | None) -> dict[str, Any]:
        if self._user_desktop is not None:
            return self._user_desktop.find_window(app_id=app_id, title=title)
        return self._private_manager().find_window(
            self._private_session_id(), app_id=app_id, title=title
        )

    def window_inspect(self, window_id: str) -> dict[str, Any]:
        if self._user_desktop is not None:
            return self._user_desktop.inspect_window(window_id)
        return self._private_manager().inspect_window(self._private_session_id(), window_id)

    def window_activate(
        self,
        window_id: str,
        *,
        expected_app_id: str | None = None,
        expected_pid: int | None = None,
    ) -> dict[str, Any]:
        self._check_window_precondition(
            window_id,
            expected_app_id=expected_app_id,
            expected_pid=expected_pid,
        )
        if self._user_desktop is not None:
            return self._user_desktop.activate_window(window_id)
        return self._private_manager().activate_window(self._private_session_id(), window_id)

    def window_set_minimized(
        self,
        window_id: str,
        minimized: bool,
        *,
        expected_app_id: str | None = None,
        expected_pid: int | None = None,
    ) -> dict[str, Any]:
        self._check_window_precondition(
            window_id,
            expected_app_id=expected_app_id,
            expected_pid=expected_pid,
        )
        if self._user_desktop is not None:
            return self._user_desktop.set_window_minimized(window_id, minimized)
        return self._private_manager().set_window_minimized(
            self._private_session_id(), window_id, minimized
        )

    def window_set_fullscreen(
        self,
        window_id: str,
        fullscreen: bool,
        *,
        expected_app_id: str | None = None,
        expected_pid: int | None = None,
    ) -> dict[str, Any]:
        self._check_window_precondition(
            window_id,
            expected_app_id=expected_app_id,
            expected_pid=expected_pid,
        )
        if self._user_desktop is not None:
            return self._user_desktop.set_window_fullscreen(window_id, fullscreen)
        return self._private_manager().set_window_fullscreen(
            self._private_session_id(), window_id, fullscreen
        )

    def window_set_maximized(
        self,
        window_id: str,
        maximized: bool,
        *,
        expected_app_id: str | None = None,
        expected_pid: int | None = None,
    ) -> dict[str, Any]:
        self._check_window_precondition(
            window_id,
            expected_app_id=expected_app_id,
            expected_pid=expected_pid,
        )
        if self._user_desktop is not None:
            return self._user_desktop.set_window_maximized(window_id, maximized)
        return self._private_manager().set_window_maximized(
            self._private_session_id(), window_id, maximized
        )

    def window_set_geometry(
        self,
        window_id: str,
        x: int,
        y: int,
        width: int,
        height: int,
        *,
        expected_app_id: str | None = None,
        expected_pid: int | None = None,
    ) -> dict[str, Any]:
        self._check_window_precondition(
            window_id,
            expected_app_id=expected_app_id,
            expected_pid=expected_pid,
        )
        if self._user_desktop is not None:
            return self._user_desktop.set_window_geometry(window_id, x, y, width, height)
        return self._private_manager().set_window_geometry(
            self._private_session_id(), window_id, x, y, width, height
        )

    def pointer_position(self) -> dict[str, Any]:
        if self._user_desktop is not None:
            position = self._user_desktop.pointer_position()
        else:
            position = self._private_manager().pointer_position(self._private_session_id())
        displays = self.display_list()["displays"]
        position["display_name"] = _display_for_point(position, displays)
        return position

    def observe(
        self,
        *,
        screenshot_output: Path | None = None,
        overwrite: bool = False,
    ) -> dict[str, Any]:
        """Collect one target-scoped desktop snapshot for an inspect/act/verify loop."""
        windows_result = self.window_list()
        displays_result = self.display_list()
        windows = windows_result["windows"]
        displays = displays_result["displays"]
        screenshot: dict[str, Any] | None = None
        mapping: dict[str, Any] | None = None
        if screenshot_output is not None:
            screenshot = self.screenshot(screenshot_output, overwrite=overwrite)
            mapping = _screenshot_mapping(screenshot, displays)

        try:
            pointer: dict[str, Any] = {"available": True} | self.pointer_position()
        except InteractionError as exc:
            pointer = {
                "available": False,
                "error": {"code": exc.code, "message": exc.message},
            }

        active_window = next((window for window in windows if window.get("active") is True), None)
        return {
            "target": self._target(),
            "snapshot_id": f"snapshot-{uuid4().hex}",
            "observed_at": datetime.now(UTC).isoformat(),
            "interaction_context": {
                "keyboard_focus_window_id": (
                    None if active_window is None else active_window.get("window_id")
                ),
                "focus_semantics": "KWin active window; focused child control is not exposed",
                "pointer": pointer,
            },
            "window_count": len(windows),
            "windows": windows,
            "display_count": len(displays),
            "displays": displays,
            "screenshot": screenshot,
            "screenshot_mapping": mapping,
            "limitations": [
                "Accessibility elements and focused child controls are not currently exposed.",
                "A screenshot-to-desktop transform is only returned for a single display.",
            ],
        }

    def window_wait(
        self,
        *,
        app_id: str | None,
        title: str | None,
        timeout: float = 10.0,
        fullscreen: bool = False,
        fills_display: bool = False,
    ) -> dict[str, Any]:
        if not _is_finite_number(timeout) or timeout < 0.1 or timeout > 60.0:
            raise InteractionError(
                "invalid_timeout",
                "Window wait timeout must be between 0.1 and 60 seconds.",
                details={"timeout_seconds": timeout},
            )
        if fullscreen and fills_display:
            raise InteractionError(
                "invalid_wait_predicate",
                "Choose either KWin fullscreen state or geometry that fills a display.",
                details={"fullscreen": fullscreen, "fills_display": fills_display},
            )
        wait_condition: dict[str, bool] | None = None
        if fullscreen:
            wait_condition = {"fullscreen": True}
        elif fills_display:
            wait_condition = {"fills_display": True}
        started = time.monotonic()
        deadline = started + timeout
        last_observation: dict[str, Any] | None = None
        while True:
            try:
                result = self.window_find(app_id=app_id, title=title)
                window = result["window"]
                display_records = self.display_list()["displays"] if fills_display else []
                matched_displays = (
                    _displays_covered_by_window(window, display_records) if fills_display else []
                )
                last_observation = result | {"matched_displays": matched_displays}
                if (
                    wait_condition is None
                    or (fullscreen and window.get("fullscreen") is True)
                    or (fills_display and bool(matched_displays))
                ):
                    waited_result = result | {
                        "waited_seconds": round(time.monotonic() - started, 3)
                    }
                    if wait_condition is not None:
                        waited_result["wait_condition"] = wait_condition
                    if fills_display:
                        waited_result["matched_displays"] = matched_displays
                    return waited_result
            except InteractionError as exc:
                if exc.code != "window_not_found":
                    raise
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise InteractionError(
                    "window_wait_timeout",
                    "The requested window state was not observed before the timeout.",
                    details={
                        "app_id": app_id,
                        "title": title,
                        "timeout_seconds": timeout,
                        "session": self._session_id or USER_DESKTOP,
                        "wait_condition": wait_condition,
                        "last_observation": last_observation,
                    },
                ) from None
            time.sleep(min(0.2, remaining))

    def input_key(
        self,
        window_id: str,
        chord: str,
        *,
        expected_app_id: str | None = None,
        expected_pid: int | None = None,
    ) -> dict[str, Any]:
        self._check_window_precondition(
            window_id,
            expected_app_id=expected_app_id,
            expected_pid=expected_pid,
        )
        if self._user_desktop is not None:
            return self._user_desktop.send_key(window_id, chord)
        return self._private_manager().send_key_to_window(
            self._private_session_id(), window_id, chord
        )

    def input_type(
        self,
        window_id: str,
        text: str,
        *,
        expected_app_id: str | None = None,
        expected_pid: int | None = None,
    ) -> dict[str, Any]:
        self._check_window_precondition(
            window_id,
            expected_app_id=expected_app_id,
            expected_pid=expected_pid,
        )
        if self._user_desktop is not None:
            return self._user_desktop.send_text(window_id, text)
        return self._private_manager().send_text_to_window(
            self._private_session_id(), window_id, text
        )

    def input_click(
        self,
        window_id: str,
        x: float,
        y: float,
        *,
        button: str = "left",
        expected_app_id: str | None = None,
        expected_pid: int | None = None,
    ) -> dict[str, Any]:
        self._check_window_precondition(
            window_id,
            expected_app_id=expected_app_id,
            expected_pid=expected_pid,
        )
        if self._user_desktop is not None:
            return self._user_desktop.click_window(window_id, x, y, button=button)
        return self._private_manager().click_window(
            self._private_session_id(), window_id, x, y, button=button
        )

    def input_click_at(
        self,
        x: int,
        y: int,
        *,
        button: str = "left",
    ) -> dict[str, Any]:
        if self._user_desktop is not None:
            return self._user_desktop.click_at(x, y, button=button)
        return self._private_manager().click(self._private_session_id(), x, y, button=button)

    def input_drag(
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
        if self._user_desktop is not None:
            return self._user_desktop.drag_at(
                start_x,
                start_y,
                end_x,
                end_y,
                button=button,
                steps=steps,
                duration_ms=duration_ms,
                hold_ms=hold_ms,
                end_pause_ms=end_pause_ms,
            )
        return self._private_manager().drag(
            self._private_session_id(),
            start_x,
            start_y,
            end_x,
            end_y,
            button=button,
            steps=steps,
            duration_ms=duration_ms,
            hold_ms=hold_ms,
            end_pause_ms=end_pause_ms,
        )

    def display_list(self) -> dict[str, Any]:
        if self._user_desktop is not None:
            displays = self._user_desktop.list_displays()
            return {
                "target": {"kind": USER_DESKTOP},
                "count": len(displays),
                "displays": displays,
            }
        session_id = self._private_session_id()
        displays = self._private_manager().displays(session_id)
        return {
            "target": {"kind": "agent-session", "session_id": session_id},
            "count": len(displays),
            "displays": displays,
        }

    def screenshot(self, output: Path, *, overwrite: bool = False) -> dict[str, Any]:
        if self._user_desktop is not None:
            return self._user_desktop.screenshot(output, overwrite=overwrite)
        return self._private_manager().screenshot(
            self._private_session_id(), output, overwrite=overwrite
        )

    def screenshot_window(
        self,
        window_id: str,
        output: Path,
        *,
        overwrite: bool = False,
        expected_app_id: str | None = None,
        expected_pid: int | None = None,
    ) -> dict[str, Any]:
        """Capture an exact window directly without activating it."""
        before = self.window_inspect(window_id)["window"]
        self._check_window_precondition(
            window_id,
            expected_app_id=expected_app_id,
            expected_pid=expected_pid,
            observed_window=before,
        )
        if self._user_desktop is not None:
            capture = self._user_desktop.screenshot_window(
                window_id,
                output,
                overwrite=overwrite,
            )
        else:
            capture = self._private_manager().screenshot_window(
                self._private_session_id(),
                window_id,
                output,
                overwrite=overwrite,
            )
        after = self.window_inspect(window_id)["window"]
        if before.get("window_id") != after.get("window_id"):
            raise InteractionError(
                "screenshot_target_changed",
                "The selected window identity changed while KWin captured it.",
                details={
                    "window_id": window_id,
                    "output": str(output),
                    "window_id_after_capture": after.get("window_id"),
                },
            )

        target = capture.get("target")
        if not isinstance(target, dict):
            target = {}
        return capture | {
            "target": target | {"window_id": after["window_id"]},
            "window": after,
            "focus_changed": before.get("active") != after.get("active"),
            "target_window_verified": True,
            "note": (
                "KWin Screenshot2 captured the selected window directly without activating it. "
                "Focus may still change independently during capture. Window decorations are "
                "included."
            ),
        }

    def capabilities(self) -> dict[str, Any]:
        capabilities = (
            _media_capabilities()
            | _audio_capabilities(live_target=self._user_desktop is not None)
            | power_inhibitor_capabilities(live_target=self._user_desktop is not None)
        )
        if self._user_desktop is not None:
            result = self._user_desktop.capabilities()
            result["capabilities"] = result["capabilities"] | capabilities
            return result
        session = self._private_manager().get_session(self._private_session_id())
        if session["status"] != "running":
            raise InteractionError(
                "session_not_running",
                "The configured private target is not running.",
                details={"session_id": self._private_session_id(), "status": session["status"]},
            )
        return {
            "target": {"kind": "agent-session", "session_id": self._private_session_id()},
            "capabilities": session["capabilities"] | capabilities,
            "window_management_protocol_version": session["window_management_protocol_version"],
            "input_protocol_version": session["input_protocol_version"],
            "limitations": [
                *session["limitations"],
                "MPRIS uses playerctl on the selected session bus; "
                "player availability is checked per call",
            ],
        }

    def _check_window_precondition(
        self,
        window_id: str,
        *,
        expected_app_id: str | None = None,
        expected_pid: int | None = None,
        observed_window: dict[str, Any] | None = None,
    ) -> None:
        if expected_app_id is None and expected_pid is None:
            return
        if expected_app_id is not None and (
            not isinstance(expected_app_id, str)
            or not expected_app_id
            or len(expected_app_id) > 512
        ):
            raise InteractionError(
                "invalid_window_precondition",
                "Expected app ID must be a non-empty string of at most 512 characters.",
                details={"expected_app_id": expected_app_id},
            )
        if expected_pid is not None and (
            isinstance(expected_pid, bool) or not isinstance(expected_pid, int) or expected_pid <= 0
        ):
            raise InteractionError(
                "invalid_window_precondition",
                "Expected PID must be a positive integer.",
                details={"expected_pid": expected_pid},
            )

        current = observed_window or self.window_inspect(window_id)["window"]
        observed = {"app_id": current.get("app_id"), "pid": current.get("pid")}
        expected = {"app_id": expected_app_id, "pid": expected_pid}
        mismatches = {
            field: {"expected": value, "observed": observed[field]}
            for field, value in expected.items()
            if value is not None and observed[field] != value
        }
        if mismatches:
            raise InteractionError(
                "stale_target",
                "The selected window no longer matches the supplied identity precondition.",
                details={
                    "window_id": current.get("window_id", window_id),
                    "expected_identity": {
                        key: value for key, value in expected.items() if value is not None
                    },
                    "observed_identity": observed,
                    "mismatches": mismatches,
                },
            )

    def media_players(self) -> dict[str, Any]:
        return self._media_backend().list_players(target=self._target())

    def media_inspect(self, player: str) -> dict[str, Any]:
        return self._media_backend().inspect_player(player, target=self._target())

    def media_control(
        self,
        player: str,
        action: str,
        *,
        seconds: float | None = None,
    ) -> dict[str, Any]:
        return self._media_backend().control_player(
            player,
            action,
            target=self._target(),
            seconds=seconds,
        )

    def media_seek_to(self, player: str, position_seconds: float) -> dict[str, Any]:
        return self._media_backend().seek_to_player(
            player,
            position_seconds,
            target=self._target(),
        )

    def media_seek_by(self, player: str, seconds: float) -> dict[str, Any]:
        return self._media_backend().seek_by_player(
            player,
            seconds,
            target=self._target(),
        )

    def media_wait_for_status(
        self,
        player: str,
        expected_status: str,
        *,
        timeout: float = 10.0,
    ) -> dict[str, Any]:
        return self._media_backend().wait_for_status(
            player,
            expected_status,
            timeout=timeout,
            target=self._target(),
        )

    def audio_list(self) -> dict[str, Any]:
        return self._pipewire_audio_backend().list_audio(target=self._target())

    def audio_set_default(self, direction: str, device_name: str) -> dict[str, Any]:
        return self._pipewire_audio_backend().set_default(
            direction,
            device_name,
            target=self._target(),
        )

    def audio_set_volume(
        self,
        direction: str,
        device_name: str,
        volume_percent: float,
    ) -> dict[str, Any]:
        return self._pipewire_audio_backend().set_volume(
            direction,
            device_name,
            volume_percent,
            target=self._target(),
        )

    def audio_set_mute(self, direction: str, device_name: str, muted: bool) -> dict[str, Any]:
        return self._pipewire_audio_backend().set_mute(
            direction,
            device_name,
            muted,
            target=self._target(),
        )

    def audio_wait_for_mute(
        self,
        direction: str,
        device_name: str,
        muted: bool,
        *,
        timeout: float = 10.0,
    ) -> dict[str, Any]:
        return self._pipewire_audio_backend().wait_for_mute(
            direction,
            device_name,
            muted,
            timeout=timeout,
            target=self._target(),
        )

    def audio_route_stream(
        self,
        stream_id: int,
        sink_name: str,
        *,
        application_name: str,
        process_id: int | None = None,
    ) -> dict[str, Any]:
        return self._pipewire_audio_backend().route_stream(
            stream_id,
            sink_name,
            application_name=application_name,
            process_id=process_id,
            target=self._target(),
        )

    def power_inhibit(
        self,
        inhibitor: str,
        *,
        duration_seconds: int = DEFAULT_POWER_INHIBITOR_SECONDS,
        reason: str | None = None,
    ) -> dict[str, Any]:
        result = self._power_inhibitor_backend().inhibit(
            inhibitor,
            duration_seconds=duration_seconds,
            reason=reason,
        )
        return result | {"target": self._target()}

    def power_list_inhibitors(self) -> dict[str, Any]:
        inhibitors = self._power_inhibitor_backend().list()
        return {
            "target": self._target(),
            "count": len(inhibitors),
            "inhibitors": inhibitors,
        }

    def power_release_inhibitor(self, inhibitor_id: str) -> dict[str, Any]:
        result = self._power_inhibitor_backend().release(inhibitor_id)
        return result | {"target": self._target()}

    def _private_manager(self) -> SessionManager:
        if self._manager is None:
            raise InteractionError("invalid_target", "No private session manager is selected.")
        return self._manager

    def _private_session_id(self) -> str:
        if self._session_id is None:
            raise InteractionError("invalid_target", "No managed private session is selected.")
        return self._session_id

    def _media_backend(self) -> MprisBackend:
        if self._user_desktop is not None:
            environ = self._user_desktop.environ
        else:
            environ = self._private_manager().runtime_environment(self._private_session_id())
        return MprisBackend(environ)

    def _pipewire_audio_backend(self) -> PipeWireAudioBackend:
        if self._user_desktop is None:
            raise InteractionError(
                "audio_requires_live_target",
                "PipeWire audio operations are live-desktop only; explicitly select "
                "--session user-desktop.",
                details={"selected_session": self._session_id},
            )
        return PipeWireAudioBackend(self._user_desktop.environ)

    def _power_inhibitor_backend(self) -> PowerInhibitorManager:
        if self._user_desktop is None:
            raise InteractionError(
                "power_inhibitors_require_live_target",
                "Power inhibitors affect the current host and are live-desktop only; "
                "explicitly select --session user-desktop.",
                details={"selected_session": self._session_id},
            )
        paths = RuntimePaths.from_environment(self._user_desktop.environ)
        return self._power_inhibitor_factory(
            paths.power_inhibitors_root,
            self._user_desktop.environ,
        )

    def _target(self) -> dict[str, str]:
        if self._user_desktop is not None:
            return {"kind": USER_DESKTOP}
        return {"kind": "agent-session", "session_id": self._private_session_id()}


def _displays_covered_by_window(
    window: dict[str, Any],
    displays: list[dict[str, Any]],
) -> list[str]:
    """Return displays fully covered by logical-pixel window geometry (2px tolerance)."""
    geometry = window.get("geometry")
    if not isinstance(geometry, dict):
        return []
    if geometry.get("coordinate_space") != "logical-desktop-pixels":
        return []
    fields = ("x", "y", "width", "height")
    values = [geometry.get(field) for field in fields]
    if not all(_is_finite_number(value) for value in values):
        return []
    x, y, width, height = (float(cast(int | float, value)) for value in values)
    if width <= 0 or height <= 0:
        return []

    right = x + width
    bottom = y + height
    matches: list[str] = []
    for display in displays:
        if display.get("coordinate_space") != "logical-desktop-pixels":
            continue
        display_values = [display.get(field) for field in fields]
        if not all(_is_finite_number(value) for value in display_values):
            continue
        display_x, display_y, display_width, display_height = (
            float(cast(int | float, value)) for value in display_values
        )
        if display_width <= 0 or display_height <= 0:
            continue
        if (
            x <= display_x + _DISPLAY_COVERAGE_TOLERANCE
            and y <= display_y + _DISPLAY_COVERAGE_TOLERANCE
            and right >= display_x + display_width - _DISPLAY_COVERAGE_TOLERANCE
            and bottom >= display_y + display_height - _DISPLAY_COVERAGE_TOLERANCE
        ):
            name = display.get("name")
            if isinstance(name, str) and name:
                matches.append(name)
    return matches


def _is_finite_number(value: Any) -> bool:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    try:
        return isfinite(float(value))
    except (OverflowError, ValueError):
        return False


def _display_for_point(
    point: dict[str, Any],
    displays: list[dict[str, Any]],
) -> str | None:
    x, y = point.get("x"), point.get("y")
    if not (_is_finite_number(x) and _is_finite_number(y)):
        return None
    for display in displays:
        values = [display.get(field) for field in ("x", "y", "width", "height")]
        if not all(_is_finite_number(value) for value in values):
            continue
        display_x, display_y, width, height = (float(cast(int | float, value)) for value in values)
        if (
            display_x <= float(cast(int | float, x)) < display_x + width
            and display_y <= float(cast(int | float, y)) < display_y + height
        ):
            name = display.get("name")
            return name if isinstance(name, str) else None
    return None


def _screenshot_mapping(
    screenshot: dict[str, Any],
    displays: list[dict[str, Any]],
) -> dict[str, Any]:
    if len(displays) != 1:
        return {
            "status": "unavailable_for_composite_capture",
            "reason": "The screenshot is a composite of multiple displays.",
        }
    display = displays[0]
    width, height = screenshot.get("width"), screenshot.get("height")
    logical_width, logical_height = display.get("width"), display.get("height")
    if not all(
        _is_finite_number(value) for value in (width, height, logical_width, logical_height)
    ):
        return {
            "status": "unavailable",
            "reason": "Screenshot or display dimensions are missing.",
        }
    pixel_width, pixel_height = float(cast(int | float, width)), float(cast(int | float, height))
    desktop_width = float(cast(int | float, logical_width))
    desktop_height = float(cast(int | float, logical_height))
    if min(pixel_width, pixel_height, desktop_width, desktop_height) <= 0:
        return {
            "status": "unavailable",
            "reason": "Screenshot or display dimensions are invalid.",
        }
    return {
        "status": "single_display_ratio",
        "display_name": display.get("name"),
        "screenshot_space": "screenshot-pixels",
        "desktop_space": "logical-desktop-pixels",
        "screenshot_origin": {"x": 0, "y": 0},
        "desktop_origin": {"x": display.get("x"), "y": display.get("y")},
        "logical_pixels_per_screenshot_pixel": {
            "x": desktop_width / pixel_width,
            "y": desktop_height / pixel_height,
        },
        "formula": (
            "desktop_x = desktop_origin_x + screenshot_x * x_ratio; "
            "desktop_y = desktop_origin_y + screenshot_y * y_ratio"
        ),
    }


def _media_capabilities() -> dict[str, bool]:
    available = find_executable("playerctl") is not None
    return {
        "media.players": available,
        "media.inspect": available,
        "media.control": available,
        "media.seek_by": available,
        "media.seek_to": available,
        "media.wait_for_status": available,
    }


def _audio_capabilities(*, live_target: bool) -> dict[str, bool]:
    available = (
        live_target
        and find_executable("wpctl") is not None
        and find_executable("pactl") is not None
    )
    return {
        "audio.devices": available,
        "audio.default": available,
        "audio.volume": available,
        "audio.mute": available,
        "audio.wait_for_mute": available,
        "audio.route_stream": available,
    }

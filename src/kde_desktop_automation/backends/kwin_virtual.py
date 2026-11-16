from __future__ import annotations

import math
import os
import re
import selectors
import shutil
import stat
import subprocess
import time
import uuid
from collections.abc import Callable, Mapping
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from urllib.parse import quote

from kde_desktop_automation.backends.displays import DisplayInfo, list_displays
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
from kde_desktop_automation.backends.kwin_window_management import (
    KWinWindowManagementClient,
    normalize_window_id,
    window_management_version,
)
from kde_desktop_automation.backends.kwin_window_management import (
    activate_window as activate_private_window,
)
from kde_desktop_automation.backends.kwin_window_management import (
    list_windows as list_private_windows,
)
from kde_desktop_automation.backends.kwin_window_management import (
    set_minimized_window as set_private_window_minimized,
)
from kde_desktop_automation.backends.kwin_window_management import (
    set_window_state as set_private_window_state,
)
from kde_desktop_automation.backends.screenshot_helper import copy_private_screenshot_registration
from kde_desktop_automation.backends.screenshots import (
    capture_fullscreen,
    capture_window,
)
from kde_desktop_automation.errors import InteractionError
from kde_desktop_automation.models import ProcessIdentity, SessionRecord
from kde_desktop_automation.paths import RuntimePaths, SessionPaths
from kde_desktop_automation.process import (
    pidfd_supported,
    process_identity,
    process_matches,
    process_start_ticks,
    processes_with_environment_marker,
    require_executable,
    run_command,
    terminate_verified,
)

_PASSTHROUGH_PREFIXES = ("LC_", "MESA_", "VK_", "LIBVA_")
_MOUNT_ESCAPE = re.compile(r"\\([0-7]{3})")
_RUNTIME_CLEANUP_RETRIES = 20
_RUNTIME_CLEANUP_RETRY_SECONDS = 0.05
_APPLICATION_CLEANUP_RETRIES = 6
_PASSTHROUGH_NAMES = {
    "LANG",
    "PATH",
    "TERM",
    "DRI_PRIME",
    "GBM_BACKEND",
    "__GLX_VENDOR_LIBRARY_NAME",
    "KDE_DESKTOP_SCREENSHOT_HELPER",
}


def build_private_environment(
    *,
    paths: SessionPaths,
    wayland_display: str,
    dbus_address: str,
    environ: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Create an allowlisted environment that cannot target the host Wayland socket."""
    source = os.environ if environ is None else environ
    environment = {
        key: value
        for key, value in source.items()
        if key in _PASSTHROUGH_NAMES or key.startswith(_PASSTHROUGH_PREFIXES)
    }
    environment.update(
        {
            "HOME": str(paths.home_dir),
            "XDG_CONFIG_HOME": str(paths.home_dir / "config"),
            "XDG_CACHE_HOME": str(paths.home_dir / "cache"),
            "XDG_DATA_HOME": str(paths.home_dir / "data"),
            "XDG_STATE_HOME": str(paths.home_dir / "state"),
            "XDG_RUNTIME_DIR": str(paths.runtime_dir),
            "XDG_CURRENT_DESKTOP": "KDE",
            "XDG_SESSION_TYPE": "wayland",
            "XDG_DATA_DIRS": _system_data_dirs(),
            "WAYLAND_DISPLAY": wayland_display,
            "DBUS_SESSION_BUS_ADDRESS": dbus_address,
            "KDE_SESSION_VERSION": "6",
            "QT_QPA_PLATFORM": "wayland",
        }
    )
    return environment


def _system_data_dirs() -> str:
    candidates = (
        Path("/usr/local/share"),
        Path("/usr/share"),
        Path("/var/lib/flatpak/exports/share"),
        Path("/var/lib/snapd/desktop"),
    )
    return ":".join(str(path) for path in candidates if path.is_dir())


class KWinVirtualBackend:
    """Manage a KWin virtual framebuffer in its own Wayland and D-Bus sessions."""

    name = "kwin-virtual"

    def __init__(self, paths: RuntimePaths, *, startup_timeout: float = 8.0) -> None:
        self.paths = paths
        self.startup_timeout = startup_timeout
        self._children: dict[int, subprocess.Popen[Any]] = {}

    def start(self, session_id: str, name: str, width: int, height: int) -> SessionRecord:
        kwin = require_executable("kwin_wayland")
        dbus_daemon = require_executable("dbus-daemon")
        if shutil.which("wlpdisplays") is None:
            raise InteractionError(
                "backend_unavailable",
                "wlpdisplays is required to verify the private output geometry.",
                details={"executable": "wlpdisplays"},
            )
        if not pidfd_supported():
            raise InteractionError(
                "safe_process_control_unavailable",
                "This Linux runtime lacks pidfd support required for safe session cleanup.",
            )

        session_paths = self.paths.for_session(session_id, create=True)
        wayland_display = f"kde-desktop-{session_id.removeprefix('agent-')}"
        bus_path = session_paths.runtime_dir / "bus"
        dbus_address = f"unix:path={quote(str(bus_path), safe='/')}"
        base_env = build_private_environment(
            paths=session_paths,
            wayland_display=wayland_display,
            dbus_address=dbus_address,
        )
        private_registration = copy_private_screenshot_registration(
            session_paths.home_dir / "data",
            env=os.environ,
        )
        if private_registration is not None:
            cache_builder = shutil.which("kbuildsycoca6", path=base_env.get("PATH"))
            if cache_builder is not None:
                # If optional cache refresh fails, KWin will report authorization on capture.
                with suppress(InteractionError):
                    run_command(
                        [cache_builder, "--noincremental"],
                        env=base_env,
                        timeout=20.0,
                    )
        base_env["KDE_DESKTOP_AUTOMATION_PROCESS_MARKER"] = session_id

        log_fd = _open_private_log(session_paths.log_file)
        dbus_process: subprocess.Popen[str] | None = None
        kwin_process: subprocess.Popen[bytes] | None = None
        dbus_identity: ProcessIdentity | None = None
        kwin_identity: ProcessIdentity | None = None
        try:
            dbus_command = [
                dbus_daemon,
                "--session",
                "--nofork",
                "--print-address=1",
                f"--address={dbus_address}",
            ]
            dbus_process = subprocess.Popen(
                dbus_command,
                env=base_env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=log_fd,
                text=True,
                close_fds=True,
                start_new_session=True,
            )
            printed_address = _read_startup_line(dbus_process, timeout=3.0)
            if not _is_private_bus_address(printed_address, dbus_address):
                raise InteractionError(
                    "private_bus_start_failed",
                    "The private D-Bus daemon did not use its session-specific address.",
                    details={
                        "expected_address": dbus_address,
                        "reported_address": printed_address,
                    },
                )
            dbus_address = printed_address
            base_env["DBUS_SESSION_BUS_ADDRESS"] = dbus_address
            dbus_identity = process_identity(
                dbus_process.pid,
                executable=dbus_daemon,
                marker=str(bus_path),
                command=dbus_command,
            )

            kwin_command = [
                kwin,
                "--virtual",
                "--socket",
                wayland_display,
                "--width",
                str(width),
                "--height",
                str(height),
                "--no-lockscreen",
                "--no-global-shortcuts",
            ]
            kwin_process = subprocess.Popen(
                kwin_command,
                env=base_env,
                stdin=subprocess.DEVNULL,
                stdout=log_fd,
                stderr=log_fd,
                close_fds=True,
                start_new_session=True,
            )
            kwin_identity = process_identity(
                kwin_process.pid,
                executable=kwin,
                marker=wayland_display,
                command=kwin_command,
            )
            self._wait_for_output(
                kwin_process,
                base_env,
                session_paths.log_file,
                width=width,
                height=height,
            )

            try:
                input_version = fake_input_version(session_paths.runtime_dir / wayland_display)
            except InteractionError:
                input_version = None
            try:
                window_version = window_management_version(
                    session_paths.runtime_dir / wayland_display
                )
            except InteractionError:
                window_version = None

            record = SessionRecord(
                session_id=session_id,
                name=name,
                backend=self.name,
                created_at=datetime.now(UTC).isoformat(),
                stopped_at=None,
                width=width,
                height=height,
                runtime_dir=str(session_paths.runtime_dir),
                home_dir=str(session_paths.home_dir),
                wayland_display=wayland_display,
                dbus_address=dbus_address,
                kwin_process=kwin_identity,
                dbus_process=dbus_identity,
                fake_input_version=input_version,
                window_management_version=window_version,
            )
            self._children[kwin_process.pid] = kwin_process
            self._children[dbus_process.pid] = dbus_process
            return record
        except Exception:
            _stop_child(kwin_process)
            _stop_child(dbus_process)
            self._stop_marker_processes(session_id, root=None, session_id=session_id)
            _remove_owned_runtime(session_paths.runtime_dir, self.paths.runtime_root / "sessions")
            raise
        finally:
            os.close(log_fd)

    def is_running(self, session: SessionRecord) -> bool:
        self._validate_session_identity(session)
        return process_matches(session.kwin_process) and process_matches(session.dbus_process)

    def stop(self, session: SessionRecord) -> list[str]:
        self._validate_session_identity(session)
        warnings: list[str] = []
        for process in session.application_processes:
            self._stop_application_processes(process, session_id=session.session_id)
            self._reap_child(process.pid, f"application {process.executable}")

        for process, label in (
            (session.kwin_process, "KWin"),
            (session.dbus_process, "private D-Bus"),
        ):
            current_start = process_start_ticks(process.pid)
            if current_start == process.start_ticks and not terminate_verified(process):
                raise InteractionError(
                    "session_stop_incomplete",
                    f"Could not confirm that the recorded {label} process has stopped.",
                    details={"session_id": session.session_id, "pid": process.pid},
                )
            self._reap_child(process.pid, label)

        # KWin and the private bus can activate helper processes (portals,
        # Xwayland) which inherit the session marker but are not direct children.
        self._stop_marker_processes(session.session_id, root=None, session_id=session.session_id)

        session_paths = self.paths.for_session(session.session_id)
        runtime_sessions = self.paths.runtime_root / "sessions"
        if not _remove_owned_runtime(session_paths.runtime_dir, runtime_sessions):
            warnings.append(
                f"Private runtime directory needs manual cleanup: {session_paths.runtime_dir}"
            )
        return warnings

    def launch_application(self, session: SessionRecord, argv: tuple[str, ...]) -> ProcessIdentity:
        self._require_running(session)
        if not argv or not argv[0] or any("\0" in item for item in argv):
            raise InteractionError(
                "invalid_application_command",
                "Provide an executable and arguments as separate command-line values.",
            )
        if len(argv) > 128 or sum(len(item.encode("utf-8")) for item in argv) > 32_768:
            raise InteractionError(
                "application_command_too_large",
                "Application commands are limited to 128 arguments and 32 KiB total.",
            )
        executable = shutil.which(argv[0])
        if executable is None:
            raise InteractionError(
                "application_not_found",
                f"Could not find application executable '{argv[0]}'.",
                details={"executable": argv[0]},
            )
        executable = str(Path(executable).resolve())
        command = (executable, *argv[1:])
        marker = f"{session.session_id}-app-{uuid.uuid4().hex}"
        environment = self.environment(session)
        environment["KDE_DESKTOP_AUTOMATION_PROCESS_MARKER"] = marker
        log_fd = _open_private_log(self.paths.for_session(session.session_id).log_file)
        try:
            try:
                child = subprocess.Popen(
                    command,
                    env=environment,
                    cwd=session.home_dir,
                    stdin=subprocess.DEVNULL,
                    stdout=log_fd,
                    stderr=log_fd,
                    close_fds=True,
                    start_new_session=True,
                )
            except OSError as exc:
                raise InteractionError(
                    "application_launch_failed",
                    f"Could not launch application '{argv[0]}'.",
                    details={"executable": executable, "reason": str(exc)},
                ) from exc
        finally:
            os.close(log_fd)

        try:
            actual_executable = os.readlink(f"/proc/{child.pid}/exe")
            identity = process_identity(
                child.pid,
                executable=actual_executable,
                marker=marker,
                command=command,
            )
            if not process_matches(identity):
                raise InteractionError(
                    "application_identity_failed",
                    "The launched process did not retain its private-session identity marker.",
                    details={"pid": child.pid, "executable": executable},
                )
        except (OSError, InteractionError) as exc:
            _stop_child(child)
            self._stop_marker_processes(marker, root=None)
            if isinstance(exc, InteractionError):
                raise
            raise InteractionError(
                "application_identity_failed",
                "Could not safely identify the launched application process.",
                details={"pid": child.pid, "executable": executable, "reason": str(exc)},
            ) from exc
        self._children[child.pid] = child
        return identity

    def stop_application(self, identity: ProcessIdentity) -> None:
        self._stop_application_processes(identity)
        self._reap_child(identity.pid, f"application {identity.executable}")

    def send_key(self, session: SessionRecord, chord: str) -> InputReceipt:
        return self._send_input(session, "key", chord)

    def send_text(self, session: SessionRecord, text: str) -> InputReceipt:
        return self._send_input(session, "text", text)

    def list_windows(self, session: SessionRecord) -> list[dict[str, Any]]:
        self._require_running(session)
        socket_path = Path(session.runtime_dir) / session.wayland_display
        return cast(list[dict[str, Any]], list_private_windows(socket_path)["windows"])

    def inspect_window(self, session: SessionRecord, window_id: str) -> dict[str, Any]:
        windows = self.list_windows(session)
        normalized_id = normalize_window_id(window_id)
        for window in windows:
            if window["window_id"] == normalized_id:
                return window
        raise InteractionError(
            "stale_target",
            "The selected window no longer exists in the private KWin session.",
            details={"session_id": session.session_id, "window_id": normalized_id},
        )

    def activate_window(self, session: SessionRecord, window_id: str) -> dict[str, Any]:
        self._require_running(session)
        socket_path = Path(session.runtime_dir) / session.wayland_display
        result = activate_private_window(socket_path, window_id)
        if not result["active_verified"]:
            raise InteractionError(
                "window_activation_unverified",
                "Private KWin did not report the selected window as active after activation.",
                details={"session_id": session.session_id, "window_id": window_id},
            )
        return cast(dict[str, Any], result["window"])

    def set_window_minimized(
        self,
        session: SessionRecord,
        window_id: str,
        minimized: bool,
    ) -> dict[str, Any]:
        self._require_running(session)
        socket_path = Path(session.runtime_dir) / session.wayland_display
        result = set_private_window_minimized(socket_path, window_id, minimized)
        if not result["minimized_verified"]:
            raise InteractionError(
                "window_state_unverified",
                "Private KWin did not report the requested minimized state.",
                details={"session_id": session.session_id, "window_id": window_id},
            )
        return cast(dict[str, Any], result["window"])

    def set_window_fullscreen(
        self,
        session: SessionRecord,
        window_id: str,
        fullscreen: bool,
    ) -> dict[str, Any]:
        self._require_running(session)
        result = set_private_window_state(
            Path(session.runtime_dir) / session.wayland_display,
            window_id,
            state="fullscreen",
            enabled=fullscreen,
        )
        if result["verified"] is not True:
            raise InteractionError(
                "window_state_unverified",
                "Private KWin did not report the requested fullscreen state.",
                details={"window_id": window_id, "fullscreen": fullscreen, "readback": result},
            )
        return result

    def set_window_maximized(
        self,
        session: SessionRecord,
        window_id: str,
        maximized: bool,
    ) -> dict[str, Any]:
        self._require_running(session)
        result = set_private_window_state(
            Path(session.runtime_dir) / session.wayland_display,
            window_id,
            state="maximized",
            enabled=maximized,
        )
        if result["verified"] is not True:
            raise InteractionError(
                "window_state_unverified",
                "Private KWin did not report the requested maximized state.",
                details={"window_id": window_id, "maximized": maximized, "readback": result},
            )
        return result

    def set_window_geometry(
        self,
        session: SessionRecord,
        window_id: str,
        x: int,
        y: int,
        width: int,
        height: int,
    ) -> dict[str, Any]:
        self._require_running(session)
        window = self.inspect_window(session, window_id)
        if window.get("fullscreen") or window.get("maximized"):
            raise InteractionError(
                "window_geometry_state_conflict",
                "Restore a maximized or fullscreen window before setting its geometry.",
                details={
                    "window_id": window_id,
                    "fullscreen": window.get("fullscreen"),
                    "maximized": window.get("maximized"),
                },
            )
        environment = self.environment(session)
        # kdotool's private-session version detection normally comes from Plasma;
        # this managed compositor deliberately runs without a Plasma shell.
        environment["KDE_SESSION_VERSION"] = "6"
        result = set_window_frame_geometry(
            window_id,
            x,
            y,
            width,
            height,
            env=environment,
        )
        return result | {
            "window": self.inspect_window(session, window_id),
            "note": "Geometry is the KWin frame rectangle; focus is not intentionally changed.",
        }

    def pointer_position(self, session: SessionRecord) -> dict[str, Any]:
        self._require_running(session)
        environment = self.environment(session)
        environment["KDE_SESSION_VERSION"] = "6"
        return read_pointer_position(env=environment)

    def send_key_to_window(
        self,
        session: SessionRecord,
        window_id: str,
        chord: str,
    ) -> dict[str, Any]:
        return self._send_window_input(session, window_id, "key", chord)

    def send_text_to_window(
        self,
        session: SessionRecord,
        window_id: str,
        text: str,
    ) -> dict[str, Any]:
        return self._send_window_input(session, window_id, "text", text)

    def click_window(
        self,
        session: SessionRecord,
        window_id: str,
        x: float,
        y: float,
        button: str = "left",
    ) -> dict[str, Any]:
        if not (math.isfinite(x) and math.isfinite(y) and 0.0 <= x <= 1.0 and 0.0 <= y <= 1.0):
            raise InteractionError(
                "coordinates_out_of_bounds",
                "Window-relative click coordinates must be normalized values from 0 to 1.",
                details={"x": x, "y": y},
            )
        self._require_running(session)
        resolved_point: tuple[int, int, str | None] | None = None

        def submit_click(
            client: KWinFakeInput,
            window: dict[str, Any],
        ) -> InputReceipt:
            nonlocal resolved_point
            geometry = window.get("geometry")
            if not isinstance(geometry, dict):
                raise InteractionError(
                    "window_geometry_unavailable",
                    "KWin has not provided geometry for the selected private window.",
                    details={"window_id": window_id},
                )
            absolute_x = round(float(geometry["x"]) + (float(geometry["width"]) - 1) * x)
            absolute_y = round(float(geometry["y"]) + (float(geometry["height"]) - 1) * y)
            if not (0 <= absolute_x < session.width and 0 <= absolute_y < session.height):
                raise InteractionError(
                    "coordinates_out_of_bounds",
                    "The resolved window-relative point is outside the private virtual output.",
                    details={
                        "x": absolute_x,
                        "y": absolute_y,
                        "width": session.width,
                        "height": session.height,
                    },
                )
            basis = window.get("geometry_basis")
            resolved_point = (
                absolute_x,
                absolute_y,
                basis if isinstance(basis, str) else None,
            )
            return client.click(absolute_x, absolute_y, button)

        receipt, active_before, active_after = self._submit_window_input(
            session,
            window_id,
            submit_click,
        )
        if resolved_point is None:
            raise InteractionError(
                "window_click_unresolved",
                "KWin input completed without resolving the selected window click point.",
            )
        absolute_x, absolute_y, geometry_basis = resolved_point
        result = self._private_input_result(
            session,
            window_id,
            receipt,
            active_before,
            active_after,
        )
        result["requested_point"] = {"x": x, "y": y, "space": "normalized-window"}
        result["resolved_point"] = {
            "x": absolute_x,
            "y": absolute_y,
            "space": "logical-desktop-pixels",
            "geometry_basis": geometry_basis,
        }
        return result

    def click(self, session: SessionRecord, x: int, y: int, button: str) -> InputReceipt:
        if not (0 <= x < session.width and 0 <= y < session.height):
            raise InteractionError(
                "coordinates_out_of_bounds",
                "Click coordinates must be inside the private output.",
                details={"x": x, "y": y, "width": session.width, "height": session.height},
            )
        self._require_running(session)
        socket_path = Path(session.runtime_dir) / session.wayland_display
        with KWinFakeInput(
            socket_path,
            application_name="kde-desktop",
            reason=f"Session-scoped click in private session {session.session_id}.",
        ) as client:
            return client.click(x, y, button)

    def drag(
        self,
        session: SessionRecord,
        start_x: int,
        start_y: int,
        end_x: int,
        end_y: int,
        button: str,
        steps: int,
        duration_ms: int = DEFAULT_DRAG_DURATION_MS,
        hold_ms: int = DEFAULT_DRAG_HOLD_MS,
        end_pause_ms: int = DEFAULT_DRAG_END_PAUSE_MS,
    ) -> InputReceipt:
        validate_drag_steps(steps)
        validate_drag_timing(duration_ms, hold_ms, end_pause_ms)
        for x, y in ((start_x, start_y), (end_x, end_y)):
            if not (0 <= x < session.width and 0 <= y < session.height):
                raise InteractionError(
                    "coordinates_out_of_bounds",
                    "Drag coordinates must be inside the private virtual output.",
                    details={"x": x, "y": y, "width": session.width, "height": session.height},
                )
        self._require_running(session)
        socket_path = Path(session.runtime_dir) / session.wayland_display
        with KWinFakeInput(
            socket_path,
            application_name="kde-desktop",
            reason=f"Session-scoped global pointer drag in private session {session.session_id}.",
        ) as client:
            return client.drag(
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

    def _send_input(self, session: SessionRecord, kind: str, value: str) -> InputReceipt:
        self._require_running(session)
        socket_path = Path(session.runtime_dir) / session.wayland_display
        with KWinFakeInput(
            socket_path,
            application_name="kde-desktop",
            reason=f"Session-scoped {kind} input in private session {session.session_id}.",
        ) as client:
            if kind == "key":
                return client.send_key(value)
            return client.send_text(value)

    def _send_window_input(
        self,
        session: SessionRecord,
        window_id: str,
        kind: str,
        value: str,
    ) -> dict[str, Any]:
        receipt, active_before, active_after = self._submit_window_input(
            session,
            window_id,
            lambda client, _window: (
                client.send_key(value) if kind == "key" else client.send_text(value)
            ),
        )
        return self._private_input_result(
            session,
            window_id,
            receipt,
            active_before,
            active_after,
        )

    def _submit_window_input(
        self,
        session: SessionRecord,
        window_id: str,
        submit: Callable[[KWinFakeInput, dict[str, Any]], InputReceipt],
    ) -> tuple[InputReceipt, str, str | None]:
        self._require_running(session)
        normalized_id = normalize_window_id(window_id)
        self.inspect_window(session, normalized_id)
        socket_path = Path(session.runtime_dir) / session.wayland_display
        with KWinFakeInput(
            socket_path,
            application_name="kde-desktop",
            reason=(
                f"Window-scoped input in private session {session.session_id} "
                f"to window {normalized_id}."
            ),
        ) as client:
            windows = self.list_windows(session)
            selected_window = next(
                (window for window in windows if window["window_id"] == normalized_id),
                None,
            )
            if selected_window is None:
                raise InteractionError(
                    "stale_target",
                    "The selected window disappeared before input submission.",
                    details={"session_id": session.session_id, "window_id": normalized_id},
                )
            active_ids = [window["window_id"] for window in windows if window["active"]]
            if len(active_ids) > 1:
                raise InteractionError(
                    "invalid_backend_data",
                    "Private KWin reported more than one active window.",
                    details={"session_id": session.session_id, "active_window_ids": active_ids},
                )
            active_before = active_ids[0] if active_ids else None
            if active_before != normalized_id:
                raise InteractionError(
                    "window_not_active",
                    "Input requires the selected private window to be active. "
                    "Activate it explicitly first.",
                    details={
                        "session_id": session.session_id,
                        "window_id": normalized_id,
                        "active_window_id": active_before,
                    },
                )
            receipt = submit(client, selected_window)
        assert active_before is not None
        return receipt, active_before, self._active_window_id(session)

    def _active_window_id(self, session: SessionRecord) -> str | None:
        socket_path = Path(session.runtime_dir) / session.wayland_display
        with KWinWindowManagementClient(socket_path) as client:
            active = [window["window_id"] for window in client.windows() if window["active"]]
        if len(active) > 1:
            raise InteractionError(
                "invalid_backend_data",
                "Private KWin reported more than one active window.",
                details={"session_id": session.session_id, "active_window_ids": active},
            )
        return active[0] if active else None

    def _private_input_result(
        self,
        session: SessionRecord,
        window_id: str,
        receipt: InputReceipt,
        active_before: str,
        active_after: str | None,
    ) -> dict[str, Any]:
        data = receipt.to_dict(
            session_id=session.session_id,
            wayland_display=session.wayland_display,
        )
        normalized_id = normalize_window_id(window_id)
        data["target"]["window_id"] = normalized_id
        data["target"]["keyboard_focus"] = (
            "active KWin window verified immediately before submission"
        )
        data["target"]["focus_before"] = active_before
        data["focus_after"] = active_after
        data["focus_still_matches_target"] = active_after == normalized_id
        data["input_semantics"] = {
            "targeting": "active-window-guard",
            "focus_verified_before_submission": True,
            "atomic_window_routing": False,
            "delivery_acknowledged": False,
        }
        if active_after != normalized_id:
            data["note"] += " KWin focus changed during the action; delivery is unknown."
        return data

    def _stop_application_processes(
        self,
        identity: ProcessIdentity,
        *,
        session_id: str | None = None,
    ) -> None:
        self._stop_marker_processes(identity.marker, root=identity, session_id=session_id)

    def _stop_marker_processes(
        self,
        marker: str,
        *,
        root: ProcessIdentity | None,
        session_id: str | None = None,
    ) -> None:
        empty_scans = 0
        for _attempt in range(_APPLICATION_CLEANUP_RETRIES):
            processes = processes_with_environment_marker(marker)
            if (
                root is not None
                and process_matches(root)
                and all(item.pid != root.pid for item in processes)
            ):
                processes.append(root)
            if not processes:
                empty_scans += 1
                if empty_scans >= 2:
                    return
                time.sleep(0.05)
                continue
            empty_scans = 0
            identity_changed = False
            for process in sorted(processes, key=lambda item: item.pid, reverse=True):
                current_start = process_start_ticks(process.pid)
                if current_start != process.start_ticks:
                    continue
                try:
                    stopped = terminate_verified(process, timeout=1.0)
                except InteractionError as exc:
                    if exc.code == "process_identity_mismatch":
                        # A marked process can exec between /proc inspection and
                        # pidfd verification. Re-scan and verify its new image.
                        identity_changed = True
                        break
                    raise
                if not stopped:
                    raise InteractionError(
                        "application_stop_incomplete",
                        "Could not confirm that a launched private-session application stopped.",
                        details={"session_id": session_id, "pid": process.pid},
                    )
            if identity_changed:
                continue
        remaining = processes_with_environment_marker(marker)
        if remaining:
            raise InteractionError(
                "application_stop_incomplete",
                "Marked application processes remained after cleanup attempts.",
                details={
                    "session_id": session_id,
                    "pids": [process.pid for process in remaining],
                },
            )

    def displays(self, session: SessionRecord) -> list[DisplayInfo]:
        self._require_running(session)
        return list_displays(env=self.environment(session))

    def screenshot(
        self,
        session: SessionRecord,
        output: Path,
        *,
        overwrite: bool,
    ) -> dict[str, object]:
        self._require_running(session)
        return capture_fullscreen(
            output,
            env=self.environment(session),
            overwrite=overwrite,
            expected_size=(session.width, session.height),
        )

    def screenshot_window(
        self,
        session: SessionRecord,
        window_id: str,
        output: Path,
        *,
        overwrite: bool,
    ) -> dict[str, object]:
        self._require_running(session)
        return capture_window(
            output,
            window_id=window_id,
            env=self.environment(session),
            overwrite=overwrite,
        )

    def environment(self, session: SessionRecord) -> dict[str, str]:
        self._validate_session_identity(session)
        session_paths = self.paths.for_session(session.session_id)
        return build_private_environment(
            paths=session_paths,
            wayland_display=session.wayland_display,
            dbus_address=session.dbus_address,
        )

    def _validate_session_identity(self, session: SessionRecord) -> None:
        session_paths = self.paths.for_session(session.session_id)
        expected_wayland = f"kde-desktop-{session.session_id.removeprefix('agent-')}"
        expected_bus_path = session_paths.runtime_dir / "bus"
        expected_bus_address = f"unix:path={quote(str(expected_bus_path), safe='/')}"
        application_markers = [process.marker for process in session.application_processes]
        valid = (
            session.backend == self.name
            and session.runtime_dir == str(session_paths.runtime_dir)
            and session.home_dir == str(session_paths.home_dir)
            and session.wayland_display == expected_wayland
            and session.kwin_process.marker == session.wayland_display
            and session.dbus_process.marker == str(expected_bus_path)
            and session.kwin_process.pid != session.dbus_process.pid
            and _is_private_bus_address(session.dbus_address, expected_bus_address)
            and len(application_markers) == len(set(application_markers))
            and all(
                marker.startswith(f"{session.session_id}-app-") for marker in application_markers
            )
            and all(
                process.pid not in {session.kwin_process.pid, session.dbus_process.pid}
                for process in session.application_processes
            )
        )
        if not valid:
            raise InteractionError(
                "session_state_mismatch",
                "Saved process identities or endpoints are not bound to this private session.",
                details={"session_id": session.session_id},
            )

    def _reap_child(self, pid: int, label: str) -> None:
        child = self._children.get(pid)
        if child is None:
            return
        try:
            child.wait(timeout=1.0)
        except subprocess.TimeoutExpired as exc:
            raise InteractionError(
                "process_reap_timeout",
                f"Could not reap the stopped {label} child process.",
                details={"pid": pid},
            ) from exc
        self._children.pop(pid, None)

    def _require_running(self, session: SessionRecord) -> None:
        if not self.is_running(session):
            raise InteractionError(
                "session_not_running",
                "The private KWin session is no longer running.",
                details={"session_id": session.session_id},
            )

    def _wait_for_output(
        self,
        process: subprocess.Popen[bytes],
        env: Mapping[str, str],
        log_file: Path,
        *,
        width: int,
        height: int,
    ) -> None:
        deadline = time.monotonic() + self.startup_timeout
        last_error = "The virtual display has not appeared yet."
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise InteractionError(
                    "kwin_start_failed",
                    "KWin exited before its private output became available.",
                    details={"log_tail": _read_log_tail(log_file)},
                )
            try:
                outputs = list_displays(env=env)
                if len(outputs) == 1 and outputs[0].width == width and outputs[0].height == height:
                    return
                if outputs:
                    last_error = (
                        f"Unexpected output geometry: {outputs[0].width}x{outputs[0].height}."
                    )
            except InteractionError as exc:
                last_error = exc.message
            time.sleep(0.1)
        raise InteractionError(
            "kwin_output_timeout",
            "KWin did not expose a virtual output before the startup timeout.",
            details={"last_error": last_error, "log_tail": _read_log_tail(log_file)},
        )


def _read_startup_line(process: subprocess.Popen[str], *, timeout: float) -> str:
    if process.stdout is None:
        raise InteractionError(
            "private_bus_start_failed",
            "Could not read private D-Bus startup output.",
        )
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            if not selector.select(timeout):
                raise InteractionError(
                    "private_bus_start_timeout", "Private D-Bus startup timed out."
                )
            line = process.stdout.readline().strip()
    finally:
        process.stdout.close()
    if process.poll() is not None and not line:
        raise InteractionError(
            "private_bus_start_failed",
            "The private D-Bus daemon exited during startup.",
        )
    return line


def _is_private_bus_address(reported: str, expected: str) -> bool:
    """Accept dbus-daemon's optional GUID while requiring our exact private socket."""
    return re.fullmatch(re.escape(expected) + r"(?:,guid=[a-f0-9]{32})?", reported) is not None


def _open_private_log(path: Path) -> int:
    flags = os.O_CREAT | os.O_APPEND | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    os.fchmod(descriptor, 0o600)
    return descriptor


def _stop_child(process: subprocess.Popen[Any] | None) -> None:
    if process is None or process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=3)
    if process.stdout is not None:
        process.stdout.close()


def _read_log_tail(path: Path, *, limit: int = 3000) -> str:
    try:
        with path.open("rb") as log_file:
            log_file.seek(0, os.SEEK_END)
            size = log_file.tell()
            log_file.seek(max(0, size - limit))
            return log_file.read().decode("utf-8", errors="replace").strip()
    except OSError:
        return ""


def _remove_owned_runtime(path: Path, allowed_root: Path) -> bool:
    """Remove only a direct, app-owned session directory; never follow symlinks."""
    if path.parent != allowed_root or path.is_symlink():
        return False
    try:
        path_stat = path.lstat()
    except FileNotFoundError:
        return True
    except OSError:
        return False
    if not stat.S_ISDIR(path_stat.st_mode) or path_stat.st_uid != os.getuid():
        return False
    for attempt in range(_RUNTIME_CLEANUP_RETRIES):
        mount_points = _read_mount_points()
        if mount_points is None or _contains_mountpoint(path, mount_points):
            return False
        if _remove_directory_contents(path, session_root=path):
            return True
        if not path.exists():
            return True
        if attempt < _RUNTIME_CLEANUP_RETRIES - 1:
            time.sleep(_RUNTIME_CLEANUP_RETRY_SECONDS)
    return False


def _read_mount_points() -> set[Path] | None:
    try:
        mountinfo = Path("/proc/self/mountinfo").read_text(encoding="utf-8")
    except OSError:
        return None

    mount_points: set[Path] = set()
    for line in mountinfo.splitlines():
        fields = line.split(" - ", maxsplit=1)[0].split()
        if len(fields) < 5:
            return None
        decoded = _MOUNT_ESCAPE.sub(lambda match: chr(int(match.group(1), 8)), fields[4])
        mount_points.add(Path(decoded))
    return mount_points or None


def _contains_mountpoint(path: Path, mount_points: set[Path]) -> bool:
    session_path = Path(os.path.abspath(path))
    return any(
        mount_point == session_path or session_path in mount_point.parents
        for mount_point in mount_points
    )


def _remove_directory_contents(directory: Path, *, session_root: Path) -> bool:
    """Recursively clean a session-owned directory without following symlinks."""
    try:
        for entry in directory.iterdir():
            mount_points = _read_mount_points()
            if mount_points is None or _contains_mountpoint(session_root, mount_points):
                return False
            try:
                entry_stat = entry.lstat()
            except OSError:
                return False
            if entry_stat.st_uid != os.getuid():
                return False
            if stat.S_ISLNK(entry_stat.st_mode):
                try:
                    entry.unlink()
                except OSError:
                    return False
            elif stat.S_ISDIR(entry_stat.st_mode):
                if not _remove_directory_contents(entry, session_root=session_root):
                    return False
            else:
                try:
                    entry.unlink()
                except OSError:
                    return False
        mount_points = _read_mount_points()
        if mount_points is None or _contains_mountpoint(session_root, mount_points):
            return False
        directory.rmdir()
    except OSError:
        return False
    return True

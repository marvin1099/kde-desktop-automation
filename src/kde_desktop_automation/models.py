from __future__ import annotations

import shutil
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class ProcessIdentity:
    """Enough information to avoid signalling a reused PID during session cleanup."""

    pid: int
    start_ticks: int
    executable: str
    marker: str
    command: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "pid": self.pid,
            "start_ticks": self.start_ticks,
            "executable": self.executable,
            "marker": self.marker,
            "command": list(self.command),
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> ProcessIdentity:
        return cls(
            pid=int(value["pid"]),
            start_ticks=int(value["start_ticks"]),
            executable=str(value["executable"]),
            marker=str(value["marker"]),
            command=tuple(str(part) for part in value["command"]),
        )


@dataclass(frozen=True, slots=True)
class SessionRecord:
    session_id: str
    name: str
    backend: str
    created_at: str
    stopped_at: str | None
    width: int
    height: int
    runtime_dir: str
    home_dir: str
    wayland_display: str
    dbus_address: str
    kwin_process: ProcessIdentity
    dbus_process: ProcessIdentity
    application_processes: tuple[ProcessIdentity, ...] = ()
    fake_input_version: int | None = None
    window_management_version: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "name": self.name,
            "backend": self.backend,
            "created_at": self.created_at,
            "stopped_at": self.stopped_at,
            "width": self.width,
            "height": self.height,
            "runtime_dir": self.runtime_dir,
            "home_dir": self.home_dir,
            "wayland_display": self.wayland_display,
            "dbus_address": self.dbus_address,
            "kwin_process": self.kwin_process.to_dict(),
            "dbus_process": self.dbus_process.to_dict(),
            "application_processes": [process.to_dict() for process in self.application_processes],
            "fake_input_version": self.fake_input_version,
            "window_management_version": self.window_management_version,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> SessionRecord:
        return cls(
            session_id=str(value["session_id"]),
            name=str(value["name"]),
            backend=str(value["backend"]),
            created_at=str(value["created_at"]),
            stopped_at=None if value.get("stopped_at") is None else str(value["stopped_at"]),
            width=int(value["width"]),
            height=int(value["height"]),
            runtime_dir=str(value["runtime_dir"]),
            home_dir=str(value["home_dir"]),
            wayland_display=str(value["wayland_display"]),
            dbus_address=str(value["dbus_address"]),
            kwin_process=ProcessIdentity.from_dict(value["kwin_process"]),
            dbus_process=ProcessIdentity.from_dict(value["dbus_process"]),
            application_processes=tuple(
                ProcessIdentity.from_dict(process)
                for process in value.get("application_processes", [])
            ),
            fake_input_version=(
                None
                if value.get("fake_input_version") is None
                else int(value["fake_input_version"])
            ),
            window_management_version=(
                None
                if value.get("window_management_version") is None
                else int(value["window_management_version"])
            ),
        )

    def public_dict(self, *, status: str) -> dict[str, Any]:
        fake_input_version = self.fake_input_version
        window_management_available = (
            self.window_management_version is not None and self.window_management_version >= 13
        )
        kdotool_available = shutil.which("kdotool") is not None
        session_key_available = fake_input_version is not None and fake_input_version >= 6
        session_click_available = fake_input_version is not None and fake_input_version >= 3
        window_key_available = window_management_available and session_key_available
        window_click_available = window_management_available and session_click_available
        if fake_input_version is None:
            input_route = "separate; KWin fake-input protocol unavailable or unprobed"
        else:
            input_route = (
                f"separate; KWin fake-input protocol v{fake_input_version}; "
                "authorization/effect acknowledgement unavailable"
            )
        return {
            "session_id": self.session_id,
            "name": self.name,
            "kind": "agent-session",
            "backend": self.backend,
            "status": status,
            "created_at": self.created_at,
            "output": {"width": self.width, "height": self.height},
            "isolation": {
                "compositor": "separate",
                "physical_displays": "unaffected",
                "input_route": input_route,
                "unix_identity": "same as CLI process",
                "filesystem": "private HOME, not an OS security sandbox",
            },
            "capabilities": {
                "display.inspect": True,
                "screenshot.capture": shutil.which("spectacle") is not None,
                "window.list": window_management_available,
                "window.find": window_management_available,
                "window.wait": window_management_available,
                "window.inspect": window_management_available,
                "window.activate": window_management_available,
                "window.set_minimized": window_management_available,
                "window.set_geometry": window_management_available and kdotool_available,
                "desktop.observe": window_management_available,
                "input.pointer_position": kdotool_available,
                "application.launch": True,
                "input.key": window_key_available,
                "input.text": window_key_available,
                "input.click": window_click_available,
                "input.inject": session_click_available,
                "input.session.click": session_click_available,
                "input.focus_guard": window_management_available and session_click_available,
                "input.active_window_guard": window_management_available
                and (session_key_available or session_click_available),
                "input.global_click": session_click_available,
                "input.global_drag": session_click_available,
                "input.effect_ack": False,
            },
            "input_semantics": {
                "routing": "KWin fake input through the selected compositor's active focus",
                "window_targeting": "active-window-guard; focus can still change during submission",
                "global_pointer": (
                    "logical desktop coordinates; not window-bound; may move the pointer "
                    "or change the active window"
                ),
                "atomic_window_routing": False,
                "delivery_acknowledged": False,
            },
            "input_protocol_version": fake_input_version,
            "window_management_protocol_version": self.window_management_version,
            "limitations": [
                "KWin fake input may be ignored; submission is not proof of application delivery",
                "the private window protocol is a privileged KDE implementation detail",
                "accessibility elements and focused child controls are not exposed",
            ],
            "launched_processes": [
                {"pid": process.pid, "executable": process.executable}
                for process in self.application_processes
            ],
        }

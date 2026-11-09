from __future__ import annotations

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
        )

    def public_dict(self, *, status: str) -> dict[str, Any]:
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
                "input_route": "separate; injection not enabled",
                "unix_identity": "same as CLI process",
                "filesystem": "private HOME, not an OS security sandbox",
            },
            "capabilities": {
                "display.inspect": True,
                "screenshot.capture": True,
                "window.inspect": False,
                "input.inject": False,
            },
        }

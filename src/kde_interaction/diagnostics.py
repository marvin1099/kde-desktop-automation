from __future__ import annotations

import os
import shutil
from typing import Any

from kde_interaction import __version__
from kde_interaction.process import pidfd_supported

_TOOLS = (
    "kwin_wayland",
    "dbus-daemon",
    "wlpdisplays",
    "spectacle",
    "kdotool",
    "dotool",
    "kdocktor",
)


def build_doctor_report() -> dict[str, Any]:
    tools = {name: shutil.which(name) for name in _TOOLS}
    kwin = tools["kwin_wayland"] is not None
    bus = tools["dbus-daemon"] is not None
    displays = tools["wlpdisplays"] is not None
    spectacle = tools["spectacle"] is not None
    process_control = pidfd_supported()
    limitations = [
        "window inspection/control is not connected to standalone KWin's D-Bus API",
        "dotool uses host-wide uinput and is deliberately disabled for agent-session",
    ]
    if not process_control:
        limitations.append("private session lifecycle requires race-safe pidfd process control")

    return {
        "cli_version": __version__,
        "host_session": {
            "type": os.environ.get("XDG_SESSION_TYPE", "unknown"),
            "desktop": os.environ.get("XDG_CURRENT_DESKTOP", "unknown"),
            "inspected": False,
        },
        "tools": tools,
        "agent_session": {
            "backend": "kwin-virtual",
            "create": kwin and bus and displays and process_control,
            "display.inspect": displays,
            "screenshot.capture": spectacle,
            "session.stop": process_control,
            "window.inspect": False,
            "input.inject": False,
            "limitations": limitations,
        },
        "user_desktop": {
            "attached": False,
            "interaction_enabled": False,
            "reason": "explicit, permissioned user-session attachment is not implemented yet",
        },
        "safety": {
            "default_scope": "agent-session",
            "silent_fallback_to_user_desktop": False,
            "os_security_sandbox": False,
        },
    }

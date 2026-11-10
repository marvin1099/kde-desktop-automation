from __future__ import annotations

import ctypes.util
import os
import shutil
from typing import Any

from kde_interaction import __version__
from kde_interaction.backends.user_desktop import user_desktop_environment_issue
from kde_interaction.process import pidfd_supported

_TOOLS = (
    "kwin_wayland",
    "dbus-daemon",
    "wlpdisplays",
    "spectacle",
    "kdotool",
    "dotool",
    "kdocktor",
    "playerctl",
)


def build_doctor_report() -> dict[str, Any]:
    tools = {name: shutil.which(name) for name in _TOOLS}
    kwin = tools["kwin_wayland"] is not None
    bus = tools["dbus-daemon"] is not None
    displays = tools["wlpdisplays"] is not None
    spectacle = tools["spectacle"] is not None
    wayland_client = ctypes.util.find_library("wayland-client") is not None
    user_desktop_issue = user_desktop_environment_issue()
    if user_desktop_issue is None and tools["kdotool"] is None:
        user_desktop_issue = "kdotool is required for live window discovery and activation."
    user_desktop_ready = user_desktop_issue is None
    process_control = pidfd_supported()
    limitations = [
        "private window management is probed per session through KWin's Plasma window protocol",
        "dotool uses host-wide uinput and is deliberately disabled for agent-session",
        "KWin fake input may be ignored; submission is not proof of application delivery",
        "input protocol versions are probed per private session; doctor reports prerequisites only",
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
            "window.list": False,
            "window.inspect": False,
            "window.activate": False,
            "window.protocol_probe_available": kwin and wayland_client,
            "input.window_targeted": False,
            "application.launch": kwin,
            "input.inject": False,
            "input.text": False,
            "input.click": False,
            "input.protocol_probe_available": kwin and wayland_client,
            "input.effect_ack": False,
            "wayland_client_library": wayland_client,
            "limitations": limitations,
        },
        "user_desktop": {
            "attached": False,
            "available": user_desktop_ready,
            "availability_kind": "environment and executable prerequisites only",
            "explicit_command_required": True,
            "window.list": None,
            "window.find": None,
            "window.wait": None,
            "window.inspect": None,
            "window.activate": None,
            "window.capability_probe_performed": False,
            "screenshot.capture": user_desktop_ready and spectacle,
            "input.protocol_version": None,
            "input.key": None,
            "input.text": None,
            "input.click": None,
            "input.capability_probe_performed": False,
            "input.effect_ack": False,
            "reason": user_desktop_issue,
            "limitations": [
                "Live window and input capabilities are not probed by doctor; "
                "select user-desktop explicitly with the capabilities command."
            ],
        },
        "safety": {
            "default_scope": "agent-session",
            "silent_fallback_to_user_desktop": False,
            "explicit_user_desktop_command_required": True,
            "os_security_sandbox": False,
        },
    }

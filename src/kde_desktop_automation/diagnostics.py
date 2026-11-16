from __future__ import annotations

import ctypes.util
import os
import shutil
from typing import Any

from kde_desktop_automation import __version__
from kde_desktop_automation.backends.screenshot_helper import screenshot_helper_status
from kde_desktop_automation.backends.user_desktop import user_desktop_environment_issue
from kde_desktop_automation.process import pidfd_supported

_TOOLS = (
    "kwin_wayland",
    "dbus-daemon",
    "wlpdisplays",
    "kde-desktop-screenshot",
    "kdotool",
    "dotool",
    "kdocktor",
    "playerctl",
    "wpctl",
    "pactl",
)


def build_doctor_report() -> dict[str, Any]:
    tools = {name: shutil.which(name) for name in _TOOLS}
    screenshot_status = screenshot_helper_status()
    helper_path = screenshot_status["helper"]
    tools["kde-desktop-screenshot"] = helper_path if isinstance(helper_path, str) else None
    kwin = tools["kwin_wayland"] is not None
    bus = tools["dbus-daemon"] is not None
    displays = tools["wlpdisplays"] is not None
    screenshot_helper_ready = screenshot_status["ready"] is True
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
        "screenshot_helper": screenshot_status,
        "agent_session": {
            "backend": "kwin-virtual",
            "create": kwin and bus and displays and process_control,
            "display.inspect": displays,
            "screenshot.capture": screenshot_helper_ready,
            "screenshot.window.capture": None if screenshot_helper_ready else False,
            "session.stop": process_control,
            "window.list": False,
            "window.inspect": False,
            "window.activate": False,
            "window.set_minimized": False,
            "window.protocol_probe_available": kwin and wayland_client,
            "input.active_window_guard": False,
            "input.global_click": False,
            "input.global_drag": False,
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
            "window.set_minimized": None,
            "window.capability_probe_performed": False,
            "screenshot.capture": user_desktop_ready and screenshot_helper_ready,
            "screenshot.window.capture": (
                None if user_desktop_ready and screenshot_helper_ready else False
            ),
            "input.protocol_version": None,
            "input.key": None,
            "input.text": None,
            "input.click": None,
            "input.active_window_guard": None,
            "input.global_click": None,
            "input.global_drag": None,
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

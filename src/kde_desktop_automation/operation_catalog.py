"""Canonical contract connecting shared operations to CLI and MCP transports.

CLI and MCP adapters keep transport-specific argument validation and rendering,
but every public ``DesktopOperations`` method must appear here with one route on
each surface. The parity test compares this catalog against the live registries.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class OperationSurface:
    operation: str
    cli_route: tuple[str, ...]
    mcp_tool: str


SHARED_OPERATION_CATALOG = (
    OperationSurface("window_list", ("window", "list"), "kde_window_list"),
    OperationSurface("window_find", ("window", "find"), "kde_window_find"),
    OperationSurface("window_wait", ("window", "wait"), "kde_window_wait"),
    OperationSurface("window_inspect", ("window", "inspect"), "kde_window_inspect"),
    OperationSurface("window_activate", ("window", "activate"), "kde_window_activate"),
    OperationSurface(
        "window_set_minimized",
        ("window", "set-minimized"),
        "kde_window_set_minimized",
    ),
    OperationSurface(
        "window_set_geometry",
        ("window", "set-geometry"),
        "kde_window_set_geometry",
    ),
    OperationSurface("screenshot_window", ("window", "screenshot"), "kde_screenshot_window"),
    OperationSurface("pointer_position", ("pointer", "position"), "kde_pointer_position"),
    OperationSurface("observe", ("observe",), "kde_desktop_observe"),
    OperationSurface("input_key", ("input", "key"), "kde_input_key"),
    OperationSurface("input_type", ("input", "type"), "kde_input_type"),
    OperationSurface("input_click", ("input", "click"), "kde_input_click"),
    OperationSurface("input_click_at", ("input", "click-at"), "kde_input_click_at"),
    OperationSurface("input_drag", ("input", "drag"), "kde_input_drag"),
    OperationSurface("display_list", ("display", "list"), "kde_display_list"),
    OperationSurface("media_players", ("media", "list"), "kde_media_list"),
    OperationSurface("media_inspect", ("media", "inspect"), "kde_media_inspect"),
    OperationSurface("media_control", ("media", "control"), "kde_media_control"),
    OperationSurface("media_seek_to", ("media", "seek-to"), "kde_media_seek_to"),
    OperationSurface("audio_list", ("audio", "list"), "kde_audio_list"),
    OperationSurface("audio_set_default", ("audio", "set-default"), "kde_audio_set_default"),
    OperationSurface("audio_set_volume", ("audio", "set-volume"), "kde_audio_set_volume"),
    OperationSurface("audio_set_mute", ("audio", "set-mute"), "kde_audio_set_mute"),
    OperationSurface("audio_route_stream", ("audio", "route-stream"), "kde_audio_route_stream"),
    OperationSurface("screenshot", ("screenshot",), "kde_screenshot"),
    OperationSurface("capabilities", ("capabilities",), "kde_capabilities"),
)

SHARED_CLI_ROOTS = frozenset(
    {
        "observe",
        "screenshot",
        "capabilities",
        "window",
        "input",
        "pointer",
        "display",
        "media",
        "audio",
    }
)

MCP_TRANSPORT_ONLY_TOOLS = frozenset(
    {
        "kde_session_list",
        "kde_session_create",
        "kde_session_show",
        "kde_session_use",
        "kde_session_stop",
        "kde_session_launch",
    }
)

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal, cast

import typer
from rich.console import Console
from rich.table import Table
from typer import Context

from kde_desktop_automation.backends.kwin_input import (
    DEFAULT_DRAG_DURATION_MS,
    DEFAULT_DRAG_END_PAUSE_MS,
    DEFAULT_DRAG_HOLD_MS,
    MAX_DRAG_DURATION_MS,
    MAX_DRAG_PAUSE_MS,
    MAX_DRAG_STEPS,
)
from kde_desktop_automation.backends.user_desktop import UserDesktopBackend
from kde_desktop_automation.diagnostics import build_doctor_report
from kde_desktop_automation.errors import InteractionError
from kde_desktop_automation.operations import DesktopOperations
from kde_desktop_automation.paths import RuntimePaths
from kde_desktop_automation.sessions import SessionManager

app = typer.Typer(
    name="kde-desktop",
    help=(
        "Inspect and operate KDE through explicit, session-scoped targets. "
        "For shared commands, put --session before the command group; without it, "
        "the private default is used, never the live desktop."
    ),
    no_args_is_help=True,
    add_completion=False,
)
session_app = typer.Typer(help="Create and manage private agent sessions.", no_args_is_help=True)
app.add_typer(session_app, name="session")
user_desktop_app = typer.Typer(
    help="Explicitly inspect and operate the live user's KDE desktop.",
    no_args_is_help=True,
)
user_window_app = typer.Typer(
    help="Inspect, focus, set frame geometry, and minimize/restore live desktop windows.",
    no_args_is_help=True,
)
user_input_app = typer.Typer(
    help="Submit window-targeted input or global pointer actions to the live desktop.",
    no_args_is_help=True,
)
window_app = typer.Typer(
    help=(
        "List, inspect, find, capture, activate, and set window geometry in the selected session. "
        "Select it with global --session before 'window'."
    ),
    no_args_is_help=True,
)
pointer_app = typer.Typer(
    help="Read the pointer position in the selected session without moving it.",
    no_args_is_help=True,
)
input_app = typer.Typer(
    help=(
        "Send input to a selected window or use global desktop pointer coordinates; "
        "choose the target with --session before 'input'."
    ),
    no_args_is_help=True,
)
display_app = typer.Typer(
    help="Inspect displays in the selected session; choose it with --session before 'display'.",
    no_args_is_help=True,
)
media_app = typer.Typer(
    help=(
        "Inspect and control MPRIS media players in the selected session. "
        "Choose it with global --session before 'media'."
    ),
    no_args_is_help=True,
)
audio_app = typer.Typer(
    help=(
        "Inspect and control live PipeWire audio devices and output streams. "
        "Live audio only: put --session user-desktop before 'audio'; "
        "private sessions are unsupported."
    ),
    no_args_is_help=True,
)
app.add_typer(user_desktop_app, name="user-desktop")
user_desktop_app.add_typer(user_window_app, name="window")
user_desktop_app.add_typer(user_input_app, name="input")
app.add_typer(window_app, name="window")
app.add_typer(input_app, name="input")
app.add_typer(pointer_app, name="pointer")
app.add_typer(display_app, name="display")
app.add_typer(media_app, name="media")
app.add_typer(audio_app, name="audio")

console = Console()
MediaControlAction = Literal["play", "pause", "play-pause", "stop", "next", "previous", "seek"]
WindowMinimizedState = Literal["minimized", "restored"]
WindowFullscreenState = Literal["fullscreen", "restored"]
WindowMaximizedState = Literal["maximized", "restored"]
MouseButton = Literal["left", "middle", "right"]


@app.callback()
def configure_target(
    ctx: Context,
    session: str | None = typer.Option(
        None,
        "--session",
        help=(
            "Before the command group, target a private session ID or explicitly "
            "'user-desktop'; overrides KDE_DESKTOP_SESSION and the private default. "
            "Audio commands require user-desktop."
        ),
    ),
) -> None:
    """Select an explicit target; without one, shared commands use the private default."""
    ctx.ensure_object(dict)
    ctx.obj["session"] = session


@app.command()
def doctor(
    json_output: bool = typer.Option(False, "--json", help="Emit a versioned JSON envelope."),
) -> None:
    """Report installed tools and safe, currently implemented capabilities."""
    _run(lambda: (build_doctor_report(), []), json_output=json_output)


@app.command()
def observe(
    ctx: Context,
    screenshot_output: Path | None = typer.Option(
        None,
        "--screenshot-output",
        help="Optionally capture a screenshot to this path and include its coordinate mapping.",
    ),
    overwrite: bool = typer.Option(
        False,
        "--overwrite",
        help="Replace an existing screenshot path; requires --screenshot-output.",
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit a versioned JSON envelope."),
) -> None:
    """Observe windows, displays, pointer/focus context, and optionally a screenshot."""

    def action() -> tuple[dict[str, Any], list[str]]:
        if overwrite and screenshot_output is None:
            raise InteractionError(
                "invalid_observe_options",
                "--overwrite requires --screenshot-output.",
            )
        return (
            _operations(ctx).observe(screenshot_output=screenshot_output, overwrite=overwrite),
            [],
        )

    _run(
        action,
        json_output=json_output,
    )


@session_app.command("create")
def create_session(
    name: str = typer.Option("agent", help="Human-readable label for the private session."),
    width: int = typer.Option(1280, min=320, max=7680, help="Virtual output width in pixels."),
    height: int = typer.Option(800, min=240, max=4320, help="Virtual output height in pixels."),
    make_default: bool = typer.Option(
        False,
        "--default",
        help="Make this private session the default for shared commands without --session.",
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit a versioned JSON envelope."),
) -> None:
    """Start a private KWin virtual output, separate from the user's compositor."""

    def action() -> tuple[dict[str, Any], list[str]]:
        manager = _manager()
        session = manager.create_agent_session(
            name=name,
            width=width,
            height=height,
            make_default=make_default,
        )
        return manager.public_record(session), []

    _run(action, json_output=json_output)


@session_app.command("list")
def list_sessions(
    json_output: bool = typer.Option(False, "--json", help="Emit a versioned JSON envelope."),
) -> None:
    """List private sessions created by this CLI; does not inspect the user desktop."""
    _run(lambda: (_manager().list_sessions(), []), json_output=json_output)


@session_app.command("show")
def show_session(
    session_id: str = typer.Argument(..., help="Managed session ID."),
    json_output: bool = typer.Option(False, "--json", help="Emit a versioned JSON envelope."),
) -> None:
    """Show status and isolation properties for one private session."""
    _run(lambda: (_manager().get_session(session_id), []), json_output=json_output)


@session_app.command("displays")
def list_session_displays(
    session_id: str = typer.Argument(..., help="Managed session ID."),
    json_output: bool = typer.Option(False, "--json", help="Emit a versioned JSON envelope."),
) -> None:
    """Read logical output geometry from the private session using wlpdisplays."""
    _run(lambda: (_manager().displays(session_id), []), json_output=json_output)


@session_app.command("screenshot")
def screenshot_session(
    session_id: str = typer.Argument(..., help="Managed session ID."),
    output: Path = typer.Option(..., "--output", "-o", help="Destination PNG path."),
    overwrite: bool = typer.Option(False, "--overwrite", help="Replace an existing output file."),
    json_output: bool = typer.Option(False, "--json", help="Emit a versioned JSON envelope."),
) -> None:
    """Capture only the private virtual output through KWin Screenshot2."""
    _run(
        lambda: (_manager().screenshot(session_id, output, overwrite=overwrite), []),
        json_output=json_output,
    )


@session_app.command("launch")
def launch_application(
    session_id: str = typer.Argument(..., help="Managed private session ID."),
    command: list[str] = typer.Argument(
        ...,
        metavar="COMMAND [ARGS]...",
        help="Executable and arguments, passed directly without a shell.",
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit a versioned JSON envelope."),
) -> None:
    """Launch an application with the selected private session's Wayland and D-Bus."""
    _run(
        lambda: (_manager().launch_application(session_id, tuple(command)), []),
        json_output=json_output,
    )


@session_app.command("key")
def send_key(
    session_id: str = typer.Argument(..., help="Managed private session ID."),
    chord: str = typer.Argument(..., help="Key or chord, for example CTRL+S or ENTER."),
    json_output: bool = typer.Option(False, "--json", help="Emit a versioned JSON envelope."),
) -> None:
    """Submit one key or chord to the private session's current keyboard focus."""
    _run(lambda: (_manager().send_key(session_id, chord), []), json_output=json_output)


@session_app.command("type")
def type_text(
    session_id: str = typer.Argument(..., help="Managed private session ID."),
    text: str = typer.Argument(..., help="Text to submit to the current keyboard focus."),
    json_output: bool = typer.Option(False, "--json", help="Emit a versioned JSON envelope."),
) -> None:
    """Submit literal text to the private session's current keyboard focus."""
    _run(lambda: (_manager().send_text(session_id, text), []), json_output=json_output)


@session_app.command("click")
def click_session(
    session_id: str = typer.Argument(..., help="Managed private session ID."),
    x: int = typer.Argument(..., help="Horizontal logical output pixel."),
    y: int = typer.Argument(..., help="Vertical logical output pixel."),
    button: str = typer.Option("left", "--button", help="Mouse button: left, middle, or right."),
    json_output: bool = typer.Option(False, "--json", help="Emit a versioned JSON envelope."),
) -> None:
    """Submit a click at an explicit point inside the private output."""
    _run(lambda: (_manager().click(session_id, x, y, button), []), json_output=json_output)


@session_app.command("stop")
def stop_session(
    session_id: str = typer.Argument(..., help="Managed session ID."),
    json_output: bool = typer.Option(False, "--json", help="Emit a versioned JSON envelope."),
) -> None:
    """Stop launched applications and the recorded private KWin/D-Bus processes."""
    _run(lambda: _manager().stop(session_id), json_output=json_output)


@session_app.command("use")
def use_session(
    session_id: str = typer.Argument(..., help="Running managed session ID to make the default."),
    json_output: bool = typer.Option(False, "--json", help="Emit a versioned JSON envelope."),
) -> None:
    """Select the private session used when a shared command omits --session."""
    _run(lambda: (_manager().set_default_session(session_id), []), json_output=json_output)


@window_app.command("list")
def list_windows(
    ctx: Context,
    json_output: bool = typer.Option(False, "--json", help="Emit a versioned JSON envelope."),
) -> None:
    """List windows for the selected target; set global --session before 'window'."""
    _run(lambda: (_operations(ctx).window_list(), []), json_output=json_output)


@window_app.command("find")
def find_window(
    ctx: Context,
    app_id: str | None = typer.Option(None, "--app-id", help="Exact KDE desktop file or class ID."),
    title: str | None = typer.Option(None, "--title", help="Case-insensitive title substring."),
    json_output: bool = typer.Option(False, "--json", help="Emit a versioned JSON envelope."),
) -> None:
    """Find exactly one window; ambiguous selectors return candidates instead of guessing."""
    _run(
        lambda: (_operations(ctx).window_find(app_id=app_id, title=title), []),
        json_output=json_output,
    )


@window_app.command("inspect")
def inspect_window(
    ctx: Context,
    window_id: str = typer.Argument(
        ..., help="UUID from window list/find for the selected session."
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit a versioned JSON envelope."),
) -> None:
    """Inspect title, app ID, fullscreen, minimized, active state, and geometry."""
    _run(lambda: (_operations(ctx).window_inspect(window_id), []), json_output=json_output)


@window_app.command("screenshot")
def screenshot_window(
    ctx: Context,
    window_id: str = typer.Argument(
        ..., help="UUID from window list/find for the selected target."
    ),
    output: Path = typer.Option(..., "--output", "-o", help="Destination PNG path."),
    overwrite: bool = typer.Option(False, "--overwrite", help="Replace an existing output file."),
    json_output: bool = typer.Option(False, "--json", help="Emit a versioned JSON envelope."),
) -> None:
    """Capture one selected window directly, without activating it."""
    _run(
        lambda: (
            _operations(ctx).screenshot_window(window_id, output, overwrite=overwrite),
            [],
        ),
        json_output=json_output,
    )


@window_app.command("activate")
def activate_window(
    ctx: Context,
    window_id: str = typer.Argument(
        ..., help="UUID from window list/find for the selected session."
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit a versioned JSON envelope."),
) -> None:
    """Activate one window and report whether KWin confirmed it became active."""
    _run(lambda: (_operations(ctx).window_activate(window_id), []), json_output=json_output)


@window_app.command("set-minimized")
def set_window_minimized(
    ctx: Context,
    window_id: str = typer.Argument(
        ..., help="UUID from window list/find for the selected session."
    ),
    state: WindowMinimizedState = typer.Argument(..., help="Desired state: minimized or restored."),
    json_output: bool = typer.Option(False, "--json", help="Emit a versioned JSON envelope."),
) -> None:
    """Set and verify a window's minimized state; restoring reverses minimization."""
    _run(
        lambda: (_operations(ctx).window_set_minimized(window_id, state == "minimized"), []),
        json_output=json_output,
    )


@window_app.command("set-fullscreen")
def set_window_fullscreen(
    ctx: Context,
    window_id: str = typer.Argument(
        ..., help="UUID from window list/find for the selected session."
    ),
    state: WindowFullscreenState = typer.Argument(
        ..., help="Desired state: fullscreen or restored."
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit a versioned JSON envelope."),
) -> None:
    """Set or restore a window's fullscreen state and verify KWin's readback."""
    _run(
        lambda: (
            _operations(ctx).window_set_fullscreen(window_id, state == "fullscreen"),
            [],
        ),
        json_output=json_output,
    )


@window_app.command("set-maximized")
def set_window_maximized(
    ctx: Context,
    window_id: str = typer.Argument(
        ..., help="UUID from window list/find for the selected session."
    ),
    state: WindowMaximizedState = typer.Argument(..., help="Desired state: maximized or restored."),
    json_output: bool = typer.Option(False, "--json", help="Emit a versioned JSON envelope."),
) -> None:
    """Set or restore a window's maximized state and verify KWin's readback."""
    _run(
        lambda: (
            _operations(ctx).window_set_maximized(window_id, state == "maximized"),
            [],
        ),
        json_output=json_output,
    )


@window_app.command("set-geometry")
def set_window_geometry(
    ctx: Context,
    window_id: str = typer.Argument(
        ..., help="UUID from window list/find for the selected session."
    ),
    x: int = typer.Argument(..., help="Requested KWin frame x position in logical desktop pixels."),
    y: int = typer.Argument(..., help="Requested KWin frame y position in logical desktop pixels."),
    width: int = typer.Argument(..., help="Requested frame width in logical desktop pixels."),
    height: int = typer.Argument(..., help="Requested frame height in logical desktop pixels."),
    json_output: bool = typer.Option(False, "--json", help="Emit a versioned JSON envelope."),
) -> None:
    """Set an explicit KWin frame rectangle and verify its effective geometry."""
    _run(
        lambda: (_operations(ctx).window_set_geometry(window_id, x, y, width, height), []),
        json_output=json_output,
    )


@window_app.command("wait")
def wait_for_window(
    ctx: Context,
    app_id: str | None = typer.Option(None, "--app-id", help="Exact KDE desktop file or class ID."),
    title: str | None = typer.Option(None, "--title", help="Case-insensitive title substring."),
    timeout: float = typer.Option(
        10.0,
        "--timeout",
        min=0.1,
        max=60.0,
        help="Maximum time to wait for one matching window (0.1–60 seconds).",
    ),
    fullscreen: bool = typer.Option(
        False,
        "--fullscreen",
        help="Wait until KWin reports the matching window as fullscreen.",
    ),
    fills_display: bool = typer.Option(
        False,
        "--fills-display",
        help="Wait until matching window geometry covers a logical display.",
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit a versioned JSON envelope."),
) -> None:
    """Wait for a unique window and optional fullscreen/fills-display state."""
    _run(
        lambda: (
            _operations(ctx).window_wait(
                app_id=app_id,
                title=title,
                timeout=timeout,
                fullscreen=fullscreen,
                fills_display=fills_display,
            ),
            [],
        ),
        json_output=json_output,
    )


@input_app.command("key")
def input_key(
    ctx: Context,
    window_id: str = typer.Argument(..., help="Target window UUID from window list/find."),
    chord: str = typer.Argument(..., help="Key or chord, for example CTRL+END or CTRL+S."),
    json_output: bool = typer.Option(False, "--json", help="Emit a versioned JSON envelope."),
) -> None:
    """Submit a key chord to the named window, which must already be active."""
    _run(lambda: (_operations(ctx).input_key(window_id, chord), []), json_output=json_output)


@input_app.command("type")
def input_type(
    ctx: Context,
    window_id: str = typer.Argument(..., help="Target window UUID from window list/find."),
    text: str = typer.Argument(..., help="Literal text; use shell quoting for newlines."),
    json_output: bool = typer.Option(False, "--json", help="Emit a versioned JSON envelope."),
) -> None:
    """Submit text to the named window, which must already be active."""
    _run(lambda: (_operations(ctx).input_type(window_id, text), []), json_output=json_output)


@input_app.command("click")
def input_click(
    ctx: Context,
    window_id: str = typer.Argument(..., help="Target window UUID from window list/find."),
    x: float = typer.Argument(..., help="Normalized horizontal window coordinate from 0 to 1."),
    y: float = typer.Argument(..., help="Normalized vertical window coordinate from 0 to 1."),
    button: str = typer.Option("left", "--button", help="Mouse button: left, middle, or right."),
    json_output: bool = typer.Option(False, "--json", help="Emit a versioned JSON envelope."),
) -> None:
    """Click a normalized window-relative point; the named window must be active."""
    _run(
        lambda: (_operations(ctx).input_click(window_id, x, y, button=button), []),
        json_output=json_output,
    )


@input_app.command("click-at")
def input_click_at(
    ctx: Context,
    x: int = typer.Argument(..., help="Logical desktop x coordinate; negative origins are valid."),
    y: int = typer.Argument(..., help="Logical desktop y coordinate; negative origins are valid."),
    button: MouseButton = typer.Option("left", "--button", help="Mouse button."),
    json_output: bool = typer.Option(False, "--json", help="Emit a versioned JSON envelope."),
) -> None:
    """Click a global logical-desktop point, not a window-relative point."""
    _run(
        lambda: (_operations(ctx).input_click_at(x, y, button=button), []),
        json_output=json_output,
    )


@input_app.command("drag")
def input_drag(
    ctx: Context,
    start_x: int = typer.Argument(..., help="Logical desktop start x coordinate."),
    start_y: int = typer.Argument(..., help="Logical desktop start y coordinate."),
    end_x: int = typer.Argument(..., help="Logical desktop end x coordinate."),
    end_y: int = typer.Argument(..., help="Logical desktop end y coordinate."),
    button: MouseButton = typer.Option("left", "--button", help="Mouse button held during drag."),
    steps: int = typer.Option(
        8,
        min=1,
        max=MAX_DRAG_STEPS,
        help=f"Interpolated movement steps (1–{MAX_DRAG_STEPS}).",
    ),
    duration_ms: int = typer.Option(
        DEFAULT_DRAG_DURATION_MS,
        "--duration-ms",
        min=0,
        max=MAX_DRAG_DURATION_MS,
        help="Time spent moving along the drag path (0 disables pacing).",
    ),
    hold_ms: int = typer.Option(
        DEFAULT_DRAG_HOLD_MS,
        "--hold-ms",
        min=0,
        max=MAX_DRAG_PAUSE_MS,
        help="Time to hold the button at the start before movement.",
    ),
    end_pause_ms: int = typer.Option(
        DEFAULT_DRAG_END_PAUSE_MS,
        "--end-pause-ms",
        min=0,
        max=MAX_DRAG_PAUSE_MS,
        help="Time to pause at the destination before releasing the button.",
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit a versioned JSON envelope."),
) -> None:
    """Drag between global logical-desktop points; every sampled point must be on a display."""
    _run(
        lambda: (
            _operations(ctx).input_drag(
                start_x,
                start_y,
                end_x,
                end_y,
                button=button,
                steps=steps,
                duration_ms=duration_ms,
                hold_ms=hold_ms,
                end_pause_ms=end_pause_ms,
            ),
            [],
        ),
        json_output=json_output,
    )


@display_app.command("list")
def list_displays(
    ctx: Context,
    json_output: bool = typer.Option(False, "--json", help="Emit a versioned JSON envelope."),
) -> None:
    """List logical displays for the selected session."""
    _run(lambda: (_operations(ctx).display_list(), []), json_output=json_output)


@pointer_app.command("position")
def pointer_position(
    ctx: Context,
    json_output: bool = typer.Option(False, "--json", help="Emit a versioned JSON envelope."),
) -> None:
    """Read the current pointer position without generating input."""
    _run(lambda: (_operations(ctx).pointer_position(), []), json_output=json_output)


@media_app.command("list")
def list_media_players(
    ctx: Context,
    json_output: bool = typer.Option(False, "--json", help="Emit a versioned JSON envelope."),
) -> None:
    """List MPRIS players registered on the selected session's D-Bus."""
    _run(lambda: (_operations(ctx).media_players(), []), json_output=json_output)


@media_app.command("inspect")
def inspect_media_player(
    ctx: Context,
    player: str = typer.Argument(..., help="Exact player name returned by media list."),
    json_output: bool = typer.Option(False, "--json", help="Emit a versioned JSON envelope."),
) -> None:
    """Read playback status, track metadata, position, and duration for one player."""
    _run(lambda: (_operations(ctx).media_inspect(player), []), json_output=json_output)


@media_app.command("control")
def control_media_player(
    ctx: Context,
    player: str = typer.Argument(..., help="Exact player name returned by media list."),
    action: MediaControlAction = typer.Argument(
        ...,
        help="Transport action or relative seek; use media seek-to for an absolute position.",
    ),
    seconds: float | None = typer.Option(
        None,
        "--seconds",
        min=-86_400,
        max=86_400,
        help="Non-zero relative seek offset; required for seek, in seconds (negative rewinds).",
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit a versioned JSON envelope."),
) -> None:
    """Submit one typed MPRIS transport request to one exact player."""
    _run(
        lambda: (_operations(ctx).media_control(player, action, seconds=seconds), []),
        json_output=json_output,
    )


@media_app.command("seek-to")
def seek_media_player_to(
    ctx: Context,
    player: str = typer.Argument(..., help="Exact player name returned by media list."),
    position_seconds: float = typer.Argument(
        ...,
        min=0,
        max=86_400,
        help="Absolute playback position in seconds (0–86400).",
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit a versioned JSON envelope."),
) -> None:
    """Submit an absolute seek to one exact MPRIS player; inspect afterward to verify it."""
    _run(
        lambda: (_operations(ctx).media_seek_to(player, position_seconds), []),
        json_output=json_output,
    )


@audio_app.command("list")
def list_audio(
    ctx: Context,
    json_output: bool = typer.Option(False, "--json", help="Emit a versioned JSON envelope."),
) -> None:
    """List PipeWire devices and streams; live only, use --session user-desktop before audio."""
    _run(lambda: (_operations(ctx).audio_list(), []), json_output=json_output)


@audio_app.command("set-default")
def set_audio_default(
    ctx: Context,
    direction: Literal["sink", "source"] = typer.Argument(
        ..., help="sink for output or source for input."
    ),
    device_name: str = typer.Argument(..., help="Exact device name returned by audio list."),
    json_output: bool = typer.Option(False, "--json", help="Emit a versioned JSON envelope."),
) -> None:
    """Set and verify a default device; live only, use --session user-desktop before audio."""
    _run(
        lambda: (_operations(ctx).audio_set_default(direction, device_name), []),
        json_output=json_output,
    )


@audio_app.command("set-volume")
def set_audio_volume(
    ctx: Context,
    direction: Literal["sink", "source"] = typer.Argument(
        ..., help="sink for output or source for input."
    ),
    device_name: str = typer.Argument(..., help="Exact device name returned by audio list."),
    volume_percent: float = typer.Argument(..., help="Absolute volume from 0 through 100 percent."),
    json_output: bool = typer.Option(False, "--json", help="Emit a versioned JSON envelope."),
) -> None:
    """Set and verify a device volume; live only, use --session user-desktop before audio."""
    _run(
        lambda: (
            _operations(ctx).audio_set_volume(direction, device_name, volume_percent),
            [],
        ),
        json_output=json_output,
    )


@audio_app.command("set-mute")
def set_audio_mute(
    ctx: Context,
    direction: Literal["sink", "source"] = typer.Argument(
        ..., help="sink for output or source for input."
    ),
    device_name: str = typer.Argument(..., help="Exact device name returned by audio list."),
    state: Literal["mute", "unmute"] = typer.Argument(..., help="Explicit mute state; no toggle."),
    json_output: bool = typer.Option(False, "--json", help="Emit a versioned JSON envelope."),
) -> None:
    """Set and verify device mute; live only, use --session user-desktop before audio."""
    _run(
        lambda: (_operations(ctx).audio_set_mute(direction, device_name, state == "mute"), []),
        json_output=json_output,
    )


@audio_app.command("route-stream")
def route_audio_stream(
    ctx: Context,
    stream_id: int = typer.Argument(..., help="Fresh sink-input stream ID from audio list."),
    sink_name: str = typer.Argument(..., help="Exact output sink name returned by audio list."),
    application_name: str = typer.Option(
        ...,
        "--application",
        help="Exact application name shown for this stream; guards against stale IDs.",
    ),
    process_id: int | None = typer.Option(
        None,
        "--process-id",
        help="Optional expected process ID for an additional identity check.",
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit a versioned JSON envelope."),
) -> None:
    """Move one verified stream; live only, use --session user-desktop before audio."""
    _run(
        lambda: (
            _operations(ctx).audio_route_stream(
                stream_id,
                sink_name,
                application_name=application_name,
                process_id=process_id,
            ),
            [],
        ),
        json_output=json_output,
    )


@app.command("screenshot")
def screenshot_target(
    ctx: Context,
    output: Path = typer.Option(..., "--output", "-o", help="Destination PNG path."),
    overwrite: bool = typer.Option(False, "--overwrite", help="Replace an existing output file."),
    json_output: bool = typer.Option(False, "--json", help="Emit a versioned JSON envelope."),
) -> None:
    """Capture the selected private output or the explicit live user desktop."""
    _run(
        lambda: (_operations(ctx).screenshot(output, overwrite=overwrite), []),
        json_output=json_output,
    )


@app.command("capabilities")
def target_capabilities(
    ctx: Context,
    json_output: bool = typer.Option(False, "--json", help="Emit a versioned JSON envelope."),
) -> None:
    """Report capabilities and limitations for the selected session target."""
    _run(lambda: (_operations(ctx).capabilities(), []), json_output=json_output)


@user_window_app.command("list")
def list_user_windows(
    json_output: bool = typer.Option(False, "--json", help="Emit a versioned JSON envelope."),
) -> None:
    """List windows in the live user desktop (an explicit user-desktop operation)."""
    _run(
        lambda: (_user_desktop_operations().window_list(), []),
        json_output=json_output,
    )


@user_window_app.command("find")
def find_user_window(
    app_id: str | None = typer.Option(None, "--app-id", help="Exact KDE desktop file or class ID."),
    title: str | None = typer.Option(None, "--title", help="Case-insensitive title substring."),
    json_output: bool = typer.Option(False, "--json", help="Emit a versioned JSON envelope."),
) -> None:
    """Find exactly one window; ambiguous selectors return candidates instead of guessing."""
    _run(
        lambda: (
            _user_desktop_operations().window_find(app_id=app_id, title=title),
            [],
        ),
        json_output=json_output,
    )


@user_window_app.command("inspect")
def inspect_user_window(
    window_id: str = typer.Argument(..., help="UUID from user-desktop window list/find."),
    json_output: bool = typer.Option(False, "--json", help="Emit a versioned JSON envelope."),
) -> None:
    """Read live window identity, geometry, fullscreen, and active state."""
    _run(
        lambda: (_user_desktop_operations().window_inspect(window_id), []),
        json_output=json_output,
    )


@user_window_app.command("screenshot")
def screenshot_user_window(
    window_id: str = typer.Argument(..., help="UUID from user-desktop window list/find."),
    output: Path = typer.Option(..., "--output", "-o", help="Destination PNG path."),
    overwrite: bool = typer.Option(False, "--overwrite", help="Replace an existing output file."),
    json_output: bool = typer.Option(False, "--json", help="Emit a versioned JSON envelope."),
) -> None:
    """Capture one live window directly through KWin without activating it."""
    _run(
        lambda: (
            _user_desktop_operations().screenshot_window(
                window_id,
                output,
                overwrite=overwrite,
            ),
            [],
        ),
        json_output=json_output,
    )


@user_window_app.command("activate")
def activate_user_window(
    window_id: str = typer.Argument(..., help="UUID from user-desktop window list/find."),
    json_output: bool = typer.Option(False, "--json", help="Emit a versioned JSON envelope."),
) -> None:
    """Explicitly activate a live user window and verify KWin reports it active."""
    _run(
        lambda: (_user_desktop_operations().window_activate(window_id), []),
        json_output=json_output,
    )


@user_window_app.command("set-minimized")
def set_user_window_minimized(
    window_id: str = typer.Argument(..., help="UUID from user-desktop window list/find."),
    state: WindowMinimizedState = typer.Argument(..., help="Desired state: minimized or restored."),
    json_output: bool = typer.Option(False, "--json", help="Emit a versioned JSON envelope."),
) -> None:
    """Set and verify a live window's minimized state; this may change user focus."""
    _run(
        lambda: (
            _user_desktop_operations().window_set_minimized(window_id, state == "minimized"),
            [],
        ),
        json_output=json_output,
    )


@user_window_app.command("set-fullscreen")
def set_user_window_fullscreen(
    window_id: str = typer.Argument(..., help="UUID from user-desktop window list/find."),
    state: WindowFullscreenState = typer.Argument(
        ..., help="Desired state: fullscreen or restored."
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit a versioned JSON envelope."),
) -> None:
    """Set or restore fullscreen on a live window; this affects the real workspace."""
    _run(
        lambda: (
            _user_desktop_operations().window_set_fullscreen(window_id, state == "fullscreen"),
            [],
        ),
        json_output=json_output,
    )


@user_window_app.command("set-maximized")
def set_user_window_maximized(
    window_id: str = typer.Argument(..., help="UUID from user-desktop window list/find."),
    state: WindowMaximizedState = typer.Argument(..., help="Desired state: maximized or restored."),
    json_output: bool = typer.Option(False, "--json", help="Emit a versioned JSON envelope."),
) -> None:
    """Set or restore maximization on a live window; this affects the real workspace."""
    _run(
        lambda: (
            _user_desktop_operations().window_set_maximized(window_id, state == "maximized"),
            [],
        ),
        json_output=json_output,
    )


@user_window_app.command("set-geometry")
def set_user_window_geometry(
    window_id: str = typer.Argument(..., help="UUID from user-desktop window list/find."),
    x: int = typer.Argument(..., help="Requested KWin frame x position in logical desktop pixels."),
    y: int = typer.Argument(..., help="Requested KWin frame y position in logical desktop pixels."),
    width: int = typer.Argument(..., help="Requested frame width in logical desktop pixels."),
    height: int = typer.Argument(..., help="Requested frame height in logical desktop pixels."),
    json_output: bool = typer.Option(False, "--json", help="Emit a versioned JSON envelope."),
) -> None:
    """Set and verify a live window's frame rectangle without intentionally changing focus."""
    _run(
        lambda: (
            _user_desktop_operations().window_set_geometry(window_id, x, y, width, height),
            [],
        ),
        json_output=json_output,
    )


@user_desktop_app.command("screenshot")
def screenshot_user_desktop(
    output: Path = typer.Option(..., "--output", "-o", help="Destination PNG path."),
    overwrite: bool = typer.Option(False, "--overwrite", help="Replace an existing output file."),
    json_output: bool = typer.Option(False, "--json", help="Emit a versioned JSON envelope."),
) -> None:
    """Capture the live user's full desktop; this does not capture a private session."""
    _run(
        lambda: (
            _user_desktop_operations().screenshot(output, overwrite=overwrite),
            [],
        ),
        json_output=json_output,
    )


@user_input_app.command("key")
def send_user_key(
    window_id: str = typer.Argument(..., help="Active window UUID from user-desktop window list."),
    chord: str = typer.Argument(..., help="Key or chord, for example CTRL+END or CTRL+S."),
    json_output: bool = typer.Option(False, "--json", help="Emit a versioned JSON envelope."),
) -> None:
    """Submit a key chord only if the named window is already active."""
    _run(
        lambda: (_user_desktop_operations().input_key(window_id, chord), []),
        json_output=json_output,
    )


@user_input_app.command("type")
def type_user_text(
    window_id: str = typer.Argument(..., help="Active window UUID from user-desktop window list."),
    text: str = typer.Argument(..., help="Literal text; use shell quoting for newlines."),
    json_output: bool = typer.Option(False, "--json", help="Emit a versioned JSON envelope."),
) -> None:
    """Submit literal text only if the named window is already active."""
    _run(
        lambda: (_user_desktop_operations().input_type(window_id, text), []),
        json_output=json_output,
    )


@user_input_app.command("click")
def click_user_window(
    window_id: str = typer.Argument(..., help="Active window UUID from user-desktop window list."),
    x: float = typer.Argument(..., help="Normalized horizontal window coordinate from 0 to 1."),
    y: float = typer.Argument(..., help="Normalized vertical window coordinate from 0 to 1."),
    json_output: bool = typer.Option(False, "--json", help="Emit a versioned JSON envelope."),
) -> None:
    """Click a normalized point in the named window, which must already be active."""
    _run(
        lambda: (_user_desktop_operations().input_click(window_id, x, y), []),
        json_output=json_output,
    )


@user_input_app.command("click-at")
def click_user_desktop_at(
    x: int = typer.Argument(..., help="Logical desktop x coordinate; negative origins are valid."),
    y: int = typer.Argument(..., help="Logical desktop y coordinate; negative origins are valid."),
    button: MouseButton = typer.Option("left", "--button", help="Mouse button."),
    json_output: bool = typer.Option(False, "--json", help="Emit a versioned JSON envelope."),
) -> None:
    """Click a global logical-desktop point; this can move the live pointer."""
    _run(
        lambda: (_user_desktop_operations().input_click_at(x, y, button=button), []),
        json_output=json_output,
    )


@user_input_app.command("drag")
def drag_user_desktop(
    start_x: int = typer.Argument(..., help="Logical desktop start x coordinate."),
    start_y: int = typer.Argument(..., help="Logical desktop start y coordinate."),
    end_x: int = typer.Argument(..., help="Logical desktop end x coordinate."),
    end_y: int = typer.Argument(..., help="Logical desktop end y coordinate."),
    button: MouseButton = typer.Option("left", "--button", help="Mouse button held during drag."),
    steps: int = typer.Option(
        8,
        min=1,
        max=MAX_DRAG_STEPS,
        help=f"Interpolated movement steps (1–{MAX_DRAG_STEPS}).",
    ),
    duration_ms: int = typer.Option(
        DEFAULT_DRAG_DURATION_MS,
        "--duration-ms",
        min=0,
        max=MAX_DRAG_DURATION_MS,
        help="Time spent moving along the drag path (0 disables pacing).",
    ),
    hold_ms: int = typer.Option(
        DEFAULT_DRAG_HOLD_MS,
        "--hold-ms",
        min=0,
        max=MAX_DRAG_PAUSE_MS,
        help="Time to hold the button at the start before movement.",
    ),
    end_pause_ms: int = typer.Option(
        DEFAULT_DRAG_END_PAUSE_MS,
        "--end-pause-ms",
        min=0,
        max=MAX_DRAG_PAUSE_MS,
        help="Time to pause at the destination before releasing the button.",
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit a versioned JSON envelope."),
) -> None:
    """Drag between live desktop points; every sampled point must be on a display."""
    _run(
        lambda: (
            _user_desktop_operations().input_drag(
                start_x,
                start_y,
                end_x,
                end_y,
                button=button,
                steps=steps,
                duration_ms=duration_ms,
                hold_ms=hold_ms,
                end_pause_ms=end_pause_ms,
            ),
            [],
        ),
        json_output=json_output,
    )


def _operations(ctx: Context) -> DesktopOperations:
    selected = cast(dict[str, Any], ctx.obj or {}).get("session")
    return DesktopOperations(
        session=selected,
        manager_factory=_manager,
        user_desktop_factory=UserDesktopBackend,
    )


def _user_desktop_operations() -> DesktopOperations:
    return DesktopOperations(user_desktop_factory=UserDesktopBackend, session="user-desktop")


def _manager() -> SessionManager:
    return SessionManager(RuntimePaths.from_environment())


def _run(
    action: Callable[[], tuple[Any, list[str]]],
    *,
    json_output: bool,
) -> None:
    try:
        data, warnings = action()
    except InteractionError as exc:
        envelope = {
            "schema_version": "1",
            "ok": False,
            "error": exc.as_dict(),
            "warnings": [],
        }
        if json_output:
            console.print_json(json.dumps(envelope))
        else:
            console.print(f"[bold red]{exc.code}[/bold red]: {exc.message}")
            if exc.details:
                console.print_json(json.dumps(exc.details))
        raise typer.Exit(code=2) from exc

    if json_output:
        console.print_json(
            json.dumps(
                {
                    "schema_version": "1",
                    "ok": True,
                    "data": data,
                    "warnings": warnings,
                }
            )
        )
        return

    _render_human(data)
    for warning in warnings:
        console.print(f"[yellow]Warning:[/yellow] {warning}")


def _render_human(data: Any) -> None:
    if isinstance(data, list):
        if not data:
            console.print("No sessions or displays.")
            return
        if all(isinstance(item, dict) for item in data):
            keys = list(dict.fromkeys(key for item in data for key in item))
            table = Table(*keys, header_style="bold cyan")
            for item in data:
                table.add_row(*(_display_value(item.get(key)) for key in keys))
            console.print(table)
            return
        console.print_json(json.dumps(data))
        return

    if isinstance(data, dict):
        table = Table(show_header=False, box=None, padding=(0, 1))
        for key, value in data.items():
            table.add_row(f"[bold]{key}[/bold]", _display_value(value))
        console.print(table)
        return

    console.print(data)


def _display_value(value: Any) -> str:
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    if value is None:
        return "—"
    return str(value)


def main() -> None:
    app()

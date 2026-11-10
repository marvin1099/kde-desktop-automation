from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import typer
from rich.console import Console
from rich.table import Table
from typer import Context

from kde_interaction.backends.user_desktop import UserDesktopBackend
from kde_interaction.diagnostics import build_doctor_report
from kde_interaction.errors import InteractionError
from kde_interaction.operations import DesktopOperations
from kde_interaction.paths import RuntimePaths
from kde_interaction.sessions import SessionManager

app = typer.Typer(
    name="kde-ai",
    help="Inspect and operate KDE through explicit, session-scoped targets.",
    no_args_is_help=True,
    add_completion=False,
)
session_app = typer.Typer(help="Create and manage private agent sessions.", no_args_is_help=True)
app.add_typer(session_app, name="session")
user_desktop_app = typer.Typer(
    help="Explicitly inspect and operate the live user's KDE desktop.",
    no_args_is_help=True,
)
user_window_app = typer.Typer(help="Inspect and focus live desktop windows.", no_args_is_help=True)
user_input_app = typer.Typer(
    help="Submit best-effort input to an explicitly selected active window.",
    no_args_is_help=True,
)
window_app = typer.Typer(
    help="List, inspect, find, and activate windows in the selected session.", no_args_is_help=True
)
input_app = typer.Typer(help="Send input to an explicitly selected window.", no_args_is_help=True)
display_app = typer.Typer(help="Inspect displays in the selected session.", no_args_is_help=True)
media_app = typer.Typer(
    help="Inspect and control MPRIS media players in the selected session.", no_args_is_help=True
)
app.add_typer(user_desktop_app, name="user-desktop")
user_desktop_app.add_typer(user_window_app, name="window")
user_desktop_app.add_typer(user_input_app, name="input")
app.add_typer(window_app, name="window")
app.add_typer(input_app, name="input")
app.add_typer(display_app, name="display")
app.add_typer(media_app, name="media")

console = Console()


@app.callback()
def configure_target(
    ctx: Context,
    session: str | None = typer.Option(
        None,
        "--session",
        help=(
            "Target a private session ID or explicitly 'user-desktop'; overrides "
            "KDE_AI_SESSION and the private default."
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
    """Capture only the private virtual output using Spectacle."""
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
    """List windows for the private default or explicit --session target."""
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


@display_app.command("list")
def list_displays(
    ctx: Context,
    json_output: bool = typer.Option(False, "--json", help="Emit a versioned JSON envelope."),
) -> None:
    """List logical displays for the selected session."""
    _run(lambda: (_operations(ctx).display_list(), []), json_output=json_output)


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
    action: str = typer.Argument(
        ...,
        help="play, pause, play-pause, stop, next, previous, or seek.",
    ),
    seconds: float | None = typer.Option(
        None,
        "--seconds",
        help="Relative seek offset; required for seek, in seconds (negative rewinds).",
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit a versioned JSON envelope."),
) -> None:
    """Submit one typed MPRIS transport request to one exact player."""
    _run(
        lambda: (_operations(ctx).media_control(player, action, seconds=seconds), []),
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

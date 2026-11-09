from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import typer
from rich.console import Console
from rich.table import Table

from kde_interaction.diagnostics import build_doctor_report
from kde_interaction.errors import InteractionError
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

console = Console()


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
    json_output: bool = typer.Option(False, "--json", help="Emit a versioned JSON envelope."),
) -> None:
    """Start a private KWin virtual output, separate from the user's compositor."""

    def action() -> tuple[dict[str, Any], list[str]]:
        manager = _manager()
        session = manager.create_agent_session(name=name, width=width, height=height)
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

from __future__ import annotations

import base64
import json
import logging
import os
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Annotated, Any

from mcp.server.fastmcp import FastMCP
from mcp.types import CallToolResult, ImageContent, TextContent, ToolAnnotations
from pydantic import Field

from kde_interaction.errors import InteractionError
from kde_interaction.operations import DesktopOperations
from kde_interaction.paths import RuntimePaths
from kde_interaction.sessions import SessionManager

_LOGGER = logging.getLogger(__name__)
_SERVER_INSTRUCTIONS = """\
KDE Interaction exposes session-scoped KDE window and input operations.

Use `session='user-desktop'` to select the real user desktop explicitly for that
call. Never infer or substitute the live desktop when a private session is
missing or stopped. The live target must always be explicitly passed.

Window input requires an already-active window. Activate it explicitly first.
Clicks use normalized window-relative coordinates from 0 to 1. Input uses KWin
fake input routed through compositor focus; the window ID is checked against
active focus, but delivery is not atomically bound to that window. Focus can
change during submission. KWin may ignore fake-input events, so a successful
tool call is not proof that an application received the action; verify with a
screenshot or independent application state.

Session IDs select a target; they are not per-client authorization tokens. MCP
clients sharing this server share the Unix user's authority over managed private
sessions, including the ability to list and stop them. `kde_session_use` changes
the persisted private default for all clients that omit a session. For a shared
multi-agent server, set KDE_AI_MCP_REQUIRE_EXPLICIT_SESSION=1 and pass each
managed session ID to every desktop tool call. Use session lifecycle tools only
through a coordinator that is authorized to manage the selected session.
"""
_OPTIONAL_SESSION_INSTRUCTIONS = """\
The `session` argument on desktop tools is optional. When omitted, tools use
KDE_AI_SESSION if it names a private session, otherwise the configured private
default. The server process may set KDE_AI_SESSION to pin unscoped calls to one
private session. For parallel agents, pass each managed session ID explicitly.
"""
_REQUIRED_SESSION_INSTRUCTIONS = """\
Every desktop tool call must include `session`; this server rejects omitted
selectors. Use `session='user-desktop'` only when explicitly opting into the
live desktop. KDE_AI_SESSION and the persisted private default do not replace the
required per-call argument.
"""

OperationsFactory = Callable[[str | None], DesktopOperations]
ManagerFactory = Callable[[], SessionManager]
TargetSession = Annotated[
    str | None,
    Field(
        description=(
            "Managed private session ID; when omitted, KDE_AI_SESSION or the private default "
            "is used unless this server requires an explicit selector. Use the exact value "
            "'user-desktop' to explicitly select the real desktop."
        )
    ),
]


def _default_manager() -> SessionManager:
    return SessionManager(RuntimePaths.from_environment())


def _success(data: dict[str, Any]) -> CallToolResult:
    envelope = {"schema_version": "1", "ok": True, "data": data}
    return CallToolResult(
        content=[TextContent(type="text", text=json.dumps(envelope, ensure_ascii=False))],
        structuredContent=envelope,
    )


def _failure(error: InteractionError) -> CallToolResult:
    envelope = {"schema_version": "1", "ok": False, "error": error.as_dict()}
    return CallToolResult(
        content=[TextContent(type="text", text=json.dumps(envelope, ensure_ascii=False))],
        structuredContent=envelope,
        isError=True,
    )


def _invoke(action: Callable[[], dict[str, Any]]) -> CallToolResult:
    try:
        return _success(action())
    except InteractionError as exc:
        return _failure(exc)


def create_mcp_server(
    *,
    operations_factory: OperationsFactory = DesktopOperations,
    manager_factory: ManagerFactory = _default_manager,
    require_explicit_session: bool = False,
) -> FastMCP:
    """Build the stdio MCP server; factories allow operation-level transport tests."""
    server = FastMCP(
        "kde-ai",
        instructions=_SERVER_INSTRUCTIONS
        + (
            _REQUIRED_SESSION_INSTRUCTIONS
            if require_explicit_session
            else _OPTIONAL_SESSION_INSTRUCTIONS
        ),
    )

    def desktop_operations(session: str | None) -> DesktopOperations:
        if require_explicit_session and session is None:
            raise InteractionError(
                "session_required",
                "This shared MCP server requires an explicit session ID on every "
                "desktop tool call.",
                details={"setting": "KDE_AI_MCP_REQUIRE_EXPLICIT_SESSION"},
            )
        return operations_factory(session)

    @server.tool(
        name="kde_session_list",
        description="List managed private KWin sessions and identify the private default.",
        annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False),
        structured_output=False,
    )
    def session_list() -> CallToolResult:
        """Return the user's managed private sessions. Does not inspect the live desktop."""
        return _invoke(lambda: {"sessions": manager_factory().list_sessions()})

    @server.tool(
        name="kde_session_create",
        description=(
            "Create an isolated private KWin session; never creates or attaches "
            "to the live desktop."
        ),
        annotations=ToolAnnotations(destructiveHint=False, openWorldHint=True),
        structured_output=False,
    )
    def session_create(
        name: str = "agent",
        width: int = 1280,
        height: int = 800,
        make_default: bool = False,
    ) -> CallToolResult:
        """Create a private desktop. The first session becomes default unless one already exists."""

        def create() -> dict[str, Any]:
            manager = manager_factory()
            record = manager.create_agent_session(
                name,
                width,
                height,
                make_default=make_default,
            )
            return manager.public_record(record)

        return _invoke(create)

    @server.tool(
        name="kde_session_show",
        description="Inspect a managed private KWin session by ID.",
        annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False),
        structured_output=False,
    )
    def session_show(session_id: str) -> CallToolResult:
        """Return status, isolation details, and capabilities for a private session."""
        return _invoke(lambda: manager_factory().get_session(session_id))

    @server.tool(
        name="kde_session_use",
        description=(
            "Set the persisted private default for all clients that omit session; "
            "this is shared server state, not a per-agent selection."
        ),
        annotations=ToolAnnotations(
            destructiveHint=False, idempotentHint=True, openWorldHint=False
        ),
        structured_output=False,
    )
    def session_use(session_id: str) -> CallToolResult:
        """Select a running private session as the default; cannot select user-desktop."""
        return _invoke(lambda: manager_factory().set_default_session(session_id))

    @server.tool(
        name="kde_session_stop",
        description=(
            "Stop a managed private session and its tracked applications. "
            "Does not affect the live desktop. On shared servers, use only through "
            "the coordinator authorized to manage that session."
        ),
        annotations=ToolAnnotations(destructiveHint=True, openWorldHint=True),
        structured_output=False,
    )
    def session_stop(session_id: str) -> CallToolResult:
        """Stop only the named private KWin session; its applications will be terminated."""
        return _invoke(lambda: _stop_private_session(manager_factory(), session_id))

    @server.tool(
        name="kde_session_launch",
        description=(
            "Launch an executable with argv inside a managed private KWin session; "
            "no shell is used."
        ),
        annotations=ToolAnnotations(destructiveHint=False, openWorldHint=True),
        structured_output=False,
    )
    def session_launch(session_id: str, argv: list[str]) -> CallToolResult:
        """Launch a process in a private session.

        argv[0] is the executable; shell parsing is not performed.
        """
        return _invoke(lambda: manager_factory().launch_application(session_id, tuple(argv)))

    @server.tool(
        name="kde_window_list",
        description="List windows in a private session or explicitly selected user-desktop.",
        annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False),
        structured_output=False,
    )
    def window_list(session: TargetSession = None) -> CallToolResult:
        """List window IDs, titles, app IDs, fullscreen/active state, and geometry."""
        return _invoke(lambda: desktop_operations(session).window_list())

    @server.tool(
        name="kde_window_find",
        description=(
            "Find exactly one window by app ID and/or title substring; "
            "ambiguity returns candidates."
        ),
        annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False),
        structured_output=False,
    )
    def window_find(
        app_id: str | None = None,
        title: str | None = None,
        session: TargetSession = None,
    ) -> CallToolResult:
        """Find one window. app_id is exact; title is a case-insensitive substring."""
        return _invoke(lambda: desktop_operations(session).window_find(app_id=app_id, title=title))

    @server.tool(
        name="kde_window_wait",
        description="Wait up to 60 seconds for one matching window to appear.",
        annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False),
        structured_output=False,
    )
    def window_wait(
        app_id: str | None = None,
        title: str | None = None,
        timeout: float = 10.0,
        session: TargetSession = None,
    ) -> CallToolResult:
        """Boundedly wait for a unique window match; ambiguous matches fail immediately."""
        return _invoke(
            lambda: desktop_operations(session).window_wait(
                app_id=app_id,
                title=title,
                timeout=timeout,
            )
        )

    @server.tool(
        name="kde_window_inspect",
        description="Inspect one window using a UUID from the same selected session.",
        annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False),
        structured_output=False,
    )
    def window_inspect(window_id: str, session: TargetSession = None) -> CallToolResult:
        """Inspect a target window's state, geometry, app ID, and title."""
        return _invoke(lambda: desktop_operations(session).window_inspect(window_id))

    @server.tool(
        name="kde_window_activate",
        description="Activate a selected window. user-desktop activation changes real user focus.",
        annotations=ToolAnnotations(destructiveHint=False, openWorldHint=True),
        structured_output=False,
    )
    def window_activate(window_id: str, session: TargetSession = None) -> CallToolResult:
        """Activate by session-scoped UUID; live activation may switch desktops and move focus."""
        return _invoke(lambda: desktop_operations(session).window_activate(window_id))

    @server.tool(
        name="kde_input_key",
        description="Send a key chord to an already-active target window; no implicit activation.",
        annotations=ToolAnnotations(destructiveHint=True, openWorldHint=True),
        structured_output=False,
    )
    def input_key(window_id: str, chord: str, session: TargetSession = None) -> CallToolResult:
        """Submit a key chord (for example CTRL+S) to the named, already-active window."""
        return _invoke(lambda: desktop_operations(session).input_key(window_id, chord))

    @server.tool(
        name="kde_input_type",
        description=(
            "Type literal text into an already-active target window; no implicit activation."
        ),
        annotations=ToolAnnotations(destructiveHint=True, openWorldHint=True),
        structured_output=False,
    )
    def input_type(window_id: str, text: str, session: TargetSession = None) -> CallToolResult:
        """Submit literal text to the named, already-active window."""
        return _invoke(lambda: desktop_operations(session).input_type(window_id, text))

    @server.tool(
        name="kde_input_click",
        description=(
            "Click an already-active target window at normalized window coordinates [0, 1]."
        ),
        annotations=ToolAnnotations(destructiveHint=True, openWorldHint=True),
        structured_output=False,
    )
    def input_click(
        window_id: str,
        x: float,
        y: float,
        session: TargetSession = None,
        button: str = "left",
    ) -> CallToolResult:
        """Click normalized window-relative coordinates; the target must already be active."""
        return _invoke(
            lambda: desktop_operations(session).input_click(
                window_id,
                x,
                y,
                button=button,
            )
        )

    @server.tool(
        name="kde_display_list",
        description="List logical display geometry in the selected target session.",
        annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False),
        structured_output=False,
    )
    def display_list(session: TargetSession = None) -> CallToolResult:
        """List displays and coordinate spaces for the selected session."""
        return _invoke(lambda: desktop_operations(session).display_list())

    @server.tool(
        name="kde_capabilities",
        description="Report supported operations and known limitations for a selected target.",
        annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False),
        structured_output=False,
    )
    def capabilities(session: TargetSession = None) -> CallToolResult:
        """Return target-scoped capability flags and backend limitations."""
        return _invoke(lambda: desktop_operations(session).capabilities())

    @server.tool(
        name="kde_screenshot",
        description="Capture the selected desktop and return the PNG as an MCP image result.",
        annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False),
        structured_output=False,
    )
    def screenshot(
        session: TargetSession = None,
        output: str | None = None,
        overwrite: bool = False,
    ) -> CallToolResult:
        """Capture a screenshot and return image pixels, optionally saving to output."""
        try:
            if output is None:
                with tempfile.TemporaryDirectory(prefix="kde-ai-mcp-") as temporary_dir:
                    screenshot_path = Path(temporary_dir) / "screenshot.png"
                    data = desktop_operations(session).screenshot(screenshot_path)
                    image_bytes = screenshot_path.read_bytes()
                data = data | {"path": None, "image_embedded": True}
            else:
                screenshot_path = Path(output).expanduser()
                data = desktop_operations(session).screenshot(
                    screenshot_path,
                    overwrite=overwrite,
                )
                image_bytes = screenshot_path.read_bytes()
                data = data | {"image_embedded": True}
            envelope = {"schema_version": "1", "ok": True, "data": data}
            return CallToolResult(
                content=[
                    TextContent(type="text", text=json.dumps(envelope, ensure_ascii=False)),
                    ImageContent(
                        type="image",
                        data=base64.b64encode(image_bytes).decode("ascii"),
                        mimeType="image/png",
                    ),
                ],
                structuredContent=envelope,
            )
        except InteractionError as exc:
            return _failure(exc)
        except OSError as exc:
            return _failure(
                InteractionError(
                    "screenshot_read_failed",
                    "The screenshot was captured but its PNG could not be read for MCP delivery.",
                    details={"reason": str(exc), "output": output},
                )
            )

    return server


def _stop_private_session(manager: SessionManager, session_id: str) -> dict[str, Any]:
    stopped, warnings = manager.stop(session_id)
    return {"session": stopped, "warnings": warnings}


def main() -> None:
    logging.basicConfig(level=logging.WARNING)
    _LOGGER.info("Starting KDE Interaction MCP server over stdio")
    require_explicit_session = os.environ.get(
        "KDE_AI_MCP_REQUIRE_EXPLICIT_SESSION", ""
    ).strip().lower() in {
        "1",
        "true",
        "yes",
    }
    create_mcp_server(require_explicit_session=require_explicit_session).run(transport="stdio")


if __name__ == "__main__":
    main()

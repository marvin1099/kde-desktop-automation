from __future__ import annotations

import base64
import json
import logging
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Annotated, Any, Literal

from mcp.server.fastmcp import FastMCP
from mcp.types import CallToolResult, ImageContent, TextContent, ToolAnnotations
from pydantic import Field

from kde_desktop_automation.backends.kwin_input import (
    DEFAULT_DRAG_DURATION_MS,
    DEFAULT_DRAG_END_PAUSE_MS,
    DEFAULT_DRAG_HOLD_MS,
    MAX_DRAG_DURATION_MS,
    MAX_DRAG_PAUSE_MS,
    MAX_DRAG_STEPS,
)
from kde_desktop_automation.errors import InteractionError
from kde_desktop_automation.operations import DesktopOperations
from kde_desktop_automation.paths import RuntimePaths
from kde_desktop_automation.sessions import SessionManager

_LOGGER = logging.getLogger(__name__)
_SERVER_INSTRUCTIONS = """\
KDE Desktop Automation exposes session-scoped KDE desktop operations.

Every desktop tool call requires an explicit `session` argument: use a managed
private session ID, or the exact value `user-desktop` to deliberately select
the real desktop. Defaults and environment variables do not select a target for
MCP calls.

Use `session='user-desktop'` to select the real user desktop explicitly for that
call. Never infer or substitute the live desktop when a private session is
missing or stopped. The live target must always be explicitly passed.

Window-targeted key, text, and normalized click input require an already-active
window; activate it explicitly first. They use KWin fake input through
compositor focus, so checking the window ID does not make delivery atomic to that
window. Global pointer click/drag tools instead take logical desktop pixels and
are not window-bound; they can move the pointer and change the active window.
Live window activation/minimization and explicit pointer actions may change user
focus. KWin may ignore fake-input events, so a successful tool call is not proof
that an application received the action; verify with a screenshot or independent
application state. Drag paths are bounded to 64 steps and must stay within the
selected desktop's output geometry.

`kde_desktop_observe` returns a fresh window/display/pointer snapshot and the
screenshot image. Screenshot-pixel mapping is explicit when a single display is
captured; composite multi-display mapping is reported as unavailable rather than
guessed. `kde_window_set_geometry` is the precise KWin frame-placement path, while
`kde_input_drag` remains a real global pointer gesture for UI interactions.
`kde_screenshot_window` uses KWin Screenshot2 to capture one selected window UUID
without activating it. Screenshots require the separately installed,
KWin-authorized `kde-desktop-screenshot` native helper; Python is not authorized
to call this restricted D-Bus API directly.

Session IDs select a target; they are not per-client authorization tokens. MCP
clients sharing this server share the Unix user's authority over managed private
sessions, including the ability to list and stop them. `kde_session_use` changes
a persisted CLI default but does not affect MCP desktop calls, which always
require an explicit selector. Use session lifecycle tools only through a
coordinator authorized to manage the selected session. Media tools query the
selected session's MPRIS bus and can reveal its current media metadata; player
names must be selected explicitly. Successful control and seek calls report
request submission, not verified playback effect.

PipeWire audio tools are live-user-desktop only. They require
`session='user-desktop'`, may reveal active application audio streams, and can
change device routing, volume, or microphone mute state. Confirm device and
application selectors with `kde_audio_list` first.
"""

OperationsFactory = Callable[[str], DesktopOperations]
ManagerFactory = Callable[[], SessionManager]
TargetSession = Annotated[
    str,
    Field(
        min_length=12,
        max_length=18,
        pattern=r"^(?:user-desktop|agent-[0-9a-f]{12})$",
        description=(
            "Required target selector: a managed private session ID, or the exact value "
            "'user-desktop' to explicitly opt into the real user desktop."
        ),
    ),
]
PrivateSessionId = Annotated[
    str,
    Field(
        min_length=18,
        max_length=18,
        pattern=r"^agent-[0-9a-f]{12}$",
        description=(
            "Managed private session ID returned by kde_session_create or kde_session_list."
        ),
    ),
]
WindowId = Annotated[str, Field(min_length=1, max_length=64)]
Selector = Annotated[str, Field(min_length=1, max_length=512)]
LogicalCoordinate = Annotated[int, Field(strict=True)]
WindowFrameCoordinate = Annotated[int, Field(strict=True, ge=-32_768, le=32_768)]
MediaAction = Literal["play", "pause", "play-pause", "stop", "next", "previous", "seek"]
AudioDirection = Literal["sink", "source"]
LiveDesktopTarget = Literal["user-desktop"]


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
) -> FastMCP:
    """Build the stdio MCP server; factories allow operation-level transport tests."""
    server = FastMCP(
        "kde-desktop-automation",
        instructions=_SERVER_INSTRUCTIONS,
    )

    def desktop_operations(session: str) -> DesktopOperations:
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
        name: Annotated[str, Field(min_length=1, max_length=128)] = "agent",
        width: Annotated[int, Field(ge=320, le=7680)] = 1280,
        height: Annotated[int, Field(ge=240, le=4320)] = 800,
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
    def session_show(session_id: PrivateSessionId) -> CallToolResult:
        """Return status, isolation details, and capabilities for a private session."""
        return _invoke(lambda: manager_factory().get_session(session_id))

    @server.tool(
        name="kde_session_use",
        description=(
            "Set the persisted private default for CLI clients that omit --session. "
            "This does not change MCP desktop calls, which require an explicit session."
        ),
        annotations=ToolAnnotations(
            destructiveHint=False, idempotentHint=True, openWorldHint=False
        ),
        structured_output=False,
    )
    def session_use(session_id: PrivateSessionId) -> CallToolResult:
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
    def session_stop(session_id: PrivateSessionId) -> CallToolResult:
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
    def session_launch(
        session_id: PrivateSessionId,
        argv: Annotated[
            list[Annotated[str, Field(min_length=1, max_length=4096)]],
            Field(min_length=1, max_length=128),
        ],
    ) -> CallToolResult:
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
    def window_list(session: TargetSession) -> CallToolResult:
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
        session: TargetSession,
        app_id: Annotated[str | None, Field(min_length=1, max_length=512)] = None,
        title: Annotated[str | None, Field(min_length=1, max_length=512)] = None,
    ) -> CallToolResult:
        """Find one window. app_id is exact; title is a case-insensitive substring."""
        return _invoke(lambda: desktop_operations(session).window_find(app_id=app_id, title=title))

    @server.tool(
        name="kde_window_wait",
        description=(
            "Wait up to 60 seconds for a unique matching window, optionally until KWin "
            "reports fullscreen or its geometry fills a logical display."
        ),
        annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False),
        structured_output=False,
    )
    def window_wait(
        session: TargetSession,
        app_id: Annotated[str | None, Field(min_length=1, max_length=512)] = None,
        title: Annotated[str | None, Field(min_length=1, max_length=512)] = None,
        timeout: Annotated[float, Field(ge=0.1, le=60.0)] = 10.0,
        fullscreen: bool = False,
        fills_display: bool = False,
    ) -> CallToolResult:
        """Wait for a unique window match and optional fullscreen state; ambiguity fails."""
        return _invoke(
            lambda: desktop_operations(session).window_wait(
                app_id=app_id,
                title=title,
                timeout=timeout,
                fullscreen=fullscreen,
                fills_display=fills_display,
            )
        )

    @server.tool(
        name="kde_window_inspect",
        description="Inspect one window using a UUID from the same selected session.",
        annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False),
        structured_output=False,
    )
    def window_inspect(session: TargetSession, window_id: WindowId) -> CallToolResult:
        """Inspect a target window's state, geometry, app ID, and title."""
        return _invoke(lambda: desktop_operations(session).window_inspect(window_id))

    @server.tool(
        name="kde_window_activate",
        description="Activate a selected window. user-desktop activation changes real user focus.",
        annotations=ToolAnnotations(destructiveHint=False, openWorldHint=True),
        structured_output=False,
    )
    def window_activate(session: TargetSession, window_id: WindowId) -> CallToolResult:
        """Activate by session-scoped UUID; live activation may switch desktops and move focus."""
        return _invoke(lambda: desktop_operations(session).window_activate(window_id))

    @server.tool(
        name="kde_window_set_minimized",
        description=(
            "Set a window's minimized state explicitly and verify it. Setting false restores it; "
            "live changes may change user focus."
        ),
        annotations=ToolAnnotations(
            destructiveHint=True,
            idempotentHint=True,
            openWorldHint=True,
        ),
        structured_output=False,
    )
    def window_set_minimized(
        session: TargetSession,
        window_id: WindowId,
        minimized: bool,
    ) -> CallToolResult:
        """Set an explicit minimized boolean for one window in the selected session."""
        return _invoke(
            lambda: desktop_operations(session).window_set_minimized(window_id, minimized)
        )

    @server.tool(
        name="kde_window_set_geometry",
        description=(
            "Set an explicit KWin frame rectangle in logical desktop pixels and verify its "
            "effective geometry. This is window placement, not a simulated mouse drag."
        ),
        annotations=ToolAnnotations(
            destructiveHint=True,
            idempotentHint=True,
            openWorldHint=True,
        ),
        structured_output=False,
    )
    def window_set_geometry(
        session: TargetSession,
        window_id: WindowId,
        x: WindowFrameCoordinate,
        y: WindowFrameCoordinate,
        width: Annotated[int, Field(strict=True, ge=1, le=32_768)],
        height: Annotated[int, Field(strict=True, ge=1, le=32_768)],
    ) -> CallToolResult:
        """Set and verify one window's KWin frame x/y/width/height."""
        return _invoke(
            lambda: desktop_operations(session).window_set_geometry(
                window_id,
                x,
                y,
                width,
                height,
            )
        )

    @server.tool(
        name="kde_input_key",
        description="Send a key chord to an already-active target window; no implicit activation.",
        annotations=ToolAnnotations(destructiveHint=True, openWorldHint=True),
        structured_output=False,
    )
    def input_key(
        session: TargetSession,
        window_id: WindowId,
        chord: Annotated[str, Field(min_length=1, max_length=128)],
    ) -> CallToolResult:
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
    def input_type(
        session: TargetSession,
        window_id: WindowId,
        text: Annotated[str, Field(max_length=4096)],
    ) -> CallToolResult:
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
        session: TargetSession,
        window_id: WindowId,
        x: Annotated[float, Field(ge=0.0, le=1.0)],
        y: Annotated[float, Field(ge=0.0, le=1.0)],
        button: Literal["left", "middle", "right"] = "left",
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
        name="kde_input_click_at",
        description=(
            "Click a global logical-desktop pixel coordinate, not a window-relative point. "
            "The point must lie on a listed display; this may move the pointer or change focus."
        ),
        annotations=ToolAnnotations(destructiveHint=True, openWorldHint=True),
        structured_output=False,
    )
    def input_click_at(
        session: TargetSession,
        x: LogicalCoordinate,
        y: LogicalCoordinate,
        button: Literal["left", "middle", "right"] = "left",
    ) -> CallToolResult:
        """Click global logical desktop pixels, including negative multi-monitor origins."""
        return _invoke(lambda: desktop_operations(session).input_click_at(x, y, button=button))

    @server.tool(
        name="kde_input_drag",
        description=(
            "Drag between global logical-desktop pixel coordinates. Every interpolated point "
            f"must lie on a listed display; steps are limited to 1–{MAX_DRAG_STEPS}. "
            "Duration and start/end pauses are bounded to make real UI drag-and-drop gestures "
            "deliverable without changing their global-coordinate semantics. "
            "This can move the pointer or change the active window and is not bound to a "
            "particular window."
        ),
        annotations=ToolAnnotations(destructiveHint=True, openWorldHint=True),
        structured_output=False,
    )
    def input_drag(
        session: TargetSession,
        start_x: LogicalCoordinate,
        start_y: LogicalCoordinate,
        end_x: LogicalCoordinate,
        end_y: LogicalCoordinate,
        button: Literal["left", "middle", "right"] = "left",
        steps: Annotated[int, Field(strict=True, ge=1, le=MAX_DRAG_STEPS)] = 8,
        duration_ms: Annotated[
            int, Field(strict=True, ge=0, le=MAX_DRAG_DURATION_MS)
        ] = DEFAULT_DRAG_DURATION_MS,
        hold_ms: Annotated[int, Field(strict=True, ge=0, le=MAX_DRAG_PAUSE_MS)] = (
            DEFAULT_DRAG_HOLD_MS
        ),
        end_pause_ms: Annotated[
            int, Field(strict=True, ge=0, le=MAX_DRAG_PAUSE_MS)
        ] = DEFAULT_DRAG_END_PAUSE_MS,
    ) -> CallToolResult:
        """Submit a bounded global pointer drag in logical desktop coordinates."""
        return _invoke(
            lambda: desktop_operations(session).input_drag(
                start_x,
                start_y,
                end_x,
                end_y,
                button=button,
                steps=steps,
                duration_ms=duration_ms,
                hold_ms=hold_ms,
                end_pause_ms=end_pause_ms,
            )
        )

    @server.tool(
        name="kde_pointer_position",
        description="Read the selected session's current pointer position without moving it.",
        annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False),
        structured_output=False,
    )
    def pointer_position(session: TargetSession) -> CallToolResult:
        """Return the current pointer coordinates and display in logical desktop pixels."""
        return _invoke(lambda: desktop_operations(session).pointer_position())

    @server.tool(
        name="kde_display_list",
        description="List logical display geometry in the selected target session.",
        annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False),
        structured_output=False,
    )
    def display_list(session: TargetSession) -> CallToolResult:
        """List displays and coordinate spaces for the selected session."""
        return _invoke(lambda: desktop_operations(session).display_list())

    @server.tool(
        name="kde_media_list",
        description="List MPRIS players registered on the selected session's D-Bus.",
        annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False),
        structured_output=False,
    )
    def media_list(session: TargetSession) -> CallToolResult:
        """List exact MPRIS player names available in one target session."""
        return _invoke(lambda: desktop_operations(session).media_players())

    @server.tool(
        name="kde_media_inspect",
        description=(
            "Read playback status, title/artist/album, current position, and duration "
            "from one explicitly selected MPRIS player."
        ),
        annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False),
        structured_output=False,
    )
    def media_inspect(session: TargetSession, player: Selector) -> CallToolResult:
        """Inspect metadata and playback position for one exact listed player."""
        return _invoke(lambda: desktop_operations(session).media_inspect(player))

    @server.tool(
        name="kde_media_control",
        description=(
            "Submit play, pause, play-pause, stop, next, previous, or a relative seek "
            "to one explicitly selected MPRIS player. For seek, seconds is required, "
            "must be non-zero, and is an offset (negative rewinds). Use kde_media_seek_to "
            "when you need an absolute playback position."
        ),
        annotations=ToolAnnotations(destructiveHint=True, openWorldHint=True),
        structured_output=False,
    )
    def media_control(
        session: TargetSession,
        player: Selector,
        action: MediaAction,
        seconds: Annotated[float | None, Field(ge=-86_400.0, le=86_400.0)] = None,
    ) -> CallToolResult:
        """Send a typed transport request; seconds is required only for relative seek."""
        return _invoke(
            lambda: desktop_operations(session).media_control(
                player,
                action,
                seconds=seconds,
            )
        )

    @server.tool(
        name="kde_media_seek_to",
        description=(
            "Seek one exact listed MPRIS player to an absolute position from 0 through "
            "86400 seconds. The request is not verified; inspect the player afterward."
        ),
        annotations=ToolAnnotations(destructiveHint=True, openWorldHint=True),
        structured_output=False,
    )
    def media_seek_to(
        session: TargetSession,
        player: Selector,
        position_seconds: Annotated[float, Field(ge=0.0, le=86_400.0)],
    ) -> CallToolResult:
        """Submit an absolute position request to one explicitly selected player."""
        return _invoke(lambda: desktop_operations(session).media_seek_to(player, position_seconds))

    @server.tool(
        name="kde_audio_list",
        description=(
            "List live PipeWire outputs, inputs, defaults, volume/mute, and active output "
            "streams. Requires session='user-desktop'."
        ),
        annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False),
        structured_output=False,
    )
    def audio_list(session: LiveDesktopTarget) -> CallToolResult:
        """Inspect live-user PipeWire audio devices and active output streams."""
        return _invoke(lambda: desktop_operations(session).audio_list())

    @server.tool(
        name="kde_audio_set_default",
        description="Set and verify the live default output sink or input source.",
        annotations=ToolAnnotations(destructiveHint=True, openWorldHint=True),
        structured_output=False,
    )
    def audio_set_default(
        session: LiveDesktopTarget,
        direction: AudioDirection,
        device_name: Selector,
    ) -> CallToolResult:
        """Set an exact listed sink/source as default; only for user-desktop."""
        return _invoke(
            lambda: desktop_operations(session).audio_set_default(direction, device_name)
        )

    @server.tool(
        name="kde_audio_set_volume",
        description="Set and verify a live sink/source volume from 0 through 100 percent.",
        annotations=ToolAnnotations(destructiveHint=True, openWorldHint=True),
        structured_output=False,
    )
    def audio_set_volume(
        session: LiveDesktopTarget,
        direction: AudioDirection,
        device_name: Selector,
        volume_percent: Annotated[float, Field(ge=0.0, le=100.0)],
    ) -> CallToolResult:
        """Set the absolute percentage on one exact listed sink/source."""
        return _invoke(
            lambda: desktop_operations(session).audio_set_volume(
                direction,
                device_name,
                volume_percent,
            )
        )

    @server.tool(
        name="kde_audio_set_mute",
        description="Explicitly mute or unmute one live sink/source and verify the result.",
        annotations=ToolAnnotations(destructiveHint=True, openWorldHint=True),
        structured_output=False,
    )
    def audio_set_mute(
        session: LiveDesktopTarget,
        direction: AudioDirection,
        device_name: Selector,
        muted: bool,
    ) -> CallToolResult:
        """Set a boolean mute state; toggling is deliberately not exposed."""
        return _invoke(
            lambda: desktop_operations(session).audio_set_mute(direction, device_name, muted)
        )

    @server.tool(
        name="kde_audio_route_stream",
        description=(
            "Move one live output stream to a sink after checking its exact stream ID and "
            "application name; requires session='user-desktop'."
        ),
        annotations=ToolAnnotations(destructiveHint=True, openWorldHint=True),
        structured_output=False,
    )
    def audio_route_stream(
        session: LiveDesktopTarget,
        stream_id: Annotated[int, Field(ge=0)],
        sink_name: Selector,
        application_name: Selector,
        process_id: Annotated[int | None, Field(gt=0)] = None,
    ) -> CallToolResult:
        """Move only the selected sink-input; optional PID further guards identity."""
        return _invoke(
            lambda: desktop_operations(session).audio_route_stream(
                stream_id,
                sink_name,
                application_name=application_name,
                process_id=process_id,
            )
        )

    @server.tool(
        name="kde_capabilities",
        description="Report supported operations and known limitations for a selected target.",
        annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False),
        structured_output=False,
    )
    def capabilities(session: TargetSession) -> CallToolResult:
        """Return target-scoped capability flags and backend limitations."""
        return _invoke(lambda: desktop_operations(session).capabilities())

    @server.tool(
        name="kde_desktop_observe",
        description=(
            "Return a fresh desktop snapshot with windows, logical display geometry, pointer/focus "
            "context, and screenshot metadata/image content when the KWin Screenshot2 helper "
            "is available."
        ),
        annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False),
        structured_output=False,
    )
    def desktop_observe(session: TargetSession) -> CallToolResult:
        """Deliver one visual and structured observation for an iterative agent loop."""
        try:
            operations = desktop_operations(session)
            capabilities = operations.capabilities().get("capabilities", {})
            if (
                not isinstance(capabilities, dict)
                or capabilities.get("screenshot.capture") is not True
            ):
                data = operations.observe()
                data["observation_note"] = (
                    "Screenshot is unavailable on this target; returned structured state only."
                )
                return _success(data)
            with tempfile.TemporaryDirectory(prefix="kde-desktop-observe-") as temporary_dir:
                screenshot_path = Path(temporary_dir) / "screenshot.png"
                data = operations.observe(screenshot_output=screenshot_path)
                image_bytes = screenshot_path.read_bytes()
            data["screenshot"] = data["screenshot"] | {"path": None, "image_embedded": True}
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
                    "The observation was captured but its PNG could not be read for MCP delivery.",
                    details={"reason": str(exc)},
                )
            )

    @server.tool(
        name="kde_screenshot",
        description="Capture the selected desktop and return the PNG as an MCP image result.",
        annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False),
        structured_output=False,
    )
    def screenshot(
        session: TargetSession,
        output: str | None = None,
        overwrite: bool = False,
    ) -> CallToolResult:
        """Capture a screenshot and return image pixels, optionally saving to output."""
        try:
            if output is None:
                with tempfile.TemporaryDirectory(prefix="kde-desktop-mcp-") as temporary_dir:
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

    @server.tool(
        name="kde_screenshot_window",
        description=(
            "Capture one selected window UUID directly through KWin Screenshot2 without "
            "activating it. Requires the authorized native helper. Returns the PNG as an MCP image."
        ),
        annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False),
        structured_output=False,
    )
    def screenshot_window(
        session: TargetSession,
        window_id: WindowId,
        output: str | None = None,
        overwrite: bool = False,
    ) -> CallToolResult:
        """Capture a selected window as image content, optionally saving its PNG."""
        try:
            operations = desktop_operations(session)
            if output is None:
                with tempfile.TemporaryDirectory(
                    prefix="kde-desktop-window-shot-"
                ) as temporary_dir:
                    screenshot_path = Path(temporary_dir) / "window.png"
                    data = operations.screenshot_window(window_id, screenshot_path)
                    image_bytes = screenshot_path.read_bytes()
                data = data | {"path": None, "image_embedded": True}
            else:
                screenshot_path = Path(output).expanduser()
                data = operations.screenshot_window(
                    window_id,
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
                    "The selected-window screenshot was captured but its PNG could not be "
                    "read for MCP delivery.",
                    details={"reason": str(exc), "output": output, "window_id": window_id},
                )
            )

    return server


def _stop_private_session(manager: SessionManager, session_id: str) -> dict[str, Any]:
    stopped, warnings = manager.stop(session_id)
    return {"session": stopped, "warnings": warnings}


def main() -> None:
    logging.basicConfig(level=logging.WARNING)
    _LOGGER.info("Starting KDE Desktop Automation MCP server over stdio")
    create_mcp_server().run(transport="stdio")


if __name__ == "__main__":
    main()

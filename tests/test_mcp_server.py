from __future__ import annotations

import asyncio
import base64
import sys
from pathlib import Path
from typing import Any, cast

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.server.fastmcp import FastMCP
from mcp.types import CallToolResult, ImageContent

from kde_desktop_automation.errors import InteractionError
from kde_desktop_automation.mcp_server import OperationsFactory, create_mcp_server


class FakeOperations:
    selected_sessions: list[str | None] = []
    screenshot_png = b"\x89PNG\r\n\x1a\nmock-png"
    screenshot_available = True

    def __init__(self, session: str | None) -> None:
        self.session = session
        self.selected_sessions.append(session)

    def window_list(self) -> dict[str, Any]:
        if self.session is None:
            raise InteractionError(
                "private_default_session_unset",
                "No private default is set.",
            )
        return {"target": {"kind": self.session}}

    def window_set_minimized(self, window_id: str, minimized: bool) -> dict[str, Any]:
        return {
            "target": {"kind": self.session, "window_id": window_id},
            "minimized": minimized,
        }

    def window_set_geometry(
        self,
        window_id: str,
        x: int,
        y: int,
        width: int,
        height: int,
    ) -> dict[str, Any]:
        return {
            "target": {"kind": self.session, "window_id": window_id},
            "requested_geometry": {"x": x, "y": y, "width": width, "height": height},
            "verified": True,
        }

    def pointer_position(self) -> dict[str, Any]:
        return {"target": {"kind": self.session}, "x": 12, "y": 34}

    def capabilities(self) -> dict[str, Any]:
        return {"capabilities": {"screenshot.capture": self.screenshot_available}}

    def observe(self, *, screenshot_output: Path | None = None, overwrite: bool = False):
        del overwrite
        if screenshot_output is not None:
            screenshot_output.write_bytes(self.screenshot_png)
        return {
            "target": {"kind": self.session},
            "snapshot_id": "snapshot-test",
            "screenshot": None
            if screenshot_output is None
            else {
                "path": str(screenshot_output),
                "width": 1,
                "height": 1,
            },
        }

    def input_click_at(self, x: int, y: int, *, button: str = "left") -> dict[str, Any]:
        return {"target": {"kind": self.session}, "point": [x, y], "button": button}

    def input_drag(
        self,
        start_x: int,
        start_y: int,
        end_x: int,
        end_y: int,
        *,
        button: str = "left",
        steps: int = 8,
        duration_ms: int = 750,
        hold_ms: int = 100,
        end_pause_ms: int = 250,
    ) -> dict[str, Any]:
        return {
            "target": {"kind": self.session},
            "path": [start_x, start_y, end_x, end_y],
            "button": button,
            "steps": steps,
            "duration_ms": duration_ms,
            "hold_ms": hold_ms,
            "end_pause_ms": end_pause_ms,
        }

    def media_players(self) -> dict[str, Any]:
        return {"target": {"kind": self.session}, "players": [{"name": "vlc"}]}

    def media_inspect(self, player: str) -> dict[str, Any]:
        return {"target": {"kind": self.session}, "player": player}

    def media_control(
        self,
        player: str,
        action: str,
        *,
        seconds: float | None = None,
    ) -> dict[str, Any]:
        return {
            "target": {"kind": self.session},
            "player": player,
            "action": action,
            "seconds": seconds,
        }

    def media_seek_to(self, player: str, position_seconds: float) -> dict[str, Any]:
        return {
            "target": {"kind": self.session},
            "player": player,
            "position_seconds": position_seconds,
        }

    def audio_list(self) -> dict[str, Any]:
        return {"target": {"kind": self.session}, "devices": {"sinks": [], "sources": []}}

    def audio_set_default(self, direction: str, device_name: str) -> dict[str, Any]:
        return {"target": {"kind": self.session}, "direction": direction, "device": device_name}

    def audio_set_volume(
        self,
        direction: str,
        device_name: str,
        volume_percent: float,
    ) -> dict[str, Any]:
        return {
            "target": {"kind": self.session},
            "direction": direction,
            "device": device_name,
            "volume_percent": volume_percent,
        }

    def audio_set_mute(self, direction: str, device_name: str, muted: bool) -> dict[str, Any]:
        return {
            "target": {"kind": self.session},
            "direction": direction,
            "device": device_name,
            "muted": muted,
        }

    def audio_route_stream(
        self,
        stream_id: int,
        sink_name: str,
        *,
        application_name: str,
        process_id: int | None = None,
    ) -> dict[str, Any]:
        return {
            "target": {"kind": self.session},
            "stream_id": stream_id,
            "sink_name": sink_name,
            "application_name": application_name,
            "process_id": process_id,
        }

    def screenshot(self, output: Path, *, overwrite: bool = False) -> dict[str, Any]:
        del overwrite
        output.write_bytes(self.screenshot_png)
        return {"target": {"kind": self.session}, "path": str(output), "width": 1, "height": 1}

    def screenshot_window(
        self,
        window_id: str,
        output: Path,
        *,
        overwrite: bool = False,
    ) -> dict[str, Any]:
        del overwrite
        output.write_bytes(self.screenshot_png)
        return {
            "target": {"kind": self.session, "window_id": window_id},
            "path": str(output),
            "width": 1,
            "height": 1,
            "capture_mode": "window",
            "target_window_verified": True,
        }


def _server() -> FastMCP:
    FakeOperations.selected_sessions.clear()
    return create_mcp_server(operations_factory=cast(OperationsFactory, FakeOperations))


def test_mcp_registers_shared_desktop_and_private_lifecycle_tools() -> None:
    server = _server()

    tools = asyncio.run(server.list_tools())
    names = {tool.name for tool in tools}

    assert {
        "kde_session_list",
        "kde_session_create",
        "kde_session_stop",
        "kde_session_launch",
        "kde_window_list",
        "kde_window_find",
        "kde_window_wait",
        "kde_window_activate",
        "kde_window_set_minimized",
        "kde_window_set_geometry",
        "kde_input_key",
        "kde_input_type",
        "kde_input_click",
        "kde_input_click_at",
        "kde_input_drag",
        "kde_pointer_position",
        "kde_display_list",
        "kde_media_list",
        "kde_media_inspect",
        "kde_media_control",
        "kde_media_seek_to",
        "kde_audio_list",
        "kde_audio_set_default",
        "kde_audio_set_volume",
        "kde_audio_set_mute",
        "kde_audio_route_stream",
        "kde_screenshot",
        "kde_screenshot_window",
        "kde_desktop_observe",
        "kde_capabilities",
    } <= names

    listing_tool = next(tool for tool in tools if tool.name == "kde_window_list")
    assert "session" in listing_tool.inputSchema["properties"]
    assert "session" in listing_tool.inputSchema.get("required", [])
    assert (
        "Required target selector"
        in listing_tool.inputSchema["properties"]["session"]["description"]
    )
    assert "user-desktop" in listing_tool.inputSchema["properties"]["session"]["description"]
    window_screenshot_tool = next(tool for tool in tools if tool.name == "kde_screenshot_window")
    assert {"session", "window_id"} <= set(window_screenshot_tool.inputSchema.get("required", []))
    desktop_tool_names = {
        "kde_window_list",
        "kde_window_find",
        "kde_window_wait",
        "kde_window_inspect",
        "kde_window_activate",
        "kde_window_set_minimized",
        "kde_window_set_geometry",
        "kde_input_key",
        "kde_input_type",
        "kde_input_click",
        "kde_input_click_at",
        "kde_input_drag",
        "kde_pointer_position",
        "kde_display_list",
        "kde_media_list",
        "kde_media_inspect",
        "kde_media_control",
        "kde_media_seek_to",
        "kde_audio_list",
        "kde_audio_set_default",
        "kde_audio_set_volume",
        "kde_audio_set_mute",
        "kde_audio_route_stream",
        "kde_screenshot",
        "kde_screenshot_window",
        "kde_desktop_observe",
        "kde_capabilities",
    }
    for tool in tools:
        if tool.name in desktop_tool_names:
            assert "session" in tool.inputSchema.get("required", []), tool.name
    wait_tool = next(tool for tool in tools if tool.name == "kde_window_wait")
    assert {"session", "fullscreen", "fills_display"} <= set(wait_tool.inputSchema["properties"])
    target_schema = wait_tool.inputSchema["properties"]["session"]
    assert target_schema["pattern"] == r"^(?:user-desktop|agent-[0-9a-f]{12})$"
    assert wait_tool.inputSchema["properties"]["timeout"]["minimum"] == 0.1
    assert wait_tool.inputSchema["properties"]["timeout"]["maximum"] == 60.0
    media_control_tool = next(tool for tool in tools if tool.name == "kde_media_control")
    assert {"session", "player", "action", "seconds"} <= set(
        media_control_tool.inputSchema["properties"]
    )
    assert media_control_tool.inputSchema["properties"]["action"]["enum"] == [
        "play",
        "pause",
        "play-pause",
        "stop",
        "next",
        "previous",
        "seek",
    ]
    media_seek_to_tool = next(tool for tool in tools if tool.name == "kde_media_seek_to")
    assert media_seek_to_tool.inputSchema["properties"]["position_seconds"]["minimum"] == 0.0
    assert media_seek_to_tool.inputSchema["properties"]["position_seconds"]["maximum"] == 86_400.0
    audio_mute_tool = next(tool for tool in tools if tool.name == "kde_audio_set_mute")
    assert {"session", "direction", "device_name", "muted"} <= set(
        audio_mute_tool.inputSchema["properties"]
    )
    assert audio_mute_tool.inputSchema["properties"]["direction"]["enum"] == ["sink", "source"]
    audio_list_tool = next(tool for tool in tools if tool.name == "kde_audio_list")
    assert audio_list_tool.inputSchema["properties"]["session"]["const"] == "user-desktop"
    volume_tool = next(tool for tool in tools if tool.name == "kde_audio_set_volume")
    assert volume_tool.inputSchema["properties"]["volume_percent"]["minimum"] == 0.0
    assert volume_tool.inputSchema["properties"]["volume_percent"]["maximum"] == 100.0
    click_tool = next(tool for tool in tools if tool.name == "kde_input_click")
    assert click_tool.inputSchema["properties"]["x"]["minimum"] == 0.0
    assert click_tool.inputSchema["properties"]["x"]["maximum"] == 1.0
    drag_tool = next(tool for tool in tools if tool.name == "kde_input_drag")
    assert drag_tool.inputSchema["properties"]["steps"]["minimum"] == 1
    assert drag_tool.inputSchema["properties"]["steps"]["maximum"] == 64
    assert drag_tool.inputSchema["properties"]["button"]["enum"] == ["left", "middle", "right"]
    minimized_tool = next(tool for tool in tools if tool.name == "kde_window_set_minimized")
    assert minimized_tool.inputSchema["properties"]["minimized"]["type"] == "boolean"
    geometry_tool = next(tool for tool in tools if tool.name == "kde_window_set_geometry")
    assert geometry_tool.inputSchema["properties"]["width"]["minimum"] == 1
    assert geometry_tool.inputSchema["properties"]["width"]["maximum"] == 32_768
    assert "session" in geometry_tool.inputSchema.get("required", [])
    text_tool = next(tool for tool in tools if tool.name == "kde_input_type")
    assert text_tool.inputSchema["properties"]["text"]["maxLength"] == 4096
    create_tool = next(tool for tool in tools if tool.name == "kde_session_create")
    assert create_tool.inputSchema["properties"]["width"]["minimum"] == 320
    assert create_tool.inputSchema["properties"]["width"]["maximum"] == 7680
    launch_tool = next(tool for tool in tools if tool.name == "kde_session_launch")
    assert launch_tool.inputSchema["properties"]["argv"]["minItems"] == 1
    assert launch_tool.inputSchema["properties"]["argv"]["maxItems"] == 128
    route_tool = next(tool for tool in tools if tool.name == "kde_audio_route_stream")
    assert route_tool.inputSchema["properties"]["stream_id"]["minimum"] == 0
    assert route_tool.inputSchema["properties"]["process_id"]["anyOf"][0]["exclusiveMinimum"] == 0


def test_mcp_routes_real_desktop_only_when_explicitly_selected() -> None:
    server = _server()

    result = asyncio.run(server.call_tool("kde_window_list", {"session": "user-desktop"}))

    assert isinstance(result, CallToolResult)
    assert result.isError is False
    assert result.structuredContent == {
        "schema_version": "1",
        "ok": True,
        "data": {"target": {"kind": "user-desktop"}},
    }
    assert FakeOperations.selected_sessions == ["user-desktop"]


def test_mcp_routes_explicit_private_session_id_without_default_lookup() -> None:
    server = _server()
    session_id = "agent-123456789abc"

    result = asyncio.run(server.call_tool("kde_window_list", {"session": session_id}))

    assert isinstance(result, CallToolResult)
    assert result.isError is False
    assert result.structuredContent == {
        "schema_version": "1",
        "ok": True,
        "data": {"target": {"kind": session_id}},
    }
    assert FakeOperations.selected_sessions == [session_id]


def test_mcp_media_control_requires_and_uses_explicit_session() -> None:
    server = _server()
    session_id = "agent-123456789abc"

    result = asyncio.run(
        server.call_tool(
            "kde_media_control",
            {
                "session": session_id,
                "player": "vlc",
                "action": "seek",
                "seconds": -15.0,
            },
        )
    )

    assert isinstance(result, CallToolResult)
    assert result.isError is False
    assert result.structuredContent["data"] == {
        "target": {"kind": session_id},
        "player": "vlc",
        "action": "seek",
        "seconds": -15.0,
    }
    assert FakeOperations.selected_sessions == [session_id]


def test_mcp_global_pointer_and_minimized_tools_use_explicit_target() -> None:
    server = _server()
    session_id = "agent-123456789abc"

    async def invoke_all() -> list[CallToolResult]:
        results = await asyncio.gather(
            server.call_tool(
                "kde_input_click_at",
                {"session": session_id, "x": -1200, "y": 240, "button": "right"},
            ),
            server.call_tool(
                "kde_input_drag",
                {
                    "session": session_id,
                    "start_x": 10,
                    "start_y": 20,
                    "end_x": 30,
                    "end_y": 40,
                    "steps": 4,
                },
            ),
            server.call_tool(
                "kde_window_set_minimized",
                {"session": session_id, "window_id": "window-uuid", "minimized": False},
            ),
        )
        return [cast(CallToolResult, result) for result in results]

    click, drag, minimized = asyncio.run(invoke_all())

    assert click.structuredContent["data"]["point"] == [-1200, 240]
    assert click.structuredContent["data"]["button"] == "right"
    assert drag.structuredContent["data"]["steps"] == 4
    assert minimized.structuredContent["data"]["minimized"] is False
    assert FakeOperations.selected_sessions == [session_id, session_id, session_id]


def test_mcp_geometry_and_pointer_position_are_generic_explicit_tools() -> None:
    server = _server()
    session_id = "agent-123456789abc"

    async def invoke_all() -> list[CallToolResult]:
        results = await asyncio.gather(
            server.call_tool(
                "kde_window_set_geometry",
                {
                    "session": session_id,
                    "window_id": "window-uuid",
                    "x": 10,
                    "y": 20,
                    "width": 640,
                    "height": 480,
                },
            ),
            server.call_tool("kde_pointer_position", {"session": session_id}),
        )
        return [cast(CallToolResult, result) for result in results]

    geometry, pointer = asyncio.run(invoke_all())

    assert geometry.structuredContent["data"]["verified"] is True
    assert geometry.structuredContent["data"]["requested_geometry"] == {
        "x": 10,
        "y": 20,
        "width": 640,
        "height": 480,
    }
    assert pointer.structuredContent["data"]["x"] == 12
    assert FakeOperations.selected_sessions == [session_id, session_id]


def test_mcp_observe_returns_structured_snapshot_and_screenshot_image() -> None:
    server = _server()

    result = asyncio.run(server.call_tool("kde_desktop_observe", {"session": "user-desktop"}))

    assert isinstance(result, CallToolResult)
    assert result.isError is False
    assert result.structuredContent["data"]["snapshot_id"] == "snapshot-test"
    assert result.structuredContent["data"]["screenshot"]["path"] is None
    assert result.structuredContent["data"]["screenshot"]["image_embedded"] is True
    image = next(item for item in result.content if isinstance(item, ImageContent))
    assert base64.b64decode(image.data) == FakeOperations.screenshot_png


def test_mcp_observe_returns_structured_snapshot_without_unavailable_screenshot() -> None:
    server = _server()
    FakeOperations.screenshot_available = False

    try:
        result = asyncio.run(server.call_tool("kde_desktop_observe", {"session": "user-desktop"}))
    finally:
        FakeOperations.screenshot_available = True

    assert isinstance(result, CallToolResult)
    assert result.isError is False
    assert result.structuredContent["data"]["screenshot"] is None
    assert "unavailable" in result.structuredContent["data"]["observation_note"]
    assert not any(isinstance(item, ImageContent) for item in result.content)


def test_mcp_media_seek_to_uses_explicit_session_and_absolute_position() -> None:
    server = _server()
    session_id = "agent-123456789abc"

    result = asyncio.run(
        server.call_tool(
            "kde_media_seek_to",
            {
                "session": session_id,
                "player": "vlc",
                "position_seconds": 142.75,
            },
        )
    )

    assert isinstance(result, CallToolResult)
    assert result.isError is False
    assert result.structuredContent["data"] == {
        "target": {"kind": session_id},
        "player": "vlc",
        "position_seconds": 142.75,
    }
    assert FakeOperations.selected_sessions == [session_id]


def test_mcp_audio_routing_requires_and_uses_explicit_live_target() -> None:
    server = _server()

    result = asyncio.run(
        server.call_tool(
            "kde_audio_route_stream",
            {
                "session": "user-desktop",
                "stream_id": 17,
                "sink_name": "headphones",
                "application_name": "Firefox",
                "process_id": 555,
            },
        )
    )

    assert isinstance(result, CallToolResult)
    assert result.isError is False
    assert result.structuredContent["data"] == {
        "target": {"kind": "user-desktop"},
        "stream_id": 17,
        "sink_name": "headphones",
        "application_name": "Firefox",
        "process_id": 555,
    }
    assert FakeOperations.selected_sessions == ["user-desktop"]


def test_mcp_screenshot_returns_png_image_and_metadata(tmp_path: Path) -> None:
    server = _server()
    output = tmp_path / "mcp-capture.png"

    result = asyncio.run(
        server.call_tool(
            "kde_screenshot",
            {"session": "agent-123456789abc", "output": str(output)},
        )
    )

    assert isinstance(result, CallToolResult)
    assert result.structuredContent is not None
    assert result.structuredContent["schema_version"] == "1"
    assert result.structuredContent["ok"] is True
    assert output.read_bytes() == FakeOperations.screenshot_png
    images = [block for block in result.content if isinstance(block, ImageContent)]
    assert len(images) == 1
    assert images[0].mimeType == "image/png"
    assert base64.b64decode(images[0].data) == FakeOperations.screenshot_png


def test_mcp_window_screenshot_returns_selected_window_image_and_metadata(
    tmp_path: Path,
) -> None:
    server = _server()
    output = tmp_path / "window.png"
    window_id = "12345678-1234-5678-1234-567812345678"

    result = asyncio.run(
        server.call_tool(
            "kde_screenshot_window",
            {
                "session": "user-desktop",
                "window_id": window_id,
                "output": str(output),
            },
        )
    )

    assert isinstance(result, CallToolResult)
    assert result.structuredContent["ok"] is True
    assert result.structuredContent["data"]["target"] == {
        "kind": "user-desktop",
        "window_id": window_id,
    }
    assert result.structuredContent["data"]["capture_mode"] == "window"
    assert result.structuredContent["data"]["target_window_verified"] is True
    assert output.read_bytes() == FakeOperations.screenshot_png
    image = next(block for block in result.content if isinstance(block, ImageContent))
    assert base64.b64decode(image.data) == FakeOperations.screenshot_png
    assert FakeOperations.selected_sessions == ["user-desktop"]


def test_mcp_instructions_state_live_desktop_safety() -> None:
    server = _server()
    instructions = " ".join((server.instructions or "").split())

    assert "Every desktop tool call requires an explicit `session` argument" in instructions
    assert "Never infer or substitute the live desktop" in instructions
    assert "not per-client authorization tokens" in instructions
    assert "kde_session_use" in instructions
    assert "player names must be selected explicitly" in instructions
    assert "PipeWire audio tools are live-user-desktop only" in instructions
    assert "not window-bound" in instructions
    assert "may change user focus" in instructions
    assert "without activating it" in instructions


def test_mcp_session_selector_is_not_implicit_for_any_server_mode() -> None:
    server = _server()
    listing_tool = next(
        tool for tool in asyncio.run(server.list_tools()) if tool.name == "kde_window_list"
    )

    assert "session" in listing_tool.inputSchema.get("required", [])


def test_shared_mcp_routes_concurrent_calls_to_their_explicit_sessions() -> None:
    server = _server()
    sessions = ["agent-111111111111", "agent-222222222222"]

    async def invoke_both() -> list[CallToolResult]:
        results = await asyncio.gather(
            *(
                server.call_tool("kde_window_list", {"session": session_id})
                for session_id in sessions
            )
        )
        return [cast(CallToolResult, result) for result in results]

    results = asyncio.run(invoke_both())

    assert all(not result.isError for result in results)
    assert FakeOperations.selected_sessions == sessions


def test_mcp_server_completes_stdio_handshake_and_lists_tools() -> None:
    async def verify() -> set[str]:
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "kde_desktop_automation.mcp_server"],
        )
        async with (
            stdio_client(params) as (read_stream, write_stream),
            ClientSession(read_stream, write_stream) as client,
        ):
            await client.initialize()
            listing = await client.list_tools()
            return {tool.name for tool in listing.tools}

    names = asyncio.run(verify())

    assert "kde_window_list" in names
    assert "kde_screenshot" in names
    assert "kde_screenshot_window" in names
    assert "kde_session_create" in names

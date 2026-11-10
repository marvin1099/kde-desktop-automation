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
        "kde_input_key",
        "kde_input_type",
        "kde_input_click",
        "kde_display_list",
        "kde_media_list",
        "kde_media_inspect",
        "kde_media_control",
        "kde_audio_list",
        "kde_audio_set_default",
        "kde_audio_set_volume",
        "kde_audio_set_mute",
        "kde_audio_route_stream",
        "kde_screenshot",
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
    desktop_tool_names = {
        "kde_window_list",
        "kde_window_find",
        "kde_window_wait",
        "kde_window_inspect",
        "kde_window_activate",
        "kde_input_key",
        "kde_input_type",
        "kde_input_click",
        "kde_display_list",
        "kde_media_list",
        "kde_media_inspect",
        "kde_media_control",
        "kde_audio_list",
        "kde_audio_set_default",
        "kde_audio_set_volume",
        "kde_audio_set_mute",
        "kde_audio_route_stream",
        "kde_screenshot",
        "kde_capabilities",
    }
    for tool in tools:
        if tool.name in desktop_tool_names:
            assert "session" in tool.inputSchema.get("required", []), tool.name
    wait_tool = next(tool for tool in tools if tool.name == "kde_window_wait")
    assert {"session", "fullscreen", "fills_display"} <= set(wait_tool.inputSchema["properties"])
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
    audio_mute_tool = next(tool for tool in tools if tool.name == "kde_audio_set_mute")
    assert {"session", "direction", "device_name", "muted"} <= set(
        audio_mute_tool.inputSchema["properties"]
    )
    assert audio_mute_tool.inputSchema["properties"]["direction"]["enum"] == ["sink", "source"]


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


def test_mcp_instructions_state_live_desktop_safety() -> None:
    server = _server()
    instructions = " ".join((server.instructions or "").split())

    assert "Every desktop tool call requires an explicit `session` argument" in instructions
    assert "Never infer or substitute the live desktop" in instructions
    assert "not per-client authorization tokens" in instructions
    assert "kde_session_use" in instructions
    assert "player names must be selected explicitly" in instructions
    assert "PipeWire audio tools are live-user-desktop only" in instructions


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
    assert "kde_session_create" in names

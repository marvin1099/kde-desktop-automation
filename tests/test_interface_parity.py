from __future__ import annotations

import asyncio
import inspect
from typing import Any

from typer.main import get_command

from kde_desktop_automation.cli import app
from kde_desktop_automation.mcp_server import create_mcp_server
from kde_desktop_automation.operation_catalog import (
    MCP_TRANSPORT_ONLY_TOOLS,
    SHARED_CLI_ROOTS,
    SHARED_OPERATION_CATALOG,
)
from kde_desktop_automation.operations import DesktopOperations


def _cli_routes(group: Any, prefix: tuple[str, ...] = ()) -> set[tuple[str, ...]]:
    routes: set[tuple[str, ...]] = set()
    for name, command in group.commands.items():
        route = (*prefix, name)
        if hasattr(command, "commands"):
            routes.update(_cli_routes(command, route))
        else:
            routes.add(route)
    return routes


def test_cli_and_mcp_expose_every_shared_desktop_operation() -> None:
    catalog = {entry.operation: entry for entry in SHARED_OPERATION_CATALOG}
    assert len(catalog) == len(SHARED_OPERATION_CATALOG)

    public_operations = {
        name
        for name, member in inspect.getmembers(DesktopOperations, predicate=callable)
        if not name.startswith("_") and name != "__init__"
    }
    assert set(catalog) == public_operations

    cli_routes = _cli_routes(get_command(app))
    shared_cli_routes = {route for route in cli_routes if route[0] in SHARED_CLI_ROOTS}
    assert shared_cli_routes == {entry.cli_route for entry in SHARED_OPERATION_CATALOG}

    mcp_tools = {tool.name for tool in asyncio.run(create_mcp_server().list_tools())}
    assert mcp_tools - MCP_TRANSPORT_ONLY_TOOLS == {
        entry.mcp_tool for entry in SHARED_OPERATION_CATALOG
    }


def test_window_screenshot_has_matching_shared_cli_and_mcp_routes() -> None:
    entry = next(item for item in SHARED_OPERATION_CATALOG if item.operation == "screenshot_window")

    assert entry.cli_route == ("window", "screenshot")
    assert entry.mcp_tool == "kde_screenshot_window"

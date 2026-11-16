from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from kde_desktop_automation.errors import InteractionError
from kde_desktop_automation.operations import DesktopOperations


class FakeManager:
    def __init__(self, default: str | None = "agent-123456789abc") -> None:
        self.default = default
        self.calls: list[tuple[Any, ...]] = []
        self.capability_record = {
            "status": "running",
            "capabilities": {"window.list": True},
            "window_management_protocol_version": 21,
            "input_protocol_version": 6,
            "limitations": [],
        }

    def default_session_id(self) -> str | None:
        self.calls.append(("default_session_id",))
        return self.default

    def list_windows(self, session_id: str) -> dict[str, Any]:
        self.calls.append(("list_windows", session_id))
        return {"target": {"kind": "agent-session", "session_id": session_id}, "windows": []}

    def inspect_window(self, session_id: str, window_id: str) -> dict[str, Any]:
        self.calls.append(("inspect_window", session_id, window_id))
        return {
            "target": {"kind": "agent-session", "session_id": session_id},
            "window": {
                "window_id": window_id,
                "app_id": "org.kde.kate",
                "pid": 1234,
            },
        }

    def activate_window(self, session_id: str, window_id: str) -> dict[str, Any]:
        self.calls.append(("activate_window", session_id, window_id))
        return {"target": {"kind": "agent-session", "session_id": session_id}}

    def find_window(self, session_id: str, *, app_id: str | None, title: str | None):
        self.calls.append(("find_window", session_id, app_id, title))
        return {"target": {"kind": "agent-session", "session_id": session_id}, "window": {}}

    def click(self, session_id: str, x: int, y: int, button: str = "left"):
        self.calls.append(("click", session_id, x, y, button))
        return {"target": {"kind": "agent-session", "session_id": session_id}}

    def drag(
        self,
        session_id: str,
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
    ):
        self.calls.append(
            (
                "drag",
                session_id,
                start_x,
                start_y,
                end_x,
                end_y,
                button,
                steps,
                duration_ms,
                hold_ms,
                end_pause_ms,
            )
        )
        return {"target": {"kind": "agent-session", "session_id": session_id}}

    def set_window_minimized(self, session_id: str, window_id: str, minimized: bool):
        self.calls.append(("set_window_minimized", session_id, window_id, minimized))
        return {"target": {"kind": "agent-session", "session_id": session_id}}

    def set_window_fullscreen(self, session_id: str, window_id: str, fullscreen: bool):
        self.calls.append(("set_window_fullscreen", session_id, window_id, fullscreen))
        return {"target": {"kind": "agent-session", "session_id": session_id}}

    def set_window_maximized(self, session_id: str, window_id: str, maximized: bool):
        self.calls.append(("set_window_maximized", session_id, window_id, maximized))
        return {"target": {"kind": "agent-session", "session_id": session_id}}

    def set_window_geometry(self, session_id, window_id, x, y, width, height):
        self.calls.append(("set_window_geometry", session_id, window_id, x, y, width, height))
        return {"target": {"kind": "agent-session", "session_id": session_id}}

    def pointer_position(self, session_id):
        self.calls.append(("pointer_position", session_id))
        return {"x": 10, "y": 20}

    def displays(self, _session_id):
        self.calls.append(("displays", _session_id))
        return [{"name": "Virtual-0", "x": 0, "y": 0, "width": 1280, "height": 800}]

    def get_session(self, _session_id: str) -> dict[str, Any]:
        return self.capability_record


class FakeUserDesktop:
    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []
        self.active_window_id: str | None = None
        self.window_identity = {"app_id": "org.kde.kate", "pid": 1234}

    def list_windows(self) -> dict[str, Any]:
        self.calls.append(("list_windows",))
        return {"target": {"kind": "user-desktop"}, "count": 0, "windows": []}

    def click_at(self, x: int, y: int, button: str = "left") -> dict[str, Any]:
        self.calls.append(("click_at", x, y, button))
        return {"target": {"kind": "user-desktop"}}

    def drag_at(
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
        self.calls.append(
            (
                "drag_at",
                start_x,
                start_y,
                end_x,
                end_y,
                button,
                steps,
                duration_ms,
                hold_ms,
                end_pause_ms,
            )
        )
        return {"target": {"kind": "user-desktop"}}

    def set_window_minimized(self, window_id: str, minimized: bool) -> dict[str, Any]:
        self.calls.append(("set_window_minimized", window_id, minimized))
        return {"target": {"kind": "user-desktop", "window_id": window_id}}

    def set_window_fullscreen(self, window_id: str, fullscreen: bool) -> dict[str, Any]:
        self.calls.append(("set_window_fullscreen", window_id, fullscreen))
        return {"target": {"kind": "user-desktop", "window_id": window_id}}

    def set_window_maximized(self, window_id: str, maximized: bool) -> dict[str, Any]:
        self.calls.append(("set_window_maximized", window_id, maximized))
        return {"target": {"kind": "user-desktop", "window_id": window_id}}

    def set_window_geometry(self, window_id, x, y, width, height):
        self.calls.append(("set_window_geometry", window_id, x, y, width, height))
        return {"target": {"kind": "user-desktop", "window_id": window_id}}

    def inspect_window(self, window_id: str) -> dict[str, Any]:
        self.calls.append(("inspect_window", window_id))
        return {
            "target": {"kind": "user-desktop"},
            "window": {
                "window_id": window_id,
                "active": self.active_window_id == window_id,
                **self.window_identity,
            },
        }

    def activate_window(self, window_id: str) -> dict[str, Any]:
        self.calls.append(("activate_window", window_id))
        self.active_window_id = window_id
        return {"active_window_verified": True}

    def screenshot_window(
        self,
        window_id: str,
        output: Path,
        *,
        overwrite: bool = False,
    ) -> dict[str, Any]:
        self.calls.append(("screenshot_window", window_id, str(output), overwrite))
        return {
            "target": {"kind": "user-desktop", "window_id": window_id},
            "path": str(output),
            "capture_mode": "window",
            "coordinate_space": "screenshot-pixels",
        }

    def pointer_position(self):
        self.calls.append(("pointer_position",))
        return {"x": 15, "y": 25}

    def list_displays(self):
        self.calls.append(("list_displays",))
        return [{"name": "DP-1", "x": 0, "y": 0, "width": 1920, "height": 1080}]


def test_private_default_target_is_resolved_without_live_desktop_fallback(monkeypatch) -> None:
    monkeypatch.delenv("KDE_DESKTOP_SESSION", raising=False)
    manager = FakeManager()
    live_desktop = FakeUserDesktop()

    operations = DesktopOperations(
        manager=manager,  # type: ignore[arg-type]
        user_desktop=live_desktop,  # type: ignore[arg-type]
    )
    result = operations.window_list()

    assert result["target"] == {"kind": "agent-session", "session_id": manager.default}
    assert manager.calls == [("default_session_id",), ("list_windows", manager.default)]
    assert live_desktop.calls == []


def test_missing_private_default_fails_closed(monkeypatch) -> None:
    monkeypatch.delenv("KDE_DESKTOP_SESSION", raising=False)
    manager = FakeManager(default=None)
    live_desktop = FakeUserDesktop()

    with pytest.raises(InteractionError) as error:
        DesktopOperations(
            manager=manager,  # type: ignore[arg-type]
            user_desktop=live_desktop,  # type: ignore[arg-type]
        )

    assert error.value.code == "private_default_session_unset"
    assert live_desktop.calls == []


def test_explicit_live_target_never_constructs_private_manager(monkeypatch) -> None:
    monkeypatch.setenv("KDE_DESKTOP_SESSION", "agent-111111111111")
    live_desktop = FakeUserDesktop()

    def unexpected_manager_factory():
        raise AssertionError("explicit user-desktop must not create a private manager")

    operations = DesktopOperations(
        "user-desktop",
        manager_factory=unexpected_manager_factory,  # type: ignore[arg-type]
        user_desktop=live_desktop,  # type: ignore[arg-type]
    )
    result = operations.window_list()

    assert result["target"]["kind"] == "user-desktop"
    assert live_desktop.calls == [("list_windows",)]


def test_window_screenshot_captures_selected_window_without_changing_focus(
    tmp_path: Path,
) -> None:
    live_desktop = FakeUserDesktop()
    operations = DesktopOperations("user-desktop", user_desktop=live_desktop)  # type: ignore[arg-type]

    result = operations.screenshot_window("window-1", tmp_path / "window.png")

    assert [call[0] for call in live_desktop.calls] == [
        "inspect_window",
        "screenshot_window",
        "inspect_window",
    ]
    assert live_desktop.calls[1][1] == "window-1"
    assert result["capture_mode"] == "window"
    assert result["window"]["active"] is False
    assert result["focus_changed"] is False
    assert result["target_window_verified"] is True


def test_window_screenshot_preserves_focus_when_target_is_already_active(tmp_path: Path) -> None:
    live_desktop = FakeUserDesktop()
    live_desktop.active_window_id = "window-1"
    operations = DesktopOperations("user-desktop", user_desktop=live_desktop)  # type: ignore[arg-type]

    result = operations.screenshot_window("window-1", tmp_path / "window.png")

    assert [call[0] for call in live_desktop.calls] == [
        "inspect_window",
        "screenshot_window",
        "inspect_window",
    ]
    assert live_desktop.calls[1][1] == "window-1"
    assert result["focus_changed"] is False


def test_global_pointer_and_minimize_operations_use_the_explicit_live_target() -> None:
    live_desktop = FakeUserDesktop()
    operations = DesktopOperations("user-desktop", user_desktop=live_desktop)  # type: ignore[arg-type]

    operations.input_click_at(-400, 250)
    operations.input_drag(10, 20, 30, 40, steps=4)
    operations.window_set_minimized("window-uuid", True)

    assert live_desktop.calls == [
        ("click_at", -400, 250, "left"),
        ("drag_at", 10, 20, 30, 40, "left", 4, 750, 100, 250),
        ("set_window_minimized", "window-uuid", True),
    ]


def test_fullscreen_and_maximize_operations_use_explicit_desired_states() -> None:
    live_desktop = FakeUserDesktop()
    operations = DesktopOperations("user-desktop", user_desktop=live_desktop)  # type: ignore[arg-type]

    operations.window_set_fullscreen("window-uuid", True)
    operations.window_set_fullscreen("window-uuid", False)
    operations.window_set_maximized("window-uuid", True)
    operations.window_set_maximized("window-uuid", False)

    manager = FakeManager()
    private_operations = DesktopOperations("agent-123456789abc", manager=manager)  # type: ignore[arg-type]
    private_operations.window_set_fullscreen("window-uuid", False)
    private_operations.window_set_maximized("window-uuid", True)

    assert live_desktop.calls == [
        ("set_window_fullscreen", "window-uuid", True),
        ("set_window_fullscreen", "window-uuid", False),
        ("set_window_maximized", "window-uuid", True),
        ("set_window_maximized", "window-uuid", False),
    ]
    assert manager.calls == [
        ("set_window_fullscreen", "agent-123456789abc", "window-uuid", False),
        ("set_window_maximized", "agent-123456789abc", "window-uuid", True),
    ]


def test_window_identity_preconditions_are_checked_before_live_mutation() -> None:
    live_desktop = FakeUserDesktop()
    operations = DesktopOperations("user-desktop", user_desktop=live_desktop)  # type: ignore[arg-type]

    operations.window_set_maximized(
        "window-uuid",
        True,
        expected_app_id="org.kde.kate",
        expected_pid=1234,
    )

    assert live_desktop.calls == [
        ("inspect_window", "window-uuid"),
        ("set_window_maximized", "window-uuid", True),
    ]


@pytest.mark.parametrize(
    ("expected_app_id", "expected_pid", "observed_identity"),
    [
        ("org.kde.konsole", None, {"app_id": "org.kde.kate", "pid": 1234}),
        (None, 5678, {"app_id": "org.kde.kate", "pid": 1234}),
    ],
)
def test_stale_window_identity_precondition_fails_before_mutation(
    expected_app_id: str | None,
    expected_pid: int | None,
    observed_identity: dict[str, Any],
) -> None:
    live_desktop = FakeUserDesktop()
    live_desktop.window_identity = observed_identity
    operations = DesktopOperations("user-desktop", user_desktop=live_desktop)  # type: ignore[arg-type]

    with pytest.raises(InteractionError) as error:
        operations.window_set_maximized(
            "window-uuid",
            True,
            expected_app_id=expected_app_id,
            expected_pid=expected_pid,
        )

    assert error.value.code == "stale_target"
    assert error.value.details["mismatches"]
    assert live_desktop.calls == [("inspect_window", "window-uuid")]


def test_window_precondition_routes_inspection_to_selected_private_session() -> None:
    manager = FakeManager()
    operations = DesktopOperations("agent-123456789abc", manager=manager)  # type: ignore[arg-type]

    operations.window_activate(
        "window-uuid",
        expected_app_id="org.kde.kate",
        expected_pid=1234,
    )

    assert manager.calls == [
        ("inspect_window", "agent-123456789abc", "window-uuid"),
        ("activate_window", "agent-123456789abc", "window-uuid"),
    ]


def test_geometry_and_pointer_queries_route_to_the_explicit_live_target() -> None:
    live_desktop = FakeUserDesktop()
    operations = DesktopOperations("user-desktop", user_desktop=live_desktop)  # type: ignore[arg-type]

    operations.window_set_geometry("window-uuid", 0, 10, 800, 600)
    pointer = operations.pointer_position()

    assert pointer["display_name"] == "DP-1"
    assert live_desktop.calls == [
        ("set_window_geometry", "window-uuid", 0, 10, 800, 600),
        ("pointer_position",),
        ("list_displays",),
    ]


def test_observe_returns_structured_context_and_reports_pointer_display() -> None:
    live_desktop = FakeUserDesktop()
    operations = DesktopOperations("user-desktop", user_desktop=live_desktop)  # type: ignore[arg-type]

    result = operations.observe()

    assert result["target"] == {"kind": "user-desktop"}
    assert result["snapshot_id"].startswith("snapshot-")
    assert result["interaction_context"]["keyboard_focus_window_id"] is None
    assert result["interaction_context"]["pointer"] == {
        "available": True,
        "x": 15,
        "y": 25,
        "display_name": "DP-1",
    }
    assert result["screenshot"] is None


def test_global_pointer_operations_route_to_selected_private_session() -> None:
    manager = FakeManager()
    operations = DesktopOperations("agent-222222222222", manager=manager)  # type: ignore[arg-type]

    operations.input_click_at(10, 20)
    operations.input_drag(10, 20, 30, 40, steps=4)
    operations.window_set_minimized("window-uuid", False)

    assert manager.calls == [
        ("click", "agent-222222222222", 10, 20, "left"),
        ("drag", "agent-222222222222", 10, 20, 30, 40, "left", 4, 750, 100, 250),
        ("set_window_minimized", "agent-222222222222", "window-uuid", False),
    ]


def test_geometry_and_pointer_queries_route_to_the_selected_private_session() -> None:
    manager = FakeManager()
    operations = DesktopOperations("agent-222222222222", manager=manager)  # type: ignore[arg-type]

    operations.window_set_geometry("window-uuid", 10, 20, 640, 480)
    pointer = operations.pointer_position()

    assert pointer["display_name"] == "Virtual-0"
    assert manager.calls == [
        ("set_window_geometry", "agent-222222222222", "window-uuid", 10, 20, 640, 480),
        ("pointer_position", "agent-222222222222"),
        ("displays", "agent-222222222222"),
    ]


def test_environment_session_overrides_private_default(monkeypatch) -> None:
    monkeypatch.setenv("KDE_DESKTOP_SESSION", "agent-222222222222")
    manager = FakeManager(default="agent-111111111111")

    operations = DesktopOperations(manager=manager)  # type: ignore[arg-type]
    result = operations.window_list()

    assert result["target"] == {"kind": "agent-session", "session_id": "agent-222222222222"}
    assert manager.calls == [("list_windows", "agent-222222222222")]


def test_explicit_private_session_overrides_environment_session(monkeypatch) -> None:
    monkeypatch.setenv("KDE_DESKTOP_SESSION", "agent-111111111111")
    manager = FakeManager()

    operations = DesktopOperations("agent-222222222222", manager=manager)  # type: ignore[arg-type]
    result = operations.window_list()

    assert result["target"]["session_id"] == "agent-222222222222"
    assert manager.calls == [("list_windows", "agent-222222222222")]


def test_environment_cannot_persistently_select_live_desktop(monkeypatch) -> None:
    monkeypatch.setenv("KDE_DESKTOP_SESSION", "user-desktop")
    manager = FakeManager()
    live_desktop = FakeUserDesktop()

    with pytest.raises(InteractionError) as error:
        DesktopOperations(
            manager=manager,  # type: ignore[arg-type]
            user_desktop=live_desktop,  # type: ignore[arg-type]
        )

    assert error.value.code == "user_desktop_requires_explicit_selection"
    assert live_desktop.calls == []


def test_environment_session_selects_target(monkeypatch) -> None:
    monkeypatch.setenv("KDE_DESKTOP_SESSION", "agent-333333333333")
    manager = FakeManager(default="agent-111111111111")

    operations = DesktopOperations(manager=manager)  # type: ignore[arg-type]
    result = operations.window_list()

    assert result["target"] == {"kind": "agent-session", "session_id": "agent-333333333333"}
    assert manager.calls == [("list_windows", "agent-333333333333")]


def test_private_capabilities_fail_when_selected_default_is_stopped() -> None:
    manager = FakeManager()
    manager.capability_record["status"] = "stopped"
    operations = DesktopOperations("agent-123456789abc", manager=manager)  # type: ignore[arg-type]

    with pytest.raises(InteractionError) as error:
        operations.capabilities()

    assert error.value.code == "session_not_running"


def test_private_capabilities_do_not_advertise_live_pipewire_controls() -> None:
    operations = DesktopOperations(
        "agent-123456789abc",
        manager=FakeManager(),  # type: ignore[arg-type]
    )

    result = operations.capabilities()

    assert result["capabilities"]["audio.devices"] is False
    assert result["capabilities"]["audio.route_stream"] is False

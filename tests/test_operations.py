from __future__ import annotations

from typing import Any

import pytest

from kde_interaction.errors import InteractionError
from kde_interaction.operations import DesktopOperations


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

    def find_window(self, session_id: str, *, app_id: str | None, title: str | None):
        self.calls.append(("find_window", session_id, app_id, title))
        return {"target": {"kind": "agent-session", "session_id": session_id}, "window": {}}

    def get_session(self, _session_id: str) -> dict[str, Any]:
        return self.capability_record


class FakeUserDesktop:
    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []

    def list_windows(self) -> dict[str, Any]:
        self.calls.append(("list_windows",))
        return {"target": {"kind": "user-desktop"}, "count": 0, "windows": []}


def test_private_default_target_is_resolved_without_live_desktop_fallback(monkeypatch) -> None:
    monkeypatch.delenv("KDE_AI_SESSION", raising=False)
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
    monkeypatch.delenv("KDE_AI_SESSION", raising=False)
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
    monkeypatch.setenv("KDE_AI_SESSION", "agent-111111111111")
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


def test_environment_session_overrides_private_default(monkeypatch) -> None:
    monkeypatch.setenv("KDE_AI_SESSION", "agent-222222222222")
    manager = FakeManager(default="agent-111111111111")

    operations = DesktopOperations(manager=manager)  # type: ignore[arg-type]
    result = operations.window_list()

    assert result["target"] == {"kind": "agent-session", "session_id": "agent-222222222222"}
    assert manager.calls == [("list_windows", "agent-222222222222")]


def test_explicit_private_session_overrides_environment_session(monkeypatch) -> None:
    monkeypatch.setenv("KDE_AI_SESSION", "agent-111111111111")
    manager = FakeManager()

    operations = DesktopOperations("agent-222222222222", manager=manager)  # type: ignore[arg-type]
    result = operations.window_list()

    assert result["target"]["session_id"] == "agent-222222222222"
    assert manager.calls == [("list_windows", "agent-222222222222")]


def test_environment_cannot_persistently_select_live_desktop(monkeypatch) -> None:
    monkeypatch.setenv("KDE_AI_SESSION", "user-desktop")
    manager = FakeManager()
    live_desktop = FakeUserDesktop()

    with pytest.raises(InteractionError) as error:
        DesktopOperations(
            manager=manager,  # type: ignore[arg-type]
            user_desktop=live_desktop,  # type: ignore[arg-type]
        )

    assert error.value.code == "user_desktop_requires_explicit_selection"
    assert live_desktop.calls == []


def test_private_capabilities_fail_when_selected_default_is_stopped() -> None:
    manager = FakeManager()
    manager.capability_record["status"] = "stopped"
    operations = DesktopOperations("agent-123456789abc", manager=manager)  # type: ignore[arg-type]

    with pytest.raises(InteractionError) as error:
        operations.capabilities()

    assert error.value.code == "session_not_running"

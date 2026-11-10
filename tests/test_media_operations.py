from __future__ import annotations

from typing import Any

from kde_desktop_automation.operations import DesktopOperations


class FakeManager:
    def __init__(self) -> None:
        self.environments: list[str] = []

    def runtime_environment(self, session_id: str) -> dict[str, str]:
        self.environments.append(session_id)
        return {"DBUS_SESSION_BUS_ADDRESS": f"unix:path=/{session_id}/bus"}


class FakeUserDesktop:
    environ = {"DBUS_SESSION_BUS_ADDRESS": "unix:path=/live-user/bus"}


class FakeMprisBackend:
    instances: list[FakeMprisBackend] = []

    def __init__(self, environ: dict[str, str]) -> None:
        self.environ = environ
        self.instances.append(self)

    def list_players(self, *, target: dict[str, Any]) -> dict[str, Any]:
        return {"target": target, "bus": self.environ["DBUS_SESSION_BUS_ADDRESS"]}

    def seek_to_player(
        self,
        player: str,
        position_seconds: float,
        *,
        target: dict[str, Any],
    ) -> dict[str, Any]:
        return {
            "target": target,
            "bus": self.environ["DBUS_SESSION_BUS_ADDRESS"],
            "player": player,
            "position_seconds": position_seconds,
        }


def test_private_media_uses_only_the_selected_private_session_bus(monkeypatch) -> None:
    manager = FakeManager()
    FakeMprisBackend.instances.clear()
    monkeypatch.setattr("kde_desktop_automation.operations.MprisBackend", FakeMprisBackend)
    operations = DesktopOperations("agent-123456789abc", manager=manager)  # type: ignore[arg-type]

    result = operations.media_players()

    assert manager.environments == ["agent-123456789abc"]
    assert result == {
        "target": {"kind": "agent-session", "session_id": "agent-123456789abc"},
        "bus": "unix:path=/agent-123456789abc/bus",
    }


def test_live_media_uses_the_explicit_live_desktop_bus(monkeypatch) -> None:
    FakeMprisBackend.instances.clear()
    monkeypatch.setattr("kde_desktop_automation.operations.MprisBackend", FakeMprisBackend)
    operations = DesktopOperations(
        "user-desktop",
        user_desktop=FakeUserDesktop(),  # type: ignore[arg-type]
    )

    result = operations.media_players()

    assert result == {"target": {"kind": "user-desktop"}, "bus": "unix:path=/live-user/bus"}


def test_media_seek_to_uses_the_selected_private_bus(monkeypatch) -> None:
    manager = FakeManager()
    FakeMprisBackend.instances.clear()
    monkeypatch.setattr("kde_desktop_automation.operations.MprisBackend", FakeMprisBackend)
    operations = DesktopOperations("agent-123456789abc", manager=manager)  # type: ignore[arg-type]

    result = operations.media_seek_to("vlc", 75.0)

    assert result == {
        "target": {"kind": "agent-session", "session_id": "agent-123456789abc"},
        "bus": "unix:path=/agent-123456789abc/bus",
        "player": "vlc",
        "position_seconds": 75.0,
    }

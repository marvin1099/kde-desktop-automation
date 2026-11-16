from __future__ import annotations

from collections.abc import Mapping, Sequence

import pytest

from kde_desktop_automation.backends import media_players
from kde_desktop_automation.backends.media_players import MprisBackend
from kde_desktop_automation.errors import InteractionError
from kde_desktop_automation.process import CommandResult

TARGET = {"kind": "user-desktop"}


def _fake_backend(
    monkeypatch: pytest.MonkeyPatch,
    players: Sequence[str],
    metadata: Mapping[str, str] | None = None,
    *,
    position: str = "12.5",
    statuses: Sequence[str] | None = None,
) -> tuple[MprisBackend, list[tuple[str, ...]]]:
    calls: list[tuple[str, ...]] = []
    status_values = list(statuses or ["Playing"])
    monkeypatch.setattr(
        "kde_desktop_automation.backends.media_players.require_executable",
        lambda _name: "/usr/bin/playerctl",
    )

    def fake_run_command(
        argv: Sequence[str],
        *,
        env: Mapping[str, str],
        timeout: float,
    ) -> CommandResult:
        assert env == {"DBUS_SESSION_BUS_ADDRESS": "unix:path=/test/bus"}
        assert 0 < timeout <= 3.0
        command = tuple(argv[1:])
        calls.append(command)
        if command == ("--list-all",):
            return CommandResult(tuple(argv), 0, "\n".join(players) + "\n", "")
        player = command[0].removeprefix("--player=")
        operation = command[1:]
        if operation == ("status",):
            status = status_values.pop(0) if len(status_values) > 1 else status_values[0]
            return CommandResult(tuple(argv), 0, f"{status}\n", "")
        if operation == ("position",):
            return CommandResult(tuple(argv), 0, position + "\n", "")
        if operation[:1] == ("metadata",):
            key = operation[1]
            if metadata is None or key not in metadata:
                return CommandResult(tuple(argv), 1, "", "metadata unavailable")
            return CommandResult(tuple(argv), 0, metadata[key] + "\n", "")
        if operation[0] in {"play", "pause", "play-pause", "stop", "next", "previous"}:
            return CommandResult(tuple(argv), 0, "", "")
        if operation[0] == "position" and len(operation) == 2:
            return CommandResult(tuple(argv), 0, "", "")
        raise AssertionError(f"Unexpected playerctl command for {player}: {operation}")

    monkeypatch.setattr(
        "kde_desktop_automation.backends.media_players.run_command", fake_run_command
    )
    return MprisBackend({"DBUS_SESSION_BUS_ADDRESS": "unix:path=/test/bus"}), calls


def test_mpris_list_returns_exact_player_names_for_selected_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend, _calls = _fake_backend(monkeypatch, ["vlc", "firefox.instance123"])

    result = backend.list_players(target=TARGET)

    assert result["target"] == TARGET
    assert result["count"] == 2
    assert result["players"] == [{"name": "vlc"}, {"name": "firefox.instance123"}]


def test_mpris_inspect_returns_status_metadata_position_and_duration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend, _calls = _fake_backend(
        monkeypatch,
        ["vlc"],
        {
            "xesam:title": "Example track",
            "xesam:artist": "Example artist",
            "xesam:album": "Example album",
            "mpris:length": "212500000",
        },
        position="42.25",
    )

    result = backend.inspect_player("vlc", target=TARGET)

    assert result["target"] == TARGET
    assert result["player"] == "vlc"
    assert result["playback_status"] == "Playing"
    assert result["metadata"] == {
        "title": "Example track",
        "artist": "Example artist",
        "album": "Example album",
    }
    assert result["position_seconds"] == 42.25
    assert result["duration_seconds"] == 212.5


def test_missing_track_metadata_is_reported_as_null(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend, _calls = _fake_backend(monkeypatch, ["vlc"])

    result = backend.inspect_player("vlc", target=TARGET)

    assert result["metadata"] == {"title": None, "artist": None, "album": None}
    assert result["position_seconds"] == 12.5
    assert result["duration_seconds"] is None


def test_mpris_control_targets_one_player_and_seeks_by_relative_seconds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend, calls = _fake_backend(monkeypatch, ["vlc"])

    result = backend.control_player("vlc", "seek", target=TARGET, seconds=12.5)

    assert calls[-1] == ("--player=vlc", "position", "12.5+")
    assert result["player"] == "vlc"
    assert result["action"] == "seek"
    assert result["seconds"] == 12.5
    assert result["command_submitted"] is True
    assert result["effect_verified"] is False


def test_mpris_seek_by_has_a_dedicated_exact_player_operation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend, calls = _fake_backend(monkeypatch, ["vlc"])

    result = backend.seek_by_player("vlc", -12.25, target=TARGET)

    assert calls[-1] == ("--player=vlc", "position", "12.25-")
    assert result["target"] == TARGET
    assert result["player"] == "vlc"
    assert result["action"] == "seek-by"
    assert result["seconds"] == -12.25
    assert result["command_submitted"] is True
    assert result["effect_verified"] is False


@pytest.mark.parametrize("seconds", [0, float("inf"), float("nan"), 86_401, True, 10**1000])
def test_mpris_seek_by_rejects_invalid_offsets_before_player_lookup(
    monkeypatch: pytest.MonkeyPatch,
    seconds: float,
) -> None:
    backend, calls = _fake_backend(monkeypatch, ["vlc"])

    with pytest.raises(InteractionError) as error:
        backend.seek_by_player("vlc", seconds, target=TARGET)  # type: ignore[arg-type]

    assert error.value.code == "invalid_seek"
    assert calls == []


def test_mpris_wait_for_status_polls_one_exact_player_until_match(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend, calls = _fake_backend(monkeypatch, ["vlc"], statuses=["Paused", "Playing"])
    monkeypatch.setattr(media_players.time, "sleep", lambda _seconds: None)

    result = backend.wait_for_status("vlc", "Playing", timeout=1.0, target=TARGET)

    assert calls.count(("--player=vlc", "status")) == 2
    assert result["target"] == TARGET
    assert result["player"] == "vlc"
    assert result["playback_status"] == "Playing"
    assert result["expected_playback_status"] == "Playing"
    assert result["waited_seconds"] >= 0
    assert result["last_observation"]["playback_status"] == "Playing"


def test_mpris_wait_timeout_includes_last_observed_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend, _calls = _fake_backend(monkeypatch, ["vlc"], statuses=["Paused"])

    with pytest.raises(InteractionError) as error:
        backend.wait_for_status("vlc", "Playing", timeout=0.1, target=TARGET)

    assert error.value.code == "media_wait_timeout"
    assert error.value.details["expected_playback_status"] == "Playing"
    assert error.value.details["last_observation"]["playback_status"] == "Paused"


@pytest.mark.parametrize(
    ("status", "timeout", "error_code"),
    [
        ("Buffering", 1.0, "invalid_playback_status"),
        ("Playing", 0.05, "invalid_timeout"),
        ("Playing", True, "invalid_timeout"),
        ("Playing", float("nan"), "invalid_timeout"),
        ("Playing", float("inf"), "invalid_timeout"),
        ("Playing", 10**1000, "invalid_timeout"),
    ],
)
def test_mpris_wait_rejects_unknown_status_and_unbounded_timeout(
    monkeypatch: pytest.MonkeyPatch,
    status: str,
    timeout: float,
    error_code: str,
) -> None:
    backend, calls = _fake_backend(monkeypatch, ["vlc"])

    with pytest.raises(InteractionError) as error:
        backend.wait_for_status("vlc", status, timeout=timeout, target=TARGET)

    assert error.value.code == error_code
    assert calls == []


def test_mpris_seek_to_uses_an_absolute_position_for_one_exact_player(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend, calls = _fake_backend(monkeypatch, ["vlc"])

    result = backend.seek_to_player("vlc", 142.75, target=TARGET)

    assert calls[-1] == ("--player=vlc", "position", "142.75")
    assert result["target"] == TARGET
    assert result["player"] == "vlc"
    assert result["action"] == "seek-to"
    assert result["position_seconds"] == 142.75
    assert result["command_submitted"] is True
    assert result["effect_verified"] is False


@pytest.mark.parametrize("position", [-0.1, float("inf"), float("nan"), 86_401.0, True])
def test_mpris_seek_to_rejects_invalid_absolute_positions(
    monkeypatch: pytest.MonkeyPatch,
    position: float,
) -> None:
    backend, calls = _fake_backend(monkeypatch, ["vlc"])

    with pytest.raises(InteractionError) as error:
        backend.seek_to_player("vlc", position, target=TARGET)

    assert error.value.code == "invalid_seek_position"
    assert calls == []


def test_mpris_seek_to_allows_zero_position(monkeypatch: pytest.MonkeyPatch) -> None:
    backend, calls = _fake_backend(monkeypatch, ["vlc"])

    result = backend.seek_to_player("vlc", 0, target=TARGET)

    assert calls[-1] == ("--player=vlc", "position", "0")
    assert result["position_seconds"] == 0


def test_mpris_seek_to_normalizes_negative_zero_to_absolute_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend, calls = _fake_backend(monkeypatch, ["vlc"])

    result = backend.seek_to_player("vlc", -0.0, target=TARGET)

    assert calls[-1] == ("--player=vlc", "position", "0")
    assert result["position_seconds"] == 0.0


def test_mpris_rejects_a_playerctl_selector_that_would_match_multiple_instances(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend, calls = _fake_backend(monkeypatch, ["firefox", "firefox.instance123"])

    with pytest.raises(InteractionError) as error:
        backend.inspect_player("firefox", target=TARGET)

    assert error.value.code == "ambiguous_player"
    assert error.value.details["candidates"] == ["firefox", "firefox.instance123"]
    assert calls == [("--list-all",)]


def test_mpris_rejects_a_player_not_in_the_selected_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend, _calls = _fake_backend(monkeypatch, ["vlc"])

    with pytest.raises(InteractionError) as error:
        backend.control_player("spotify", "pause", target=TARGET)

    assert error.value.code == "player_not_found"
    assert error.value.details["available_players"] == ["vlc"]


@pytest.mark.parametrize("seconds", [0.0, float("inf"), float("nan"), 86401.0])
def test_mpris_rejects_invalid_or_excessive_seek_offsets(
    monkeypatch: pytest.MonkeyPatch,
    seconds: float,
) -> None:
    backend, _calls = _fake_backend(monkeypatch, ["vlc"])

    with pytest.raises(InteractionError) as error:
        backend.control_player("vlc", "seek", target=TARGET, seconds=seconds)

    assert error.value.code == "invalid_seek"


def test_mpris_transport_action_rejects_unexpected_seek_argument(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend, calls = _fake_backend(monkeypatch, ["vlc"])

    with pytest.raises(InteractionError) as error:
        backend.control_player("vlc", "pause", target=TARGET, seconds=1)

    assert error.value.code == "invalid_media_command"
    assert calls == [("--list-all",)]


def test_mpris_rejects_unknown_actions(monkeypatch: pytest.MonkeyPatch) -> None:
    backend, calls = _fake_backend(monkeypatch, ["vlc"])

    with pytest.raises(InteractionError) as error:
        backend.control_player("vlc", "open", target=TARGET)

    assert error.value.code == "invalid_media_command"
    assert calls == [("--list-all",)]

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

import pytest

import kde_interaction.backends.pipewire_audio as pipewire_audio
from kde_interaction.backends.pipewire_audio import PipeWireAudioBackend
from kde_interaction.errors import InteractionError
from kde_interaction.process import CommandResult

TARGET = {"kind": "user-desktop"}


def _volume(percent: float) -> dict[str, dict[str, Any]]:
    value = round(percent / 100 * 65_536)
    return {
        "front-left": {"value": value, "value_percent": f"{percent:g}%"},
        "front-right": {"value": value, "value_percent": f"{percent:g}%"},
    }


def _state() -> dict[str, Any]:
    return {
        "server_name": "PulseAudio (on PipeWire 1.2.3)",
        "default_sink": "speakers",
        "default_source": "usb_mic",
        "sinks": [
            {
                "index": 1,
                "name": "speakers",
                "description": "Built-in Speakers",
                "state": "RUNNING",
                "mute": False,
                "volume": _volume(80),
            },
            {
                "index": 2,
                "name": "headphones",
                "description": "USB Headphones",
                "state": "IDLE",
                "mute": False,
                "volume": _volume(100),
            },
        ],
        "sources": [
            {
                "index": 3,
                "name": "usb_mic",
                "description": "USB Microphone",
                "mute": False,
                "volume": _volume(75),
                "monitor_of_sink": -1,
            },
            {
                "index": 4,
                "name": "speakers.monitor",
                "description": "Monitor of Built-in Speakers",
                "mute": False,
                "volume": _volume(100),
                "monitor_of_sink": 1,
            },
        ],
        "sink_inputs": [
            {
                "index": 17,
                "sink": 1,
                "mute": False,
                "volume": _volume(100),
                "properties": {
                    "application.name": "Firefox",
                    "application.process.binary": "firefox",
                    "application.process.id": "555",
                    "media.name": "Video audio",
                },
            },
            {
                "index": 18,
                "sink": 1,
                "mute": False,
                "volume": _volume(100),
                "properties": {
                    "application.name": "Discord",
                    "application.process.binary": "discord",
                    "application.process.id": "777",
                },
            },
        ],
    }


def _fake_backend(
    monkeypatch: pytest.MonkeyPatch,
    state: dict[str, Any] | None = None,
) -> tuple[PipeWireAudioBackend, list[tuple[str, ...]], dict[str, Any]]:
    current = state or _state()
    calls: list[tuple[str, ...]] = []
    monkeypatch.setattr(
        "kde_interaction.backends.pipewire_audio.require_executable",
        lambda name: f"/usr/bin/{name}",
    )

    def fake_run_command(
        argv: Sequence[str],
        *,
        env: Mapping[str, str],
        timeout: float,
    ) -> CommandResult:
        assert env == {"XDG_RUNTIME_DIR": "/run/user/test"}
        command = tuple(argv[1:])
        calls.append(command)
        if argv[0].endswith("wpctl"):
            assert command == ("status", "--name")
            return CommandResult(tuple(argv), 0, "PipeWire status\n", "")
        if command == ("info",):
            output = (
                f"Server Name: {current['server_name']}\n"
                f"Default Sink: {current['default_sink']}\n"
                f"Default Source: {current['default_source']}\n"
            )
            return CommandResult(tuple(argv), 0, output, "")
        if command[:2] == ("--format=json", "list"):
            key = {
                "sinks": "sinks",
                "sources": "sources",
                "sink-inputs": "sink_inputs",
            }[command[2]]
            return CommandResult(tuple(argv), 0, json.dumps(current[key]), "")
        if command[0] == "set-default-sink":
            current["default_sink"] = command[1]
            return CommandResult(tuple(argv), 0, "", "")
        if command[0] == "set-default-source":
            current["default_source"] = command[1]
            return CommandResult(tuple(argv), 0, "", "")
        if command[0] in {"set-sink-volume", "set-source-volume"}:
            records = current["sinks"] if command[0] == "set-sink-volume" else current["sources"]
            record = next(item for item in records if item["name"] == command[1])
            percent = float(command[2].removesuffix("%"))
            record["volume"] = _volume(percent)
            return CommandResult(tuple(argv), 0, "", "")
        if command[0] in {"set-sink-mute", "set-source-mute"}:
            records = current["sinks"] if command[0] == "set-sink-mute" else current["sources"]
            record = next(item for item in records if item["name"] == command[1])
            record["mute"] = command[2] == "1"
            return CommandResult(tuple(argv), 0, "", "")
        if command[0] == "move-sink-input":
            stream = next(
                item for item in current["sink_inputs"] if item["index"] == int(command[1])
            )
            sink = next(item for item in current["sinks"] if item["name"] == command[2])
            stream["sink"] = sink["index"]
            return CommandResult(tuple(argv), 0, "", "")
        raise AssertionError(f"Unexpected audio command: {command}")

    monkeypatch.setattr("kde_interaction.backends.pipewire_audio.run_command", fake_run_command)
    backend = PipeWireAudioBackend({"XDG_RUNTIME_DIR": "/run/user/test"})
    return backend, calls, current


def test_audio_list_reports_defaults_devices_and_stream_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend, _calls, _state_data = _fake_backend(monkeypatch)

    result = backend.list_audio(target=TARGET)

    assert result["backend"] == "pipewire-pulse"
    assert result["default_devices"] == {"sink": "speakers", "source": "usb_mic"}
    assert result["devices"]["sinks"][0]["default"] is True
    assert result["devices"]["sources"][1]["is_monitor"] is True
    assert result["streams"][0] == {
        "stream_id": 17,
        "application_name": "Firefox",
        "process_id": 555,
        "media_name": "Video audio",
        "client_name": "firefox",
        "sink_index": 1,
        "sink_name": "speakers",
        "muted": False,
        "volume_percent": 100.0,
    }


def test_audio_operations_refuse_a_non_pipewire_pulse_server(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_data = _state()
    state_data["server_name"] = "pulseaudio"
    backend, calls, _state_data = _fake_backend(monkeypatch, state_data)

    with pytest.raises(InteractionError) as error:
        backend.list_audio(target=TARGET)

    assert error.value.code == "audio_backend_unavailable"
    assert calls == [("status", "--name"), ("info",)]


def test_set_default_sink_changes_and_verifies_only_the_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend, calls, state_data = _fake_backend(monkeypatch)

    result = backend.set_default("sink", "headphones", target=TARGET)

    assert state_data["default_sink"] == "headphones"
    assert result["verified"] is True
    assert result["device"]["name"] == "headphones"
    assert ("set-default-sink", "headphones") in calls
    assert state_data["sink_inputs"][0]["sink"] == 1


def test_set_volume_and_mute_verify_a_specific_input_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend, calls, state_data = _fake_backend(monkeypatch)

    volume_result = backend.set_volume("source", "usb_mic", 50, target=TARGET)
    mute_result = backend.set_mute("source", "usb_mic", True, target=TARGET)

    assert state_data["sources"][0]["volume"] == _volume(50)
    assert state_data["sources"][0]["mute"] is True
    assert volume_result["device"]["volume_percent"] == 50.0
    assert mute_result["device"]["muted"] is True
    assert mute_result["verified"] is True
    assert ("set-source-volume", "usb_mic", "50%") in calls
    assert ("set-source-mute", "usb_mic", "1") in calls


def test_route_stream_checks_identity_and_moves_only_that_output_stream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend, calls, state_data = _fake_backend(monkeypatch)

    result = backend.route_stream(
        17,
        "headphones",
        application_name="Firefox",
        process_id=555,
        target=TARGET,
    )

    assert result["previous_sink"] == "speakers"
    assert result["effective_sink"]["name"] == "headphones"
    assert result["verified"] is True
    assert state_data["sink_inputs"][0]["sink"] == 2
    assert state_data["sink_inputs"][1]["sink"] == 1
    assert ("move-sink-input", "17", "headphones") in calls


def test_route_stream_fails_closed_if_stream_identity_changed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend, calls, _state_data = _fake_backend(monkeypatch)

    with pytest.raises(InteractionError) as error:
        backend.route_stream(
            17,
            "headphones",
            application_name="Chrome",
            target=TARGET,
        )

    assert error.value.code == "audio_stream_identity_mismatch"
    assert not any(call[0] == "move-sink-input" for call in calls)


def test_route_stream_rechecks_identity_after_move(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend, _calls, state_data = _fake_backend(monkeypatch)
    original_run_command = pipewire_audio.run_command

    def replace_stream_identity(argv, *, env, timeout):
        result = original_run_command(argv, env=env, timeout=timeout)
        if tuple(argv[1:3]) == ("move-sink-input", "17"):
            state_data["sink_inputs"][0]["properties"]["application.name"] = "Chrome"
        return result

    monkeypatch.setattr(pipewire_audio, "run_command", replace_stream_identity)

    with pytest.raises(InteractionError) as error:
        backend.route_stream(
            17,
            "headphones",
            application_name="Firefox",
            target=TARGET,
        )

    assert error.value.code == "audio_stream_identity_mismatch"


def test_audio_volume_is_bounded_to_safe_percentages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend, calls, _state_data = _fake_backend(monkeypatch)

    with pytest.raises(InteractionError) as error:
        backend.set_volume("sink", "speakers", 125, target=TARGET)

    assert error.value.code == "invalid_audio_volume"
    assert calls == []

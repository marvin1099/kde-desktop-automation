from __future__ import annotations

import json
import math
import time
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any, cast

from kde_desktop_automation.errors import InteractionError
from kde_desktop_automation.process import (
    CommandResult,
    require_executable,
    require_success,
    run_command,
)

_MAX_VOLUME_PERCENT = 100.0
_PULSE_VOLUME_NORM = 65_536
_MIN_AUDIO_WAIT_SECONDS = 0.1
_MAX_AUDIO_WAIT_SECONDS = 60.0
_AUDIO_POLL_INTERVAL_SECONDS = 0.2
_AUDIO_COMMAND_TIMEOUT_SECONDS = 5.0


class PipeWireAudioBackend:
    """Read/control PipeWire through its PulseAudio compatibility server."""

    def __init__(self, environ: Mapping[str, str]) -> None:
        self.environ = dict(environ)
        self.wpctl = require_executable("wpctl")
        self.pactl = require_executable("pactl")

    def list_audio(self, *, target: dict[str, Any]) -> dict[str, Any]:
        snapshot = self._snapshot()
        return snapshot | {
            "target": target,
            "observed_at": datetime.now(UTC).isoformat(),
            "note": (
                "Audio devices and active output streams are read from the selected live "
                "PipeWire session; stream IDs can change at any time."
            ),
        }

    def set_default(
        self,
        direction: str,
        device_name: str,
        *,
        target: dict[str, Any],
    ) -> dict[str, Any]:
        self._validate_direction(direction)
        snapshot = self._snapshot()
        device = self._resolve_device(snapshot, direction, device_name)
        command = "set-default-sink" if direction == "sink" else "set-default-source"
        self._pactl(command, device["name"])
        verified = self._snapshot()
        effective_name = verified["default_devices"][direction]
        if effective_name != device["name"]:
            raise InteractionError(
                "audio_state_unverified",
                "PipeWire accepted the default-device request, but readback differs.",
                details={
                    "direction": direction,
                    "requested": device["name"],
                    "effective": effective_name,
                },
            )
        return {
            "target": target,
            "action": "set-default",
            "direction": direction,
            "device": device,
            "verified": True,
            "note": "Existing application streams may remain on their current device.",
        }

    def set_volume(
        self,
        direction: str,
        device_name: str,
        volume_percent: float,
        *,
        target: dict[str, Any],
    ) -> dict[str, Any]:
        self._validate_direction(direction)
        if (
            isinstance(volume_percent, bool)
            or not isinstance(volume_percent, (int, float))
            or not math.isfinite(volume_percent)
            or volume_percent < 0
            or volume_percent > _MAX_VOLUME_PERCENT
        ):
            raise InteractionError(
                "invalid_audio_volume",
                "Volume must be a finite percentage from 0 through 100.",
                details={"volume_percent": volume_percent},
            )
        snapshot = self._snapshot()
        device = self._resolve_device(snapshot, direction, device_name)
        command = "set-sink-volume" if direction == "sink" else "set-source-volume"
        self._pactl(command, device["name"], f"{volume_percent:g}%")
        updated = self._snapshot()
        effective = self._resolve_device(updated, direction, device_name)
        actual = effective["volume_percent"]
        if actual is None or abs(actual - volume_percent) > 1.0:
            raise InteractionError(
                "audio_state_unverified",
                "PipeWire accepted the volume request, but could not verify the effective level.",
                details={
                    "direction": direction,
                    "device": device_name,
                    "requested_volume_percent": volume_percent,
                    "effective_volume_percent": actual,
                },
            )
        return {
            "target": target,
            "action": "set-volume",
            "direction": direction,
            "device": effective,
            "verified": True,
        }

    def set_mute(
        self,
        direction: str,
        device_name: str,
        muted: bool,
        *,
        target: dict[str, Any],
    ) -> dict[str, Any]:
        self._validate_direction(direction)
        if not isinstance(muted, bool):
            raise InteractionError(
                "invalid_audio_mute_state",
                "Mute state must be explicitly true or false; toggle is not supported.",
                details={"muted": muted},
            )
        snapshot = self._snapshot()
        device = self._resolve_device(snapshot, direction, device_name)
        command = "set-sink-mute" if direction == "sink" else "set-source-mute"
        self._pactl(command, device["name"], "1" if muted else "0")
        updated = self._snapshot()
        effective = self._resolve_device(updated, direction, device_name)
        if effective["muted"] is not muted:
            raise InteractionError(
                "audio_state_unverified",
                "PipeWire accepted the mute request, but readback differs.",
                details={
                    "direction": direction,
                    "device": device_name,
                    "requested_muted": muted,
                    "effective_muted": effective["muted"],
                },
            )
        return {
            "target": target,
            "action": "set-mute",
            "direction": direction,
            "device": effective,
            "verified": True,
        }

    def wait_for_mute(
        self,
        direction: str,
        device_name: str,
        muted: bool,
        *,
        timeout: float,
        target: dict[str, Any],
    ) -> dict[str, Any]:
        """Wait a bounded time for one exact sink/source mute state, without mutating it."""
        self._validate_direction(direction)
        if not isinstance(muted, bool):
            raise InteractionError(
                "invalid_audio_mute_state",
                "Expected mute state must be explicitly true or false.",
                details={"muted": muted},
            )
        try:
            timeout_seconds = (
                float(timeout)
                if not isinstance(timeout, bool) and isinstance(timeout, (int, float))
                else None
            )
        except OverflowError:
            timeout_seconds = None
        if (
            timeout_seconds is None
            or not math.isfinite(timeout_seconds)
            or timeout_seconds < _MIN_AUDIO_WAIT_SECONDS
            or timeout_seconds > _MAX_AUDIO_WAIT_SECONDS
        ):
            raise InteractionError(
                "invalid_timeout",
                "Audio mute-state wait timeout must be between 0.1 and 60 seconds.",
                details={
                    "timeout_seconds": timeout,
                    "minimum_seconds": _MIN_AUDIO_WAIT_SECONDS,
                    "maximum_seconds": _MAX_AUDIO_WAIT_SECONDS,
                },
            )

        started = time.monotonic()
        deadline = started + timeout_seconds

        def raise_timeout(
            last_observation: dict[str, Any] | None,
            *,
            query_error: InteractionError | None = None,
        ) -> None:
            details: dict[str, Any] = {
                "direction": direction,
                "device": device_name,
                "expected_muted": muted,
                "timeout_seconds": timeout_seconds,
                "last_observation": last_observation,
            }
            if query_error is not None:
                details["last_query_error"] = query_error.as_dict()
            raise InteractionError(
                "audio_wait_timeout",
                "The requested device mute state was not observed before the timeout.",
                details=details,
            ) from query_error

        last_observation: dict[str, Any] | None = None
        last_query_error: InteractionError | None = None
        server_info: dict[str, str | None] | None = None
        default_name: str | None = None
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise_timeout(last_observation, query_error=last_query_error)
            try:
                if server_info is None:
                    server_info = self._require_pipewire(
                        timeout=min(_AUDIO_COMMAND_TIMEOUT_SECONDS, remaining / 3)
                    )
                    default_name = server_info[
                        "default_sink" if direction == "sink" else "default_source"
                    ]
                    last_query_error = None
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise_timeout(last_observation)
                records = self._json_list(
                    "sinks" if direction == "sink" else "sources",
                    timeout=min(_AUDIO_COMMAND_TIMEOUT_SECONDS, remaining),
                )
            except InteractionError as exc:
                if exc.code != "backend_timeout":
                    raise
                last_query_error = exc
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise_timeout(last_observation, query_error=last_query_error)
                time.sleep(min(_AUDIO_POLL_INTERVAL_SECONDS, remaining))
                continue
            devices = self._normalize_devices(records, direction, default_name)
            device = self._resolve_device(
                {"devices": {direction + "s": devices}},
                direction,
                device_name,
            )
            last_query_error = None
            last_observation = {
                "device": device,
                "observed_at": datetime.now(UTC).isoformat(),
            }
            if device["muted"] is muted:
                return {
                    "target": target,
                    "action": "wait-for-mute",
                    "direction": direction,
                    "expected_muted": muted,
                    "device": device,
                    "observed_at": last_observation["observed_at"],
                    "waited_seconds": time.monotonic() - started,
                    "matched": True,
                }
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise_timeout(last_observation, query_error=last_query_error)
            time.sleep(min(_AUDIO_POLL_INTERVAL_SECONDS, remaining))

    def route_stream(
        self,
        stream_id: int,
        sink_name: str,
        *,
        application_name: str,
        target: dict[str, Any],
        process_id: int | None = None,
    ) -> dict[str, Any]:
        if isinstance(stream_id, bool) or not isinstance(stream_id, int) or stream_id < 0:
            raise InteractionError(
                "invalid_audio_stream_id",
                "Stream ID must be a non-negative integer from audio list.",
                details={"stream_id": stream_id},
            )
        if not application_name or application_name != application_name.strip():
            raise InteractionError(
                "invalid_audio_stream_selector",
                "Pass the exact application name reported by audio list.",
                details={"application_name": application_name},
            )
        if process_id is not None and (
            isinstance(process_id, bool) or not isinstance(process_id, int) or process_id <= 0
        ):
            raise InteractionError(
                "invalid_audio_stream_selector",
                "Process ID must be a positive integer.",
                details={"process_id": process_id},
            )

        before = self._snapshot()
        sink = self._resolve_device(before, "sink", sink_name)
        if sink["is_monitor"]:
            raise InteractionError(
                "audio_monitor_not_routable",
                "A monitor endpoint cannot be used as an output sink.",
                details={"sink": sink_name},
            )
        stream = self._resolve_stream(before, stream_id)
        self._verify_stream_identity(
            stream,
            stream_id=stream_id,
            application_name=application_name,
            process_id=process_id,
        )

        self._pactl("move-sink-input", str(stream_id), sink["name"])
        after = self._snapshot()
        moved = self._resolve_stream(after, stream_id)
        self._verify_stream_identity(
            moved,
            stream_id=stream_id,
            application_name=application_name,
            process_id=process_id,
        )
        if moved["sink_name"] != sink["name"]:
            raise InteractionError(
                "audio_state_unverified",
                "PipeWire accepted the stream-routing request, but the stream remains elsewhere.",
                details={
                    "stream_id": stream_id,
                    "requested_sink": sink["name"],
                    "effective_sink": moved["sink_name"],
                },
            )
        return {
            "target": target,
            "action": "route-stream",
            "stream": moved,
            "previous_sink": stream["sink_name"],
            "effective_sink": sink,
            "verified": True,
            "note": "Only this sink-input stream was moved; other streams are unchanged.",
        }

    def _snapshot(self) -> dict[str, Any]:
        server_info = self._require_pipewire()
        sinks = self._normalize_devices(
            self._json_list("sinks"), "sink", server_info["default_sink"]
        )
        sources = self._normalize_devices(
            self._json_list("sources"), "source", server_info["default_source"]
        )
        sink_indexes = {device["index"]: device["name"] for device in sinks}
        streams = [
            _normalize_stream(record, sink_indexes) for record in self._json_list("sink-inputs")
        ]
        return {
            "backend": "pipewire-pulse",
            "server_name": server_info["server_name"],
            "default_devices": {
                "sink": server_info["default_sink"],
                "source": server_info["default_source"],
            },
            "devices": {"sinks": sinks, "sources": sources},
            "streams": streams,
        }

    def _require_pipewire(self, *, timeout: float = 4.0) -> dict[str, str | None]:
        status = run_command([self.wpctl, "status", "--name"], env=self.environ, timeout=timeout)
        require_success(status)
        result = run_command([self.pactl, "info"], env=self.environ, timeout=timeout)
        require_success(result)
        values: dict[str, str] = {}
        for line in result.stdout.splitlines():
            key, separator, value = line.partition(":")
            if separator:
                values[key.strip()] = value.strip()
        server_name = values.get("Server Name")
        if server_name is None:
            raise InteractionError(
                "invalid_backend_data",
                "pactl info did not include a server name.",
            )
        if "pipewire" not in server_name.casefold():
            raise InteractionError(
                "audio_backend_unavailable",
                "Audio control requires PipeWire's PulseAudio compatibility server.",
                details={"server_name": server_name},
            )
        return {
            "server_name": server_name,
            "default_sink": values.get("Default Sink"),
            "default_source": values.get("Default Source"),
        }

    def _json_list(
        self,
        object_type: str,
        *,
        timeout: float = _AUDIO_COMMAND_TIMEOUT_SECONDS,
    ) -> list[dict[str, Any]]:
        result = self._pactl("--format=json", "list", object_type, timeout=timeout)
        try:
            records = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise InteractionError(
                "invalid_backend_data",
                f"pactl returned invalid JSON for {object_type}.",
                details={"object_type": object_type},
            ) from exc
        if not isinstance(records, list) or not all(isinstance(item, dict) for item in records):
            raise InteractionError(
                "invalid_backend_data",
                f"pactl JSON for {object_type} must be a list of objects.",
                details={"object_type": object_type},
            )
        return records

    def _pactl(
        self,
        *args: str,
        timeout: float = _AUDIO_COMMAND_TIMEOUT_SECONDS,
    ) -> CommandResult:
        result = run_command([self.pactl, *args], env=self.environ, timeout=timeout)
        return require_success(result)

    def _normalize_devices(
        self,
        records: list[dict[str, Any]],
        direction: str,
        default_name: str | None,
    ) -> list[dict[str, Any]]:
        devices: list[dict[str, Any]] = []
        for record in records:
            index = _nonnegative_integer(record.get("index"))
            name = record.get("name")
            if index is None or not isinstance(name, str) or not name:
                raise InteractionError(
                    "invalid_backend_data",
                    f"pactl returned a {direction} without a valid index and name.",
                )
            description = record.get("description")
            monitor_sink = record.get("monitor_of_sink")
            is_monitor = monitor_sink not in (None, -1, "-1", "n/a") or name.endswith(".monitor")
            devices.append(
                {
                    "index": index,
                    "name": name,
                    "description": description if isinstance(description, str) else None,
                    "direction": direction,
                    "default": name == default_name,
                    "muted": record.get("mute") if isinstance(record.get("mute"), bool) else None,
                    "volume_percent": _volume_percent(record.get("volume")),
                    "state": record.get("state") if isinstance(record.get("state"), str) else None,
                    "is_monitor": is_monitor,
                }
            )
        _reject_duplicate_names(devices, "audio device")
        return devices

    def _resolve_device(
        self,
        snapshot: dict[str, Any],
        direction: str,
        device_name: str,
    ) -> dict[str, Any]:
        if not device_name or device_name != device_name.strip():
            raise InteractionError(
                "invalid_audio_device_selector",
                "Pass one exact device name returned by audio list.",
                details={"device": device_name, "direction": direction},
            )
        devices = snapshot["devices"]["sinks" if direction == "sink" else "sources"]
        matches = [device for device in devices if device["name"] == device_name]
        if len(matches) != 1:
            raise InteractionError(
                "audio_device_not_found",
                "The exact audio device is not currently available.",
                details={
                    "direction": direction,
                    "device": device_name,
                    "available_devices": [device["name"] for device in devices],
                },
            )
        return cast(dict[str, Any], matches[0])

    def _resolve_stream(self, snapshot: dict[str, Any], stream_id: int) -> dict[str, Any]:
        matches = [stream for stream in snapshot["streams"] if stream["stream_id"] == stream_id]
        if len(matches) != 1:
            raise InteractionError(
                "audio_stream_not_found",
                "The sink-input stream is no longer available; list audio and select it again.",
                details={"stream_id": stream_id},
            )
        return cast(dict[str, Any], matches[0])

    @staticmethod
    def _verify_stream_identity(
        stream: dict[str, Any],
        *,
        stream_id: int,
        application_name: str,
        process_id: int | None,
    ) -> None:
        if stream["application_name"] != application_name or (
            process_id is not None and stream["process_id"] != process_id
        ):
            raise InteractionError(
                "audio_stream_identity_mismatch",
                "The stream no longer matches the requested application identity.",
                details={
                    "stream_id": stream_id,
                    "requested_application_name": application_name,
                    "observed_application_name": stream["application_name"],
                    "requested_process_id": process_id,
                    "observed_process_id": stream["process_id"],
                },
            )

    @staticmethod
    def _validate_direction(direction: str) -> None:
        if direction not in {"sink", "source"}:
            raise InteractionError(
                "invalid_audio_direction",
                "Audio direction must be sink (output) or source (input).",
                details={"direction": direction},
            )


def _normalize_stream(
    record: dict[str, Any],
    sink_indexes: dict[int, str],
) -> dict[str, Any]:
    stream_id = _nonnegative_integer(record.get("index"))
    sink_index = _nonnegative_integer(record.get("sink"))
    properties = record.get("properties")
    if stream_id is None or not isinstance(properties, dict):
        raise InteractionError(
            "invalid_backend_data",
            "pactl returned a sink-input without a valid ID or properties.",
        )
    application_name = _first_text(
        properties,
        "application.name",
        "application.process.binary",
    )
    process_id = _positive_integer(properties.get("application.process.id"))
    media_name = _first_text(properties, "media.name", "media.title")
    client_name = _first_text(properties, "application.process.binary", "application.name")
    return {
        "stream_id": stream_id,
        "application_name": application_name,
        "process_id": process_id,
        "media_name": media_name,
        "client_name": client_name,
        "sink_index": sink_index,
        "sink_name": sink_indexes.get(sink_index) if sink_index is not None else None,
        "muted": record.get("mute") if isinstance(record.get("mute"), bool) else None,
        "volume_percent": _volume_percent(record.get("volume")),
    }


def _first_text(values: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = values.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _nonnegative_integer(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and value >= 0:
        return value
    if isinstance(value, str) and value.isdecimal():
        return int(value)
    return None


def _positive_integer(value: Any) -> int | None:
    result = _nonnegative_integer(value)
    return result if result is not None and result > 0 else None


def _volume_percent(value: Any) -> float | None:
    if not isinstance(value, dict) or not value:
        return None
    percentages: list[float] = []
    for channel in value.values():
        if not isinstance(channel, dict):
            continue
        percentage = channel.get("value_percent")
        if isinstance(percentage, str) and percentage.endswith("%"):
            try:
                parsed = float(percentage[:-1])
            except ValueError:
                continue
        elif isinstance(percentage, (int, float)) and not isinstance(percentage, bool):
            parsed = float(percentage)
        else:
            raw = channel.get("value")
            if not isinstance(raw, (int, float)) or isinstance(raw, bool):
                continue
            parsed = float(raw) / _PULSE_VOLUME_NORM * 100
        if math.isfinite(parsed):
            percentages.append(parsed)
    if not percentages:
        return None
    return round(sum(percentages) / len(percentages), 2)


def _reject_duplicate_names(devices: list[dict[str, Any]], kind: str) -> None:
    names = [device["name"] for device in devices]
    if len(names) != len(set(names)):
        raise InteractionError(
            "ambiguous_audio_device",
            f"PipeWire returned duplicate names for {kind}; use the CLI after the devices change.",
        )

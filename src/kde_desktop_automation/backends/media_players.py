from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from math import isfinite
from typing import Any

from kde_desktop_automation.errors import InteractionError
from kde_desktop_automation.process import (
    CommandResult,
    require_executable,
    require_success,
    run_command,
)

_TRANSPORT_ACTIONS = frozenset({"play", "pause", "play-pause", "stop", "next", "previous"})
_MAX_SEEK_SECONDS = 24 * 60 * 60


class MprisBackend:
    """Target-scoped access to players that publish the MPRIS D-Bus interface."""

    def __init__(self, environ: Mapping[str, str]) -> None:
        self.environ = dict(environ)
        self.playerctl = require_executable("playerctl")

    def list_players(self, *, target: dict[str, Any]) -> dict[str, Any]:
        names = self._available_players()
        return {
            "target": target,
            "observed_at": datetime.now(UTC).isoformat(),
            "count": len(names),
            "players": [{"name": name} for name in names],
            "note": "Player names are selectors for this target's D-Bus session.",
        }

    def inspect_player(self, player: str, *, target: dict[str, Any]) -> dict[str, Any]:
        selected = self._resolve_player(player)
        status_result = self._run_player(selected, "status")
        status = require_success(status_result).stdout.strip() or None

        title = self._optional_metadata(selected, "xesam:title")
        artist = self._optional_metadata(selected, "xesam:artist")
        album = self._optional_metadata(selected, "xesam:album")
        length = self._optional_metadata(selected, "mpris:length")
        position = self._optional_player_query(selected, "position")

        return {
            "target": target,
            "observed_at": datetime.now(UTC).isoformat(),
            "player": selected,
            "playback_status": status,
            "metadata": {
                "title": title,
                "artist": artist,
                "album": album,
            },
            "position_seconds": _parse_optional_float(position, "position"),
            "duration_seconds": _parse_duration(length),
            "note": "MPRIS properties are queried sequentially and may change during inspection.",
        }

    def control_player(
        self,
        player: str,
        action: str,
        *,
        target: dict[str, Any],
        seconds: float | None = None,
    ) -> dict[str, Any]:
        selected = self._resolve_player(player)
        if action in _TRANSPORT_ACTIONS:
            if seconds is not None:
                raise InteractionError(
                    "invalid_media_command",
                    "The seconds argument is only valid for the seek action.",
                    details={"action": action},
                )
            argv = [action]
        elif action == "seek":
            if (
                seconds is None
                or isinstance(seconds, bool)
                or not isinstance(seconds, (int, float))
                or not isfinite(seconds)
                or seconds == 0
            ):
                raise InteractionError(
                    "invalid_seek",
                    "Seek requires a finite, non-zero relative offset in seconds.",
                    details={"seconds": seconds},
                )
            if abs(seconds) > _MAX_SEEK_SECONDS:
                raise InteractionError(
                    "invalid_seek",
                    "A relative seek cannot exceed 24 hours in either direction.",
                    details={"seconds": seconds, "maximum_absolute_seconds": _MAX_SEEK_SECONDS},
                )
            magnitude = f"{abs(seconds):.6f}".rstrip("0").rstrip(".")
            offset = f"{magnitude}{'+' if seconds > 0 else '-'}"
            argv = ["position", offset]
        else:
            raise InteractionError(
                "invalid_media_command",
                "Unsupported media action.",
                details={"action": action, "supported": sorted(_TRANSPORT_ACTIONS | {"seek"})},
            )

        require_success(self._run_player(selected, *argv))
        return {
            "target": target,
            "player": selected,
            "action": action,
            "seconds": seconds if action == "seek" else None,
            "command_submitted": True,
            "effect_verified": False,
            "note": (
                "The player accepted the MPRIS request; inspect status or position "
                "to verify effect."
            ),
        }

    def seek_to_player(
        self,
        player: str,
        position_seconds: float,
        *,
        target: dict[str, Any],
    ) -> dict[str, Any]:
        """Submit an absolute seek to one exact MPRIS player."""
        if (
            isinstance(position_seconds, bool)
            or not isinstance(position_seconds, (int, float))
            or not isfinite(position_seconds)
            or position_seconds < 0
            or position_seconds > _MAX_SEEK_SECONDS
        ):
            raise InteractionError(
                "invalid_seek_position",
                "Absolute position must be a finite value from 0 through 86400 seconds.",
                details={
                    "position_seconds": position_seconds,
                    "maximum_position_seconds": _MAX_SEEK_SECONDS,
                },
            )

        selected = self._resolve_player(player)
        normalized_position = 0.0 if position_seconds == 0 else float(position_seconds)
        position = f"{normalized_position:.6f}".rstrip("0").rstrip(".")
        require_success(self._run_player(selected, "position", position))
        return {
            "target": target,
            "player": selected,
            "action": "seek-to",
            "position_seconds": normalized_position,
            "command_submitted": True,
            "effect_verified": False,
            "note": (
                "The player accepted the absolute MPRIS position request; inspect position "
                "to verify effect."
            ),
        }

    def _available_players(self) -> list[str]:
        result = run_command([self.playerctl, "--list-all"], env=self.environ, timeout=3.0)
        require_success(result)
        names = list(
            dict.fromkeys(line.strip() for line in result.stdout.splitlines() if line.strip())
        )
        if any("," in name or "\0" in name for name in names):
            raise InteractionError(
                "invalid_backend_data",
                "playerctl returned a player name that cannot be selected unambiguously.",
            )
        return names

    def _resolve_player(self, player: str) -> str:
        if not player or player != player.strip() or "," in player or "\0" in player:
            raise InteractionError(
                "invalid_player_selector",
                "Pass one exact player name returned by media list.",
                details={"player": player},
            )
        players = self._available_players()
        if player not in players:
            raise InteractionError(
                "player_not_found",
                "Select one exact player name returned by media list.",
                details={"player": player, "available_players": players},
            )
        related = [name for name in players if name == player or name.startswith(f"{player}.")]
        if len(related) > 1:
            raise InteractionError(
                "ambiguous_player",
                "This name would control multiple MPRIS instances; select one exact instance.",
                details={"player": player, "candidates": related},
            )
        return player

    def _run_player(self, player: str, *args: str) -> CommandResult:
        return run_command(
            [self.playerctl, f"--player={player}", *args],
            env=self.environ,
            timeout=3.0,
        )

    def _optional_metadata(self, player: str, key: str) -> str | None:
        return self._optional_player_query(player, "metadata", key)

    def _optional_player_query(self, player: str, *args: str) -> str | None:
        result = self._run_player(player, *args)
        if result.returncode != 0:
            return None
        value = result.stdout.rstrip("\r\n")
        return value or None


def _parse_optional_float(value: str | None, field: str) -> float | None:
    if value is None:
        return None
    try:
        result = float(value)
    except ValueError as exc:
        raise InteractionError(
            "invalid_backend_data",
            f"playerctl returned an invalid {field} value.",
            details={"field": field, "value": value},
        ) from exc
    if not isfinite(result) or result < 0:
        raise InteractionError(
            "invalid_backend_data",
            f"playerctl returned an invalid {field} value.",
            details={"field": field, "value": value},
        )
    return result


def _parse_duration(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        microseconds = int(value)
    except ValueError as exc:
        raise InteractionError(
            "invalid_backend_data",
            "playerctl returned an invalid MPRIS duration.",
            details={"field": "mpris:length", "value": value},
        ) from exc
    if microseconds < 0:
        raise InteractionError(
            "invalid_backend_data",
            "playerctl returned an invalid MPRIS duration.",
            details={"field": "mpris:length", "value": value},
        )
    try:
        duration = microseconds / 1_000_000
    except OverflowError as exc:
        raise InteractionError(
            "invalid_backend_data",
            "playerctl returned an invalid MPRIS duration.",
            details={"field": "mpris:length", "value": value},
        ) from exc
    if not isfinite(duration):
        raise InteractionError(
            "invalid_backend_data",
            "playerctl returned an invalid MPRIS duration.",
            details={"field": "mpris:length", "value": value},
        )
    return duration

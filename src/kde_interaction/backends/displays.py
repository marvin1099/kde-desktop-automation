from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from typing import Any

from kde_interaction.errors import InteractionError
from kde_interaction.process import require_executable, require_success, run_command


@dataclass(frozen=True, slots=True)
class DisplayInfo:
    name: str
    x: int
    y: int
    width: int
    height: int
    scale: float
    transform: str
    refresh_hz: float | None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self) | {"coordinate_space": "logical-desktop-pixels"}


def parse_display_list(payload: str) -> list[DisplayInfo]:
    try:
        records = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise InteractionError(
            "invalid_backend_data",
            "wlpdisplays returned invalid JSON.",
        ) from exc
    if not isinstance(records, list):
        raise InteractionError("invalid_backend_data", "wlpdisplays JSON must be a list.")

    displays: list[DisplayInfo] = []
    for index, record in enumerate(records):
        if not isinstance(record, dict):
            raise InteractionError(
                "invalid_backend_data",
                "A wlpdisplays entry was not an object.",
                details={"index": index},
            )
        displays.append(_parse_display(record, index=index))
    return displays


def _parse_display(record: Mapping[str, Any], *, index: int) -> DisplayInfo:
    try:
        name = str(record["name"])
        x = _integer(record.get("logical_x", record.get("x", 0)))
        y = _integer(record.get("logical_y", record.get("y", 0)))
        width = _integer(record.get("logical_width", record.get("width_px")))
        height = _integer(record.get("logical_height", record.get("height_px")))
        scale = float(record.get("scale", 1.0))
        transform = str(record.get("output_transform", "normal"))
        raw_refresh = record.get("refresh_hz")
        refresh = None if raw_refresh is None else float(raw_refresh)
    except (KeyError, TypeError, ValueError) as exc:
        raise InteractionError(
            "invalid_backend_data",
            "A wlpdisplays entry is missing valid logical geometry.",
            details={"index": index},
        ) from exc

    if not name or width <= 0 or height <= 0 or scale <= 0:
        raise InteractionError(
            "invalid_backend_data",
            "A wlpdisplays entry has invalid dimensions or scale.",
            details={"index": index, "name": name},
        )
    return DisplayInfo(
        name=name,
        x=x,
        y=y,
        width=width,
        height=height,
        scale=scale,
        transform=transform,
        refresh_hz=refresh,
    )


def _integer(value: Any) -> int:
    if isinstance(value, bool):
        raise TypeError("boolean is not a coordinate")
    return int(value)


def list_displays(*, env: Mapping[str, str]) -> list[DisplayInfo]:
    executable = require_executable("wlpdisplays")
    result = require_success(run_command([executable, "-c"], env=env, timeout=5.0))
    return parse_display_list(result.stdout)

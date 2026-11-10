from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from kde_interaction.errors import InteractionError


def find_one_window(
    windows: Sequence[dict[str, Any]],
    *,
    app_id: str | None,
    title: str | None,
) -> dict[str, Any]:
    if not app_id and not title:
        raise InteractionError(
            "window_selector_required",
            "Provide an exact --app-id or a --title substring; selectors are never guessed.",
        )
    matches = [
        window
        for window in windows
        if (app_id is None or window["app_id"] == app_id)
        and (title is None or title.casefold() in window["title"].casefold())
    ]
    if not matches:
        raise InteractionError(
            "window_not_found",
            "No window matched the requested selector in the selected session.",
            details={"app_id": app_id, "title": title},
        )
    if len(matches) != 1:
        raise InteractionError(
            "ambiguous_target",
            "More than one window matched; inspect candidates and choose an exact window ID.",
            details={"matches": matches},
        )
    return matches[0]

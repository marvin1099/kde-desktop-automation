from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import pytest

from kde_interaction.errors import InteractionError
from kde_interaction.operations import DesktopOperations

SESSION_ID = "agent-123456789abc"
WINDOW_ID = "b5ff2329-3976-4c31-bf31-1538cb97c8f0"
OBSERVED_AT = "2026-11-10T12:00:00+00:00"


class FakeManager:
    def __init__(
        self,
        observations: Sequence[dict[str, Any] | InteractionError],
        displays: list[dict[str, Any]] | None = None,
    ) -> None:
        self.observations = list(observations)
        self.display_records = displays or []
        self.calls = 0

    def find_window(
        self,
        _session_id: str,
        *,
        app_id: str | None,
        title: str | None,
    ) -> dict[str, Any]:
        assert app_id == "org.example.Game"
        assert title is None
        self.calls += 1
        observation = (
            self.observations.pop(0) if len(self.observations) > 1 else self.observations[0]
        )
        if isinstance(observation, InteractionError):
            raise observation
        return observation

    def displays(self, _session_id: str) -> list[dict[str, Any]]:
        return self.display_records


def _window(fullscreen: bool | None, geometry: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "target": {"kind": "agent-session", "session_id": SESSION_ID},
        "observed_at": OBSERVED_AT,
        "window": {
            "window_id": WINDOW_ID,
            "app_id": "org.example.Game",
            "title": "Game",
            "fullscreen": fullscreen,
            "geometry": geometry,
        },
    }


def _display_manager(
    observations: Sequence[dict[str, Any] | InteractionError],
) -> FakeManager:
    return FakeManager(
        observations,
        displays=[
            {
                "name": "DP-1",
                "x": 0,
                "y": 0,
                "width": 1920,
                "height": 1080,
                "coordinate_space": "logical-desktop-pixels",
            }
        ],
    )


def _operations(manager: FakeManager) -> DesktopOperations:
    return DesktopOperations(SESSION_ID, manager=manager)  # type: ignore[arg-type]


def test_window_wait_keeps_polling_until_unique_window_is_kde_fullscreen(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = FakeManager([_window(False), _window(True)])
    monkeypatch.setattr("kde_interaction.operations.time.sleep", lambda _seconds: None)

    result = _operations(manager).window_wait(
        app_id="org.example.Game",
        title=None,
        timeout=1,
        fullscreen=True,
    )

    assert manager.calls == 2
    assert result["window"]["fullscreen"] is True
    assert result["wait_condition"] == {"fullscreen": True}
    assert result["waited_seconds"] >= 0


def test_window_wait_tolerates_window_not_yet_appearing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = FakeManager(
        [
            InteractionError("window_not_found", "No matching window."),
            _window(True),
        ]
    )
    monkeypatch.setattr("kde_interaction.operations.time.sleep", lambda _seconds: None)

    result = _operations(manager).window_wait(
        app_id="org.example.Game",
        title=None,
        timeout=1,
        fullscreen=True,
    )

    assert manager.calls == 2
    assert result["window"]["fullscreen"] is True


def test_window_wait_reports_last_non_fullscreen_observation_on_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = FakeManager([_window(False)])
    ticks = iter((0.0, 0.0, 0.11))
    monkeypatch.setattr("kde_interaction.operations.time.monotonic", lambda: next(ticks))
    monkeypatch.setattr("kde_interaction.operations.time.sleep", lambda _seconds: None)

    with pytest.raises(InteractionError) as error:
        _operations(manager).window_wait(
            app_id="org.example.Game",
            title=None,
            timeout=0.1,
            fullscreen=True,
        )

    assert error.value.code == "window_wait_timeout"
    assert error.value.details["wait_condition"] == {"fullscreen": True}
    assert error.value.details["last_observation"]["observed_at"] == OBSERVED_AT
    assert error.value.details["last_observation"]["window"]["fullscreen"] is False


def test_unknown_fullscreen_state_does_not_satisfy_wait(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = FakeManager([_window(None)])
    ticks = iter((0.0, 0.0, 0.11))
    monkeypatch.setattr("kde_interaction.operations.time.monotonic", lambda: next(ticks))
    monkeypatch.setattr("kde_interaction.operations.time.sleep", lambda _seconds: None)

    with pytest.raises(InteractionError) as error:
        _operations(manager).window_wait(
            app_id="org.example.Game",
            title=None,
            timeout=0.1,
            fullscreen=True,
        )

    assert error.value.code == "window_wait_timeout"
    assert error.value.details["last_observation"]["window"]["fullscreen"] is None


def test_window_wait_timeout_without_a_matching_window_has_null_last_observation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = FakeManager([InteractionError("window_not_found", "No matching window.")])
    ticks = iter((0.0, 0.0, 0.11))
    monkeypatch.setattr("kde_interaction.operations.time.monotonic", lambda: next(ticks))
    monkeypatch.setattr("kde_interaction.operations.time.sleep", lambda _seconds: None)

    with pytest.raises(InteractionError) as error:
        _operations(manager).window_wait(
            app_id="org.example.Game",
            title=None,
            timeout=0.1,
            fullscreen=True,
        )

    assert error.value.code == "window_wait_timeout"
    assert error.value.details["last_observation"] is None


def test_window_wait_can_detect_a_borderless_window_covering_a_display(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    geometry = {
        "x": 1,
        "y": 0,
        "width": 1919,
        "height": 1080,
        "coordinate_space": "logical-desktop-pixels",
    }
    manager = _display_manager([_window(False, geometry)])

    result = _operations(manager).window_wait(
        app_id="org.example.Game",
        title=None,
        timeout=1,
        fills_display=True,
    )

    assert result["wait_condition"] == {"fills_display": True}
    assert result["matched_displays"] == ["DP-1"]


def test_fills_display_does_not_match_a_window_within_only_one_dimension(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    geometry = {
        "x": 0,
        "y": 0,
        "width": 1910,
        "height": 1080,
        "coordinate_space": "logical-desktop-pixels",
    }
    manager = _display_manager([_window(False, geometry)])
    ticks = iter((0.0, 0.0, 0.11))
    monkeypatch.setattr("kde_interaction.operations.time.monotonic", lambda: next(ticks))
    monkeypatch.setattr("kde_interaction.operations.time.sleep", lambda _seconds: None)

    with pytest.raises(InteractionError) as error:
        _operations(manager).window_wait(
            app_id="org.example.Game",
            title=None,
            timeout=0.1,
            fills_display=True,
        )

    assert error.value.code == "window_wait_timeout"
    assert error.value.details["last_observation"]["matched_displays"] == []


@pytest.mark.parametrize(
    "error",
    [
        InteractionError("ambiguous_target", "More than one window matched."),
        InteractionError("capability_unavailable", "Window listing is unavailable."),
    ],
)
def test_window_wait_propagates_ambiguity_and_backend_errors(
    error: InteractionError,
) -> None:
    manager = FakeManager([error])

    with pytest.raises(InteractionError) as caught:
        _operations(manager).window_wait(
            app_id="org.example.Game",
            title=None,
            timeout=1,
            fullscreen=True,
        )

    assert caught.value is error


def test_window_wait_rejects_conflicting_presentation_predicates() -> None:
    manager = _display_manager([_window(True)])

    with pytest.raises(InteractionError, match="Choose either"):
        _operations(manager).window_wait(
            app_id="org.example.Game",
            title=None,
            timeout=1,
            fullscreen=True,
            fills_display=True,
        )

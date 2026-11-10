from __future__ import annotations

from typing import Any

import pytest

from kde_desktop_automation.backends import kwin_virtual
from kde_desktop_automation.backends.kwin_input import InputReceipt
from kde_desktop_automation.errors import InteractionError
from kde_desktop_automation.models import ProcessIdentity, SessionRecord

WINDOW_ID = "b5ff2329-3976-4c31-bf31-1538cb97c8f0"


def _session() -> SessionRecord:
    process = ProcessIdentity(1, 1, "/fake/kwin", "marker", ("/fake/kwin",))
    return SessionRecord(
        session_id="agent-0123456789ab",
        name="test",
        backend="kwin-virtual",
        created_at="2026-11-09T00:00:00+00:00",
        stopped_at=None,
        width=800,
        height=600,
        runtime_dir="/fake/runtime",
        home_dir="/fake/home",
        wayland_display="kde-desktop-test",
        dbus_address="unix:path=/fake/bus",
        kwin_process=process,
        dbus_process=process,
    )


def _backend(
    monkeypatch: pytest.MonkeyPatch,
    window: dict[str, Any],
) -> tuple[kwin_virtual.KWinVirtualBackend, list[tuple[int, int]]]:
    backend = object.__new__(kwin_virtual.KWinVirtualBackend)
    monkeypatch.setattr(backend, "_require_running", lambda _session: None)
    monkeypatch.setattr(backend, "inspect_window", lambda _session, _window_id: window)
    monkeypatch.setattr(backend, "list_windows", lambda _session: [window])
    monkeypatch.setattr(backend, "_active_window_id", lambda _session: WINDOW_ID)
    clicks: list[tuple[int, int]] = []

    class FakeInput:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        def __enter__(self) -> FakeInput:
            return self

        def __exit__(self, *_exc: object) -> None:
            return

        def click(self, x: int, y: int, _button: str = "left") -> InputReceipt:
            clicks.append((x, y))
            return InputReceipt("click left", 3, True)

    monkeypatch.setattr(kwin_virtual, "KWinFakeInput", FakeInput)
    return backend, clicks


@pytest.mark.parametrize(
    ("frame", "content", "basis", "expected"),
    [
        (
            {"x": 0, "y": 0, "width": 300, "height": 200},
            {"x": 20, "y": 30, "width": 200, "height": 100},
            "content",
            (120, 80),
        ),
        (
            {"x": 10, "y": 20, "width": 100, "height": 80},
            None,
            "frame",
            (60, 60),
        ),
    ],
)
def test_private_click_uses_available_content_then_frame_geometry(
    monkeypatch: pytest.MonkeyPatch,
    frame: dict[str, int],
    content: dict[str, int] | None,
    basis: str,
    expected: tuple[int, int],
) -> None:
    geometry = content or frame
    window = {
        "window_id": WINDOW_ID,
        "active": True,
        "geometry": geometry,
        "frame_geometry": frame,
        "content_geometry": content,
        "geometry_basis": basis,
    }
    backend, clicks = _backend(monkeypatch, window)

    result = backend.click_window(_session(), WINDOW_ID, 0.5, 0.5)

    assert clicks == [expected]
    assert result["resolved_point"]["geometry_basis"] == basis


def test_private_click_rejects_missing_geometry_without_submitting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    window = {
        "window_id": WINDOW_ID,
        "active": True,
        "geometry": None,
        "geometry_basis": None,
    }
    backend, clicks = _backend(monkeypatch, window)

    with pytest.raises(InteractionError, match="has not provided geometry"):
        backend.click_window(_session(), WINDOW_ID, 0.5, 0.5)

    assert clicks == []


def test_private_click_rejects_resolved_point_outside_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    geometry = {"x": 790, "y": 0, "width": 30, "height": 20}
    window = {
        "window_id": WINDOW_ID,
        "active": True,
        "geometry": geometry,
        "geometry_basis": "content",
    }
    backend, clicks = _backend(monkeypatch, window)

    with pytest.raises(InteractionError, match="outside the private virtual output"):
        backend.click_window(_session(), WINDOW_ID, 1.0, 0.5)

    assert clicks == []

from __future__ import annotations

import pytest

from kde_interaction.backends.kwin_window_management import _Window, normalize_window_id
from kde_interaction.errors import InteractionError

WINDOW_ID = "b5ff2329-3976-4c31-bf31-1538cb97c8f0"


def test_private_window_snapshot_uses_shared_window_record_shape() -> None:
    window = _Window(
        window_id=WINDOW_ID,
        proxy=1,
        title="Kate — notes.txt",
        app_id="org.kde.kate",
        state=0x1 | 0x2 | 0x4 | 0x8,
        frame_geometry=(0, 0, 320, 240),
        client_geometry=(10, 20, 300, 200),
        pid=1234,
        resource_name="kate",
        desktops={"desktop-1"},
        has_transient_parent=True,
        initialized=True,
    )

    record = window.to_dict()

    assert record == {
        "window_id": WINDOW_ID,
        "title": "Kate — notes.txt",
        "app_id": "org.kde.kate",
        "resource_name": "kate",
        "role": "",
        "pid": 1234,
        "fullscreen": True,
        "minimized": True,
        "maximized": True,
        "active": True,
        "has_transient_parent": True,
        "desktops": ["desktop-1"],
        "geometry": {
            "x": 10,
            "y": 20,
            "width": 300,
            "height": 200,
            "coordinate_space": "logical-desktop-pixels",
        },
        "frame_geometry": {
            "x": 0,
            "y": 0,
            "width": 320,
            "height": 240,
            "coordinate_space": "logical-desktop-pixels",
        },
        "content_geometry": {
            "x": 10,
            "y": 20,
            "width": 300,
            "height": 200,
            "coordinate_space": "logical-desktop-pixels",
        },
        "geometry_basis": "content",
    }


def test_private_window_snapshot_reports_unavailable_geometry() -> None:
    record = _Window(window_id=WINDOW_ID, proxy=1).to_dict()

    assert record["geometry"] is None
    assert record["frame_geometry"] is None
    assert record["content_geometry"] is None
    assert record["geometry_basis"] is None


def test_window_uuid_normalization_is_strict_and_target_scoped() -> None:
    assert normalize_window_id("{" + WINDOW_ID.upper() + "}") == WINDOW_ID
    with pytest.raises(InteractionError, match="UUIDs"):
        normalize_window_id("not-a-window-id")

from __future__ import annotations

import ctypes
import gc
from pathlib import Path
from typing import Any

import pytest

from kde_desktop_automation.backends import kwin_window_management
from kde_desktop_automation.backends.kwin_window_management import _Window, normalize_window_id
from kde_desktop_automation.errors import InteractionError

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
        "minimizable": False,
        "maximizable": False,
        "fullscreenable": False,
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


def test_wayland_listener_function_pointer_array_is_kept_alive() -> None:
    class FakeLibrary:
        listener_address: int | None = None

        def wl_proxy_add_listener(self, _proxy, listener, _data) -> int:
            self.listener_address = ctypes.cast(listener, ctypes.c_void_p).value
            return 0

    class FakeConnection:
        lib = FakeLibrary()

    callback_type = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_void_p)
    callback = callback_type(lambda _data, _proxy: None)

    callback_refs: list[Any] = [callback]
    kwin_window_management._add_listener(FakeConnection(), 1, callback_refs)
    gc.collect()

    assert FakeConnection.lib.listener_address is not None
    assert len(callback_refs) == 2
    listener_array = callback_refs[-1]
    assert isinstance(listener_array, ctypes.Array)
    assert listener_array._type_ is ctypes.c_void_p
    assert listener_array[0] == ctypes.cast(callback, ctypes.c_void_p).value


@pytest.mark.parametrize(("initial", "requested"), [(False, True), (True, False)])
def test_private_minimize_operation_reports_the_read_back_state(
    monkeypatch: pytest.MonkeyPatch,
    initial: bool,
    requested: bool,
) -> None:
    class FakeClient:
        def __init__(self, _socket_path: Path) -> None:
            self.minimized = initial

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            pass

        def windows(self):
            return [{"window_id": WINDOW_ID, "minimized": self.minimized}]

        def set_minimized(self, window_id: str, minimized: bool) -> None:
            assert window_id == WINDOW_ID
            self.minimized = minimized

    monkeypatch.setattr(kwin_window_management, "KWinWindowManagementClient", FakeClient)

    result = kwin_window_management.set_minimized_window(Path("/fake/socket"), WINDOW_ID, requested)

    assert result == {
        "window": {"window_id": WINDOW_ID, "minimized": requested},
        "minimized_verified": True,
    }

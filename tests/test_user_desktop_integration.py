from __future__ import annotations

import os

import pytest

from kde_desktop_automation.backends.user_desktop import UserDesktopBackend

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("KDE_DESKTOP_RUN_USER_DESKTOP_INTEGRATION") != "1",
        reason="set KDE_DESKTOP_RUN_USER_DESKTOP_INTEGRATION=1 to inspect the live user desktop",
    ),
]


def test_live_user_desktop_window_discovery_is_read_only() -> None:
    backend = UserDesktopBackend()

    listing = backend.list_windows()

    assert listing["target"] == {"kind": "user-desktop"}
    assert listing["count"] == len(listing["windows"])
    active_windows = [window for window in listing["windows"] if window["active"]]
    assert len(active_windows) <= 1
    if listing["windows"]:
        first = listing["windows"][0]
        inspected = backend.inspect_window(first["window_id"])
        assert inspected["window"]["window_id"] == first["window_id"]

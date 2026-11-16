from __future__ import annotations

from pathlib import Path

from kde_desktop_automation.backends.screenshot_helper import screenshot_helper_status


def test_screenshot_helper_is_ready_only_with_matching_registered_interface(tmp_path: Path) -> None:
    executable = tmp_path / "bin" / "kde-desktop-screenshot"
    executable.parent.mkdir()
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o700)
    data_home = tmp_path / "data"
    applications = data_home / "applications"
    applications.mkdir(parents=True)
    desktop_entry = applications / "kde-desktop-screenshot.desktop"
    desktop_entry.write_text(
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Name=KDE Desktop Automation Screenshot Helper\n"
        f'Exec="{executable}"\n'
        "NoDisplay=true\n"
        "X-KDE-DBUS-Restricted-Interfaces=org.kde.KWin.ScreenShot2;\n",
        encoding="utf-8",
    )

    status = screenshot_helper_status(
        {
            "PATH": str(executable.parent),
            "XDG_DATA_HOME": str(data_home),
            "XDG_DATA_DIRS": "",
        }
    )

    assert status == {
        "available": True,
        "helper": str(executable),
        "registered": True,
        "desktop_entry": str(desktop_entry),
        "ready": True,
        "reason": None,
    }


def test_screenshot_helper_is_not_ready_when_registration_is_missing(tmp_path: Path) -> None:
    executable = tmp_path / "bin" / "kde-desktop-screenshot"
    executable.parent.mkdir()
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o700)

    status = screenshot_helper_status(
        {
            "PATH": str(executable.parent),
            "XDG_DATA_HOME": str(tmp_path / "data"),
            "XDG_DATA_DIRS": "",
        }
    )

    assert status == {
        "available": True,
        "helper": str(executable),
        "registered": False,
        "desktop_entry": None,
        "ready": False,
        "reason": "desktop_entry_missing_or_untrusted",
    }

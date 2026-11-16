from __future__ import annotations

import configparser
import os
import shlex
import shutil
from collections.abc import Mapping
from pathlib import Path

_SCREENSHOT_HELPER = "kde-desktop-screenshot"


def screenshot_helper_path(env: Mapping[str, str] | None = None) -> str | None:
    environment = os.environ if env is None else env
    executable = environment.get("KDE_DESKTOP_SCREENSHOT_HELPER", _SCREENSHOT_HELPER)
    return shutil.which(executable, path=environment.get("PATH"))


def screenshot_helper_available(env: Mapping[str, str] | None = None) -> bool:
    return screenshot_helper_path(env) is not None


def copy_private_screenshot_registration(
    private_data_home: Path,
    *,
    env: Mapping[str, str],
) -> Path | None:
    """Mirror only the installed helper's explicit KWin authorization into a private HOME."""
    helper = screenshot_helper_path(env)
    if helper is None:
        return None
    helper_path = Path(helper).resolve()
    data_home = Path(env.get("XDG_DATA_HOME", Path.home() / ".local/share")).expanduser()
    data_dirs = env.get("XDG_DATA_DIRS", "/usr/local/share:/usr/share").split(":")
    source_dirs = (
        data_home,
        *(Path(directory).expanduser() for directory in data_dirs if directory),
    )

    for data_dir in source_dirs:
        source = data_dir / "applications" / "kde-desktop-screenshot.desktop"
        try:
            contents = source.read_text(encoding="utf-8")
        except OSError:
            continue
        if not _registration_authorizes(contents, helper_path):
            continue

        destination = private_data_home / "applications" / source.name
        try:
            destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            destination.write_text(contents, encoding="utf-8")
            destination.chmod(0o600)
        except OSError:
            return None
        return destination
    return None


def _registration_authorizes(contents: str, helper_path: Path) -> bool:
    parser = configparser.ConfigParser(interpolation=None)
    try:
        parser.read_string(contents)
        entry = parser["Desktop Entry"]
        command = shlex.split(entry.get("Exec", ""))
        interfaces = {
            interface.strip()
            for interface in entry.get("X-KDE-DBUS-Restricted-Interfaces", "").split(";")
            if interface.strip()
        }
        executable = Path(command[0]).expanduser().resolve() if command else None
    except (KeyError, configparser.Error, OSError, ValueError):
        return False
    return executable == helper_path and "org.kde.KWin.ScreenShot2" in interfaces

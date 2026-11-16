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


def screenshot_helper_status(env: Mapping[str, str] | None = None) -> dict[str, object]:
    environment = os.environ if env is None else env
    helper = screenshot_helper_path(environment)
    if helper is None:
        return {
            "available": False,
            "helper": None,
            "registered": False,
            "desktop_entry": None,
            "ready": False,
            "reason": "helper_not_found",
        }

    helper_path = Path(helper).resolve()
    registration = _find_screenshot_registration(helper_path, environment)
    return {
        "available": True,
        "helper": str(helper_path),
        "registered": registration is not None,
        "desktop_entry": str(registration) if registration is not None else None,
        "ready": registration is not None,
        "reason": None if registration is not None else "desktop_entry_missing_or_untrusted",
    }


def screenshot_helper_ready(env: Mapping[str, str] | None = None) -> bool:
    return screenshot_helper_status(env)["ready"] is True


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
    source = _find_screenshot_registration(helper_path, env)
    if source is None:
        return None

    destination = private_data_home / "applications" / source.name
    try:
        destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        destination.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
        destination.chmod(0o600)
    except OSError:
        return None
    return destination


def _find_screenshot_registration(helper_path: Path, env: Mapping[str, str]) -> Path | None:
    home = Path(env.get("HOME", str(Path.home()))).expanduser()
    data_home_value = env.get("XDG_DATA_HOME")
    data_home = Path(data_home_value).expanduser() if data_home_value else home / ".local/share"
    data_dirs_value = env.get("XDG_DATA_DIRS") or "/usr/local/share:/usr/share"
    data_dirs = data_dirs_value.split(":")
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
        return source
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

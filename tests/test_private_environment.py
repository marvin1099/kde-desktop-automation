from kde_desktop_automation.backends.kwin_virtual import build_private_environment
from kde_desktop_automation.paths import SessionPaths


def test_private_environment_does_not_inherit_host_desktop_or_secrets(tmp_path) -> None:
    paths = SessionPaths(
        session_id="agent-0123456789ab",
        runtime_dir=tmp_path / "runtime",
        home_dir=tmp_path / "home",
        record_file=tmp_path / "session.json",
        log_file=tmp_path / "kwin.log",
    )
    host_env = {
        "PATH": "/usr/bin:/bin",
        "HOME": "/home/user",
        "WAYLAND_DISPLAY": "wayland-1",
        "DISPLAY": ":0",
        "DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/user/1000/bus",
        "AWS_SECRET_ACCESS_KEY": "must-not-leak",
        "MESA_LOADER_DRIVER_OVERRIDE": "iris",
        "LANG": "en_US.UTF-8",
    }

    env = build_private_environment(
        paths=paths,
        wayland_display="kde-desktop-private",
        dbus_address="unix:path=/private/bus",
        environ=host_env,
    )

    assert env["WAYLAND_DISPLAY"] == "kde-desktop-private"
    assert env["DBUS_SESSION_BUS_ADDRESS"] == "unix:path=/private/bus"
    assert env["QT_QPA_PLATFORM"] == "wayland"
    assert env["XDG_RUNTIME_DIR"] == str(paths.runtime_dir)
    assert env["HOME"] == str(paths.home_dir)
    assert "DISPLAY" not in env
    assert "AWS_SECRET_ACCESS_KEY" not in env
    assert env["MESA_LOADER_DRIVER_OVERRIDE"] == "iris"
    assert env["LANG"] == "en_US.UTF-8"

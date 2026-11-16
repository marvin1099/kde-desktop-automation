import json
import os
import struct
import zlib
from pathlib import Path

import pytest

from kde_desktop_automation.backends import screenshots
from kde_desktop_automation.backends.screenshots import _safe_output_parent
from kde_desktop_automation.errors import InteractionError
from kde_desktop_automation.process import CommandResult


def _png_chunk(chunk_type: bytes, data: bytes) -> bytes:
    checksum = zlib.crc32(chunk_type + data) & 0xFFFFFFFF
    return struct.pack(">I", len(data)) + chunk_type + data + struct.pack(">I", checksum)


def _png_image(width: int, height: int, compressed_data: bytes | None = None) -> bytes:
    header = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    row = b"\0" + b"\0\0\0\xff" * width
    image_data = compressed_data if compressed_data is not None else zlib.compress(row * height)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", header)
        + _png_chunk(b"IDAT", image_data)
        + _png_chunk(b"IEND", b"")
    )


@pytest.fixture(autouse=True)
def screenshot_helper_is_ready(monkeypatch) -> None:
    monkeypatch.setattr(
        screenshots,
        "screenshot_helper_status",
        lambda _env=None: {
            "available": True,
            "helper": "/usr/bin/kde-desktop-screenshot",
            "registered": True,
            "desktop_entry": "/home/test/.local/share/applications/kde-desktop-screenshot.desktop",
            "ready": True,
            "reason": None,
        },
    )


def test_capture_window_uses_kwin_screenshot2_for_exact_uuid(tmp_path, monkeypatch) -> None:
    output = tmp_path / "window.png"
    window_id = "e1a03123-8d0c-488d-9d55-1be270c44d50"
    session_env = {
        "WAYLAND_DISPLAY": "agent-wayland",
        "DBUS_SESSION_BUS_ADDRESS": "private-bus",
    }

    def fake_run(argv, *, env, timeout):
        assert argv[1:4] == ["capture-window", "--window-id", window_id]
        assert argv[4] == "--output"
        assert env == session_env
        assert timeout == 30.0
        Path(argv[-1]).write_bytes(_png_image(520, 360))
        metadata = json.dumps({"width": 520, "height": 360, "scale": 1.25, "window_id": window_id})
        return CommandResult(tuple(argv), 0, metadata, "")

    monkeypatch.setattr(screenshots, "run_command", fake_run)
    result = screenshots.capture_window(output, window_id=window_id, env=session_env)

    assert result["source"] == "kwin-screenshot2"
    assert result["api"] == "org.kde.KWin.ScreenShot2"
    assert result["capture_mode"] == "window"
    assert result["window_id"] == window_id
    assert result["scale"] == 1.25
    assert result["width"] == 520
    assert result["height"] == 360
    assert os.stat(output).st_mode & 0o777 == 0o600


def test_capture_fails_with_actionable_error_without_authorized_registration(
    tmp_path,
    monkeypatch,
) -> None:
    output = tmp_path / "not-authorized.png"
    monkeypatch.setattr(
        screenshots,
        "screenshot_helper_status",
        lambda _env=None: {
            "available": True,
            "helper": "/usr/bin/kde-desktop-screenshot",
            "registered": False,
            "desktop_entry": None,
            "ready": False,
            "reason": "desktop_entry_missing_or_untrusted",
        },
    )

    with pytest.raises(InteractionError) as error:
        screenshots.capture_fullscreen(output, env={})

    assert error.value.code == "screenshot_helper_not_authorized"
    assert not output.exists()


def test_capture_fullscreen_uses_kwin_screenshot2_in_requested_environment(
    tmp_path, monkeypatch
) -> None:
    output = tmp_path / "private.png"
    session_env = {"WAYLAND_DISPLAY": "agent-wayland"}

    def fake_run(argv, *, env, timeout):
        assert argv[1] == "capture-workspace"
        assert env == session_env
        assert timeout == 30.0
        Path(argv[-1]).write_bytes(_png_image(1280, 800))
        return CommandResult(tuple(argv), 0, '{"width": 1280, "height": 800, "scale": 1.0}', "")

    monkeypatch.setattr(screenshots, "run_command", fake_run)
    result = screenshots.capture_fullscreen(output, env=session_env)

    assert result["width"] == 1280
    assert result["height"] == 800
    assert os.stat(output).st_mode & 0o777 == 0o600


def test_capture_refuses_to_overwrite_existing_file(tmp_path, monkeypatch) -> None:
    output = tmp_path / "existing.png"
    output.write_bytes(b"keep")
    with pytest.raises(InteractionError, match="Refusing to overwrite"):
        screenshots.capture_fullscreen(output, env={})
    assert output.read_bytes() == b"keep"


def test_capture_removes_invalid_temporary_output(tmp_path, monkeypatch) -> None:
    output = tmp_path / "bad.png"

    def fake_run(argv, *, env, timeout):
        Path(argv[-1]).write_bytes(b"not a png")
        return CommandResult(tuple(argv), 0, "", "")

    monkeypatch.setattr(screenshots, "run_command", fake_run)

    with pytest.raises(InteractionError, match="not a complete, valid PNG"):
        screenshots.capture_fullscreen(output, env={})
    assert not output.exists()


def test_failed_overwrite_preserves_existing_screenshot(tmp_path, monkeypatch) -> None:
    output = tmp_path / "existing.png"
    output.write_bytes(b"keep the previous capture")

    def fake_run(argv, *, env, timeout):
        Path(argv[-1]).write_bytes(_png_image(1024, 768))
        return CommandResult(tuple(argv), 0, '{"width": 1024, "height": 768, "scale": 1.0}', "")

    monkeypatch.setattr(screenshots, "run_command", fake_run)

    with pytest.raises(InteractionError, match="did not match"):
        screenshots.capture_fullscreen(
            output,
            env={},
            overwrite=True,
            expected_size=(800, 600),
        )

    assert output.read_bytes() == b"keep the previous capture"


def test_capture_rejects_unsafe_shared_output_directory(tmp_path, monkeypatch) -> None:
    shared = tmp_path / "shared"
    shared.mkdir()
    shared.chmod(0o777)

    def unexpected_run(*args, **kwargs):
        raise AssertionError("must not invoke KWin Screenshot2")

    monkeypatch.setattr(screenshots, "run_command", unexpected_run)

    with pytest.raises(InteractionError, match="Shared screenshot output directories"):
        screenshots.capture_fullscreen(shared / "capture.png", env={})


def test_shared_output_parent_requires_trusted_sticky_directory() -> None:
    assert _safe_output_parent(0o1777, 0)
    assert not _safe_output_parent(0o1777, os.getuid() + 1)
    assert not _safe_output_parent(0o777, os.getuid())


def test_capture_rejects_truncated_png_before_install(tmp_path, monkeypatch) -> None:
    output = tmp_path / "truncated.png"

    def fake_run(argv, *, env, timeout):
        Path(argv[-1]).write_bytes(_png_image(10, 10)[:24])
        return CommandResult(tuple(argv), 0, "", "")

    monkeypatch.setattr(screenshots, "run_command", fake_run)

    with pytest.raises(InteractionError, match="not a complete, valid PNG"):
        screenshots.capture_fullscreen(output, env={})
    assert not output.exists()


def test_capture_rejects_invalid_compressed_image_data(tmp_path, monkeypatch) -> None:
    output = tmp_path / "invalid-idat.png"

    def fake_run(argv, *, env, timeout):
        Path(argv[-1]).write_bytes(_png_image(10, 10, compressed_data=b"not-zlib"))
        return CommandResult(tuple(argv), 0, "", "")

    monkeypatch.setattr(screenshots, "run_command", fake_run)

    with pytest.raises(InteractionError, match="compressed image data is invalid"):
        screenshots.capture_fullscreen(output, env={})
    assert not output.exists()

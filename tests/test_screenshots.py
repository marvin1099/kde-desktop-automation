import os
import struct
import zlib
from pathlib import Path

import pytest

from kde_interaction.backends import screenshots
from kde_interaction.backends.screenshots import _safe_output_parent
from kde_interaction.errors import InteractionError
from kde_interaction.process import CommandResult


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


def test_capture_fullscreen_uses_spectacle_in_requested_environment(tmp_path, monkeypatch) -> None:
    output = tmp_path / "private.png"
    session_env = {"WAYLAND_DISPLAY": "agent-wayland"}
    monkeypatch.setattr(screenshots.shutil, "which", lambda _: "/usr/bin/spectacle")

    def fake_run(argv, *, env, timeout):
        assert env == session_env
        assert timeout == 20.0
        Path(argv[-1]).write_bytes(_png_image(1280, 800))
        return CommandResult(tuple(argv), 0, "", "")

    monkeypatch.setattr(screenshots, "run_command", fake_run)
    result = screenshots.capture_fullscreen(output, env=session_env)

    assert result["width"] == 1280
    assert result["height"] == 800
    assert os.stat(output).st_mode & 0o777 == 0o600


def test_capture_refuses_to_overwrite_existing_file(tmp_path, monkeypatch) -> None:
    output = tmp_path / "existing.png"
    output.write_bytes(b"keep")
    monkeypatch.setattr(screenshots.shutil, "which", lambda _: "/usr/bin/spectacle")

    with pytest.raises(InteractionError, match="Refusing to overwrite"):
        screenshots.capture_fullscreen(output, env={})
    assert output.read_bytes() == b"keep"


def test_capture_removes_invalid_temporary_output(tmp_path, monkeypatch) -> None:
    output = tmp_path / "bad.png"
    monkeypatch.setattr(screenshots.shutil, "which", lambda _: "/usr/bin/spectacle")

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
    monkeypatch.setattr(screenshots.shutil, "which", lambda _: "/usr/bin/spectacle")

    def fake_run(argv, *, env, timeout):
        Path(argv[-1]).write_bytes(_png_image(1024, 768))
        return CommandResult(tuple(argv), 0, "", "")

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
    monkeypatch.setattr(screenshots.shutil, "which", lambda _: "/usr/bin/spectacle")

    def unexpected_run(*args, **kwargs):
        raise AssertionError("must not invoke Spectacle")

    monkeypatch.setattr(screenshots, "run_command", unexpected_run)

    with pytest.raises(InteractionError, match="Shared screenshot output directories"):
        screenshots.capture_fullscreen(shared / "capture.png", env={})


def test_shared_output_parent_requires_trusted_sticky_directory() -> None:
    assert _safe_output_parent(0o1777, 0)
    assert not _safe_output_parent(0o1777, os.getuid() + 1)
    assert not _safe_output_parent(0o777, os.getuid())


def test_capture_rejects_truncated_png_before_install(tmp_path, monkeypatch) -> None:
    output = tmp_path / "truncated.png"
    monkeypatch.setattr(screenshots.shutil, "which", lambda _: "/usr/bin/spectacle")

    def fake_run(argv, *, env, timeout):
        Path(argv[-1]).write_bytes(_png_image(10, 10)[:24])
        return CommandResult(tuple(argv), 0, "", "")

    monkeypatch.setattr(screenshots, "run_command", fake_run)

    with pytest.raises(InteractionError, match="not a complete, valid PNG"):
        screenshots.capture_fullscreen(output, env={})
    assert not output.exists()


def test_capture_rejects_invalid_compressed_image_data(tmp_path, monkeypatch) -> None:
    output = tmp_path / "invalid-idat.png"
    monkeypatch.setattr(screenshots.shutil, "which", lambda _: "/usr/bin/spectacle")

    def fake_run(argv, *, env, timeout):
        Path(argv[-1]).write_bytes(_png_image(10, 10, compressed_data=b"not-zlib"))
        return CommandResult(tuple(argv), 0, "", "")

    monkeypatch.setattr(screenshots, "run_command", fake_run)

    with pytest.raises(InteractionError, match="compressed image data is invalid"):
        screenshots.capture_fullscreen(output, env={})
    assert not output.exists()

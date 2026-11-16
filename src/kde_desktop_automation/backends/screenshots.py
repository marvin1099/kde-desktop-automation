from __future__ import annotations

import json
import math
import os
import stat
import struct
import tempfile
import zlib
from collections.abc import Mapping
from contextlib import suppress
from pathlib import Path
from typing import Any, BinaryIO, Literal

from kde_desktop_automation.backends.screenshot_helper import screenshot_helper_status
from kde_desktop_automation.errors import InteractionError
from kde_desktop_automation.process import require_success, run_command

_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_PNG_CHANNELS = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}
_PNG_BIT_DEPTHS = {0: {1, 2, 4, 8, 16}, 2: {8, 16}, 3: {1, 2, 4, 8}, 4: {8, 16}, 6: {8, 16}}
_ADAM7_PASSES = (
    (0, 0, 8, 8),
    (4, 0, 8, 8),
    (0, 4, 4, 8),
    (2, 0, 4, 4),
    (0, 2, 2, 4),
    (1, 0, 2, 2),
    (0, 1, 1, 2),
)
_MAX_PNG_WIDTH = 7680
_MAX_PNG_HEIGHT = 4320
_MAX_PNG_PIXELS = 16_777_216
_SCREENSHOT_TIMEOUT_SECONDS = 30.0


def capture_fullscreen(
    output: Path,
    *,
    env: Mapping[str, str],
    overwrite: bool = False,
    expected_size: tuple[int, int] | None = None,
) -> dict[str, Any]:
    return _capture_screenshot(
        output,
        env=env,
        overwrite=overwrite,
        expected_size=expected_size,
        mode="workspace",
    )


def capture_window(
    output: Path,
    *,
    window_id: str,
    env: Mapping[str, str],
    overwrite: bool = False,
) -> dict[str, Any]:
    """Capture one exact KWin window through the authorized Screenshot2 helper."""
    return _capture_screenshot(
        output,
        env=env,
        overwrite=overwrite,
        expected_size=None,
        mode="window",
        window_id=window_id,
    )


def _capture_screenshot(
    output: Path,
    *,
    env: Mapping[str, str],
    overwrite: bool,
    expected_size: tuple[int, int] | None,
    mode: Literal["workspace", "window"],
    window_id: str | None = None,
) -> dict[str, Any]:
    helper_status = screenshot_helper_status(env)
    if helper_status["available"] is not True:
        target_description = "KWin Screenshot2 helper"
        raise InteractionError(
            "backend_unavailable",
            f"The native {target_description} is required to capture screenshots.",
            details={"executable": "kde-desktop-screenshot"},
        )
    if helper_status["ready"] is not True:
        raise InteractionError(
            "screenshot_helper_not_authorized",
            "The Screenshot2 helper lacks a matching desktop entry declaring KWin authorization.",
            details={
                "helper": helper_status["helper"],
                "desktop_entry": helper_status["desktop_entry"],
                "reason": helper_status["reason"],
                "setup": "Install the helper and refresh the KDE application service cache.",
            },
        )
    helper = str(helper_status["helper"])
    if mode == "window" and not window_id:
        raise InteractionError(
            "invalid_window_id",
            "A window UUID is required for a selected-window screenshot.",
        )

    destination = output.expanduser().absolute()
    try:
        destination = destination.parent.resolve() / destination.name
        parent_stat = destination.parent.stat()
    except OSError as exc:
        raise InteractionError(
            "output_directory_missing",
            "Could not safely resolve the screenshot output directory.",
            details={"directory": str(destination.parent), "reason": str(exc)},
        ) from exc
    if not destination.parent.is_dir():
        raise InteractionError(
            "output_directory_missing",
            "The screenshot output directory does not exist.",
            details={"directory": str(destination.parent)},
        )
    parent_mode = stat.S_IMODE(parent_stat.st_mode)
    if not _safe_output_parent(parent_mode, parent_stat.st_uid):
        raise InteractionError(
            "unsafe_output_directory",
            "Shared screenshot output directories must be sticky and owned by this user or root.",
            details={"directory": str(destination.parent)},
        )
    if not overwrite and destination.exists():
        raise InteractionError(
            "output_exists",
            "Refusing to overwrite an existing screenshot without --overwrite.",
            details={"path": str(destination)},
        )

    try:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{destination.name}.",
            suffix=".png",
            dir=destination.parent,
        )
        os.fchmod(descriptor, 0o600)
        os.close(descriptor)
    except OSError as exc:
        raise InteractionError(
            "output_unavailable",
            "Could not create a private temporary screenshot beside the destination.",
            details={"path": str(destination), "reason": str(exc)},
        ) from exc
    temporary = Path(temporary_name)

    try:
        command = [helper, "capture-workspace" if mode == "workspace" else "capture-window"]
        if window_id is not None:
            command.extend(("--window-id", window_id))
        command.extend(("--output", str(temporary)))
        result = run_command(
            command,
            env=env,
            timeout=_SCREENSHOT_TIMEOUT_SECONDS,
        )
        require_success(result)
        width, height = _read_png_dimensions(temporary)
        metadata = _parse_helper_metadata(result.stdout, mode=mode)
        if (width, height) != (metadata["width"], metadata["height"]):
            raise InteractionError(
                "screenshot_metadata_mismatch",
                "The Screenshot2 helper metadata did not match the captured PNG dimensions.",
                details={
                    "metadata": {"width": metadata["width"], "height": metadata["height"]},
                    "png": {"width": width, "height": height},
                },
            )
        if mode == "window" and metadata.get("window_id") not in (None, window_id):
            raise InteractionError(
                "screenshot_target_mismatch",
                "KWin Screenshot2 returned a different window than the requested UUID.",
                details={
                    "requested_window_id": window_id,
                    "captured_window_id": metadata["window_id"],
                },
            )
        if expected_size is not None and (width, height) != expected_size:
            raise InteractionError(
                "screenshot_target_mismatch",
                "Screenshot dimensions did not match the private virtual output.",
                details={
                    "expected": {"width": expected_size[0], "height": expected_size[1]},
                    "actual": {"width": width, "height": height},
                },
            )
        temporary.chmod(0o600)
        _install_screenshot(temporary, destination, overwrite=overwrite)
    finally:
        with suppress(OSError):
            temporary.unlink(missing_ok=True)
            # The validated destination has already been installed atomically;
            # a leftover hidden temp is safer than reporting a false failure.

    capture = {
        "path": str(destination),
        "width": width,
        "height": height,
        "coordinate_space": (
            "private-output-pixels" if expected_size is not None else "screenshot-pixels"
        ),
        "scale": metadata["scale"],
        "source": "kwin-screenshot2",
        "api": "org.kde.KWin.ScreenShot2",
    }
    capture["capture_mode"] = mode
    if window_id is not None:
        capture["window_id"] = window_id
    return capture


def _parse_helper_metadata(stdout: str, *, mode: Literal["workspace", "window"]) -> dict[str, Any]:
    try:
        metadata = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise InteractionError(
            "screenshot_metadata_invalid",
            "The KWin Screenshot2 helper returned invalid metadata.",
            details={"reason": str(exc)},
        ) from exc
    if not isinstance(metadata, dict):
        raise InteractionError(
            "screenshot_metadata_invalid",
            "The KWin Screenshot2 helper returned invalid metadata.",
        )
    width = metadata.get("width")
    height = metadata.get("height")
    scale = metadata.get("scale")
    if (
        type(width) is not int
        or type(height) is not int
        or width < 1
        or height < 1
        or not isinstance(scale, (int, float))
        or isinstance(scale, bool)
        or not math.isfinite(scale)
        or scale <= 0
    ):
        raise InteractionError(
            "screenshot_metadata_invalid",
            "KWin Screenshot2 returned invalid dimensions or scale metadata.",
            details={"metadata": metadata},
        )
    if (
        mode == "window"
        and metadata.get("window_id") is not None
        and not isinstance(metadata["window_id"], str)
    ):
        raise InteractionError(
            "screenshot_metadata_invalid",
            "KWin Screenshot2 returned an invalid window identity.",
        )
    return {"width": width, "height": height, "scale": float(scale)} | (
        {"window_id": metadata["window_id"]}
        if mode == "window" and metadata.get("window_id") is not None
        else {}
    )


def _install_screenshot(temporary: Path, destination: Path, *, overwrite: bool) -> None:
    if overwrite:
        try:
            os.replace(temporary, destination)
        except OSError as exc:
            raise InteractionError(
                "output_unavailable",
                "Could not atomically install the validated screenshot.",
                details={"path": str(destination), "reason": str(exc)},
            ) from exc
        return

    try:
        os.link(temporary, destination)
    except FileExistsError as exc:
        raise InteractionError(
            "output_exists",
            "Refusing to overwrite an existing screenshot without --overwrite.",
            details={"path": str(destination)},
        ) from exc
    except OSError as exc:
        raise InteractionError(
            "output_unavailable",
            "Could not atomically install the screenshot without overwriting.",
            details={"path": str(destination), "reason": str(exc)},
        ) from exc


def _read_png_dimensions(path: Path) -> tuple[int, int]:
    try:
        with path.open("rb") as screenshot_file:
            signature = screenshot_file.read(len(_PNG_SIGNATURE))
            if signature != _PNG_SIGNATURE:
                raise _invalid_png(path, "PNG signature is missing or invalid.")

            ihdr_length, ihdr_type = _read_chunk_header(screenshot_file, path)
            if ihdr_type != b"IHDR" or ihdr_length != 13:
                raise _invalid_png(path, "PNG must begin with a 13-byte IHDR chunk.")
            ihdr = _read_exact(screenshot_file, ihdr_length, path)
            _check_chunk_crc(screenshot_file, ihdr_type, ihdr, path)
            width, height = struct.unpack(">II", ihdr[:8])
            expected_image_bytes = _expected_image_bytes(width, height, ihdr, path)
            inflater = zlib.decompressobj()

            saw_idat = False
            saw_idat_data = False
            idat_ended = False
            inflated_size = 0
            while True:
                chunk_length, chunk_type = _read_chunk_header(screenshot_file, path)
                if chunk_type == b"IHDR":
                    raise _invalid_png(path, "PNG contains more than one IHDR chunk.")
                if chunk_type == b"IDAT":
                    if idat_ended:
                        raise _invalid_png(path, "PNG IDAT chunks must be consecutive.")
                    saw_idat = True
                    saw_idat_data = saw_idat_data or chunk_length > 0
                elif saw_idat and chunk_type != b"IEND":
                    idat_ended = True

                checksum = zlib.crc32(chunk_type)
                remaining = chunk_length
                while remaining:
                    chunk = screenshot_file.read(min(64 * 1024, remaining))
                    if not chunk:
                        raise _invalid_png(path, "PNG ended before a chunk was complete.")
                    checksum = zlib.crc32(chunk, checksum)
                    if chunk_type == b"IDAT":
                        inflated_size = _inflate_idat_block(
                            inflater,
                            chunk,
                            inflated_size,
                            expected_image_bytes,
                            path,
                        )
                    remaining -= len(chunk)
                checksum_bytes = _read_exact(screenshot_file, 4, path)
                actual_checksum = struct.unpack(">I", checksum_bytes)[0]
                if checksum & 0xFFFFFFFF != actual_checksum:
                    raise _invalid_png(path, "PNG chunk checksum is invalid.")

                if chunk_type == b"IEND":
                    if chunk_length != 0 or not saw_idat_data:
                        raise _invalid_png(path, "PNG is missing valid image data or IEND.")
                    if not inflater.eof or inflater.unused_data:
                        raise _invalid_png(path, "PNG IDAT compressed image data is incomplete.")
                    if inflated_size != expected_image_bytes:
                        raise _invalid_png(path, "PNG scanline data has an invalid size.")
                    if screenshot_file.read(1):
                        raise _invalid_png(path, "PNG has unexpected data after IEND.")
                    return width, height
    except OSError as exc:
        raise InteractionError(
            "screenshot_read_failed",
            "Could not read KWin Screenshot2's screenshot output.",
            details={"path": str(path), "reason": str(exc)},
        ) from exc


def _read_chunk_header(screenshot_file: BinaryIO, path: Path) -> tuple[int, bytes]:
    header = _read_exact(screenshot_file, 8, path)
    length = struct.unpack(">I", header[:4])[0]
    chunk_type = header[4:]
    if length > 0x7FFFFFFF or len(chunk_type) != 4:
        raise _invalid_png(path, "PNG chunk has an invalid size or type.")
    return length, chunk_type


def _read_exact(screenshot_file: BinaryIO, length: int, path: Path) -> bytes:
    data = screenshot_file.read(length)
    if len(data) != length:
        raise _invalid_png(path, "PNG ended before a chunk was complete.")
    return data


def _check_chunk_crc(
    screenshot_file: BinaryIO,
    chunk_type: bytes,
    data: bytes,
    path: Path,
) -> None:
    expected = struct.unpack(">I", _read_exact(screenshot_file, 4, path))[0]
    actual = zlib.crc32(chunk_type + data) & 0xFFFFFFFF
    if expected != actual:
        raise _invalid_png(path, "PNG chunk checksum is invalid.")


def _invalid_png(path: Path, reason: str) -> InteractionError:
    return InteractionError(
        "invalid_screenshot",
        f"KWin Screenshot2 output was not a complete, valid PNG image: {reason}",
        details={"path": str(path)},
    )


def _expected_image_bytes(width: int, height: int, ihdr: bytes, path: Path) -> int:
    bit_depth, color_type, compression, filter_method, interlace = ihdr[8:13]
    if (
        width <= 0
        or height <= 0
        or width > _MAX_PNG_WIDTH
        or height > _MAX_PNG_HEIGHT
        or width * height > _MAX_PNG_PIXELS
    ):
        raise _invalid_png(path, "PNG dimensions exceed the supported output limits.")
    if color_type not in _PNG_CHANNELS or bit_depth not in _PNG_BIT_DEPTHS[color_type]:
        raise _invalid_png(path, "PNG color type and bit depth are invalid.")
    if compression != 0 or filter_method != 0 or interlace not in (0, 1):
        raise _invalid_png(
            path, "PNG uses an unsupported compression, filter, or interlace method."
        )

    bits_per_pixel = _PNG_CHANNELS[color_type] * bit_depth
    if interlace == 0:
        return height * (1 + (width * bits_per_pixel + 7) // 8)

    total = 0
    for start_x, start_y, step_x, step_y in _ADAM7_PASSES:
        pass_width = _pass_dimension(width, start_x, step_x)
        pass_height = _pass_dimension(height, start_y, step_y)
        if pass_width and pass_height:
            row_bytes = (pass_width * bits_per_pixel + 7) // 8
            total += pass_height * (1 + row_bytes)
    return total


def _pass_dimension(length: int, start: int, step: int) -> int:
    if length <= start:
        return 0
    return (length - start + step - 1) // step


def _inflate_idat_block(
    inflater: Any,
    compressed: bytes,
    inflated_size: int,
    expected_size: int,
    path: Path,
) -> int:
    pending = compressed
    while True:
        output_limit = min(64 * 1024, expected_size - inflated_size + 1)
        if output_limit <= 0:
            raise _invalid_png(path, "PNG contains more scanline data than its dimensions allow.")
        try:
            output = inflater.decompress(pending, output_limit)
        except zlib.error as exc:
            raise _invalid_png(path, "PNG IDAT compressed image data is invalid.") from exc
        inflated_size += len(output)
        if inflated_size > expected_size:
            raise _invalid_png(path, "PNG contains more scanline data than its dimensions allow.")
        if inflater.unused_data:
            raise _invalid_png(path, "PNG IDAT contains data after the compressed image stream.")

        pending = inflater.unconsumed_tail
        if pending:
            if not output:
                raise _invalid_png(path, "PNG IDAT compressed data could not be decoded.")
            continue
        if len(output) == output_limit:
            pending = b""
            continue
        return inflated_size


def _safe_output_parent(mode: int, owner_uid: int) -> bool:
    if not mode & 0o022:
        return True
    return bool(mode & stat.S_ISVTX) and owner_uid in {0, os.getuid()}

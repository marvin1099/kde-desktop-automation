from __future__ import annotations

import os
import shutil
import stat
import struct
import tempfile
import zlib
from collections.abc import Mapping
from contextlib import suppress
from pathlib import Path
from typing import Any, BinaryIO

from kde_interaction.errors import InteractionError
from kde_interaction.process import require_success, run_command

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


def capture_fullscreen(
    output: Path,
    *,
    env: Mapping[str, str],
    overwrite: bool = False,
    expected_size: tuple[int, int] | None = None,
) -> dict[str, Any]:
    spectacle = shutil.which("spectacle")
    if spectacle is None:
        raise InteractionError(
            "backend_unavailable",
            "Spectacle is required to capture the private KWin output.",
            details={"executable": "spectacle"},
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
        result = run_command(
            [spectacle, "--background", "--fullscreen", "--nonotify", "--output", str(temporary)],
            env=env,
            timeout=20.0,
        )
        require_success(result)
        width, height = _read_png_dimensions(temporary)
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

    return {
        "path": str(destination),
        "width": width,
        "height": height,
        "coordinate_space": "private-output-pixels",
        "source": "spectacle",
    }


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
            "Could not read Spectacle's screenshot output.",
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
        f"Spectacle output was not a complete, valid PNG image: {reason}",
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

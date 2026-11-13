from __future__ import annotations

import fcntl
import json
import os
import re
import stat
import tempfile
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from kde_desktop_automation.backends.displays import DisplayInfo
from kde_desktop_automation.backends.kwin_input import (
    DEFAULT_DRAG_DURATION_MS,
    DEFAULT_DRAG_END_PAUSE_MS,
    DEFAULT_DRAG_HOLD_MS,
    InputReceipt,
    validate_drag_steps,
    validate_drag_timing,
)
from kde_desktop_automation.backends.kwin_virtual import KWinVirtualBackend
from kde_desktop_automation.errors import InteractionError
from kde_desktop_automation.models import ProcessIdentity, SessionRecord
from kde_desktop_automation.paths import RuntimePaths, validate_session_id
from kde_desktop_automation.window_selection import find_one_window

_NAME = re.compile(r"^[\w .-]{1,64}$", flags=re.UNICODE)
_MAX_OUTPUT_PIXELS = 16_777_216


class SessionBackend(Protocol):
    name: str

    def start(self, session_id: str, name: str, width: int, height: int) -> SessionRecord: ...

    def is_running(self, session: SessionRecord) -> bool: ...

    def stop(self, session: SessionRecord) -> list[str]: ...

    def environment(self, session: SessionRecord) -> dict[str, str]: ...

    def displays(self, session: SessionRecord) -> list[DisplayInfo]: ...

    def screenshot(
        self,
        session: SessionRecord,
        output: Path,
        *,
        overwrite: bool,
    ) -> dict[str, object]: ...

    def launch_application(
        self,
        session: SessionRecord,
        argv: tuple[str, ...],
    ) -> ProcessIdentity: ...

    def stop_application(self, identity: ProcessIdentity) -> None: ...

    def send_key(self, session: SessionRecord, chord: str) -> InputReceipt: ...

    def send_text(self, session: SessionRecord, text: str) -> InputReceipt: ...

    def click(self, session: SessionRecord, x: int, y: int, button: str) -> InputReceipt: ...

    def drag(
        self,
        session: SessionRecord,
        start_x: int,
        start_y: int,
        end_x: int,
        end_y: int,
        button: str,
        steps: int,
        duration_ms: int,
        hold_ms: int,
        end_pause_ms: int,
    ) -> InputReceipt: ...

    def list_windows(self, session: SessionRecord) -> list[dict[str, Any]]: ...

    def inspect_window(self, session: SessionRecord, window_id: str) -> dict[str, Any]: ...

    def activate_window(self, session: SessionRecord, window_id: str) -> dict[str, Any]: ...

    def set_window_minimized(
        self,
        session: SessionRecord,
        window_id: str,
        minimized: bool,
    ) -> dict[str, Any]: ...

    def set_window_geometry(
        self,
        session: SessionRecord,
        window_id: str,
        x: int,
        y: int,
        width: int,
        height: int,
    ) -> dict[str, Any]: ...

    def pointer_position(self, session: SessionRecord) -> dict[str, Any]: ...

    def send_key_to_window(
        self, session: SessionRecord, window_id: str, chord: str
    ) -> dict[str, Any]: ...

    def send_text_to_window(
        self, session: SessionRecord, window_id: str, text: str
    ) -> dict[str, Any]: ...

    def click_window(
        self,
        session: SessionRecord,
        window_id: str,
        x: float,
        y: float,
        button: str,
    ) -> dict[str, Any]: ...


class SessionStore:
    """Atomic, user-private persistence for managed session metadata."""

    def __init__(self, paths: RuntimePaths) -> None:
        self.root = paths.sessions_root
        self.default_path = self.root / "default-session"

    def save(self, session: SessionRecord) -> None:
        validate_session_id(session.session_id)
        destination = self.root / f"{session.session_id}.json"
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{session.session_id}-",
            suffix=".tmp",
            dir=self.root,
        )
        temporary_path = Path(temporary_name)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as state_file:
                json.dump(session.to_dict(), state_file, indent=2, sort_keys=True)
                state_file.write("\n")
                state_file.flush()
                os.fsync(state_file.fileno())
            os.replace(temporary_path, destination)
            destination.chmod(0o600)
        except OSError as exc:
            temporary_path.unlink(missing_ok=True)
            raise InteractionError(
                "session_state_write_failed",
                "Could not persist private session state.",
                details={"path": str(destination), "reason": str(exc)},
            ) from exc

    @contextmanager
    def lock(self, session_id: str) -> Iterator[None]:
        """Serialize session-record mutations across concurrent CLI processes."""
        validate_session_id(session_id)
        with self._file_lock(self.root / f".{session_id}.lock"):
            yield

    @contextmanager
    def _file_lock(self, lock_path: Path) -> Iterator[None]:
        flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor: int | None = None
        try:
            descriptor = os.open(lock_path, flags, 0o600)
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid():
                os.close(descriptor)
                descriptor = None
                raise InteractionError(
                    "session_lock_unsafe",
                    "Refusing to lock a session record through an unsafe lock file.",
                    details={"path": str(lock_path)},
                )
            os.fchmod(descriptor, 0o600)
            fcntl.flock(descriptor, fcntl.LOCK_EX)
        except InteractionError:
            if descriptor is not None:
                os.close(descriptor)
            raise
        except OSError as exc:
            if descriptor is not None:
                os.close(descriptor)
            raise InteractionError(
                "session_lock_failed",
                "Could not serialize updates to the private session record.",
                details={"path": str(lock_path), "reason": str(exc)},
            ) from exc
        try:
            yield
        finally:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)

    def load(self, session_id: str) -> SessionRecord:
        validate_session_id(session_id)
        path = self.root / f"{session_id}.json"
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(value, dict):
                raise ValueError("session record must be a JSON object")
            record = SessionRecord.from_dict(value)
        except FileNotFoundError as exc:
            raise InteractionError(
                "session_not_found",
                f"No managed session named '{session_id}' exists.",
                details={"session_id": session_id},
            ) from exc
        except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            raise InteractionError(
                "session_state_invalid",
                "The saved session record is unreadable or invalid.",
                details={"path": str(path), "reason": str(exc)},
            ) from exc
        if record.session_id != session_id:
            raise InteractionError(
                "session_state_mismatch",
                "The saved session ID does not match its requested record path.",
                details={
                    "requested_session_id": session_id,
                    "recorded_session_id": record.session_id,
                },
            )
        return record

    def list(self) -> list[SessionRecord]:
        records: list[SessionRecord] = []
        for path in sorted(self.root.glob("agent-*.json")):
            records.append(self.load(path.stem))
        return records

    def default_session_id(self) -> str | None:
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(self.default_path, flags)
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise InteractionError(
                "default_session_read_failed",
                "Could not safely read the configured private default session.",
                details={"path": str(self.default_path), "reason": str(exc)},
            ) from exc
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid():
                raise InteractionError(
                    "default_session_unsafe",
                    "The private default-session setting is not a user-owned regular file.",
                    details={"path": str(self.default_path)},
                )
            with os.fdopen(descriptor, "r", encoding="utf-8") as default_file:
                descriptor = -1
                session_id = default_file.read(128).strip()
        except InteractionError:
            raise
        except (OSError, UnicodeError) as exc:
            raise InteractionError(
                "default_session_read_failed",
                "Could not safely read the configured private default session.",
                details={"path": str(self.default_path), "reason": str(exc)},
            ) from exc
        finally:
            if descriptor >= 0:
                os.close(descriptor)
        try:
            validate_session_id(session_id)
        except InteractionError as exc:
            raise InteractionError(
                "default_session_invalid",
                "The configured private default session ID is invalid.",
                details={"path": str(self.default_path), "session_id": session_id},
            ) from exc
        return session_id

    def set_default_session(self, session_id: str) -> None:
        validate_session_id(session_id)
        self.load(session_id)
        with self._file_lock(self.root / ".default-session.lock"):
            self._write_default_session(session_id)

    def set_default_session_if_unset(self, session_id: str) -> bool:
        """Atomically select the first private session unless a default exists."""
        validate_session_id(session_id)
        self.load(session_id)
        with self._file_lock(self.root / ".default-session.lock"):
            if self.default_session_id() is not None:
                return False
            self._write_default_session(session_id)
            return True

    def _write_default_session(self, session_id: str) -> None:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=".default-session-",
            suffix=".tmp",
            dir=self.root,
        )
        temporary_path = Path(temporary_name)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as default_file:
                default_file.write(f"{session_id}\n")
                default_file.flush()
                os.fsync(default_file.fileno())
            os.replace(temporary_path, self.default_path)
        except OSError as exc:
            temporary_path.unlink(missing_ok=True)
            raise InteractionError(
                "default_session_write_failed",
                "Could not update the private default-session setting.",
                details={"path": str(self.default_path), "reason": str(exc)},
            ) from exc


class SessionManager:
    def __init__(self, paths: RuntimePaths, backend: SessionBackend | None = None) -> None:
        self.paths = paths
        self.backend = backend or KWinVirtualBackend(paths)
        self.store = SessionStore(paths)

    def create_agent_session(
        self,
        name: str,
        width: int,
        height: int,
        *,
        make_default: bool = False,
    ) -> SessionRecord:
        _validate_request(name, width, height)
        name = name.strip()
        session_id = f"agent-{uuid.uuid4().hex[:12]}"
        record = self.backend.start(session_id, name, width, height)
        try:
            self.store.save(record)
        except Exception:
            self.backend.stop(record)
            raise
        try:
            if make_default:
                self.store.set_default_session(session_id)
            else:
                self.store.set_default_session_if_unset(session_id)
        except Exception as exc:
            cleanup_warnings: list[str] = []
            cleanup_error: str | None = None
            try:
                cleanup_warnings = self.backend.stop(record)
                stopped = replace(record, stopped_at=datetime.now(UTC).isoformat())
                self.store.save(stopped)
            except Exception as cleanup_exc:
                cleanup_error = str(cleanup_exc)
            raise InteractionError(
                "session_default_selection_failed",
                "The private session started, but its default could not be selected. "
                "Cleanup was attempted; inspect the session record before retrying.",
                details={
                    "session_id": session_id,
                    "selection_error": str(exc),
                    "cleanup_error": cleanup_error,
                    "cleanup_warnings": cleanup_warnings,
                    "session_record_retained": True,
                },
            ) from exc
        return record

    def list_sessions(self) -> list[dict[str, Any]]:
        return [self.public_record(record) for record in self.store.list()]

    def default_session_id(self) -> str | None:
        return self.store.default_session_id()

    def set_default_session(self, session_id: str) -> dict[str, Any]:
        self._require_running(session_id)
        self.store.set_default_session(session_id)
        return self.get_session(session_id)

    def get_session(self, session_id: str) -> dict[str, Any]:
        return self.public_record(self.store.load(session_id))

    def runtime_environment(self, session_id: str) -> dict[str, str]:
        """Return the selected running session's isolated process environment."""
        record = self._require_running(session_id)
        return self.backend.environment(record)

    def displays(self, session_id: str) -> list[dict[str, Any]]:
        record = self._require_running(session_id)
        return [display.to_dict() for display in self.backend.displays(record)]

    def screenshot(
        self,
        session_id: str,
        output: Path,
        *,
        overwrite: bool = False,
    ) -> dict[str, object]:
        record = self._require_running(session_id)
        capture = self.backend.screenshot(record, output, overwrite=overwrite)
        return capture | {
            "target": {"kind": "agent-session", "session_id": session_id},
            "coordinate_space": "screenshot-pixels",
        }

    def launch_application(self, session_id: str, argv: tuple[str, ...]) -> dict[str, Any]:
        with self.store.lock(session_id):
            record = self._require_running(session_id)
            process = self.backend.launch_application(record, argv)
            updated = replace(
                record, application_processes=record.application_processes + (process,)
            )
            try:
                self.store.save(updated)
            except Exception:
                self.backend.stop_application(process)
                raise
            return {
                "target": {"kind": "agent-session", "session_id": session_id},
                "action": "application launched",
                "process": {"pid": process.pid, "executable": process.executable},
                "process_started": True,
                "window_opened": None,
                "note": (
                    "The process started in the private session; inspect a screenshot "
                    "to verify its window."
                ),
            }

    def send_key(self, session_id: str, chord: str) -> dict[str, Any]:
        return self._input_receipt(session_id, self.backend.send_key, chord)

    def send_text(self, session_id: str, text: str) -> dict[str, Any]:
        return self._input_receipt(session_id, self.backend.send_text, text)

    def click(self, session_id: str, x: int, y: int, button: str = "left") -> dict[str, Any]:
        record = self._require_running(session_id)
        if (
            isinstance(x, bool)
            or not isinstance(x, int)
            or isinstance(y, bool)
            or not isinstance(y, int)
        ):
            raise InteractionError(
                "invalid_coordinates",
                "Click coordinates must be integer logical output pixels.",
                details={"x": x, "y": y},
            )
        if not (0 <= x < record.width and 0 <= y < record.height):
            raise InteractionError(
                "coordinates_out_of_bounds",
                "Click coordinates must be inside the private output.",
                details={"x": x, "y": y, "width": record.width, "height": record.height},
            )
        receipt = self.backend.click(record, x, y, button)
        result = receipt.to_dict(session_id=session_id, wayland_display=record.wayland_display)
        result["requested_point"] = {
            "x": x,
            "y": y,
            "space": "logical-desktop-pixels",
        }
        return _global_pointer_semantics(result)

    def drag(
        self,
        session_id: str,
        start_x: int,
        start_y: int,
        end_x: int,
        end_y: int,
        *,
        button: str = "left",
        steps: int = 8,
        duration_ms: int = DEFAULT_DRAG_DURATION_MS,
        hold_ms: int = DEFAULT_DRAG_HOLD_MS,
        end_pause_ms: int = DEFAULT_DRAG_END_PAUSE_MS,
    ) -> dict[str, Any]:
        validate_drag_steps(steps)
        validate_drag_timing(duration_ms, hold_ms, end_pause_ms)
        record = self._require_running(session_id)
        for x, y in ((start_x, start_y), (end_x, end_y)):
            if (
                isinstance(x, bool)
                or not isinstance(x, int)
                or isinstance(y, bool)
                or not isinstance(y, int)
            ):
                raise InteractionError(
                    "invalid_coordinates",
                    "Drag coordinates must be integer logical output pixels.",
                    details={"x": x, "y": y},
                )
            if not (0 <= x < record.width and 0 <= y < record.height):
                raise InteractionError(
                    "coordinates_out_of_bounds",
                    "Drag coordinates must be integer pixels inside the private output.",
                    details={
                        "x": x,
                        "y": y,
                        "width": record.width,
                        "height": record.height,
                    },
                )
        receipt = self.backend.drag(
            record,
            start_x,
            start_y,
            end_x,
            end_y,
            button,
            steps,
            duration_ms,
            hold_ms,
            end_pause_ms,
        )
        result = receipt.to_dict(session_id=session_id, wayland_display=record.wayland_display)
        result["requested_path"] = {
            "start": {"x": start_x, "y": start_y},
            "end": {"x": end_x, "y": end_y},
            "steps": steps,
            "duration_ms": duration_ms,
            "hold_ms": hold_ms,
            "end_pause_ms": end_pause_ms,
            "space": "logical-desktop-pixels",
        }
        return _global_pointer_semantics(result)

    def list_windows(self, session_id: str) -> dict[str, Any]:
        record = self._require_window_management(session_id)
        windows = self.backend.list_windows(record)
        return {
            "target": {"kind": "agent-session", "session_id": session_id},
            "observed_at": datetime.now(UTC).isoformat(),
            "count": len(windows),
            "windows": windows,
            "note": (
                "Window IDs are scoped to this private session and become stale when windows close."
            ),
        }

    def find_window(
        self,
        session_id: str,
        *,
        app_id: str | None,
        title: str | None,
    ) -> dict[str, Any]:
        listing = self.list_windows(session_id)
        window = find_one_window(listing["windows"], app_id=app_id, title=title)
        return {
            "target": listing["target"],
            "observed_at": listing["observed_at"],
            "window": window,
        }

    def inspect_window(self, session_id: str, window_id: str) -> dict[str, Any]:
        record = self._require_window_management(session_id)
        window = self.backend.inspect_window(record, window_id)
        return {
            "target": {"kind": "agent-session", "session_id": session_id},
            "window": window,
        }

    def activate_window(self, session_id: str, window_id: str) -> dict[str, Any]:
        record = self._require_window_management(session_id)
        window = self.backend.activate_window(record, window_id)
        return {
            "target": {"kind": "agent-session", "session_id": session_id, "window_id": window_id},
            "action": "window activated",
            "active_window_verified": bool(window["active"]),
            "window": window,
            "note": "Activation is scoped to this private KWin session.",
        }

    def set_window_minimized(
        self,
        session_id: str,
        window_id: str,
        minimized: bool,
    ) -> dict[str, Any]:
        if not isinstance(minimized, bool):
            raise InteractionError(
                "invalid_window_state",
                "Minimized state must be explicitly true or false.",
                details={"minimized": minimized},
            )
        record = self._require_window_management(session_id)
        window = self.backend.set_window_minimized(record, window_id, minimized)
        if window.get("minimized") is not minimized:
            raise InteractionError(
                "window_state_unverified",
                "The private KWin backend did not report the requested minimized state.",
                details={
                    "session_id": session_id,
                    "window_id": window_id,
                    "requested_minimized": minimized,
                    "effective_minimized": window.get("minimized"),
                },
            )
        return {
            "target": {"kind": "agent-session", "session_id": session_id, "window_id": window_id},
            "action": "set-minimized",
            "minimized": minimized,
            "verified": True,
            "window": window,
            "note": "Minimized state is scoped to this private KWin session.",
        }

    def set_window_geometry(
        self,
        session_id: str,
        window_id: str,
        x: int,
        y: int,
        width: int,
        height: int,
    ) -> dict[str, Any]:
        record = self._require_window_management(session_id)
        result = self.backend.set_window_geometry(record, window_id, x, y, width, height)
        if result.get("verified") is not True:
            raise InteractionError(
                "window_geometry_unverified",
                "The private KWin backend did not verify the requested frame geometry.",
                details={
                    "session_id": session_id,
                    "window_id": window_id,
                    "result": result,
                },
            )
        return result | {
            "target": {"kind": "agent-session", "session_id": session_id, "window_id": window_id},
            "action": "set-geometry",
        }

    def pointer_position(self, session_id: str) -> dict[str, Any]:
        record = self._require_running(session_id)
        return self.backend.pointer_position(record) | {
            "target": {"kind": "agent-session", "session_id": session_id}
        }

    def send_key_to_window(
        self,
        session_id: str,
        window_id: str,
        chord: str,
    ) -> dict[str, Any]:
        record = self._require_window_management(session_id)
        return self.backend.send_key_to_window(record, window_id, chord)

    def send_text_to_window(
        self,
        session_id: str,
        window_id: str,
        text: str,
    ) -> dict[str, Any]:
        record = self._require_window_management(session_id)
        return self.backend.send_text_to_window(record, window_id, text)

    def click_window(
        self,
        session_id: str,
        window_id: str,
        x: float,
        y: float,
        button: str = "left",
    ) -> dict[str, Any]:
        record = self._require_window_management(session_id)
        return self.backend.click_window(record, window_id, x, y, button)

    def stop(self, session_id: str) -> tuple[dict[str, Any], list[str]]:
        with self.store.lock(session_id):
            record = self.store.load(session_id)
            warnings = self.backend.stop(record)
            if self.backend.is_running(record):
                raise InteractionError(
                    "session_stop_incomplete",
                    "The private session still has a verified backend process running.",
                    details={"session_id": session_id, "warnings": warnings},
                )
            stopped = replace(record, stopped_at=datetime.now(UTC).isoformat())
            self.store.save(stopped)
            return self.public_record(stopped), warnings

    def public_record(self, record: SessionRecord) -> dict[str, Any]:
        if record.stopped_at is not None:
            status = "stopped"
        elif self.backend.is_running(record):
            status = "running"
        else:
            status = "degraded"
        return record.public_dict(status=status) | {
            "is_default": self.store.default_session_id() == record.session_id
        }

    def _require_window_management(self, session_id: str) -> SessionRecord:
        record = self._require_running(session_id)
        if record.window_management_version is None or record.window_management_version < 13:
            raise InteractionError(
                "capability_unavailable",
                "The selected private session does not support window enumeration or targeting.",
                details={
                    "session_id": session_id,
                    "capability": (
                        "window.list / window.find / window.wait / window.inspect / "
                        "window.activate / window-targeted input / window.set_minimized"
                    ),
                    "window_management_protocol_version": record.window_management_version,
                },
            )
        return record

    def _require_running(self, session_id: str) -> SessionRecord:
        record = self.store.load(session_id)
        if record.stopped_at is not None or not self.backend.is_running(record):
            raise InteractionError(
                "session_not_running",
                "The private KWin session is not running.",
                details={"session_id": session_id},
            )
        return record

    def _input_receipt(
        self,
        session_id: str,
        action: Callable[[SessionRecord, str], InputReceipt],
        value: str,
    ) -> dict[str, Any]:
        record = self._require_running(session_id)
        receipt = action(record, value)
        return receipt.to_dict(session_id=session_id, wayland_display=record.wayland_display)


def _global_pointer_semantics(result: dict[str, Any]) -> dict[str, Any]:
    result["target"]["keyboard_focus"] = "not targeted by this global pointer action"
    result["input_routing"] = "global pointer coordinates; not directly window-bound"
    result["input_semantics"] = {
        "targeting": "global-coordinate",
        "atomic_window_routing": False,
        "delivery_acknowledged": False,
    }
    result["note"] += " Global pointer input may move the pointer or change the active window."
    return result


def _validate_request(name: str, width: int, height: int) -> None:
    if not _NAME.fullmatch(name.strip()):
        raise InteractionError(
            "invalid_session_name",
            "Session names must be 1–64 letters, numbers, spaces, dots, underscores, or hyphens.",
        )
    if width < 320 or width > 7680 or height < 240 or height > 4320:
        raise InteractionError(
            "invalid_output_size",
            "Virtual output size must be between 320×240 and 7680×4320.",
        )
    if width * height > _MAX_OUTPUT_PIXELS:
        raise InteractionError(
            "output_too_large",
            "Virtual output exceeds the 16-megapixel safety limit.",
        )

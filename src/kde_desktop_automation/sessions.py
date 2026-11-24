from __future__ import annotations

import fcntl
import json
import os
import re
import select
import stat
import subprocess
import sys
import tempfile
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal, Protocol

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
from kde_desktop_automation.process import process_identity, process_matches, terminate_verified
from kde_desktop_automation.window_selection import find_one_window

_NAME = re.compile(r"^[\w .-]{1,64}$", flags=re.UNICODE)
_MAX_OUTPUT_PIXELS = 16_777_216
DEFAULT_SESSION_LEASE_SECONDS = 6 * 60 * 60
_LEASE_WORKER_MODULE = "kde_desktop_automation.session_lease_worker"
_LEASE_READY_TIMEOUT_SECONDS = 5.0
_LEASE_WORKER_POLL_SECONDS = 30.0
_PROCESS_MARKER_ENV = "KDE_DESKTOP_AUTOMATION_PROCESS_MARKER"
_READY_FD_ENV = "KDE_DESKTOP_AUTOMATION_READY_FD"


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

    def screenshot_window(
        self,
        session: SessionRecord,
        window_id: str,
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

    def set_window_fullscreen(
        self,
        session: SessionRecord,
        window_id: str,
        fullscreen: bool,
    ) -> dict[str, Any]: ...

    def set_window_maximized(
        self,
        session: SessionRecord,
        window_id: str,
        maximized: bool,
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

    def clear_default_session_if(self, session_id: str) -> bool:
        """Clear the private default only when it still names the stopped session."""
        validate_session_id(session_id)
        with self._file_lock(self.root / ".default-session.lock"):
            if self.default_session_id() != session_id:
                return False
            try:
                self.default_path.unlink()
            except FileNotFoundError:
                return False
            except OSError as exc:
                raise InteractionError(
                    "default_session_write_failed",
                    "Could not clear the stopped private session from the default setting.",
                    details={"path": str(self.default_path), "reason": str(exc)},
                ) from exc
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
    def __init__(
        self,
        paths: RuntimePaths,
        backend: SessionBackend | None = None,
        *,
        lease_worker_starter: Callable[[str], ProcessIdentity | None] | None = None,
    ) -> None:
        self.paths = paths
        self.backend = backend or KWinVirtualBackend(paths)
        self.store = SessionStore(paths)
        self._lease_worker_starter = lease_worker_starter or self._start_lease_worker
        self._lease_worker_processes: dict[str, subprocess.Popen[bytes]] = {}

    def create_agent_session(
        self,
        name: str,
        width: int,
        height: int,
        *,
        make_default: bool = False,
        duration_seconds: int = DEFAULT_SESSION_LEASE_SECONDS,
    ) -> SessionRecord:
        _validate_request(name, width, height)
        _validate_lease_duration(duration_seconds)
        name = name.strip()
        session_id = f"agent-{uuid.uuid4().hex[:12]}"
        record = self.backend.start(session_id, name, width, height)
        expires_at = _lease_expiration(duration_seconds)
        record = replace(record, expires_at=expires_at)
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

        if expires_at is not None:
            worker_identity: ProcessIdentity | None = None
            try:
                # Keep the worker from observing an incomplete record between startup
                # and persisting its verified process identity.
                with self.store.lock(session_id):
                    worker_identity = self._lease_worker_starter(session_id)
                    record = replace(record, lease_worker=worker_identity)
                    self.store.save(record)
            except Exception as exc:
                monitor_cleanup_warnings: list[str] = []
                monitor_cleanup_error: str | None = None
                if worker_identity is not None:
                    monitor_cleanup_warnings.extend(self._stop_lease_worker(worker_identity))
                try:
                    monitor_cleanup_warnings.extend(self.backend.stop(record))
                    stopped = replace(record, stopped_at=datetime.now(UTC).isoformat())
                    self.store.save(stopped)
                except Exception as cleanup_exc:
                    monitor_cleanup_error = str(cleanup_exc)
                raise InteractionError(
                    "session_lease_monitor_start_failed",
                    "The private session started, but its automatic-expiry monitor could not "
                    "be started. Cleanup was attempted; inspect the session record "
                    "before retrying.",
                    details={
                        "session_id": session_id,
                        "monitor_error": str(exc),
                        "cleanup_error": monitor_cleanup_error,
                        "cleanup_warnings": monitor_cleanup_warnings,
                        "session_record_retained": True,
                    },
                ) from exc
        return record

    def list_sessions(self) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        for record in self.store.list():
            self._expire_session_if_due(record.session_id)
            record = self._ensure_lease_worker(record.session_id)
            records.append(self.public_record(record))
        return records

    def default_session_id(self) -> str | None:
        return self.store.default_session_id()

    def set_default_session(self, session_id: str) -> dict[str, Any]:
        self._require_running(session_id)
        self.store.set_default_session(session_id)
        return self.get_session(session_id)

    def get_session(self, session_id: str) -> dict[str, Any]:
        self._expire_session_if_due(session_id)
        record = self._ensure_lease_worker(session_id)
        return self.public_record(record)

    def extend_session(
        self,
        session_id: str,
        *,
        duration_seconds: int = DEFAULT_SESSION_LEASE_SECONDS,
    ) -> dict[str, Any]:
        """Add time to a finite lease, or switch the session to an indefinite lease."""
        _validate_lease_duration(duration_seconds)
        expired_worker: ProcessIdentity | None = None
        expired = False
        with self.store.lock(session_id):
            record = self.store.load(session_id)
            if record.stopped_at is not None or not self.backend.is_running(record):
                raise InteractionError(
                    "session_not_running",
                    "Only a running private session can have its lease extended.",
                    details={"session_id": session_id},
                )

            now = datetime.now(UTC)
            expiration = _parse_lease_expiration(record.expires_at)
            if expiration is not None and expiration <= now:
                self._stop_record_locked(record)
                expired_worker = record.lease_worker
                expired = True
            else:
                new_expiration = (
                    None
                    if duration_seconds == -1
                    else _lease_expiration(
                        duration_seconds,
                        base=(expiration if expiration is not None else now),
                    )
                )
                updated = replace(record, expires_at=new_expiration)
                if new_expiration is None:
                    expired_worker = record.lease_worker
                    updated = replace(updated, lease_worker=None)
                else:
                    updated = self._ensure_lease_worker_locked(updated)
                self.store.save(updated)
                record = updated

        if expired_worker is not None:
            self._stop_lease_worker(expired_worker)
        if expired:
            raise InteractionError(
                "session_lease_expired",
                "The private session lease already expired and the session was stopped; "
                "create a new session instead.",
                details={"session_id": session_id},
            )
        return self.public_record(record)

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

    def screenshot_window(
        self,
        session_id: str,
        window_id: str,
        output: Path,
        *,
        overwrite: bool = False,
    ) -> dict[str, object]:
        record = self._require_running(session_id)
        capture = self.backend.screenshot_window(
            record,
            window_id,
            output,
            overwrite=overwrite,
        )
        return capture | {
            "target": {
                "kind": "agent-session",
                "session_id": session_id,
                "window_id": window_id,
            },
            "coordinate_space": "screenshot-pixels",
        }

    def launch_application(self, session_id: str, argv: tuple[str, ...]) -> dict[str, Any]:
        self._expire_session_if_due(session_id)
        with self.store.lock(session_id):
            record = self._ensure_lease_worker_locked(self._require_running_record(session_id))
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

    def set_window_fullscreen(
        self,
        session_id: str,
        window_id: str,
        fullscreen: bool,
    ) -> dict[str, Any]:
        return self._set_window_boolean_state(
            session_id,
            window_id,
            state="fullscreen",
            enabled=fullscreen,
        )

    def set_window_maximized(
        self,
        session_id: str,
        window_id: str,
        maximized: bool,
    ) -> dict[str, Any]:
        return self._set_window_boolean_state(
            session_id,
            window_id,
            state="maximized",
            enabled=maximized,
        )

    def _set_window_boolean_state(
        self,
        session_id: str,
        window_id: str,
        *,
        state: Literal["fullscreen", "maximized"],
        enabled: bool,
    ) -> dict[str, Any]:
        if not isinstance(enabled, bool):
            raise InteractionError(
                "invalid_window_state",
                f"{state.capitalize()} state must be explicitly true or false.",
                details={state: enabled},
            )
        record = self._require_window_management(session_id)
        result = (
            self.backend.set_window_fullscreen(record, window_id, enabled)
            if state == "fullscreen"
            else self.backend.set_window_maximized(record, window_id, enabled)
        )
        if result.get("verified") is not True:
            raise InteractionError(
                "window_state_unverified",
                f"The private KWin backend did not verify the requested {state} state.",
                details={
                    "session_id": session_id,
                    "window_id": window_id,
                    "requested_state": state,
                    "enabled": enabled,
                    "result": result,
                },
            )
        return {
            "target": {"kind": "agent-session", "session_id": session_id, "window_id": window_id},
            "action": f"set-{state}",
            state: enabled,
            "changed": result.get("changed") is True,
            "verified": True,
            "focus_changed": result.get("focus_changed") is True,
            "window": result["window"],
            "note": (
                f"{state.capitalize()} state is scoped to this private KWin session; "
                "minimized or conflicting fullscreen/maximized state must be restored first."
            ),
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
            if record.stopped_at is None:
                stopped, warnings = self._stop_record_locked(record)
            else:
                stopped, warnings = record, []
        if record.lease_worker is not None:
            warnings.extend(self._stop_lease_worker(record.lease_worker))
        self.store.clear_default_session_if(session_id)
        return self.public_record(stopped), warnings

    def public_record(self, record: SessionRecord) -> dict[str, Any]:
        if record.stopped_at is not None:
            status = "stopped"
        elif self.backend.is_running(record):
            status = "running"
        else:
            status = "degraded"
        return record.public_dict(status=status) | {
            "is_default": self.store.default_session_id() == record.session_id,
            "lease_monitor_active": (
                None
                if record.expires_at is None
                else self._lease_worker_is_active(record.lease_worker)
            ),
        }

    def run_session_lease_worker(self, session_id: str, worker_pid: int) -> None:
        """Watch one session record and stop its backend when the current lease expires."""
        while True:
            try:
                with self.store.lock(session_id):
                    record = self.store.load(session_id)
                    if (
                        record.stopped_at is not None
                        or record.lease_worker is None
                        or record.lease_worker.pid != worker_pid
                    ):
                        return
                    expiration = _parse_lease_expiration(record.expires_at)
                    if expiration is not None:
                        remaining = (expiration - datetime.now(UTC)).total_seconds()
                        if remaining <= 0:
                            self._stop_record_locked(record)
                            return
                    else:
                        remaining = _LEASE_WORKER_POLL_SECONDS
                time.sleep(min(_LEASE_WORKER_POLL_SECONDS, max(1.0, remaining)))
            except InteractionError as exc:
                if exc.code == "session_not_found":
                    return
                # A transient stop error should not disable the lease permanently.
                time.sleep(_LEASE_WORKER_POLL_SECONDS)

    def _expire_session_if_due(self, session_id: str) -> bool:
        worker: ProcessIdentity | None = None
        with self.store.lock(session_id):
            record = self.store.load(session_id)
            expiration = _parse_lease_expiration(record.expires_at)
            if (
                record.stopped_at is not None
                or expiration is None
                or expiration > datetime.now(UTC)
            ):
                return False
            self._stop_record_locked(record)
            worker = record.lease_worker
        if worker is not None:
            self._stop_lease_worker(worker)
        return True

    def _ensure_lease_worker(self, session_id: str) -> SessionRecord:
        with self.store.lock(session_id):
            record = self.store.load(session_id)
            return self._ensure_lease_worker_locked(record)

    def _ensure_lease_worker_locked(self, record: SessionRecord) -> SessionRecord:
        if record.stopped_at is not None or record.expires_at is None:
            return record
        if self._lease_worker_is_active(record.lease_worker):
            return record
        identity = self._lease_worker_starter(record.session_id)
        updated = replace(record, lease_worker=identity)
        self.store.save(updated)
        return updated

    def _lease_worker_is_active(self, identity: ProcessIdentity | None) -> bool:
        if identity is None:
            return False
        try:
            return process_matches(identity)
        except InteractionError:
            return False

    def _stop_lease_worker(self, identity: ProcessIdentity) -> list[str]:
        if identity.pid == os.getpid() or not self._lease_worker_is_active(identity):
            return []
        try:
            stopped = terminate_verified(identity)
        except InteractionError as exc:
            return [f"Could not stop the session lease monitor (PID {identity.pid}): {exc.message}"]
        if not stopped:
            return [f"The session lease monitor (PID {identity.pid}) did not stop."]
        process = self._lease_worker_processes.pop(identity.marker, None)
        if process is not None:
            process.wait(timeout=1.0)
        return []

    def _start_lease_worker(self, session_id: str) -> ProcessIdentity:
        validate_session_id(session_id)
        marker = f"session-lease-{session_id}-{uuid.uuid4().hex}"
        argv = (sys.executable, "-m", _LEASE_WORKER_MODULE, session_id)
        read_fd, write_fd = os.pipe()
        environment = os.environ | {
            "XDG_RUNTIME_DIR": str(self.paths.runtime_root.parent),
            "XDG_STATE_HOME": str(self.paths.state_root.parent),
            _PROCESS_MARKER_ENV: marker,
            _READY_FD_ENV: str(write_fd),
        }
        try:
            process = subprocess.Popen(
                argv,
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True,
                pass_fds=(write_fd,),
                start_new_session=True,
            )
        except OSError as exc:
            os.close(read_fd)
            os.close(write_fd)
            raise InteractionError(
                "session_lease_monitor_start_failed",
                "Could not start the private session lease monitor.",
                details={"session_id": session_id, "reason": str(exc)},
            ) from exc
        os.close(write_fd)
        try:
            ready, _, _ = select.select([read_fd], [], [], _LEASE_READY_TIMEOUT_SECONDS)
            line = os.read(read_fd, 128).decode("utf-8", errors="replace").strip() if ready else ""
        finally:
            os.close(read_fd)
        if line.startswith("READY:"):
            try:
                ready_pid = int(line.removeprefix("READY:"))
            except ValueError:
                ready_pid = -1
            if ready_pid == process.pid:
                try:
                    identity = process_identity(
                        ready_pid,
                        executable=sys.executable,
                        marker=marker,
                        command=argv,
                    )
                except InteractionError:
                    identity = None
                if identity is not None:
                    self._lease_worker_processes[marker] = process
                    return identity
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=1.0)
        raise InteractionError(
            "session_lease_monitor_start_failed",
            "The private session lease monitor did not become ready.",
            details={
                "session_id": session_id,
                "startup_timeout_seconds": _LEASE_READY_TIMEOUT_SECONDS,
                "reason": line or "worker exited without a readiness response",
            },
        )

    def _stop_record_locked(
        self,
        record: SessionRecord,
    ) -> tuple[SessionRecord, list[str]]:
        warnings = self.backend.stop(record)
        if self.backend.is_running(record):
            raise InteractionError(
                "session_stop_incomplete",
                "The private session still has a verified backend process running.",
                details={"session_id": record.session_id, "warnings": warnings},
            )
        stopped = replace(record, stopped_at=datetime.now(UTC).isoformat())
        self.store.save(stopped)
        self.store.clear_default_session_if(record.session_id)
        return stopped, warnings

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
        self._expire_session_if_due(session_id)
        record = self._require_running_record(session_id)
        record = self._ensure_lease_worker(session_id)
        expiration = _parse_lease_expiration(record.expires_at)
        if (
            record.stopped_at is not None
            or not self.backend.is_running(record)
            or (expiration is not None and expiration <= datetime.now(UTC))
        ):
            self._expire_session_if_due(session_id)
            raise InteractionError(
                "session_not_running",
                "The private KWin session is not running or its lease has expired.",
                details={"session_id": session_id},
            )
        return record

    def _require_running_record(self, session_id: str) -> SessionRecord:
        record = self.store.load(session_id)
        if record.stopped_at is not None or not self.backend.is_running(record):
            raise InteractionError(
                "session_not_running",
                "The private KWin session is not running.",
                details={"session_id": session_id},
            )
        expiration = _parse_lease_expiration(record.expires_at)
        if expiration is not None and expiration <= datetime.now(UTC):
            self._stop_record_locked(record)
            raise InteractionError(
                "session_not_running",
                "The private KWin session lease expired.",
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


def _validate_lease_duration(duration_seconds: int) -> None:
    if (
        isinstance(duration_seconds, bool)
        or not isinstance(duration_seconds, int)
        or (duration_seconds != -1 and duration_seconds < 1)
    ):
        raise InteractionError(
            "invalid_session_lease",
            "Session lease duration must be a positive number of seconds, or -1 for indefinite.",
            details={"duration_seconds": duration_seconds},
        )


def _lease_expiration(
    duration_seconds: int,
    *,
    base: datetime | None = None,
) -> str | None:
    if duration_seconds == -1:
        return None
    try:
        expiration = (base or datetime.now(UTC)) + timedelta(seconds=duration_seconds)
    except OverflowError as exc:
        raise InteractionError(
            "invalid_session_lease",
            "Session lease duration is too large to represent.",
            details={"duration_seconds": duration_seconds},
        ) from exc
    return expiration.astimezone(UTC).isoformat()


def _parse_lease_expiration(value: str | None) -> datetime | None:
    if value is None:
        return None
    try:
        expiration = datetime.fromisoformat(value)
        if expiration.tzinfo is None or expiration.utcoffset() is None:
            raise ValueError("lease expiration must include a timezone")
    except (OverflowError, ValueError) as exc:
        raise InteractionError(
            "session_state_invalid",
            "The saved session lease expiration is invalid.",
            details={"expires_at": value, "reason": str(exc)},
        ) from exc
    return expiration.astimezone(UTC)


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

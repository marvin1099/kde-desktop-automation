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
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager, suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from kde_desktop_automation.errors import InteractionError
from kde_desktop_automation.models import ProcessIdentity
from kde_desktop_automation.process import (
    find_executable,
    pidfd_supported,
    process_identity,
    process_matches,
    process_start_ticks,
    process_state,
    processes_with_environment_marker,
    terminate_verified,
)

PowerInhibitor = Literal[
    "screen-lock",
    "power-management",
    "sleep",
    "idle",
    "shutdown",
    "handle-power-key",
    "handle-suspend-key",
    "handle-hibernate-key",
    "handle-lid-switch",
]

POWER_INHIBITORS = frozenset(
    {
        "screen-lock",
        "power-management",
        "sleep",
        "idle",
        "shutdown",
        "handle-power-key",
        "handle-suspend-key",
        "handle-hibernate-key",
        "handle-lid-switch",
    }
)
DBUS_INHIBITORS = frozenset({"screen-lock", "power-management"})
SYSTEMD_INHIBITORS = POWER_INHIBITORS - DBUS_INHIBITORS
MAX_POWER_INHIBITOR_SECONDS = 86_400
DEFAULT_POWER_INHIBITOR_SECONDS = 300
_READY_TIMEOUT_SECONDS = 5.0
_PROCESS_MARKER_ENV = "KDE_DESKTOP_AUTOMATION_PROCESS_MARKER"
_READY_FD_ENV = "KDE_DESKTOP_AUTOMATION_READY_FD"
_INHIBITOR_ID = re.compile(r"^[0-9a-f]{32}$")
_WORKER_MODULE = "kde_desktop_automation.backends.power_inhibitor_worker"
_DESCRIPTIONS = {
    "screen-lock": "Prevent the KDE screen saver or automatic screen lock",
    "power-management": "Inhibit KDE power-management actions",
    "sleep": "Block system sleep, suspend, and hibernation",
    "idle": "Block systemd idle actions",
    "shutdown": "Block system shutdown",
    "handle-power-key": "Block systemd handling of the power key",
    "handle-suspend-key": "Block systemd handling of the suspend key",
    "handle-hibernate-key": "Block systemd handling of the hibernate key",
    "handle-lid-switch": "Block systemd handling of the laptop lid switch",
}


class PowerInhibitorManager:
    """Manage live-desktop inhibitors with private, cross-process lifecycle records."""

    def __init__(self, root: Path, environ: Mapping[str, str]) -> None:
        self.root = root
        self.environ = dict(environ)
        _validate_root(root)

    def inhibit(
        self,
        inhibitor: str,
        *,
        duration_seconds: int = DEFAULT_POWER_INHIBITOR_SECONDS,
        reason: str | None = None,
    ) -> dict[str, Any]:
        _validate_inhibitor(inhibitor)
        _validate_duration(duration_seconds)
        normalized_reason = _normalize_reason(reason)
        if not pidfd_supported():
            raise InteractionError(
                "safe_process_control_unavailable",
                "This Linux runtime lacks pidfd support; refusing to start an inhibitor "
                "that could not be safely released.",
            )

        systemd_executable: str | None = None
        if inhibitor in SYSTEMD_INHIBITORS:
            systemd_executable = find_executable("systemd-inhibit")
            if systemd_executable is None:
                raise InteractionError(
                    "power_inhibitor_unavailable",
                    "The systemd-inhibit executable is required for this inhibitor.",
                    details={"inhibitor": inhibitor, "executable": "systemd-inhibit"},
                )
        elif not self.environ.get("DBUS_SESSION_BUS_ADDRESS"):
            raise InteractionError(
                "power_inhibitor_unavailable",
                "A live session D-Bus address is required for screen-lock inhibition.",
                details={"inhibitor": inhibitor},
            )

        with self._locked():
            inhibitor_id = uuid4().hex
            marker = f"power-inhibitor-{inhibitor_id}"
            try:
                worker_argv: tuple[str, ...]
                if inhibitor in DBUS_INHIBITORS:
                    worker_argv = (
                        sys.executable,
                        "-m",
                        _WORKER_MODULE,
                        inhibitor,
                        str(duration_seconds),
                        f"--reason={normalized_reason}",
                    )
                    process, ready_pid = self._spawn_and_wait(worker_argv, marker)
                    owner = process_identity(
                        ready_pid,
                        executable=sys.executable,
                        marker=marker,
                        command=worker_argv,
                    )
                    supervisor = None
                else:
                    assert systemd_executable is not None
                    worker_argv = (
                        sys.executable,
                        "-m",
                        _WORKER_MODULE,
                        "hold",
                        str(duration_seconds),
                    )
                    wrapper_argv = (
                        systemd_executable,
                        f"--what={inhibitor}",
                        f"--who=KDE Desktop Automation ({inhibitor_id})",
                        (f"--why={normalized_reason} [kde-desktop-automation:{inhibitor_id}]"),
                        "--mode=block",
                        "--",
                        *worker_argv,
                    )
                    process, ready_pid = self._spawn_and_wait(wrapper_argv, marker)
                    supervisor = process_identity(
                        process.pid,
                        executable=systemd_executable,
                        marker=marker,
                        command=wrapper_argv,
                    )
                    owner = process_identity(
                        ready_pid,
                        executable=sys.executable,
                        marker=marker,
                        command=worker_argv,
                    )

                created_at = datetime.now(UTC)
                expires_at = (
                    None
                    if duration_seconds == -1
                    else (created_at + timedelta(seconds=duration_seconds)).isoformat()
                )
                record = {
                    "version": 1,
                    "id": inhibitor_id,
                    "inhibitor": inhibitor,
                    "description": _DESCRIPTIONS[inhibitor],
                    "duration_seconds": duration_seconds,
                    "expires_at": expires_at,
                    "reason": normalized_reason,
                    "created_at": created_at.isoformat(),
                    "owner_process": owner.to_dict(),
                    "supervisor_process": None if supervisor is None else supervisor.to_dict(),
                }
                if not self._identity_is_running(owner):
                    raise InteractionError(
                        "power_inhibitor_start_failed",
                        "The inhibitor worker exited before it could be recorded.",
                        details={"inhibitor": inhibitor},
                    )
                self._save(inhibitor_id, record)
            except InteractionError as exc:
                cleanup_errors = self._cleanup_marker(marker)
                if cleanup_errors:
                    raise InteractionError(
                        "power_inhibitor_cleanup_failed",
                        "The inhibitor could not be fully started or tracked, and cleanup "
                        "could not stop every worker.",
                        details={
                            "start_error": exc.as_dict(),
                            "cleanup_errors": cleanup_errors,
                        },
                    ) from exc
                raise
            except (OSError, ValueError, TypeError, KeyError) as exc:
                cleanup_errors = self._cleanup_marker(marker)
                if cleanup_errors:
                    raise InteractionError(
                        "power_inhibitor_cleanup_failed",
                        "The inhibitor could not be fully started or tracked, and cleanup "
                        "could not stop every worker.",
                        details={
                            "start_error": str(exc),
                            "cleanup_errors": cleanup_errors,
                        },
                    ) from exc
                raise InteractionError(
                    "power_inhibitor_start_failed",
                    "Could not start or track the requested power inhibitor.",
                    details={"inhibitor": inhibitor, "reason": str(exc)},
                ) from exc

        return _public_record(record)

    def list(self) -> list[dict[str, Any]]:
        with self._locked():
            inhibitors: list[dict[str, Any]] = []
            for path in sorted(self.root.glob("*.json")):
                inhibitor_id = path.stem
                if not _INHIBITOR_ID.fullmatch(inhibitor_id):
                    continue
                record = self._load(inhibitor_id)
                owner, supervisor = _record_processes(record)
                if not self._identity_is_running(owner) or (
                    supervisor is not None and not self._identity_is_running(supervisor)
                ):
                    self._stop_record_processes(owner, supervisor)
                    path.unlink(missing_ok=True)
                    continue
                inhibitors.append(_public_record(record))
        return inhibitors

    def release(self, inhibitor_id: str) -> dict[str, Any]:
        _validate_id(inhibitor_id)
        with self._locked():
            record = self._load(inhibitor_id)
            owner, supervisor = _record_processes(record)
            self._stop_record_processes(owner, supervisor)
            self._record_path(inhibitor_id).unlink(missing_ok=True)
        return {
            "id": inhibitor_id,
            "released": True,
            "inhibitor": record["inhibitor"],
            "reason": record["reason"],
        }

    def _spawn_and_wait(
        self,
        argv: Sequence[str],
        marker: str,
    ) -> tuple[subprocess.Popen[bytes], int]:
        read_fd, write_fd = os.pipe()
        environment = self.environ | {
            _PROCESS_MARKER_ENV: marker,
            _READY_FD_ENV: str(write_fd),
        }
        try:
            process = subprocess.Popen(
                tuple(argv),
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
                "power_inhibitor_start_failed",
                "Could not launch the power-inhibitor worker.",
                details={"executable": argv[0], "reason": str(exc)},
            ) from exc
        os.close(write_fd)

        try:
            ready, _, _ = select.select([read_fd], [], [], _READY_TIMEOUT_SECONDS)
            line = os.read(read_fd, 128).decode("utf-8", errors="replace").strip() if ready else ""
        finally:
            os.close(read_fd)

        if line.startswith("READY:"):
            try:
                ready_pid = int(line.removeprefix("READY:"))
            except ValueError:
                ready_pid = -1
            if ready_pid > 0:
                return process, ready_pid

        if process.poll() is None:
            with suppress(OSError, subprocess.TimeoutExpired):
                process.terminate()
                process.wait(timeout=1.0)
        raise InteractionError(
            "power_inhibitor_start_failed",
            "The inhibitor worker did not acquire its inhibitor before the startup deadline.",
            details={
                "executable": argv[0],
                "startup_timeout_seconds": _READY_TIMEOUT_SECONDS,
                "reason": line or "worker exited without a readiness response",
            },
        )

    def _cleanup_marker(self, marker: str) -> Sequence[dict[str, Any]]:
        errors: list[dict[str, Any]] = []
        for identity in sorted(
            processes_with_environment_marker(marker),
            key=lambda process: process.pid,
            reverse=True,
        ):
            try:
                if not terminate_verified(identity):
                    errors.append({"pid": identity.pid, "error": "process did not exit"})
            except InteractionError as exc:
                errors.append({"pid": identity.pid, "error": exc.as_dict()})
        return errors

    def _identity_is_running(self, identity: ProcessIdentity) -> bool:
        start_ticks = process_start_ticks(identity.pid)
        if start_ticks is None or start_ticks != identity.start_ticks:
            return False
        if process_state(identity.pid) == "Z":
            return False
        if process_matches(identity):
            return True
        raise InteractionError(
            "power_inhibitor_identity_mismatch",
            "A recorded inhibitor PID is still present but its process identity "
            "cannot be verified.",
            details={"pid": identity.pid},
        )

    def _stop_record_processes(
        self,
        owner: ProcessIdentity,
        supervisor: ProcessIdentity | None,
    ) -> None:
        for identity in (owner, supervisor):
            if identity is None or not self._identity_is_running(identity):
                continue
            if not terminate_verified(identity):
                raise InteractionError(
                    "power_inhibitor_release_failed",
                    "The verified inhibitor process did not stop; its record was kept.",
                    details={"pid": identity.pid},
                )

    @contextmanager
    def _locked(self) -> Iterator[None]:
        _validate_root(self.root)
        lock_path = self.root / ".lock"
        flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor: int | None = None
        try:
            descriptor = os.open(lock_path, flags, 0o600)
            metadata = os.fstat(descriptor)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_uid != os.getuid()
                or stat.S_IMODE(metadata.st_mode) & 0o077
            ):
                raise InteractionError(
                    "power_inhibitor_store_unsafe",
                    "Refusing to use an unsafe inhibitor-store lock file.",
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
                "power_inhibitor_store_unavailable",
                "Could not lock the power-inhibitor registry.",
                details={"path": str(lock_path), "reason": str(exc)},
            ) from exc
        try:
            yield
        finally:
            with suppress(OSError):
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    def _record_path(self, inhibitor_id: str) -> Path:
        _validate_id(inhibitor_id)
        return self.root / f"{inhibitor_id}.json"

    def _save(self, inhibitor_id: str, record: dict[str, Any]) -> None:
        destination = self._record_path(inhibitor_id)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{inhibitor_id}-",
            suffix=".tmp",
            dir=self.root,
        )
        temporary_path = Path(temporary_name)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as state_file:
                json.dump(record, state_file, indent=2, sort_keys=True)
                state_file.write("\n")
                state_file.flush()
                os.fsync(state_file.fileno())
            os.replace(temporary_path, destination)
            destination.chmod(0o600)
        except OSError as exc:
            temporary_path.unlink(missing_ok=True)
            raise InteractionError(
                "power_inhibitor_store_write_failed",
                "Could not persist the inhibitor process identity.",
                details={"path": str(destination), "reason": str(exc)},
            ) from exc

    def _load(self, inhibitor_id: str) -> dict[str, Any]:
        path = self._record_path(inhibitor_id)
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor: int | None = None
        try:
            descriptor = os.open(path, flags)
            metadata = os.fstat(descriptor)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_uid != os.getuid()
                or stat.S_IMODE(metadata.st_mode) & 0o077
            ):
                os.close(descriptor)
                descriptor = None
                raise InteractionError(
                    "power_inhibitor_store_unsafe",
                    "Refusing to read an unsafe inhibitor registry record.",
                    details={"path": str(path)},
                )
            with os.fdopen(descriptor, "r", encoding="utf-8") as state_file:
                descriptor = None
                value = json.load(state_file)
        except InteractionError:
            raise
        except FileNotFoundError as exc:
            raise InteractionError(
                "power_inhibitor_not_found",
                "No active power inhibitor has that ID.",
                details={"id": inhibitor_id},
            ) from exc
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise InteractionError(
                "power_inhibitor_store_invalid",
                "The inhibitor registry record is unreadable or invalid.",
                details={"path": str(path), "reason": str(exc)},
            ) from exc
        finally:
            if descriptor is not None:
                os.close(descriptor)
        if (
            not isinstance(value, dict)
            or value.get("version") != 1
            or value.get("id") != inhibitor_id
        ):
            raise InteractionError(
                "power_inhibitor_store_invalid",
                "The inhibitor registry record does not match its requested ID.",
                details={"path": str(path)},
            )
        return value


def _validate_root(root: Path) -> None:
    try:
        metadata = root.lstat()
    except OSError as exc:
        raise InteractionError(
            "power_inhibitor_store_unavailable",
            "The private runtime directory for power inhibitors is unavailable.",
            details={"path": str(root), "reason": str(exc)},
        ) from exc
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or stat.S_IMODE(metadata.st_mode) & 0o077
    ):
        raise InteractionError(
            "power_inhibitor_store_unsafe",
            "The power-inhibitor registry must be a private directory owned by this user.",
            details={"path": str(root)},
        )


def _validate_inhibitor(inhibitor: str) -> None:
    if not isinstance(inhibitor, str) or inhibitor not in POWER_INHIBITORS:
        raise InteractionError(
            "invalid_power_inhibitor",
            "Choose a supported typed power inhibitor.",
            details={"inhibitor": inhibitor, "available": sorted(POWER_INHIBITORS)},
        )


def _validate_duration(duration_seconds: int) -> None:
    if (
        isinstance(duration_seconds, bool)
        or not isinstance(duration_seconds, int)
        or (duration_seconds != -1 and not 1 <= duration_seconds <= MAX_POWER_INHIBITOR_SECONDS)
    ):
        raise InteractionError(
            "invalid_power_inhibitor_duration",
            "Duration must be -1 for indefinite or between 1 and 86400 seconds.",
            details={"duration_seconds": duration_seconds},
        )


def _normalize_reason(reason: str | None) -> str:
    if reason is None:
        return "Temporary request from KDE Desktop Automation"
    if not isinstance(reason, str) or not reason.strip() or len(reason) > 512 or "\0" in reason:
        raise InteractionError(
            "invalid_power_inhibitor_reason",
            "Reason must be a non-empty string of at most 512 characters without NUL bytes.",
        )
    return reason.strip()


def _validate_id(inhibitor_id: str) -> None:
    if not isinstance(inhibitor_id, str) or not _INHIBITOR_ID.fullmatch(inhibitor_id):
        raise InteractionError(
            "invalid_power_inhibitor_id",
            "Inhibitor ID must be a 32-character hexadecimal ID returned by the start operation.",
            details={"id": inhibitor_id},
        )


def _record_processes(
    record: dict[str, Any],
) -> tuple[ProcessIdentity, ProcessIdentity | None]:
    try:
        owner = ProcessIdentity.from_dict(record["owner_process"])
        supervisor_data = record.get("supervisor_process")
        supervisor = None if supervisor_data is None else ProcessIdentity.from_dict(supervisor_data)
    except (KeyError, TypeError, ValueError) as exc:
        raise InteractionError(
            "power_inhibitor_store_invalid",
            "The inhibitor registry record has an invalid process identity.",
            details={"id": record.get("id"), "reason": str(exc)},
        ) from exc
    if record.get("inhibitor") not in POWER_INHIBITORS:
        raise InteractionError(
            "power_inhibitor_store_invalid",
            "The inhibitor registry record contains an unknown inhibitor type.",
            details={"id": record.get("id")},
        )
    return owner, supervisor


def _public_record(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": record["id"],
        "inhibitor": record["inhibitor"],
        "description": record["description"],
        "duration_seconds": record["duration_seconds"],
        "expires_at": record["expires_at"],
        "reason": record["reason"],
        "created_at": record["created_at"],
        "active": True,
    }


def power_inhibitor_capabilities(*, live_target: bool) -> dict[str, bool]:
    safe_process_control = live_target and pidfd_supported()
    systemd_available = safe_process_control and find_executable("systemd-inhibit") is not None
    return {
        "power.inhibit": systemd_available,
        "power.screen_lock_inhibit": safe_process_control,
        "power.kde_power_management_inhibit": safe_process_control,
        "power.inhibitors": safe_process_control,
        "power.inhibitor_release": safe_process_control,
    }

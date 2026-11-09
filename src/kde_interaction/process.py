from __future__ import annotations

import ctypes
import ctypes.util
import os
import select
import shutil
import signal
import subprocess
from collections.abc import Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from kde_interaction.errors import InteractionError
from kde_interaction.models import ProcessIdentity

_LIBC: Any = None
try:
    _LIBC = ctypes.CDLL(ctypes.util.find_library("c") or None, use_errno=True)
except OSError:  # pragma: no cover - unusual non-glibc/minimal Linux environments
    _LIBC = None

_LIBC_PIDFD_OPEN = getattr(_LIBC, "pidfd_open", None)
_LIBC_PIDFD_SEND_SIGNAL = getattr(_LIBC, "pidfd_send_signal", None)
if _LIBC_PIDFD_OPEN is not None:
    _LIBC_PIDFD_OPEN.argtypes = [ctypes.c_int, ctypes.c_uint]
    _LIBC_PIDFD_OPEN.restype = ctypes.c_int
if _LIBC_PIDFD_SEND_SIGNAL is not None:
    _LIBC_PIDFD_SEND_SIGNAL.argtypes = [
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_void_p,
        ctypes.c_uint,
    ]
    _LIBC_PIDFD_SEND_SIGNAL.restype = ctypes.c_int


@dataclass(frozen=True, slots=True)
class CommandResult:
    argv: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str


def find_executable(name: str) -> str | None:
    return shutil.which(name)


def require_executable(name: str) -> str:
    executable = find_executable(name)
    if executable is None:
        raise InteractionError(
            "backend_unavailable",
            f"Required executable '{name}' was not found in PATH.",
            details={"executable": name},
        )
    return str(Path(executable).resolve())


def run_command(
    argv: Sequence[str],
    *,
    env: Mapping[str, str],
    timeout: float = 5.0,
) -> CommandResult:
    command = tuple(str(part) for part in argv)
    try:
        result = subprocess.run(
            command,
            env=dict(env),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            check=False,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError as exc:
        raise InteractionError(
            "backend_unavailable",
            f"Required executable '{command[0]}' was not found.",
            details={"executable": command[0]},
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise InteractionError(
            "backend_timeout",
            f"Command '{command[0]}' did not finish before the timeout.",
            details={"executable": command[0], "timeout_seconds": timeout},
        ) from exc
    except OSError as exc:
        raise InteractionError(
            "backend_execution_failed",
            f"Could not execute '{command[0]}'.",
            details={"executable": command[0], "reason": str(exc)},
        ) from exc

    return CommandResult(
        argv=command,
        returncode=result.returncode,
        stdout=result.stdout,
        stderr=result.stderr,
    )


def require_success(result: CommandResult) -> CommandResult:
    if result.returncode != 0:
        raise InteractionError(
            "backend_command_failed",
            f"Command '{result.argv[0]}' returned a failure status.",
            details={
                "executable": result.argv[0],
                "returncode": result.returncode,
                "stderr": result.stderr.strip()[-2000:],
            },
        )
    return result


def process_identity(
    pid: int,
    *,
    executable: str,
    marker: str,
    command: Sequence[str],
) -> ProcessIdentity:
    start_ticks = process_start_ticks(pid)
    if start_ticks is None:
        raise InteractionError(
            "process_start_failed",
            "The started backend process exited before it could be recorded.",
            details={"pid": pid, "executable": executable},
        )
    return ProcessIdentity(
        pid=pid,
        start_ticks=start_ticks,
        executable=str(Path(executable).resolve()),
        marker=marker,
        command=tuple(str(part) for part in command),
    )


def process_start_ticks(pid: int) -> int | None:
    """Read Linux /proc start ticks, which distinguishes a reused PID."""
    try:
        stat_text = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
        fields_after_comm = stat_text[stat_text.rfind(")") + 2 :].split()
        return int(fields_after_comm[19])
    except FileNotFoundError:
        return None
    except PermissionError as exc:
        raise InteractionError(
            "process_identity_unavailable",
            "Permission was denied while verifying a private session process.",
            details={"pid": pid},
        ) from exc
    except OSError as exc:
        raise InteractionError(
            "process_identity_unavailable",
            "Could not read the process identity needed for safe session cleanup.",
            details={"pid": pid, "reason": str(exc)},
        ) from exc
    except (ValueError, IndexError) as exc:
        raise InteractionError(
            "process_identity_invalid",
            "The operating system returned invalid process identity data.",
            details={"pid": pid},
        ) from exc


def process_state(pid: int) -> str | None:
    try:
        stat_text = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
        fields_after_comm = stat_text[stat_text.rfind(")") + 2 :].split()
        return fields_after_comm[0]
    except (OSError, IndexError):
        return None


def process_matches(identity: ProcessIdentity) -> bool:
    if process_start_ticks(identity.pid) != identity.start_ticks:
        return False
    try:
        actual_executable = os.readlink(f"/proc/{identity.pid}/exe")
        actual_command = Path(f"/proc/{identity.pid}/cmdline").read_bytes().decode().split("\0")
    except PermissionError:
        # Some compositors make /proc/<pid>/exe unreadable after startup. The
        # immutable start time plus exact argv[0] and our random session marker
        # still identify the process without relaxing PID-reuse protection.
        try:
            actual_command = Path(f"/proc/{identity.pid}/cmdline").read_bytes().decode().split("\0")
        except OSError:
            return False
        actual_executable = None
    except OSError:
        return False

    actual_command = [part for part in actual_command if part]
    if not actual_command:
        return False
    if actual_executable is not None:
        if actual_executable != identity.executable:
            return False
    elif not identity.command or actual_command[0] != identity.command[0]:
        return False
    return any(identity.marker in part for part in actual_command)


def terminate_verified(identity: ProcessIdentity, *, timeout: float = 3.0) -> bool:
    """Stop a verified process through a pidfd, avoiding PID-reuse races."""
    if not pidfd_supported():
        raise InteractionError(
            "safe_process_control_unavailable",
            "This Linux runtime lacks pidfd support; refusing race-prone PID signalling.",
        )

    try:
        pidfd = _pidfd_open(identity.pid)
    except ProcessLookupError:
        return True
    except PermissionError as exc:
        raise InteractionError(
            "process_permission_denied",
            "Permission was denied while opening a private session process handle.",
            details={"pid": identity.pid},
        ) from exc
    except OSError as exc:
        raise InteractionError(
            "safe_process_control_failed",
            "Could not open a race-safe handle for the private session process.",
            details={"pid": identity.pid, "reason": str(exc)},
        ) from exc

    try:
        if process_start_ticks(identity.pid) != identity.start_ticks:
            return True
        if process_state(identity.pid) == "Z":
            return True
        if not process_matches(identity):
            if process_start_ticks(identity.pid) != identity.start_ticks:
                return True
            raise InteractionError(
                "process_identity_mismatch",
                "The recorded process is still present but its identity could not be verified.",
                details={"pid": identity.pid},
            )

        try:
            _pidfd_send_signal(pidfd, signal.SIGTERM)
        except ProcessLookupError:
            return True
        except PermissionError as exc:
            raise InteractionError(
                "process_permission_denied",
                "Permission was denied while stopping a private session backend.",
                details={"pid": identity.pid},
            ) from exc
        except OSError as exc:
            raise InteractionError(
                "safe_process_signal_failed",
                "Could not signal the verified private session process.",
                details={"pid": identity.pid, "reason": str(exc)},
            ) from exc
        if _pidfd_exited(pidfd, timeout):
            return True

        try:
            _pidfd_send_signal(pidfd, signal.SIGKILL)
        except ProcessLookupError:
            return True
        except PermissionError as exc:
            raise InteractionError(
                "process_permission_denied",
                "Permission was denied while force-stopping a private session backend.",
                details={"pid": identity.pid},
            ) from exc
        except OSError as exc:
            raise InteractionError(
                "safe_process_signal_failed",
                "Could not force-stop the verified private session process.",
                details={"pid": identity.pid, "reason": str(exc)},
            ) from exc
        return _pidfd_exited(pidfd, timeout)
    finally:
        os.close(pidfd)


def pidfd_supported() -> bool:
    symbols_available = (
        callable(getattr(os, "pidfd_open", None)) or _LIBC_PIDFD_OPEN is not None
    ) and (
        callable(getattr(signal, "pidfd_send_signal", None)) or _LIBC_PIDFD_SEND_SIGNAL is not None
    )
    if not symbols_available:
        return False

    try:
        pidfd = _pidfd_open(os.getpid())
    except OSError:
        return False
    try:
        _pidfd_send_signal(pidfd, 0)
    except OSError:
        return False
    finally:
        with suppress(OSError):
            os.close(pidfd)
    return True


def _pidfd_open(pid: int) -> int:
    python_function = getattr(os, "pidfd_open", None)
    if callable(python_function):
        return cast(int, python_function(pid, 0))
    if _LIBC_PIDFD_OPEN is None:
        raise OSError("libc does not expose pidfd_open")
    result = _LIBC_PIDFD_OPEN(pid, 0)
    if result < 0:
        _raise_libc_error("pidfd_open")
    return int(result)


def _pidfd_send_signal(pidfd: int, sig: int) -> None:
    python_function = getattr(signal, "pidfd_send_signal", None)
    if callable(python_function):
        python_function(pidfd, sig, None, 0)
        return
    if _LIBC_PIDFD_SEND_SIGNAL is None:
        raise OSError("libc does not expose pidfd_send_signal")
    result = _LIBC_PIDFD_SEND_SIGNAL(pidfd, sig, None, 0)
    if result < 0:
        _raise_libc_error("pidfd_send_signal")


def _raise_libc_error(function: str) -> None:
    error_number = ctypes.get_errno() or 5
    raise OSError(error_number, f"{function} failed: {os.strerror(error_number)}")


def _pidfd_exited(pidfd: int, timeout: float) -> bool:
    poller = select.poll()
    poller.register(pidfd, select.POLLIN | select.POLLHUP | select.POLLERR)
    timeout_ms = max(0, int(timeout * 1000))
    return bool(poller.poll(timeout_ms))

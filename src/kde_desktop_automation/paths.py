from __future__ import annotations

import os
import re
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from kde_desktop_automation.errors import InteractionError

_SESSION_ID = re.compile(r"^agent-[a-f0-9]{12}$")


def validate_session_id(session_id: str) -> None:
    if not _SESSION_ID.fullmatch(session_id):
        raise InteractionError(
            "invalid_session_id",
            "The session ID is not a managed agent session ID.",
        )


@dataclass(frozen=True, slots=True)
class SessionPaths:
    session_id: str
    runtime_dir: Path
    home_dir: Path
    record_file: Path
    log_file: Path


@dataclass(frozen=True, slots=True)
class RuntimePaths:
    runtime_root: Path
    state_root: Path
    sessions_root: Path
    homes_root: Path
    power_inhibitors_root: Path

    @classmethod
    def from_environment(cls, environ: Mapping[str, str] | None = None) -> RuntimePaths:
        values = os.environ if environ is None else environ
        runtime_value = values.get("XDG_RUNTIME_DIR")
        if not runtime_value:
            raise InteractionError(
                "runtime_dir_unavailable",
                "XDG_RUNTIME_DIR is required to create a private Wayland session.",
            )

        runtime_dir = Path(runtime_value)
        if not runtime_dir.is_absolute():
            raise InteractionError(
                "invalid_runtime_dir",
                "XDG_RUNTIME_DIR must be an absolute path.",
            )
        try:
            runtime_stat = runtime_dir.lstat()
        except OSError as exc:
            raise InteractionError(
                "runtime_dir_unavailable",
                "XDG_RUNTIME_DIR does not exist or cannot be accessed.",
                details={"path": str(runtime_dir)},
            ) from exc

        if (
            not stat.S_ISDIR(runtime_stat.st_mode)
            or stat.S_ISLNK(runtime_stat.st_mode)
            or runtime_stat.st_uid != os.getuid()
        ):
            raise InteractionError(
                "unsafe_runtime_dir",
                "XDG_RUNTIME_DIR must be a directory owned by the current user.",
                details={"path": str(runtime_dir)},
            )
        if stat.S_IMODE(runtime_stat.st_mode) & 0o077:
            raise InteractionError(
                "unsafe_runtime_dir",
                "XDG_RUNTIME_DIR must not be accessible by group or other users.",
                details={"path": str(runtime_dir)},
            )

        state_value = values.get("XDG_STATE_HOME")
        if state_value:
            state_home = Path(state_value)
            if not state_home.is_absolute():
                raise InteractionError(
                    "invalid_state_dir",
                    "XDG_STATE_HOME must be an absolute path.",
                )
        else:
            state_home = Path.home() / ".local" / "state"

        app_runtime = runtime_dir / "kde-desktop-automation"
        app_state = state_home / "kde-desktop-automation"
        runtime_sessions = app_runtime / "sessions"
        power_inhibitors = app_runtime / "power-inhibitors"
        state_sessions = app_state / "sessions"
        homes = app_state / "homes"
        for directory in (
            app_runtime,
            runtime_sessions,
            power_inhibitors,
            app_state,
            state_sessions,
            homes,
        ):
            _ensure_private_directory(directory)

        return cls(
            runtime_root=app_runtime,
            state_root=app_state,
            sessions_root=state_sessions,
            homes_root=homes,
            power_inhibitors_root=power_inhibitors,
        )

    def for_session(self, session_id: str, *, create: bool = False) -> SessionPaths:
        validate_session_id(session_id)

        runtime_dir = self.runtime_root / "sessions" / session_id
        home_dir = self.homes_root / session_id
        record_file = self.sessions_root / f"{session_id}.json"
        log_file = self.sessions_root / f"{session_id}.log"
        if create:
            _ensure_private_directory(runtime_dir)
            _ensure_private_directory(home_dir)
            for xdg_dir in ("config", "cache", "data", "state"):
                _ensure_private_directory(home_dir / xdg_dir)
        return SessionPaths(
            session_id=session_id,
            runtime_dir=runtime_dir,
            home_dir=home_dir,
            record_file=record_file,
            log_file=log_file,
        )


def _ensure_private_directory(path: Path) -> None:
    try:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        path_stat = path.lstat()
    except OSError as exc:
        raise InteractionError(
            "directory_unavailable",
            "Could not create a private application directory.",
            details={"path": str(path), "reason": str(exc)},
        ) from exc

    if not stat.S_ISDIR(path_stat.st_mode) or stat.S_ISLNK(path_stat.st_mode):
        raise InteractionError(
            "unsafe_directory",
            "A private application path exists but is not a real directory.",
            details={"path": str(path)},
        )
    if path_stat.st_uid != os.getuid():
        raise InteractionError(
            "unsafe_directory_owner",
            "A private application directory is not owned by the current user.",
            details={"path": str(path)},
        )
    try:
        path.chmod(0o700)
    except OSError as exc:
        raise InteractionError(
            "directory_permissions",
            "Could not restrict permissions on an application directory.",
            details={"path": str(path), "reason": str(exc)},
        ) from exc

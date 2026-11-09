from __future__ import annotations

import json
import os
import re
import tempfile
import uuid
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from kde_interaction.backends.displays import DisplayInfo
from kde_interaction.backends.kwin_virtual import KWinVirtualBackend
from kde_interaction.errors import InteractionError
from kde_interaction.models import SessionRecord
from kde_interaction.paths import RuntimePaths, validate_session_id

_NAME = re.compile(r"^[\w .-]{1,64}$", flags=re.UNICODE)
_MAX_OUTPUT_PIXELS = 16_777_216


class SessionBackend(Protocol):
    name: str

    def start(self, session_id: str, name: str, width: int, height: int) -> SessionRecord: ...

    def is_running(self, session: SessionRecord) -> bool: ...

    def stop(self, session: SessionRecord) -> list[str]: ...

    def displays(self, session: SessionRecord) -> list[DisplayInfo]: ...

    def screenshot(
        self,
        session: SessionRecord,
        output: Path,
        *,
        overwrite: bool,
    ) -> dict[str, object]: ...


class SessionStore:
    """Atomic, user-private persistence for managed session metadata."""

    def __init__(self, paths: RuntimePaths) -> None:
        self.root = paths.sessions_root

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


class SessionManager:
    def __init__(self, paths: RuntimePaths, backend: SessionBackend | None = None) -> None:
        self.paths = paths
        self.backend = backend or KWinVirtualBackend(paths)
        self.store = SessionStore(paths)

    def create_agent_session(self, name: str, width: int, height: int) -> SessionRecord:
        _validate_request(name, width, height)
        name = name.strip()
        session_id = f"agent-{uuid.uuid4().hex[:12]}"
        record = self.backend.start(session_id, name, width, height)
        try:
            self.store.save(record)
        except Exception:
            self.backend.stop(record)
            raise
        return record

    def list_sessions(self) -> list[dict[str, Any]]:
        return [self.public_record(record) for record in self.store.list()]

    def get_session(self, session_id: str) -> dict[str, Any]:
        return self.public_record(self.store.load(session_id))

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
        return self.backend.screenshot(record, output, overwrite=overwrite)

    def stop(self, session_id: str) -> tuple[dict[str, Any], list[str]]:
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
        return record.public_dict(status=status)

    def _require_running(self, session_id: str) -> SessionRecord:
        record = self.store.load(session_id)
        if record.stopped_at is not None or not self.backend.is_running(record):
            raise InteractionError(
                "session_not_running",
                "The private KWin session is not running.",
                details={"session_id": session_id},
            )
        return record


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

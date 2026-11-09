from __future__ import annotations

import os
from pathlib import Path

import pytest

from kde_interaction.backends.displays import DisplayInfo
from kde_interaction.errors import InteractionError
from kde_interaction.models import ProcessIdentity, SessionRecord
from kde_interaction.paths import RuntimePaths
from kde_interaction.sessions import SessionManager


class FakeBackend:
    name = "fake"

    def __init__(self) -> None:
        self.running: set[str] = set()

    def start(self, session_id: str, name: str, width: int, height: int) -> SessionRecord:
        self.running.add(session_id)
        process = ProcessIdentity(
            pid=os.getpid(),
            start_ticks=1,
            executable="/fake/kwin",
            marker=session_id,
            command=("/fake/kwin",),
        )
        return SessionRecord(
            session_id=session_id,
            name=name,
            backend=self.name,
            created_at="2026-11-09T00:00:00+00:00",
            stopped_at=None,
            width=width,
            height=height,
            runtime_dir="/fake/runtime",
            home_dir="/fake/home",
            wayland_display="fake-display",
            dbus_address="unix:path=/fake/bus",
            kwin_process=process,
            dbus_process=process,
        )

    def is_running(self, session: SessionRecord) -> bool:
        return session.session_id in self.running

    def stop(self, session: SessionRecord) -> list[str]:
        self.running.discard(session.session_id)
        return []

    def displays(self, session: SessionRecord) -> list[DisplayInfo]:
        return [DisplayInfo("Virtual-0", 0, 0, session.width, session.height, 1.0, "normal", 60.0)]

    def screenshot(
        self,
        session: SessionRecord,
        output: Path,
        *,
        overwrite: bool,
    ) -> dict[str, object]:
        return {"path": str(output), "width": session.width, "height": session.height}


@pytest.fixture
def manager(tmp_path) -> SessionManager:
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o700)
    runtime.chmod(0o700)
    paths = RuntimePaths.from_environment(
        {"XDG_RUNTIME_DIR": str(runtime), "XDG_STATE_HOME": str(tmp_path / "state")}
    )
    return SessionManager(paths, backend=FakeBackend())


def test_session_lifecycle_is_scoped_to_agent_session(manager: SessionManager) -> None:
    created = manager.create_agent_session("triage", 1280, 800)
    public = manager.get_session(created.session_id)

    assert public["kind"] == "agent-session"
    assert public["status"] == "running"
    assert public["isolation"]["physical_displays"] == "unaffected"
    assert public["capabilities"]["input.inject"] is False

    displays = manager.displays(created.session_id)
    assert displays[0]["name"] == "Virtual-0"
    assert displays[0]["coordinate_space"] == "logical-desktop-pixels"

    stopped, warnings = manager.stop(created.session_id)
    assert not warnings
    assert stopped["status"] == "stopped"
    assert manager.list_sessions()[0]["status"] == "stopped"


@pytest.mark.parametrize(
    ("name", "width", "height"),
    [("bad/name", 1280, 800), ("ok", 100, 800), ("ok", 7680, 4320)],
)
def test_invalid_session_requests_are_rejected(
    manager: SessionManager,
    name: str,
    width: int,
    height: int,
) -> None:
    with pytest.raises(InteractionError):
        manager.create_agent_session(name, width, height)


def test_unknown_session_is_not_retargeted(manager: SessionManager) -> None:
    with pytest.raises(InteractionError, match="No managed session"):
        manager.get_session("agent-000000000000")


def test_mismatched_record_session_id_cannot_retarget_stop(manager: SessionManager) -> None:
    first = manager.create_agent_session("first", 1280, 800)
    second = manager.create_agent_session("second", 1280, 800)
    first_path = manager.store.root / f"{first.session_id}.json"
    second_path = manager.store.root / f"{second.session_id}.json"
    first_path.write_text(second_path.read_text(encoding="utf-8"), encoding="utf-8")

    with pytest.raises(InteractionError, match="does not match its requested record path"):
        manager.stop(first.session_id)

    assert manager.backend.is_running(first)
    assert manager.backend.is_running(second)


def test_stopped_session_cannot_be_screenshotted(manager: SessionManager, tmp_path) -> None:
    session = manager.create_agent_session("triage", 1280, 800)
    manager.stop(session.session_id)

    with pytest.raises(InteractionError, match="not running"):
        manager.screenshot(session.session_id, tmp_path / "shot.png")

from __future__ import annotations

import os
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

import pytest

from kde_interaction.backends.displays import DisplayInfo
from kde_interaction.backends.kwin_input import InputReceipt
from kde_interaction.errors import InteractionError
from kde_interaction.models import ProcessIdentity, SessionRecord
from kde_interaction.paths import RuntimePaths
from kde_interaction.sessions import SessionManager


class FakeBackend:
    name = "fake"

    def __init__(self) -> None:
        self.running: set[str] = set()
        self.launch_calls: list[tuple[str, ...]] = []
        self.launch_delay = 0.0

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
            fake_input_version=6,
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

    def launch_application(
        self,
        session: SessionRecord,
        argv: tuple[str, ...],
    ) -> ProcessIdentity:
        self.launch_calls.append(argv)
        time.sleep(self.launch_delay)
        return ProcessIdentity(
            pid=999_999 + len(self.launch_calls),
            start_ticks=2,
            executable="/usr/bin/kate",
            marker=f"{session.session_id}-app-{uuid.uuid4().hex}",
            command=argv,
        )

    def stop_application(self, identity: ProcessIdentity) -> None:
        pass

    def send_key(self, session: SessionRecord, chord: str) -> InputReceipt:
        return InputReceipt(f"key {chord}", 4, True)

    def send_text(self, session: SessionRecord, text: str) -> InputReceipt:
        return InputReceipt(f"type text ({len(text)} Unicode characters)", len(text) * 2, True)

    def click(self, session: SessionRecord, x: int, y: int, button: str) -> InputReceipt:
        return InputReceipt(f"click {button} at ({x}, {y})", 3, True)


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
    assert public["capabilities"]["input.inject"] is True
    assert public["capabilities"]["input.effect_ack"] is False

    displays = manager.displays(created.session_id)
    assert displays[0]["name"] == "Virtual-0"
    assert displays[0]["coordinate_space"] == "logical-desktop-pixels"

    stopped, warnings = manager.stop(created.session_id)
    assert not warnings
    assert stopped["status"] == "stopped"
    assert manager.list_sessions()[0]["status"] == "stopped"


def test_session_capabilities_follow_the_probed_fake_input_version(
    manager: SessionManager,
) -> None:
    session = manager.create_agent_session("versioned-input", 1280, 800)

    older = replace(session, fake_input_version=5).public_dict(status="running")
    assert older["capabilities"]["input.inject"] is False
    assert older["capabilities"]["input.text"] is False
    assert older["capabilities"]["input.click"] is True

    legacy_record = session.to_dict()
    legacy_record.pop("application_processes")
    legacy_record.pop("fake_input_version")
    restored = SessionRecord.from_dict(legacy_record)
    assert restored.application_processes == ()
    assert restored.fake_input_version is None


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


def test_application_launch_is_persisted_and_keeps_arguments_separate(
    manager: SessionManager,
) -> None:
    session = manager.create_agent_session("kate", 1280, 800)

    result = manager.launch_application(session.session_id, ("kate", "--new"))

    assert result["target"] == {"kind": "agent-session", "session_id": session.session_id}
    assert result["process_started"] is True
    assert result["window_opened"] is None
    assert manager.backend.launch_calls == [("kate", "--new")]
    assert manager.get_session(session.session_id)["launched_processes"] == [
        {"pid": 1_000_000, "executable": "/usr/bin/kate"}
    ]


def test_concurrent_application_launches_preserve_all_cleanup_records(
    manager: SessionManager,
) -> None:
    session = manager.create_agent_session("parallel-launch", 1280, 800)
    manager.backend.launch_delay = 0.05

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(
                manager.launch_application,
                session.session_id,
                ("kate", f"--new-{index}"),
            )
            for index in range(2)
        ]
        [future.result(timeout=2.0) for future in futures]

    processes = manager.get_session(session.session_id)["launched_processes"]
    assert len(processes) == 2
    assert len({process["pid"] for process in processes}) == 2


def test_input_receipts_do_not_claim_the_effect_was_verified(manager: SessionManager) -> None:
    session = manager.create_agent_session("input", 640, 480)

    receipt = manager.send_key(session.session_id, "CTRL+N")

    assert receipt["action"] == "key CTRL+N"
    assert receipt["server_roundtrip_completed"] is True
    assert receipt["effect_verified"] is False
    assert receipt["target"]["session_id"] == session.session_id


def test_click_requires_coordinates_inside_the_selected_private_output(
    manager: SessionManager,
) -> None:
    session = manager.create_agent_session("input", 640, 480)

    with pytest.raises(InteractionError, match="inside the private output"):
        manager.click(session.session_id, 640, 0)


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

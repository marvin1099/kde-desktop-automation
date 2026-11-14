from __future__ import annotations

import os
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

import pytest

from kde_desktop_automation import models
from kde_desktop_automation.backends.displays import DisplayInfo
from kde_desktop_automation.backends.kwin_input import InputReceipt
from kde_desktop_automation.errors import InteractionError
from kde_desktop_automation.models import ProcessIdentity, SessionRecord
from kde_desktop_automation.paths import RuntimePaths
from kde_desktop_automation.sessions import SessionManager


class FakeBackend:
    name = "fake"

    def __init__(self) -> None:
        self.running: set[str] = set()
        self.launch_calls: list[tuple[str, ...]] = []
        self.launch_delay = 0.0
        self.drag_calls: list[tuple[int, int, int, int, str, int, int, int, int]] = []
        self.minimized_calls: list[tuple[str, bool]] = []
        self.geometry_calls: list[tuple[str, int, int, int, int]] = []

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

    def environment(self, session: SessionRecord) -> dict[str, str]:
        return {"DBUS_SESSION_BUS_ADDRESS": session.dbus_address}

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

    def screenshot_active_window(
        self,
        session: SessionRecord,
        output: Path,
        *,
        overwrite: bool,
    ) -> dict[str, object]:
        return {
            "path": str(output),
            "width": 320,
            "height": 240,
            "capture_mode": "active-window",
        }

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
    ) -> InputReceipt:
        self.drag_calls.append(
            (start_x, start_y, end_x, end_y, button, steps, duration_ms, hold_ms, end_pause_ms)
        )
        return InputReceipt("drag left from (10, 20) to (50, 40)", steps + 3, True)

    def set_window_minimized(
        self,
        session: SessionRecord,
        window_id: str,
        minimized: bool,
    ) -> dict[str, object]:
        self.minimized_calls.append((window_id, minimized))
        return {"window_id": window_id, "minimized": minimized, "active": False}

    def set_window_geometry(
        self,
        _session: SessionRecord,
        window_id: str,
        x: int,
        y: int,
        width: int,
        height: int,
    ) -> dict[str, object]:
        self.geometry_calls.append((window_id, x, y, width, height))
        return {
            "window_id": window_id,
            "verified": True,
            "effective_geometry": {"x": x, "y": y, "width": width, "height": height},
        }

    def pointer_position(self, _session: SessionRecord) -> dict[str, object]:
        return {"x": 3, "y": 4, "coordinate_space": "logical-desktop-pixels"}


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
    assert public["is_default"] is True
    assert manager.default_session_id() == created.session_id

    displays = manager.displays(created.session_id)
    assert displays[0]["name"] == "Virtual-0"
    assert displays[0]["coordinate_space"] == "logical-desktop-pixels"

    stopped, warnings = manager.stop(created.session_id)
    assert not warnings
    assert stopped["status"] == "stopped"
    assert manager.list_sessions()[0]["status"] == "stopped"


def test_private_capabilities_report_screenshot_executable_availability(
    manager: SessionManager,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = manager.create_agent_session("screenshot-capability", 640, 480)
    monkeypatch.setattr(
        models.shutil,
        "which",
        lambda executable: "/usr/bin/spectacle" if executable == "spectacle" else None,
    )

    assert manager.get_session(session.session_id)["capabilities"]["screenshot.capture"] is True
    assert (
        manager.get_session(session.session_id)["capabilities"]["screenshot.window.capture"]
        is False
    )

    session = replace(session, window_management_version=13)
    manager.store.save(session)
    assert (
        manager.get_session(session.session_id)["capabilities"]["screenshot.window.capture"] is True
    )

    monkeypatch.setattr(models.shutil, "which", lambda _executable: None)
    assert manager.get_session(session.session_id)["capabilities"]["screenshot.capture"] is False
    assert (
        manager.get_session(session.session_id)["capabilities"]["screenshot.window.capture"]
        is False
    )


def test_runtime_environment_is_for_one_running_private_session(manager: SessionManager) -> None:
    created = manager.create_agent_session("media", 640, 480)

    environment = manager.runtime_environment(created.session_id)

    assert environment == {"DBUS_SESSION_BUS_ADDRESS": "unix:path=/fake/bus"}
    manager.stop(created.session_id)
    with pytest.raises(InteractionError) as error:
        manager.runtime_environment(created.session_id)
    assert error.value.code == "session_not_running"


def test_private_default_selection_is_explicit_and_does_not_follow_new_sessions(
    manager: SessionManager,
) -> None:
    first = manager.create_agent_session("first", 640, 480)
    second = manager.create_agent_session("second", 640, 480)

    assert manager.default_session_id() == first.session_id
    assert manager.get_session(second.session_id)["is_default"] is False

    selected = manager.set_default_session(second.session_id)

    assert selected["is_default"] is True
    assert manager.get_session(first.session_id)["is_default"] is False
    assert manager.default_session_id() == second.session_id


def test_first_default_claim_is_serialized_across_concurrent_creators(
    manager: SessionManager,
) -> None:
    first = manager.create_agent_session("first", 640, 480)
    second = manager.create_agent_session("second", 640, 480)
    manager.store.default_path.unlink()

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(
            executor.map(
                manager.store.set_default_session_if_unset,
                (first.session_id, second.session_id),
            )
        )

    assert sorted(results) == [False, True]
    assert manager.default_session_id() in {first.session_id, second.session_id}


def test_failed_first_default_selection_stops_and_records_the_new_session(
    manager: SessionManager,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_default(_session_id: str) -> bool:
        raise InteractionError("default_session_write_failed", "cannot write default")

    monkeypatch.setattr(manager.store, "set_default_session_if_unset", fail_default)

    with pytest.raises(InteractionError) as error:
        manager.create_agent_session("default-failure", 640, 480)

    session_id = error.value.details["session_id"]
    assert error.value.code == "session_default_selection_failed"
    assert manager.store.load(session_id).stopped_at is not None
    assert session_id not in manager.backend.running


def test_private_window_operations_fail_closed_when_protocol_was_not_probed(
    manager: SessionManager,
) -> None:
    session = manager.create_agent_session("no-window-protocol", 640, 480)

    with pytest.raises(InteractionError) as error:
        manager.list_windows(session.session_id)

    assert error.value.code == "capability_unavailable"
    assert error.value.details["capability"] == (
        "window.list / window.find / window.wait / window.inspect / "
        "window.activate / window-targeted input / window.set_minimized"
    )


def test_session_capabilities_follow_the_probed_fake_input_version(
    manager: SessionManager,
) -> None:
    session = manager.create_agent_session("versioned-input", 1280, 800)
    session = replace(session, window_management_version=13)

    older = replace(session, fake_input_version=5).public_dict(status="running")
    assert older["capabilities"]["input.inject"] is True
    assert older["capabilities"]["input.key"] is False
    assert older["capabilities"]["input.text"] is False
    assert older["capabilities"]["input.click"] is True
    assert older["capabilities"]["input.global_click"] is True
    assert older["capabilities"]["input.global_drag"] is True
    assert older["capabilities"]["input.active_window_guard"] is True
    assert "input.window_targeted" not in older["capabilities"]
    assert older["capabilities"]["window.wait"] is True
    assert older["input_semantics"]["delivery_acknowledged"] is False
    assert "focus can still change" in older["input_semantics"]["window_targeting"]

    legacy_record = session.to_dict()
    legacy_record.pop("application_processes")
    legacy_record.pop("fake_input_version")
    restored = SessionRecord.from_dict(legacy_record)
    assert restored.application_processes == ()
    assert restored.fake_input_version is None


def test_global_drag_is_bounded_to_the_private_virtual_output(manager: SessionManager) -> None:
    session = manager.create_agent_session("global-drag", 640, 480)

    result = manager.drag(session.session_id, 10, 20, 50, 40, steps=4)

    assert result["target"]["kind"] == "agent-session"
    assert result["target"]["session_id"] == session.session_id
    assert result["target"]["keyboard_focus"] == "not targeted by this global pointer action"
    assert result["requested_path"] == {
        "start": {"x": 10, "y": 20},
        "end": {"x": 50, "y": 40},
        "steps": 4,
        "duration_ms": 750,
        "hold_ms": 100,
        "end_pause_ms": 250,
        "space": "logical-desktop-pixels",
    }
    assert manager.backend.drag_calls == [(10, 20, 50, 40, "left", 4, 750, 100, 250)]

    with pytest.raises(InteractionError) as error:
        manager.drag(session.session_id, 10, 20, 640, 40, steps=4)

    assert error.value.code == "coordinates_out_of_bounds"


def test_private_global_click_reports_desktop_coordinates_and_pointer_routing(
    manager: SessionManager,
) -> None:
    session = manager.create_agent_session("global-click", 640, 480)

    result = manager.click(session.session_id, 20, 30)

    assert result["requested_point"] == {
        "x": 20,
        "y": 30,
        "space": "logical-desktop-pixels",
    }
    assert result["input_routing"] == "global pointer coordinates; not directly window-bound"
    assert result["input_semantics"]["targeting"] == "global-coordinate"


@pytest.mark.parametrize("coordinate", [True, 10.5])
def test_private_click_rejects_non_integer_coordinates(
    manager: SessionManager,
    coordinate: bool | float,
) -> None:
    session = manager.create_agent_session("invalid-click-coordinate", 640, 480)

    with pytest.raises(InteractionError) as error:
        manager.click(session.session_id, coordinate, 20)  # type: ignore[arg-type]

    assert error.value.code == "invalid_coordinates"


def test_private_window_minimized_state_is_explicit_and_verified(manager: SessionManager) -> None:
    session = manager.create_agent_session("minimize-window", 640, 480)
    session = replace(session, window_management_version=13)
    manager.store.save(session)

    result = manager.set_window_minimized(session.session_id, "window-uuid", True)

    assert result["action"] == "set-minimized"
    assert result["minimized"] is True
    assert result["verified"] is True
    assert manager.backend.minimized_calls == [("window-uuid", True)]


def test_private_window_geometry_and_pointer_position_are_target_scoped(
    manager: SessionManager,
) -> None:
    session = manager.create_agent_session("window-geometry", 640, 480)
    manager.store.save(replace(session, window_management_version=13))

    geometry = manager.set_window_geometry(session.session_id, "window-uuid", 10, 20, 400, 300)
    pointer = manager.pointer_position(session.session_id)

    assert geometry["action"] == "set-geometry"
    assert geometry["verified"] is True
    assert geometry["target"]["session_id"] == session.session_id
    assert manager.backend.geometry_calls == [("window-uuid", 10, 20, 400, 300)]
    assert pointer["target"]["session_id"] == session.session_id
    assert pointer["x"] == 3


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


def test_private_screenshot_includes_target_and_shared_coordinate_space(
    manager: SessionManager,
    tmp_path: Path,
) -> None:
    session = manager.create_agent_session("triage", 1280, 800)

    capture = manager.screenshot(session.session_id, tmp_path / "shot.png")

    assert capture["target"] == {"kind": "agent-session", "session_id": session.session_id}
    assert capture["coordinate_space"] == "screenshot-pixels"


def test_private_active_window_screenshot_reports_target_and_mode(
    manager: SessionManager,
    tmp_path: Path,
) -> None:
    session = manager.create_agent_session("window-screenshot", 800, 600)

    capture = manager.screenshot_active_window(session.session_id, tmp_path / "window.png")

    assert capture["target"] == {"kind": "agent-session", "session_id": session.session_id}
    assert capture["coordinate_space"] == "screenshot-pixels"
    assert capture["capture_mode"] == "active-window"

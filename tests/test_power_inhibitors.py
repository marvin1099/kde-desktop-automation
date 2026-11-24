from __future__ import annotations

import asyncio
import os
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest
from dbus_next import MessageType

from kde_desktop_automation.backends import power_inhibitor_worker, power_inhibitors
from kde_desktop_automation.backends.power_inhibitors import PowerInhibitorManager
from kde_desktop_automation.errors import InteractionError
from kde_desktop_automation.models import ProcessIdentity

ENVIRONMENT = {"DBUS_SESSION_BUS_ADDRESS": "unix:path=/tmp/mock-session-bus"}


class FakeProcess:
    next_pid = 40_000
    instances: list[FakeProcess] = []

    def __init__(self, argv: Sequence[str], *, pass_fds: tuple[int, ...], **_kwargs: Any) -> None:
        self.argv = tuple(argv)
        self.pid = self.next_pid
        type(self).next_pid += 10
        self.returncode: int | None = None
        is_dbus_inhibitor = any(
            inhibitor in self.argv for inhibitor in ("screen-lock", "power-management")
        )
        ready_pid = self.pid if is_dbus_inhibitor else self.pid + 1
        os.write(pass_fds[0], f"READY:{ready_pid}\n".encode())
        self.instances.append(self)

    def poll(self) -> int | None:
        return self.returncode

    def terminate(self) -> None:
        self.returncode = -15

    def wait(self, timeout: float | None = None) -> int:
        del timeout
        return self.returncode or 0


@pytest.fixture
def inhibitor_fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    root = tmp_path / "power-inhibitors"
    root.mkdir(mode=0o700)
    root.chmod(0o700)
    FakeProcess.next_pid = 40_000
    FakeProcess.instances.clear()
    running: dict[int, ProcessIdentity] = {}
    terminated: list[int] = []

    def make_identity(
        pid: int,
        *,
        executable: str,
        marker: str,
        command: Sequence[str],
    ) -> ProcessIdentity:
        identity = ProcessIdentity(
            pid=pid,
            start_ticks=pid + 100,
            executable=str(Path(executable).resolve()),
            marker=marker,
            command=tuple(command),
        )
        running[pid] = identity
        return identity

    monkeypatch.setattr(power_inhibitors.subprocess, "Popen", FakeProcess)
    monkeypatch.setattr(power_inhibitors, "pidfd_supported", lambda: True)
    monkeypatch.setattr(
        power_inhibitors,
        "find_executable",
        lambda name: "/usr/bin/systemd-inhibit" if name == "systemd-inhibit" else None,
    )
    monkeypatch.setattr(power_inhibitors, "process_identity", make_identity)
    monkeypatch.setattr(
        power_inhibitors,
        "process_start_ticks",
        lambda pid: running[pid].start_ticks if pid in running else None,
    )
    monkeypatch.setattr(
        power_inhibitors,
        "process_state",
        lambda pid: "S" if pid in running else None,
    )
    monkeypatch.setattr(
        power_inhibitors,
        "process_matches",
        lambda identity: running.get(identity.pid) == identity,
    )
    monkeypatch.setattr(
        power_inhibitors,
        "processes_with_environment_marker",
        lambda marker: [identity for identity in running.values() if identity.marker == marker],
    )

    def terminate(identity: ProcessIdentity, *, timeout: float = 3.0) -> bool:
        del timeout
        terminated.append(identity.pid)
        running.pop(identity.pid, None)
        return True

    monkeypatch.setattr(power_inhibitors, "terminate_verified", terminate)
    return PowerInhibitorManager(root, ENVIRONMENT), running, terminated


def test_systemd_inhibitor_is_tracked_listed_and_released_only_by_its_id(
    inhibitor_fixture,
) -> None:
    manager, running, terminated = inhibitor_fixture

    started = manager.inhibit("sleep", duration_seconds=-1, reason="long render")

    assert started["inhibitor"] == "sleep"
    assert started["duration_seconds"] == -1
    assert started["expires_at"] is None
    assert started["active"] is True
    wrapper = FakeProcess.instances[0]
    assert wrapper.argv[0] == "/usr/bin/systemd-inhibit"
    assert wrapper.argv[1] == "--what=sleep"
    assert wrapper.argv[2].startswith("--who=KDE Desktop Automation")
    assert wrapper.argv[3].startswith("--why=long render")
    assert wrapper.argv[4] == "--mode=block"
    assert manager.list() == [started]

    released = manager.release(started["id"])

    assert released["released"] is True
    assert terminated == [40_001, 40_000]
    assert running == {}
    assert manager.list() == []


@pytest.mark.parametrize("inhibitor", ["screen-lock", "power-management"])
def test_dbus_inhibitor_uses_its_worker_without_systemd(
    inhibitor_fixture,
    inhibitor: str,
) -> None:
    manager, _running, _terminated = inhibitor_fixture

    started = manager.inhibit(inhibitor, duration_seconds=60, reason="presentation")

    assert started["inhibitor"] == inhibitor
    assert started["duration_seconds"] == 60
    worker = FakeProcess.instances[0]
    assert worker.argv[1:5] == (
        "-m",
        "kde_desktop_automation.backends.power_inhibitor_worker",
        inhibitor,
        "60",
    )
    assert "--reason=presentation" in worker.argv


@pytest.mark.parametrize("duration", [0, -2, 86_401, True, 1.5])
def test_power_inhibitor_rejects_invalid_durations_before_launch(
    inhibitor_fixture,
    duration: int | float,
) -> None:
    manager, _running, _terminated = inhibitor_fixture

    with pytest.raises(InteractionError) as error:
        manager.inhibit("sleep", duration_seconds=duration)  # type: ignore[arg-type]

    assert error.value.code == "invalid_power_inhibitor_duration"
    assert FakeProcess.instances == []


def test_power_inhibitor_rejects_arbitrary_inhibitor_names(inhibitor_fixture) -> None:
    manager, _running, _terminated = inhibitor_fixture

    with pytest.raises(InteractionError) as error:
        manager.inhibit("shell-command")

    assert error.value.code == "invalid_power_inhibitor"
    assert FakeProcess.instances == []


def test_listing_prunes_an_expired_owner_and_stops_its_systemd_wrapper(inhibitor_fixture) -> None:
    manager, running, terminated = inhibitor_fixture
    manager.inhibit("idle", duration_seconds=1)
    running.pop(40_001)

    assert manager.list() == []
    assert terminated == [40_000]
    assert running == {}


@pytest.mark.parametrize(
    ("inhibitor", "service", "object_path", "interface"),
    [
        (
            "screen-lock",
            "org.freedesktop.ScreenSaver",
            "/ScreenSaver",
            "org.freedesktop.ScreenSaver",
        ),
        (
            "power-management",
            "org.freedesktop.PowerManagement.Inhibit",
            "/org/freedesktop/PowerManagement/Inhibit",
            "org.freedesktop.PowerManagement.Inhibit",
        ),
    ],
)
def test_dbus_power_worker_uses_cookie_and_uninhibits_on_expiry(
    monkeypatch: pytest.MonkeyPatch,
    inhibitor: str,
    service: str,
    object_path: str,
    interface: str,
) -> None:
    class FakeReply:
        message_type = MessageType.METHOD_RETURN
        body = [77]

    class FakeBus:
        def __init__(self) -> None:
            self.messages = []
            self.disconnected = False

        async def connect(self):
            return self

        async def call(self, message):
            self.messages.append(message)
            return FakeReply()

        def disconnect(self) -> None:
            self.disconnected = True

    bus = FakeBus()
    monkeypatch.setattr(power_inhibitor_worker, "MessageBus", lambda: bus)
    read_fd, write_fd = os.pipe()
    monkeypatch.setenv("KDE_DESKTOP_AUTOMATION_READY_FD", str(write_fd))
    try:
        asyncio.run(power_inhibitor_worker._hold_dbus_inhibitor(inhibitor, 0.01, "demo"))  # type: ignore[arg-type]
        ready_message = os.read(read_fd, 128).decode()
    finally:
        os.close(read_fd)
        with pytest.raises(OSError):
            os.close(write_fd)

    assert ready_message.startswith("READY:")
    assert [message.member for message in bus.messages] == ["Inhibit", "UnInhibit"]
    assert all(message.destination == service for message in bus.messages)
    assert all(message.path == object_path for message in bus.messages)
    assert all(message.interface == interface for message in bus.messages)
    assert bus.messages[0].signature == "ss"
    assert bus.messages[0].body == ["KDE Desktop Automation", "demo"]
    assert bus.messages[1].signature == "u"
    assert bus.messages[1].body == [77]
    assert bus.disconnected is True


def test_list_refuses_to_signal_a_pid_with_a_mismatched_identity(inhibitor_fixture, monkeypatch):
    manager, _running, terminated = inhibitor_fixture
    started = manager.inhibit("sleep", duration_seconds=-1)
    owner_pid = 40_001
    original_matches = power_inhibitors.process_matches
    monkeypatch.setattr(
        power_inhibitors,
        "process_matches",
        lambda identity: False if identity.pid == owner_pid else original_matches(identity),
    )

    with pytest.raises(InteractionError) as error:
        manager.list()

    assert error.value.code == "power_inhibitor_identity_mismatch"
    assert terminated == []
    assert (manager.root / f"{started['id']}.json").is_file()


def test_real_worker_handshake_and_release_do_not_call_systemd(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if not power_inhibitors.pidfd_supported():
        pytest.skip("safe worker cleanup requires pidfd support")

    root = tmp_path / "power-inhibitors"
    root.mkdir(mode=0o700)
    root.chmod(0o700)
    fake_systemd = tmp_path / "systemd-inhibit"
    fake_systemd.write_text(
        '#!/bin/sh\nwhile [ "$1" != "--" ]; do shift; done\nshift\nexec "$@"\n',
        encoding="utf-8",
    )
    fake_systemd.chmod(0o700)
    original_process_identity = power_inhibitors.process_identity

    def identity_for_actual_executable(
        pid: int,
        *,
        executable: str,
        marker: str,
        command: Sequence[str],
    ) -> ProcessIdentity:
        del executable
        actual_executable = os.readlink(f"/proc/{pid}/exe")
        return original_process_identity(
            pid,
            executable=actual_executable,
            marker=marker,
            command=command,
        )

    monkeypatch.setattr(
        power_inhibitors,
        "find_executable",
        lambda _name: str(fake_systemd),
    )
    monkeypatch.setattr(
        power_inhibitors,
        "process_identity",
        identity_for_actual_executable,
    )
    manager = PowerInhibitorManager(root, os.environ | ENVIRONMENT)

    started = manager.inhibit("sleep", duration_seconds=-1, reason="test only")

    assert started["active"] is True
    assert manager.list() == [started]
    assert manager.release(started["id"])["released"] is True
    assert manager.list() == []

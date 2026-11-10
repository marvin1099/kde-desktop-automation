import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

from kde_desktop_automation.backends import kwin_virtual
from kde_desktop_automation.backends.kwin_virtual import KWinVirtualBackend
from kde_desktop_automation.errors import InteractionError
from kde_desktop_automation.models import ProcessIdentity, SessionRecord
from kde_desktop_automation.paths import RuntimePaths
from kde_desktop_automation.process import (
    pidfd_supported,
    process_identity,
    process_matches,
    process_start_ticks,
    process_state,
    processes_with_environment_marker,
    terminate_verified,
)


def _current_command() -> tuple[str, ...]:
    return tuple(
        part
        for part in Path(f"/proc/{os.getpid()}/cmdline").read_bytes().decode().split("\0")
        if part
    )


def test_process_identity_can_be_verified_without_signalling() -> None:
    identity = ProcessIdentity(
        pid=os.getpid(),
        start_ticks=process_start_ticks(os.getpid()) or 0,
        executable=str(Path(f"/proc/{os.getpid()}/exe").resolve()),
        marker="pytest",
        command=_current_command(),
    )

    assert process_matches(identity)
    assert process_state(os.getpid()) not in (None, "Z")


def test_process_identity_can_use_an_environment_marker() -> None:
    marker = f"kde-desktop-marker-{uuid.uuid4().hex}"
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        env=os.environ | {"KDE_DESKTOP_AUTOMATION_PROCESS_MARKER": marker},
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        identity = process_identity(
            child.pid,
            executable=str(Path(sys.executable).resolve()),
            marker=marker,
            command=child.args,
        )

        assert process_matches(identity)
        assert terminate_verified(identity, timeout=1.0)
        child.wait(timeout=1.0)
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=1.0)


def test_environment_marker_finds_descendants_for_session_cleanup(
    tmp_path,
) -> None:
    marker = f"kde-desktop-tree-{uuid.uuid4().hex}"
    child_ready = tmp_path / "child-ready"
    child_script = (
        "from pathlib import Path; import time; "
        f"Path({str(child_ready)!r}).write_text('ready'); time.sleep(60)"
    )
    script = (
        "import subprocess, sys, time; "
        f"subprocess.Popen([sys.executable, '-c', {child_script!r}]); "
        "time.sleep(60)"
    )
    parent = subprocess.Popen(
        [sys.executable, "-c", script],
        env=os.environ | {"KDE_DESKTOP_AUTOMATION_PROCESS_MARKER": marker},
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        identities = []
        for _attempt in range(50):
            identities = processes_with_environment_marker(marker)
            if len(identities) >= 2:
                break
            time.sleep(0.02)

        assert len(identities) >= 2
        for identity in sorted(identities, key=lambda item: item.pid, reverse=True):
            assert terminate_verified(identity, timeout=1.0)
        parent.wait(timeout=1.0)
        assert processes_with_environment_marker(marker) == []
    finally:
        if parent.poll() is None:
            parent.kill()
            parent.wait(timeout=1.0)


def test_application_cleanup_rescans_for_child_spawned_during_parent_shutdown(tmp_path) -> None:
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o700)
    runtime.chmod(0o700)
    paths = RuntimePaths.from_environment(
        {"XDG_RUNTIME_DIR": str(runtime), "XDG_STATE_HOME": str(tmp_path / "state")}
    )
    marker = f"agent-0123456789ab-app-{uuid.uuid4().hex}"
    child_ready = tmp_path / "fast-child-ready"
    parent_ready = tmp_path / "parent-ready"
    child_script = (
        "from pathlib import Path; import time; "
        f"Path({str(child_ready)!r}).write_text('ready'); time.sleep(60)"
    )
    parent_script = (
        "import signal, subprocess, sys, time; "
        "signal.signal(signal.SIGTERM, lambda *_: ("
        f"subprocess.Popen([sys.executable, '-c', {child_script!r}]), "
        "time.sleep(0.05), sys.exit(0))); "
        f"open({str(parent_ready)!r}, 'w').close(); "
        "time.sleep(60)"
    )
    parent = subprocess.Popen(
        [sys.executable, "-c", parent_script],
        env=os.environ | {"KDE_DESKTOP_AUTOMATION_PROCESS_MARKER": marker},
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        for _attempt in range(50):
            if parent_ready.is_file():
                break
            time.sleep(0.01)
        assert parent_ready.is_file()
        identity = process_identity(
            parent.pid,
            executable=os.readlink(f"/proc/{parent.pid}/exe"),
            marker=marker,
            command=parent.args,
        )
        backend = KWinVirtualBackend(paths)

        backend.stop_application(identity)
        parent.wait(timeout=1.0)

        assert child_ready.is_file()
        assert processes_with_environment_marker(marker) == []
    finally:
        if parent.poll() is None:
            parent.kill()
            parent.wait(timeout=1.0)
        for identity in processes_with_environment_marker(marker):
            terminate_verified(identity, timeout=1.0)


def test_application_cleanup_retries_a_marked_process_identity_change(
    tmp_path,
    monkeypatch,
) -> None:
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o700)
    runtime.chmod(0o700)
    paths = RuntimePaths.from_environment(
        {"XDG_RUNTIME_DIR": str(runtime), "XDG_STATE_HOME": str(tmp_path / "state")}
    )
    marker = f"agent-0123456789ab-app-{uuid.uuid4().hex}"
    process = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        env=os.environ | {"KDE_DESKTOP_AUTOMATION_PROCESS_MARKER": marker},
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        identity = process_identity(
            process.pid,
            executable=os.readlink(f"/proc/{process.pid}/exe"),
            marker=marker,
            command=process.args,
        )
        real_terminate = kwin_virtual.terminate_verified
        attempts = 0

        def report_one_exec_race(identity, *, timeout: float = 3.0) -> bool:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise InteractionError("process_identity_mismatch", "Simulated exec race.")
            return real_terminate(identity, timeout=timeout)

        monkeypatch.setattr(kwin_virtual, "terminate_verified", report_one_exec_race)
        KWinVirtualBackend(paths).stop_application(identity)
        process.wait(timeout=1.0)

        assert attempts >= 2
        assert processes_with_environment_marker(marker) == []
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=1.0)


def test_fast_exiting_launcher_failure_cleans_up_marked_descendant(tmp_path, monkeypatch) -> None:
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o700)
    runtime.chmod(0o700)
    paths = RuntimePaths.from_environment(
        {"XDG_RUNTIME_DIR": str(runtime), "XDG_STATE_HOME": str(tmp_path / "state")}
    )
    backend = KWinVirtualBackend(paths)
    backend._require_running = lambda _session: None
    backend.environment = lambda _session: os.environ.copy()
    current = process_identity(
        os.getpid(),
        executable=os.readlink(f"/proc/{os.getpid()}/exe"),
        marker="pytest",
        command=_current_command(),
    )
    session = SessionRecord(
        session_id="agent-0123456789ab",
        name="test",
        backend="kwin-virtual",
        created_at="2026-11-09T00:00:00+00:00",
        stopped_at=None,
        width=640,
        height=480,
        runtime_dir=str(runtime),
        home_dir=str(tmp_path),
        wayland_display="test-wayland",
        dbus_address="unix:path=/tmp/test-bus",
        kwin_process=current,
        dbus_process=current,
    )
    child_ready = tmp_path / "fast-child-ready"
    child_script = (
        "from pathlib import Path; import time; "
        f"Path({str(child_ready)!r}).write_text('ready'); time.sleep(60)"
    )
    command = (
        sys.executable,
        "-c",
        f"import subprocess, sys; subprocess.Popen([sys.executable, '-c', {child_script!r}])",
    )
    markers: list[str] = []

    def fail_identity_capture(_pid: int, *, marker: str, **_kwargs) -> ProcessIdentity:
        markers.append(marker)
        for _attempt in range(50):
            if child_ready.is_file():
                break
            time.sleep(0.01)
        raise InteractionError(
            "process_start_failed",
            "Simulated fast exit during identity capture.",
        )

    monkeypatch.setattr(kwin_virtual, "process_identity", fail_identity_capture)
    with pytest.raises(InteractionError, match="Simulated fast exit"):
        backend.launch_application(session, command)

    assert child_ready.is_file()
    assert markers
    assert processes_with_environment_marker(markers[0]) == []


def test_process_identity_falls_back_to_verified_argv_when_exe_is_restricted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identity = ProcessIdentity(
        pid=os.getpid(),
        start_ticks=process_start_ticks(os.getpid()) or 0,
        executable=str(Path(f"/proc/{os.getpid()}/exe").resolve()),
        marker="pytest",
        command=_current_command(),
    )
    original_readlink = os.readlink

    def restricted_readlink(path: str | os.PathLike[str]) -> str:
        if str(path) == f"/proc/{os.getpid()}/exe":
            raise PermissionError("proc executable link is restricted")
        return original_readlink(path)

    monkeypatch.setattr(os, "readlink", restricted_readlink)

    assert process_matches(identity)


def test_process_start_ticks_reject_pid_reuse_marker() -> None:
    identity = ProcessIdentity(
        pid=os.getpid(),
        start_ticks=-1,
        executable=str(Path(f"/proc/{os.getpid()}/exe").resolve()),
        marker="pytest",
        command=_current_command(),
    )

    assert not process_matches(identity)


@pytest.mark.skipif(
    not pidfd_supported(),
    reason="safe termination requires Linux pidfd support",
)
def test_terminate_verified_uses_a_pidfd_for_the_target_process() -> None:
    marker = f"kde-desktop-child-{uuid.uuid4().hex}"
    child = subprocess.Popen(
        [sys.executable, "-c", "import sys, time; print(sys.argv[1]); time.sleep(60)", marker],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        identity = process_identity(
            child.pid,
            executable=str(Path(sys.executable).resolve()),
            marker=marker,
            command=child.args,
        )

        assert terminate_verified(identity, timeout=1.0)
        assert child.wait(timeout=1.0) != 0
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=1.0)

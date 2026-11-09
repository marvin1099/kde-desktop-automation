import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

from kde_interaction.models import ProcessIdentity
from kde_interaction.process import (
    pidfd_supported,
    process_identity,
    process_matches,
    process_start_ticks,
    process_state,
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
    marker = f"kde-ai-child-{uuid.uuid4().hex}"
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

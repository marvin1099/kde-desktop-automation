import os
from urllib.parse import quote

import pytest

from kde_interaction.backends import kwin_virtual
from kde_interaction.backends.kwin_virtual import KWinVirtualBackend, _remove_owned_runtime
from kde_interaction.errors import InteractionError
from kde_interaction.models import ProcessIdentity, SessionRecord
from kde_interaction.paths import RuntimePaths


def test_runtime_cleanup_is_limited_to_session_directory(tmp_path) -> None:
    allowed = tmp_path / "sessions"
    session = allowed / "agent-0123456789ab"
    nested = session / "portal"
    nested.mkdir(parents=True)
    owned_file = nested / "private.sock"
    owned_file.write_text("session-owned", encoding="utf-8")
    external = tmp_path / "keep.txt"
    external.write_text("not ours", encoding="utf-8")
    (session / "link").symlink_to(external)

    assert _remove_owned_runtime(session, allowed)
    assert not session.exists()
    assert external.read_text(encoding="utf-8") == "not ours"


def test_runtime_cleanup_refuses_path_outside_owned_sessions_root(tmp_path) -> None:
    allowed = tmp_path / "sessions"
    outside = tmp_path / "outside"
    outside.mkdir()

    assert not _remove_owned_runtime(outside, allowed)
    assert outside.exists()


def test_runtime_cleanup_refuses_mountpoint_descendants(tmp_path, monkeypatch) -> None:
    allowed = tmp_path / "sessions"
    session = allowed / "agent-0123456789ab"
    mountpoint = session / "mounted-portal"
    mountpoint.mkdir(parents=True)
    mounted_data = mountpoint / "foreign-data.txt"
    mounted_data.write_text("must not be recursively removed", encoding="utf-8")
    monkeypatch.setattr(kwin_virtual, "_read_mount_points", lambda: {mountpoint})

    assert not _remove_owned_runtime(session, allowed)
    assert mounted_data.read_text(encoding="utf-8") == "must not be recursively removed"


def test_runtime_cleanup_rechecks_mounts_during_recursion(tmp_path, monkeypatch) -> None:
    allowed = tmp_path / "sessions"
    session = allowed / "agent-0123456789ab"
    mountpoint = session / "portal"
    mountpoint.mkdir(parents=True)
    mounted_data = mountpoint / "foreign-data.txt"
    mounted_data.write_text("must not be removed after a late mount", encoding="utf-8")
    reads = 0

    def mount_table():
        nonlocal reads
        reads += 1
        return {mountpoint} if reads >= 3 else set()

    monkeypatch.setattr(kwin_virtual, "_read_mount_points", mount_table)

    assert not _remove_owned_runtime(session, allowed)
    assert mounted_data.read_text(encoding="utf-8") == "must not be removed after a late mount"


def test_stop_preserves_runtime_if_process_identity_is_unverified(tmp_path, monkeypatch) -> None:
    host_runtime = tmp_path / "runtime"
    host_runtime.mkdir(mode=0o700)
    host_runtime.chmod(0o700)
    paths = RuntimePaths.from_environment(
        {
            "XDG_RUNTIME_DIR": str(host_runtime),
            "XDG_STATE_HOME": str(tmp_path / "state"),
        }
    )
    session_id = "agent-0123456789ab"
    session_paths = paths.for_session(session_id, create=True)
    owned_marker = session_paths.runtime_dir / "keep-for-manual-cleanup"
    owned_marker.write_text("do not delete while process status is unknown", encoding="utf-8")
    wayland_display = f"kde-ai-{session_id.removeprefix('agent-')}"
    kwin_process = ProcessIdentity(
        pid=os.getpid(),
        start_ticks=123,
        executable="/usr/bin/kwin_wayland",
        marker=wayland_display,
        command=("/usr/bin/kwin_wayland", "--socket", wayland_display),
    )
    dbus_process = ProcessIdentity(
        pid=os.getpid() + 1,
        start_ticks=456,
        executable="/usr/bin/dbus-daemon",
        marker=str(session_paths.runtime_dir / "bus"),
        command=("/usr/bin/dbus-daemon", "--address", str(session_paths.runtime_dir / "bus")),
    )
    session = SessionRecord(
        session_id=session_id,
        name="test",
        backend="kwin-virtual",
        created_at="2026-11-09T00:00:00+00:00",
        stopped_at=None,
        width=1024,
        height=768,
        runtime_dir=str(session_paths.runtime_dir),
        home_dir=str(session_paths.home_dir),
        wayland_display=wayland_display,
        dbus_address=(
            f"unix:path={quote(str(session_paths.runtime_dir / 'bus'), safe='/')},guid={'a' * 32}"
        ),
        kwin_process=kwin_process,
        dbus_process=dbus_process,
    )
    monkeypatch.setattr(kwin_virtual, "process_start_ticks", lambda _: 123)

    def fail_unverified(_identity):
        raise InteractionError("process_identity_mismatch", "Cannot verify process identity.")

    monkeypatch.setattr(kwin_virtual, "terminate_verified", fail_unverified)

    with pytest.raises(InteractionError, match="Cannot verify process identity"):
        KWinVirtualBackend(paths).stop(session)

    assert (
        owned_marker.read_text(encoding="utf-8") == "do not delete while process status is unknown"
    )


def test_stop_rejects_process_identities_from_another_session(tmp_path) -> None:
    host_runtime = tmp_path / "runtime"
    host_runtime.mkdir(mode=0o700)
    host_runtime.chmod(0o700)
    paths = RuntimePaths.from_environment(
        {
            "XDG_RUNTIME_DIR": str(host_runtime),
            "XDG_STATE_HOME": str(tmp_path / "state"),
        }
    )
    session_id = "agent-0123456789ab"
    session_paths = paths.for_session(session_id, create=True)
    other_session = "agent-abcdef012345"
    kwin_process = ProcessIdentity(
        pid=os.getpid(),
        start_ticks=123,
        executable="/usr/bin/kwin_wayland",
        marker=f"kde-ai-{other_session.removeprefix('agent-')}",
        command=("/usr/bin/kwin_wayland", "--socket", other_session),
    )
    dbus_process = ProcessIdentity(
        pid=os.getpid() + 1,
        start_ticks=123,
        executable="/usr/bin/dbus-daemon",
        marker=f"{host_runtime}/kde-interaction/sessions/{other_session}/bus",
        command=("/usr/bin/dbus-daemon",),
    )
    session = SessionRecord(
        session_id=session_id,
        name="test",
        backend="kwin-virtual",
        created_at="2026-11-09T00:00:00+00:00",
        stopped_at=None,
        width=1024,
        height=768,
        runtime_dir=str(session_paths.runtime_dir),
        home_dir=str(session_paths.home_dir),
        wayland_display=f"kde-ai-{session_id.removeprefix('agent-')}",
        dbus_address=f"unix:path={session_paths.runtime_dir / 'bus'},guid={'a' * 32}",
        kwin_process=kwin_process,
        dbus_process=dbus_process,
    )

    with pytest.raises(InteractionError, match="not bound to this private session"):
        KWinVirtualBackend(paths).stop(session)

    assert session_paths.runtime_dir.is_dir()

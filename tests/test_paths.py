import stat

import pytest

from kde_desktop_automation.errors import InteractionError
from kde_desktop_automation.paths import RuntimePaths


def test_runtime_paths_create_private_directories(tmp_path) -> None:
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o700)
    runtime.chmod(0o700)

    paths = RuntimePaths.from_environment(
        {"XDG_RUNTIME_DIR": str(runtime), "XDG_STATE_HOME": str(tmp_path / "state")}
    )
    session_id = "agent-0123456789ab"
    session = paths.for_session(session_id, create=True)

    assert session.runtime_dir.is_dir()
    assert session.home_dir.is_dir()
    assert stat.S_IMODE(session.runtime_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE(session.home_dir.stat().st_mode) == 0o700
    assert session.record_file.parent == paths.sessions_root


def test_runtime_paths_reject_world_accessible_runtime(tmp_path) -> None:
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o755)
    runtime.chmod(0o755)

    with pytest.raises(InteractionError, match="must not be accessible"):
        RuntimePaths.from_environment({"XDG_RUNTIME_DIR": str(runtime)})


def test_runtime_paths_reject_unmanaged_session_ids(tmp_path) -> None:
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o700)
    runtime.chmod(0o700)
    paths = RuntimePaths.from_environment({"XDG_RUNTIME_DIR": str(runtime)})

    with pytest.raises(InteractionError, match="managed agent session ID"):
        paths.for_session("../../user-desktop", create=True)


def test_runtime_paths_reject_missing_runtime_dir(monkeypatch) -> None:
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    with pytest.raises(InteractionError, match="XDG_RUNTIME_DIR is required"):
        RuntimePaths.from_environment({})

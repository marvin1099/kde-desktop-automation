import os
from pathlib import Path

import pytest

from kde_interaction.backends.kwin_virtual import KWinVirtualBackend
from kde_interaction.paths import RuntimePaths
from kde_interaction.process import process_start_ticks
from kde_interaction.sessions import SessionManager

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("KDE_AI_RUN_KWIN_INTEGRATION") != "1",
        reason="set KDE_AI_RUN_KWIN_INTEGRATION=1 to start a private KWin compositor",
    ),
]


def test_private_kwin_output_and_screenshot_round_trip(tmp_path) -> None:
    host_runtime = os.environ.get("XDG_RUNTIME_DIR")
    if not host_runtime:
        pytest.skip("an XDG_RUNTIME_DIR is required")
    paths = RuntimePaths.from_environment(
        {
            "XDG_RUNTIME_DIR": host_runtime,
            "XDG_STATE_HOME": str(tmp_path / "state"),
        }
    )
    backend = KWinVirtualBackend(paths)
    manager = SessionManager(paths, backend)
    session = manager.create_agent_session("integration", 1024, 768)
    screenshot = tmp_path / "private-output.png"
    try:
        displays = manager.displays(session.session_id)
        assert len(displays) == 1
        assert displays[0]["width"] == 1024
        assert displays[0]["height"] == 768

        capture = manager.screenshot(session.session_id, screenshot)
        assert capture["width"] == 1024
        assert capture["height"] == 768
        assert Path(capture["path"]).is_file()
    finally:
        _, warnings = manager.stop(session.session_id)
        assert warnings in (
            [],
            [f"Private runtime directory needs manual cleanup: {session.runtime_dir}"],
        )
        assert process_start_ticks(session.kwin_process.pid) != session.kwin_process.start_ticks
        assert process_start_ticks(session.dbus_process.pid) != session.dbus_process.start_ticks
        if warnings:
            assert Path(session.runtime_dir).is_dir()
        else:
            assert not Path(session.runtime_dir).exists()

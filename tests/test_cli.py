import json

from typer.testing import CliRunner

from kde_interaction.cli import app


def test_doctor_json_requires_explicit_user_desktop_commands() -> None:
    result = CliRunner().invoke(app, ["doctor", "--json"])

    assert result.exit_code == 0
    report = json.loads(result.stdout)
    assert report["ok"] is True
    assert report["data"]["user_desktop"]["explicit_command_required"] is True
    assert report["data"]["user_desktop"]["window.list"] is None
    assert report["data"]["user_desktop"]["window.capability_probe_performed"] is False
    assert report["data"]["safety"]["silent_fallback_to_user_desktop"] is False
    assert report["data"]["safety"]["explicit_user_desktop_command_required"] is True
    assert report["data"]["agent_session"]["input.inject"] is False
    assert report["data"]["agent_session"]["input.effect_ack"] is False
    assert "playerctl" in report["data"]["tools"]


def test_media_control_cli_passes_explicit_player_action_and_seek_offset(monkeypatch) -> None:
    calls = []

    class FakeOperations:
        def __init__(self, session, **_kwargs):
            self.session = session

        def media_control(self, player, action, *, seconds=None):
            calls.append((self.session, player, action, seconds))
            return {
                "target": {"kind": self.session},
                "player": player,
                "action": action,
                "seconds": seconds,
            }

    monkeypatch.setattr("kde_interaction.cli.DesktopOperations", FakeOperations)

    result = CliRunner().invoke(
        app,
        [
            "--session",
            "user-desktop",
            "media",
            "control",
            "spotify",
            "seek",
            "--seconds",
            "-15",
            "--json",
        ],
    )

    assert result.exit_code == 0
    assert calls == [("user-desktop", "spotify", "seek", -15.0)]
    assert json.loads(result.stdout)["data"]["seconds"] == -15.0


def test_media_list_and_inspect_cli_return_json_for_selected_target(monkeypatch) -> None:
    calls = []

    class FakeOperations:
        def __init__(self, session, **_kwargs):
            self.session = session

        def media_players(self):
            calls.append((self.session, "list"))
            return {"target": {"kind": self.session}, "players": [{"name": "vlc"}]}

        def media_inspect(self, player):
            calls.append((self.session, "inspect", player))
            return {"target": {"kind": self.session}, "player": player, "position_seconds": 3.5}

    monkeypatch.setattr("kde_interaction.cli.DesktopOperations", FakeOperations)
    runner = CliRunner()

    listing = runner.invoke(
        app,
        ["--session", "agent-123456789abc", "media", "list", "--json"],
    )
    inspected = runner.invoke(
        app,
        [
            "--session",
            "agent-123456789abc",
            "media",
            "inspect",
            "vlc",
            "--json",
        ],
    )

    assert listing.exit_code == 0
    assert inspected.exit_code == 0
    assert calls == [
        ("agent-123456789abc", "list"),
        ("agent-123456789abc", "inspect", "vlc"),
    ]
    assert json.loads(inspected.stdout)["data"]["position_seconds"] == 3.5


def test_session_list_json_is_empty_without_starting_a_session(tmp_path, monkeypatch) -> None:
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o700)
    runtime.chmod(0o700)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))

    result = CliRunner().invoke(app, ["session", "list", "--json"])

    assert result.exit_code == 0
    assert json.loads(result.stdout)["data"] == []


def test_unknown_session_error_is_json_when_requested(tmp_path, monkeypatch) -> None:
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o700)
    runtime.chmod(0o700)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))

    result = CliRunner().invoke(app, ["session", "show", "agent-000000000000", "--json"])

    assert result.exit_code == 2
    payload = json.loads(result.stdout)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "session_not_found"


def test_key_command_requires_a_managed_session(tmp_path, monkeypatch) -> None:
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o700)
    runtime.chmod(0o700)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))

    result = CliRunner().invoke(
        app,
        ["session", "key", "agent-000000000000", "CTRL+N", "--json"],
    )

    assert result.exit_code == 2
    payload = json.loads(result.stdout)
    assert payload["error"]["code"] == "session_not_found"


def test_user_desktop_window_list_requires_explicit_namespace_and_returns_json(
    monkeypatch,
) -> None:
    class FakeUserDesktopBackend:
        def list_windows(self):
            return {
                "target": {"kind": "user-desktop"},
                "count": 0,
                "windows": [],
            }

    monkeypatch.setattr("kde_interaction.cli.UserDesktopBackend", FakeUserDesktopBackend)
    runner = CliRunner()

    result = runner.invoke(app, ["user-desktop", "window", "list", "--json"])
    root_help = runner.invoke(app, ["--help"])

    assert result.exit_code == 0
    assert json.loads(result.stdout)["data"]["target"]["kind"] == "user-desktop"
    assert "user-desktop" in root_help.stdout


def test_shared_window_commands_use_explicit_user_desktop_session_only(monkeypatch) -> None:
    class FakeUserDesktopBackend:
        def list_windows(self):
            return {"target": {"kind": "user-desktop"}, "count": 0, "windows": []}

    def unexpected_private_manager():
        raise AssertionError("user-desktop must not instantiate or call a private session manager")

    monkeypatch.setattr("kde_interaction.cli.UserDesktopBackend", FakeUserDesktopBackend)
    monkeypatch.setattr("kde_interaction.cli._manager", unexpected_private_manager)

    result = CliRunner().invoke(
        app,
        ["--session", "user-desktop", "window", "list", "--json"],
    )

    assert result.exit_code == 0
    assert json.loads(result.stdout)["data"]["target"]["kind"] == "user-desktop"


def test_shared_commands_without_selector_use_private_default_only(monkeypatch) -> None:
    monkeypatch.delenv("KDE_AI_SESSION", raising=False)
    default_id = "agent-123456789abc"
    calls: list[str] = []

    class FakeManager:
        def default_session_id(self):
            return default_id

        def list_windows(self, session_id):
            calls.append(session_id)
            return {
                "target": {"kind": "agent-session", "session_id": session_id},
                "count": 0,
                "windows": [],
            }

    monkeypatch.setattr("kde_interaction.cli._manager", FakeManager)

    result = CliRunner().invoke(app, ["window", "list", "--json"])

    assert result.exit_code == 0
    assert json.loads(result.stdout)["data"]["target"] == {
        "kind": "agent-session",
        "session_id": default_id,
    }
    assert calls == [default_id]


def test_environment_session_selects_private_target_for_shared_cli(monkeypatch) -> None:
    environment_id = "agent-123456789abc"
    default_id = "agent-abcdef123456"
    monkeypatch.setenv("KDE_AI_SESSION", environment_id)
    calls: list[str] = []

    class FakeManager:
        def default_session_id(self):
            return default_id

        def list_windows(self, session_id):
            calls.append(session_id)
            return {
                "target": {"kind": "agent-session", "session_id": session_id},
                "count": 0,
                "windows": [],
            }

    monkeypatch.setattr("kde_interaction.cli._manager", FakeManager)
    result = CliRunner().invoke(app, ["window", "list", "--json"])

    assert result.exit_code == 0
    assert json.loads(result.stdout)["data"]["target"]["session_id"] == environment_id
    assert calls == [environment_id]


def test_missing_private_default_never_falls_back_to_user_desktop(monkeypatch) -> None:
    class FakeManager:
        def default_session_id(self):
            return None

    def unexpected_user_desktop():
        raise AssertionError("missing private default must not attach to the live desktop")

    monkeypatch.setattr("kde_interaction.cli._manager", FakeManager)
    monkeypatch.setattr("kde_interaction.cli.UserDesktopBackend", unexpected_user_desktop)

    result = CliRunner().invoke(app, ["window", "list", "--json"])

    assert result.exit_code == 2
    payload = json.loads(result.stdout)
    assert payload["error"]["code"] == "private_default_session_unset"


def test_shared_input_uses_matching_window_command_for_private_target(monkeypatch) -> None:
    session_id = "agent-123456789abc"
    window_id = "b5ff2329-3976-4c31-bf31-1538cb97c8f0"
    calls: list[tuple[str, str, str]] = []

    class FakeManager:
        def send_key_to_window(self, target_session_id, target_window_id, chord):
            calls.append((target_session_id, target_window_id, chord))
            return {
                "target": {
                    "kind": "agent-session",
                    "session_id": target_session_id,
                    "window_id": target_window_id,
                },
                "action": f"key {chord}",
                "effect_verified": False,
            }

    monkeypatch.setattr("kde_interaction.cli._manager", FakeManager)

    result = CliRunner().invoke(
        app,
        ["--session", session_id, "input", "key", window_id, "CTRL+S", "--json"],
    )

    assert result.exit_code == 0
    assert calls == [(session_id, window_id, "CTRL+S")]
    assert json.loads(result.stdout)["data"]["target"]["window_id"] == window_id


def test_window_wait_returns_a_unique_match_for_selected_target(monkeypatch) -> None:
    session_id = "agent-123456789abc"

    class FakeManager:
        def find_window(self, target_session_id, *, app_id, title):
            assert target_session_id == session_id
            assert app_id == "org.kde.kate"
            assert title == "notes"
            return {
                "target": {"kind": "agent-session", "session_id": target_session_id},
                "observed_at": "2026-11-10T12:00:00+00:00",
                "window": {
                    "window_id": "b5ff2329-3976-4c31-bf31-1538cb97c8f0",
                    "fullscreen": True,
                },
            }

    monkeypatch.setattr("kde_interaction.cli._manager", FakeManager)

    result = CliRunner().invoke(
        app,
        [
            "--session",
            session_id,
            "window",
            "wait",
            "--app-id",
            "org.kde.kate",
            "--title",
            "notes",
            "--timeout",
            "1",
            "--fullscreen",
            "--json",
        ],
    )

    assert result.exit_code == 0
    data = json.loads(result.stdout)["data"]
    assert data["window"]["window_id"] == "b5ff2329-3976-4c31-bf31-1538cb97c8f0"
    assert data["waited_seconds"] >= 0
    assert data["wait_condition"] == {"fullscreen": True}


def test_window_wait_cli_supports_borderless_display_coverage(monkeypatch) -> None:
    session_id = "agent-123456789abc"

    class FakeManager:
        def find_window(self, target_session_id, *, app_id, title):
            assert target_session_id == session_id
            assert app_id == "org.example.Game"
            assert title is None
            return {
                "target": {"kind": "agent-session", "session_id": target_session_id},
                "observed_at": "2026-11-10T12:00:00+00:00",
                "window": {
                    "window_id": "b5ff2329-3976-4c31-bf31-1538cb97c8f0",
                    "app_id": app_id,
                    "geometry": {
                        "x": 0,
                        "y": 0,
                        "width": 1920,
                        "height": 1080,
                        "coordinate_space": "logical-desktop-pixels",
                    },
                },
            }

        def displays(self, target_session_id):
            assert target_session_id == session_id
            return [
                {
                    "name": "DP-1",
                    "x": 0,
                    "y": 0,
                    "width": 1920,
                    "height": 1080,
                    "coordinate_space": "logical-desktop-pixels",
                }
            ]

    monkeypatch.setattr("kde_interaction.cli._manager", FakeManager)

    result = CliRunner().invoke(
        app,
        [
            "--session",
            session_id,
            "window",
            "wait",
            "--app-id",
            "org.example.Game",
            "--fills-display",
            "--json",
        ],
    )

    assert result.exit_code == 0
    data = json.loads(result.stdout)["data"]
    assert data["wait_condition"] == {"fills_display": True}
    assert data["matched_displays"] == ["DP-1"]

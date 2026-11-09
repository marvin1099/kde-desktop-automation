import json

from typer.testing import CliRunner

from kde_interaction.cli import app


def test_doctor_json_reports_user_desktop_as_disabled() -> None:
    result = CliRunner().invoke(app, ["doctor", "--json"])

    assert result.exit_code == 0
    report = json.loads(result.stdout)
    assert report["ok"] is True
    assert report["data"]["user_desktop"]["interaction_enabled"] is False
    assert report["data"]["safety"]["silent_fallback_to_user_desktop"] is False
    assert report["data"]["agent_session"]["input.inject"] is False
    assert report["data"]["agent_session"]["input.effect_ack"] is False


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

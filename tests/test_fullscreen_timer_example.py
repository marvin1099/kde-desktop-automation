from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

EXAMPLE = Path(__file__).parents[1] / "examples" / "start-timer-on-fullscreen.sh"
WINDOW_ID = "b5ff2329-3976-4c31-bf31-1538cb97c8f0"
APP_ID = "org.example.Game"


def _fake_binaries(tmp_path: Path) -> tuple[Path, Path]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake_cli = bin_dir / "kde-ai"
    fake_cli.write_text(
        """#!/usr/bin/env python3
import json
import os
import sys
from pathlib import Path

args = sys.argv[1:]
Path(os.environ['KDE_AI_CALL_LOG']).open('a').write(json.dumps(args) + '\\n')
if 'wait' in args:
    count_path = Path(os.environ['KDE_AI_WAIT_COUNT'])
    count = int(count_path.read_text()) if count_path.exists() else 0
    count += 1
    count_path.write_text(str(count))
    if count > 1 and os.environ.get('KDE_AI_FAIL_AFTER_RECHECK') == '1':
        error = {'schema_version': '1', 'ok': False, 'error': {'code': 'ambiguous_target'}}
        print(json.dumps(error))
        raise SystemExit(2)
    if os.environ.get('KDE_AI_FAIL_WAIT') == '1':
        error = {'schema_version': '1', 'ok': False, 'error': {'code': 'backend_unavailable'}}
        print(json.dumps(error))
        raise SystemExit(2)
    app_id = args[args.index('--app-id') + 1]
    data = {'window': {
        'window_id': '"""
        + WINDOW_ID
        + """',
        'app_id': app_id,
        'title': 'Game',
        'fullscreen': True,
    }}
    if '--fills-display' in args:
        data['matched_displays'] = ['DP-1']
    print(json.dumps({'schema_version': '1', 'ok': True, 'data': data}))
elif 'inspect' in args:
    app_id = args[args.index('inspect') + 2]
    fullscreen = os.environ.get('KDE_AI_INSPECT_FULLSCREEN', 'true') == 'true'
    print(json.dumps({'schema_version': '1', 'ok': True, 'data': {
        'window': {
            'window_id': '"""
        + WINDOW_ID
        + """',
            'app_id': '"""
        + APP_ID
        + """',
            'title': 'Game',
            'fullscreen': fullscreen,
        }
    }}))
else:
    raise SystemExit(3)
""",
        encoding="utf-8",
    )
    fake_cli.chmod(0o755)

    fake_timer = bin_dir / "fake-timer"
    fake_timer.write_text(
        """#!/usr/bin/env python3
import json
import os
import sys
from pathlib import Path
Path(os.environ['TIMER_RESULT']).write_text(json.dumps({
    'args': sys.argv[1:],
    'target': os.environ.get('KDE_AI_TARGET'),
    'window_id': os.environ.get('KDE_AI_WINDOW_ID'),
}))
""",
        encoding="utf-8",
    )
    fake_timer.chmod(0o755)
    return fake_cli, fake_timer


def _environment(tmp_path: Path, fake_cli: Path) -> dict[str, str]:
    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{fake_cli.parent}:{env['PATH']}",
            "KDE_AI_CALL_LOG": str(tmp_path / "cli-calls.jsonl"),
            "KDE_AI_WAIT_COUNT": str(tmp_path / "wait-count"),
            "TIMER_RESULT": str(tmp_path / "timer-result.json"),
        }
    )
    return env


def test_fullscreen_timer_wrapper_preserves_timer_argv_and_explicit_target(
    tmp_path: Path,
) -> None:
    fake_cli, fake_timer = _fake_binaries(tmp_path)
    result = subprocess.run(
        [
            "bash",
            str(EXAMPLE),
            "agent-123456789abc",
            APP_ID,
            "fullscreen",
            "--",
            str(fake_timer),
            "--category",
            "Any% No Major Glitches",
        ],
        check=False,
        capture_output=True,
        text=True,
        env=_environment(tmp_path, fake_cli),
        timeout=5,
    )

    assert result.returncode == 0, result.stderr
    timer_result = json.loads((tmp_path / "timer-result.json").read_text())
    assert timer_result == {
        "args": ["--category", "Any% No Major Glitches"],
        "target": "agent-123456789abc",
        "window_id": WINDOW_ID,
    }
    calls = [json.loads(line) for line in (tmp_path / "cli-calls.jsonl").read_text().splitlines()]
    assert calls[0][:4] == ["--session", "agent-123456789abc", "window", "wait"]
    assert "--fullscreen" in calls[0]
    assert calls[1][3:5] == ["inspect", WINDOW_ID]


def test_fullscreen_timer_wrapper_never_starts_timer_after_state_changes(
    tmp_path: Path,
) -> None:
    fake_cli, fake_timer = _fake_binaries(tmp_path)
    env = _environment(tmp_path, fake_cli)
    env["KDE_AI_INSPECT_FULLSCREEN"] = "false"
    env["KDE_AI_FAIL_AFTER_RECHECK"] = "1"
    result = subprocess.run(
        [
            "bash",
            str(EXAMPLE),
            "agent-123456789abc",
            APP_ID,
            "fullscreen",
            "--",
            str(fake_timer),
        ],
        check=False,
        capture_output=True,
        text=True,
        env=env,
        timeout=5,
    )

    assert result.returncode == 1
    assert "Fullscreen wait failed" in result.stderr
    assert not (tmp_path / "timer-result.json").exists()


def test_fullscreen_timer_wrapper_rechecks_borderless_display_coverage(
    tmp_path: Path,
) -> None:
    fake_cli, fake_timer = _fake_binaries(tmp_path)
    result = subprocess.run(
        [
            "bash",
            str(EXAMPLE),
            "agent-123456789abc",
            APP_ID,
            "fills-display",
            "--",
            str(fake_timer),
        ],
        check=False,
        capture_output=True,
        text=True,
        env=_environment(tmp_path, fake_cli),
        timeout=5,
    )

    assert result.returncode == 0, result.stderr
    calls = [json.loads(line) for line in (tmp_path / "cli-calls.jsonl").read_text().splitlines()]
    assert "--fills-display" in calls[0]
    assert "--title" in calls[2]
    assert json.loads((tmp_path / "timer-result.json").read_text())["window_id"] == WINDOW_ID


def test_fullscreen_timer_wrapper_rejects_missing_selector_or_timer() -> None:
    result = subprocess.run(
        ["bash", str(EXAMPLE), "org.example.Game"],
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
    )

    assert result.returncode == 2
    assert "Usage:" in result.stderr

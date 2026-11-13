from __future__ import annotations

import subprocess
import sys
import tomllib
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
RELEASE_TAG_CHECK = PROJECT_ROOT / "scripts" / "check_release_tag.py"


def test_release_tag_check_accepts_project_version() -> None:
    with (PROJECT_ROOT / "pyproject.toml").open("rb") as project_file:
        version = tomllib.load(project_file)["project"]["version"]

    result = subprocess.run(
        [sys.executable, str(RELEASE_TAG_CHECK), f"v{version}"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        check=False,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert f"matches project version {version}" in result.stdout


def test_release_tag_check_rejects_a_different_version() -> None:
    result = subprocess.run(
        [sys.executable, str(RELEASE_TAG_CHECK), "v0.0.0"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        check=False,
        text=True,
    )

    assert result.returncode == 1
    assert "does not match project version" in result.stderr

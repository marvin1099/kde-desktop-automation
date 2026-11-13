from __future__ import annotations

import sys
import tomllib
from pathlib import Path


def main() -> int:
    if len(sys.argv) != 2:
        print("Usage: python scripts/check_release_tag.py vX.Y.Z", file=sys.stderr)
        return 2

    tag = sys.argv[1]
    project_file_path = Path(__file__).resolve().parents[1] / "pyproject.toml"
    with project_file_path.open("rb") as project_file:
        version = tomllib.load(project_file)["project"]["version"]
    expected_tag = f"v{version}"
    if tag != expected_tag:
        print(
            f"Release tag {tag!r} does not match project version {version!r}; "
            f"expected {expected_tag!r}.",
            file=sys.stderr,
        )
        return 1

    print(f"Release tag {tag} matches project version {version}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

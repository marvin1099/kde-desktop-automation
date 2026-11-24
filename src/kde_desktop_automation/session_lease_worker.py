from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Sequence

from kde_desktop_automation.paths import RuntimePaths
from kde_desktop_automation.sessions import SessionManager

_READY_FD_ENV = "KDE_DESKTOP_AUTOMATION_READY_FD"


def _announce_ready() -> None:
    ready_fd = int(os.environ[_READY_FD_ENV])
    os.write(ready_fd, f"READY:{os.getpid()}\n".encode())
    os.close(ready_fd)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("session_id")
    arguments = parser.parse_args(argv)
    try:
        manager = SessionManager(RuntimePaths.from_environment())
        # Fail before acknowledging startup if the requested record is unavailable.
        manager.store.load(arguments.session_id)
        _announce_ready()
        manager.run_session_lease_worker(arguments.session_id, os.getpid())
    except Exception as exc:
        print(f"session lease worker failed: {exc}", file=sys.stderr, flush=True)
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised by the manager subprocess
    raise SystemExit(main())

from __future__ import annotations

import argparse
import asyncio
import os
import signal
import sys
import time
from collections.abc import Sequence
from contextlib import suppress
from typing import Any

from dbus_next import Message, MessageType  # type: ignore[attr-defined]
from dbus_next.aio import MessageBus  # type: ignore[attr-defined]

_DBUS_INHIBITORS = {
    "screen-lock": (
        "org.freedesktop.ScreenSaver",
        "/ScreenSaver",
        "org.freedesktop.ScreenSaver",
    ),
    "power-management": (
        "org.freedesktop.PowerManagement.Inhibit",
        "/org/freedesktop/PowerManagement/Inhibit",
        "org.freedesktop.PowerManagement.Inhibit",
    ),
}
_READY_FD_ENV = "KDE_DESKTOP_AUTOMATION_READY_FD"


def _announce_ready() -> None:
    ready_fd = int(os.environ[_READY_FD_ENV])
    os.write(ready_fd, f"READY:{os.getpid()}\n".encode())
    os.close(ready_fd)


async def _hold_dbus_inhibitor(
    inhibitor: str,
    duration_seconds: int,
    reason: str,
) -> None:
    service, object_path, interface = _DBUS_INHIBITORS[inhibitor]
    loop = asyncio.get_running_loop()
    stop_requested = asyncio.Event()
    for signum in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(signum, stop_requested.set)

    bus: Any = MessageBus()
    bus = await bus.connect()
    cookie: int | None = None
    try:
        reply = await bus.call(
            Message(
                destination=service,
                path=object_path,
                interface=interface,
                member="Inhibit",
                signature="ss",
                body=["KDE Desktop Automation", reason],
            )
        )
        assert reply is not None
        if reply.message_type == MessageType.ERROR:
            raise RuntimeError(reply.body[0] if reply.body else "ScreenSaver.Inhibit failed")
        cookie = int(reply.body[0])
        _announce_ready()

        if duration_seconds == -1:
            await stop_requested.wait()
        else:
            with suppress(TimeoutError):
                await asyncio.wait_for(stop_requested.wait(), timeout=duration_seconds)

        reply = await bus.call(
            Message(
                destination=service,
                path=object_path,
                interface=interface,
                member="UnInhibit",
                signature="u",
                body=[cookie],
            )
        )
        assert reply is not None
        if reply.message_type == MessageType.ERROR:
            raise RuntimeError(reply.body[0] if reply.body else "ScreenSaver.UnInhibit failed")
        cookie = None
    finally:
        # Disconnecting also releases the cookie if explicit UnInhibit fails.
        bus.disconnect()
        if cookie is not None:
            await bus.wait_for_disconnect()


async def _run(mode: str, duration_seconds: int, reason: str | None) -> None:
    if mode == "hold":
        _announce_ready()
        if duration_seconds == -1:
            while True:
                signal.pause()
        else:
            time.sleep(duration_seconds)
        return
    if reason is None:
        raise ValueError("a reason is required for a D-Bus power inhibitor")
    await _hold_dbus_inhibitor(mode, duration_seconds, reason)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("hold", *_DBUS_INHIBITORS))
    parser.add_argument("duration_seconds", type=int)
    parser.add_argument("--reason")
    arguments = parser.parse_args(argv)
    try:
        asyncio.run(_run(arguments.mode, arguments.duration_seconds, arguments.reason))
    except Exception as exc:
        print(f"power inhibitor worker failed: {exc}", file=sys.stderr, flush=True)
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised by the manager subprocess
    raise SystemExit(main())

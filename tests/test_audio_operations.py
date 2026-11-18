from __future__ import annotations

from typing import Any

import pytest

from kde_desktop_automation.errors import InteractionError
from kde_desktop_automation.operations import DesktopOperations


class FakeUserDesktop:
    environ = {"XDG_RUNTIME_DIR": "/run/user/live"}


class FakeAudioBackend:
    def __init__(self, environ: dict[str, str]) -> None:
        self.environ = environ

    def list_audio(self, *, target: dict[str, Any]) -> dict[str, Any]:
        return {"target": target, "runtime_dir": self.environ["XDG_RUNTIME_DIR"]}

    def wait_for_mute(
        self,
        direction: str,
        device_name: str,
        muted: bool,
        *,
        timeout: float,
        target: dict[str, Any],
    ) -> dict[str, Any]:
        return {
            "target": target,
            "direction": direction,
            "device": device_name,
            "muted": muted,
            "timeout": timeout,
        }


def test_audio_operations_require_explicit_live_desktop_without_private_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unexpected_backend(_environ: dict[str, str]) -> FakeAudioBackend:
        raise AssertionError("private targets must never construct a host audio backend")

    monkeypatch.setattr(
        "kde_desktop_automation.operations.PipeWireAudioBackend", unexpected_backend
    )
    operations = DesktopOperations("agent-123456789abc", manager=object())  # type: ignore[arg-type]

    with pytest.raises(InteractionError) as error:
        operations.audio_list()

    assert error.value.code == "audio_requires_live_target"


def test_audio_operations_use_the_explicit_live_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("kde_desktop_automation.operations.PipeWireAudioBackend", FakeAudioBackend)
    operations = DesktopOperations(
        "user-desktop",
        user_desktop=FakeUserDesktop(),  # type: ignore[arg-type]
    )

    assert operations.audio_list() == {
        "target": {"kind": "user-desktop"},
        "runtime_dir": "/run/user/live",
    }

    assert operations.audio_wait_for_mute("source", "usb_mic", True, timeout=3.0) == {
        "target": {"kind": "user-desktop"},
        "direction": "source",
        "device": "usb_mic",
        "muted": True,
        "timeout": 3.0,
    }

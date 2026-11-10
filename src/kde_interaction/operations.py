from __future__ import annotations

import os
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from kde_interaction.backends.user_desktop import UserDesktopBackend
from kde_interaction.errors import InteractionError
from kde_interaction.paths import RuntimePaths
from kde_interaction.sessions import SessionManager

USER_DESKTOP = "user-desktop"


class DesktopOperations:
    """Shared target-aware desktop operations used by CLI and MCP transports."""

    def __init__(
        self,
        session: str | None = None,
        *,
        manager: SessionManager | None = None,
        manager_factory: Callable[[], SessionManager] | None = None,
        user_desktop: UserDesktopBackend | None = None,
        user_desktop_factory: Callable[[], UserDesktopBackend] | None = None,
    ) -> None:
        self._session_id: str | None
        self._manager: SessionManager | None
        self._user_desktop: UserDesktopBackend | None
        if session == USER_DESKTOP:
            self._session_id = None
            self._manager = None
            self._user_desktop = user_desktop or (user_desktop_factory or UserDesktopBackend)()
            return

        self._manager = manager or (manager_factory or self._default_manager)()
        selected = session
        if selected is None:
            configured_session = os.environ.get("KDE_AI_SESSION")
            if configured_session == USER_DESKTOP:
                raise InteractionError(
                    "user_desktop_requires_explicit_selection",
                    "KDE_AI_SESSION may select only a managed private session. "
                    "Select the live desktop explicitly with --session user-desktop "
                    "or session='user-desktop' in MCP.",
                )
            selected = (
                configured_session
                if configured_session is not None
                else self._manager.default_session_id()
            )
            if selected is None:
                raise InteractionError(
                    "private_default_session_unset",
                    "No private session is selected. Set KDE_AI_SESSION, configure a "
                    "private default, or pass a session ID (--session in CLI); live "
                    "desktop access always requires explicit selection.",
                )
        if not isinstance(selected, str) or not selected:
            raise InteractionError(
                "invalid_session_selector",
                "The session selector must be a managed session ID or 'user-desktop'.",
            )
        self._session_id = selected
        self._user_desktop = None

    @staticmethod
    def _default_manager() -> SessionManager:
        return SessionManager(RuntimePaths.from_environment())

    @property
    def session_id(self) -> str | None:
        return self._session_id

    def window_list(self) -> dict[str, Any]:
        if self._user_desktop is not None:
            return self._user_desktop.list_windows()
        return self._private_manager().list_windows(self._private_session_id())

    def window_find(self, *, app_id: str | None, title: str | None) -> dict[str, Any]:
        if self._user_desktop is not None:
            return self._user_desktop.find_window(app_id=app_id, title=title)
        return self._private_manager().find_window(
            self._private_session_id(), app_id=app_id, title=title
        )

    def window_inspect(self, window_id: str) -> dict[str, Any]:
        if self._user_desktop is not None:
            return self._user_desktop.inspect_window(window_id)
        return self._private_manager().inspect_window(self._private_session_id(), window_id)

    def window_activate(self, window_id: str) -> dict[str, Any]:
        if self._user_desktop is not None:
            return self._user_desktop.activate_window(window_id)
        return self._private_manager().activate_window(self._private_session_id(), window_id)

    def window_wait(
        self,
        *,
        app_id: str | None,
        title: str | None,
        timeout: float = 10.0,
    ) -> dict[str, Any]:
        if timeout < 0.1 or timeout > 60.0:
            raise InteractionError(
                "invalid_timeout",
                "Window wait timeout must be between 0.1 and 60 seconds.",
                details={"timeout_seconds": timeout},
            )
        started = time.monotonic()
        deadline = started + timeout
        while True:
            try:
                result = self.window_find(app_id=app_id, title=title)
                return result | {"waited_seconds": round(time.monotonic() - started, 3)}
            except InteractionError as exc:
                if exc.code != "window_not_found":
                    raise
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise InteractionError(
                    "window_wait_timeout",
                    "No matching window appeared before the timeout.",
                    details={
                        "app_id": app_id,
                        "title": title,
                        "timeout_seconds": timeout,
                        "session": self._session_id or USER_DESKTOP,
                    },
                ) from None
            time.sleep(min(0.2, remaining))

    def input_key(self, window_id: str, chord: str) -> dict[str, Any]:
        if self._user_desktop is not None:
            return self._user_desktop.send_key(window_id, chord)
        return self._private_manager().send_key_to_window(
            self._private_session_id(), window_id, chord
        )

    def input_type(self, window_id: str, text: str) -> dict[str, Any]:
        if self._user_desktop is not None:
            return self._user_desktop.send_text(window_id, text)
        return self._private_manager().send_text_to_window(
            self._private_session_id(), window_id, text
        )

    def input_click(
        self,
        window_id: str,
        x: float,
        y: float,
        *,
        button: str = "left",
    ) -> dict[str, Any]:
        if self._user_desktop is not None:
            return self._user_desktop.click_window(window_id, x, y, button=button)
        return self._private_manager().click_window(
            self._private_session_id(), window_id, x, y, button=button
        )

    def display_list(self) -> dict[str, Any]:
        if self._user_desktop is not None:
            displays = self._user_desktop.list_displays()
            return {
                "target": {"kind": USER_DESKTOP},
                "count": len(displays),
                "displays": displays,
            }
        session_id = self._private_session_id()
        displays = self._private_manager().displays(session_id)
        return {
            "target": {"kind": "agent-session", "session_id": session_id},
            "count": len(displays),
            "displays": displays,
        }

    def screenshot(self, output: Path, *, overwrite: bool = False) -> dict[str, Any]:
        if self._user_desktop is not None:
            return self._user_desktop.screenshot(output, overwrite=overwrite)
        return self._private_manager().screenshot(
            self._private_session_id(), output, overwrite=overwrite
        )

    def capabilities(self) -> dict[str, Any]:
        if self._user_desktop is not None:
            return self._user_desktop.capabilities()
        session = self._private_manager().get_session(self._private_session_id())
        if session["status"] != "running":
            raise InteractionError(
                "session_not_running",
                "The configured private target is not running.",
                details={"session_id": self._private_session_id(), "status": session["status"]},
            )
        return {
            "target": {"kind": "agent-session", "session_id": self._private_session_id()},
            "capabilities": session["capabilities"],
            "window_management_protocol_version": session["window_management_protocol_version"],
            "input_protocol_version": session["input_protocol_version"],
            "limitations": session["limitations"],
        }

    def _private_manager(self) -> SessionManager:
        if self._manager is None:
            raise InteractionError("invalid_target", "No private session manager is selected.")
        return self._manager

    def _private_session_id(self) -> str:
        if self._session_id is None:
            raise InteractionError("invalid_target", "No managed private session is selected.")
        return self._session_id

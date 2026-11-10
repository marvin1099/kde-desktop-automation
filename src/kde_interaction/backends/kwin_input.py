from __future__ import annotations

import ctypes
import ctypes.util
import os
import socket
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from kde_interaction.errors import InteractionError

_FAKE_INPUT_INTERFACE = "org_kde_kwin_fake_input"
_FAKE_INPUT_VERSION = 6
_KEY_STATE_RELEASED = 0
_KEY_STATE_PRESSED = 1
_BTN_LEFT = 0x110
_BTN_RIGHT = 0x111
_BTN_MIDDLE = 0x112

# Request order and signatures from KDE's fake-input protocol. This protocol is
# deliberately treated as best-effort: KWin exposes no authorization or event
# delivery acknowledgement, and explicitly permits compositors to ignore events.
_FAKE_INPUT_REQUESTS = (
    ("authenticate", "ss"),
    ("pointer_motion", "ff"),
    ("button", "uu"),
    ("axis", "uf"),
    ("touch_down", "2uff"),
    ("touch_motion", "2uff"),
    ("touch_up", "2u"),
    ("touch_cancel", "2"),
    ("touch_frame", "2"),
    ("pointer_motion_absolute", "3ff"),
    ("keyboard_key", "4uu"),
    ("destroy", "5"),
    ("keyboard_keysym", "6uu"),
)


class _WlMessage(ctypes.Structure):
    _fields_ = [
        ("name", ctypes.c_char_p),
        ("signature", ctypes.c_char_p),
        ("types", ctypes.c_void_p),
    ]


class _WlInterface(ctypes.Structure):
    _fields_ = [
        ("name", ctypes.c_char_p),
        ("version", ctypes.c_int),
        ("method_count", ctypes.c_int),
        ("methods", ctypes.POINTER(_WlMessage)),
        ("event_count", ctypes.c_int),
        ("events", ctypes.POINTER(_WlMessage)),
    ]


_FAKE_INPUT_MESSAGES = (_WlMessage * len(_FAKE_INPUT_REQUESTS))(
    *(
        _WlMessage(name.encode(), signature.encode(), None)
        for name, signature in _FAKE_INPUT_REQUESTS
    )
)
_FAKE_INPUT_WL_INTERFACE = _WlInterface(
    _FAKE_INPUT_INTERFACE.encode(),
    _FAKE_INPUT_VERSION,
    len(_FAKE_INPUT_REQUESTS),
    _FAKE_INPUT_MESSAGES,
    0,
    None,
)

_GlobalCallback = ctypes.CFUNCTYPE(
    None,
    ctypes.c_void_p,
    ctypes.c_void_p,
    ctypes.c_uint32,
    ctypes.c_char_p,
    ctypes.c_uint32,
)
_GlobalRemoveCallback = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32)


class _RegistryListener(ctypes.Structure):
    _fields_ = [("global_", _GlobalCallback), ("global_remove", _GlobalRemoveCallback)]


@dataclass(frozen=True, slots=True)
class InputReceipt:
    action: str
    events_submitted: int
    server_roundtrip_completed: bool
    effect_verified: bool = False

    def to_dict(
        self,
        *,
        session_id: str | None,
        wayland_display: str,
        target_kind: Literal["agent-session", "user-desktop"] = "agent-session",
    ) -> dict[str, Any]:
        target: dict[str, Any] = {
            "kind": target_kind,
            "wayland_display": wayland_display,
            "keyboard_focus": "current KWin focus; not independently identified",
        }
        if session_id is not None:
            target["session_id"] = session_id
        return {
            "target": target,
            "action": self.action,
            "events_submitted": self.events_submitted,
            "server_roundtrip_completed": self.server_roundtrip_completed,
            "effect_verified": self.effect_verified,
            "input_routing": "selected compositor active focus; not directly window-bound",
            "note": (
                "KWin's fake-input protocol does not acknowledge authorization or app delivery. "
                "The request reached the selected Wayland server and is routed through compositor "
                "focus; independently inspect a fresh screenshot or app state before treating it "
                "as effective."
            ),
        }


def parse_key_chord(value: str) -> tuple[tuple[str, int, int], ...]:
    """Convert a simple KEY or CTRL+SHIFT+KEY chord to XKB keysym transitions."""
    parts = [part.strip().upper() for part in value.split("+")]
    if not parts or any(not part for part in parts):
        raise InteractionError("invalid_key", "Key must be a name or chord such as CTRL+SHIFT+S.")

    modifiers = {
        "CTRL": ("CTRL", 0xFFE3),
        "CONTROL": ("CTRL", 0xFFE3),
        "LCTRL": ("CTRL", 0xFFE3),
        "RCTRL": ("CTRL", 0xFFE4),
        "SHIFT": ("SHIFT", 0xFFE1),
        "LSHIFT": ("SHIFT", 0xFFE1),
        "RSHIFT": ("SHIFT", 0xFFE2),
        "ALT": ("ALT", 0xFFE9),
        "LALT": ("ALT", 0xFFE9),
        "RALT": ("ALT", 0xFFEA),
        "META": ("META", 0xFFEB),
        "SUPER": ("META", 0xFFEB),
        "WIN": ("META", 0xFFEB),
    }
    modifier_parts = parts[:-1]
    if any(part not in modifiers for part in modifier_parts):
        raise InteractionError(
            "invalid_key",
            "Only CTRL, SHIFT, ALT, and META modifiers are supported in key chords.",
        )
    normalized_modifiers = [modifiers[part] for part in modifier_parts]
    if len({name for name, _ in normalized_modifiers}) != len(normalized_modifiers):
        raise InteractionError("invalid_key", "A key chord cannot repeat a modifier.")

    key_name = parts[-1]
    keysym = _keysym(key_name)
    presses = [(name, value, _KEY_STATE_PRESSED) for name, value in normalized_modifiers]
    presses.append((key_name, keysym, _KEY_STATE_PRESSED))
    releases = [(key_name, keysym, _KEY_STATE_RELEASED)]
    releases.extend(
        (name, value, _KEY_STATE_RELEASED) for name, value in reversed(normalized_modifiers)
    )
    return tuple((name, code, state) for name, code, state in (*presses, *releases))


def _keysym(name: str) -> int:
    if len(name) == 1 and name.isascii() and name.isalpha():
        return ord(name.lower())
    if len(name) == 1 and name.isascii() and name.isdigit():
        return ord(name)
    if name in _NAMED_KEYSYMS:
        return _NAMED_KEYSYMS[name]
    if name.startswith("F") and name[1:].isdigit() and 1 <= int(name[1:]) <= 12:
        function_key = int(name[1:])
        return 0xFFBE + function_key - 1
    raise InteractionError(
        "invalid_key",
        f"Unsupported key name '{name}'. Use a letter, digit, F1–F12, or a documented key name.",
        details={"key": name},
    )


def _text_keysyms(text: str) -> tuple[int, ...]:
    if not text:
        raise InteractionError("empty_text", "Text input must not be empty.")
    if len(text) > 4096:
        raise InteractionError(
            "text_too_long",
            "A single text action is limited to 4096 Unicode characters.",
            details={"characters": len(text), "maximum": 4096},
        )

    normalized_text = text.replace("\r\n", "\n").replace("\r", "\n")
    keysyms: list[int] = []
    for character in normalized_text:
        codepoint = ord(character)
        if character == "\n" or character == "\r":
            keysym = 0xFF0D  # XKB_KEY_Return
        elif character == "\t":
            keysym = 0xFF09  # XKB_KEY_Tab
        elif codepoint < 0x20 or 0x7F <= codepoint < 0xA0:
            raise InteractionError(
                "unsupported_text_character",
                "Text input contains an unsupported control character.",
                details={"codepoint": f"U+{codepoint:04X}"},
            )
        elif 0xD800 <= codepoint <= 0xDFFF:
            raise InteractionError(
                "unsupported_text_character",
                "Text input contains an invalid Unicode surrogate.",
                details={"codepoint": f"U+{codepoint:04X}"},
            )
        elif codepoint <= 0xFF:
            keysym = codepoint
        else:
            keysym = 0x01000000 | codepoint
        keysyms.append(keysym)
    return tuple(keysyms)


def _fixed(value: int) -> int:
    return value * 256


class _WaylandConnection:
    """Registry-only Wayland connection; never submits compositor actions."""

    def __init__(self, socket_path: Path) -> None:
        self.socket_path = socket_path
        self.lib = _wayland_library()
        self.display: int | None = None
        self.registry: int | None = None
        self.globals: dict[str, tuple[int, int]] = {}
        self._listener: _RegistryListener | None = None
        self._connect()

    def close(self) -> None:
        if self.registry is not None:
            self.lib.wl_proxy_destroy(self.registry)
            self.registry = None
        if self.display is not None:
            self.lib.wl_display_disconnect(self.display)
            self.display = None

    def __enter__(self) -> _WaylandConnection:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def roundtrip(self) -> None:
        if self.display is None:
            raise InteractionError("wayland_connection_closed", "The Wayland connection is closed.")
        result = self.lib.wl_display_roundtrip(self.display)
        if result < 0:
            error_code = self.lib.wl_display_get_error(self.display)
            raise InteractionError(
                "wayland_roundtrip_failed",
                "The Wayland server closed or rejected the connection.",
                details={"socket": str(self.socket_path), "error_code": error_code},
            )

    def _connect(self) -> None:
        try:
            socket_stat = self.socket_path.lstat()
        except OSError as exc:
            raise InteractionError(
                "wayland_socket_unavailable",
                "The selected Wayland socket does not exist or cannot be inspected.",
                details={"socket": str(self.socket_path), "reason": str(exc)},
            ) from exc
        if not stat.S_ISSOCK(socket_stat.st_mode) or socket_stat.st_uid != os.getuid():
            raise InteractionError(
                "unsafe_wayland_socket",
                "Refusing to connect to a Wayland endpoint that is not a user-owned socket.",
                details={"socket": str(self.socket_path)},
            )

        client_socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            client_socket.connect(str(self.socket_path))
            fd = client_socket.detach()
            self.display = self.lib.wl_display_connect_to_fd(fd)
            if not self.display:
                os.close(fd)
                raise OSError("libwayland-client could not create a display connection")
        except OSError as exc:
            client_socket.close()
            raise InteractionError(
                "wayland_connection_failed",
                "Could not connect to the selected Wayland compositor.",
                details={"socket": str(self.socket_path), "reason": str(exc)},
            ) from exc

        registry_interface = ctypes.addressof(
            _WlInterface.in_dll(self.lib, "wl_registry_interface")
        )
        registry_version = self.lib.wl_proxy_get_version(self.display)
        self.registry = self.lib.wl_proxy_marshal_flags(
            self.display,
            1,  # wl_display.get_registry
            registry_interface,
            registry_version,
            0,
            None,
        )
        if not self.registry:
            self.close()
            raise InteractionError(
                "wayland_registry_failed", "Could not create a Wayland registry client."
            )

        @_GlobalCallback  # type: ignore[untyped-decorator]
        def on_global(
            _data: int,
            _registry: int,
            name: int,
            interface_name: bytes,
            version: int,
        ) -> None:
            self.globals[interface_name.decode("ascii", errors="replace")] = (name, version)

        @_GlobalRemoveCallback  # type: ignore[untyped-decorator]
        def on_global_remove(_data: int, _registry: int, name: int) -> None:
            for interface_name, (global_name, _version) in tuple(self.globals.items()):
                if global_name == name:
                    self.globals.pop(interface_name, None)

        self._listener = _RegistryListener(on_global, on_global_remove)
        listener_pointer = ctypes.cast(
            ctypes.pointer(self._listener), ctypes.POINTER(ctypes.c_void_p)
        )
        if self.lib.wl_proxy_add_listener(self.registry, listener_pointer, None) != 0:
            self.close()
            raise InteractionError(
                "wayland_registry_failed", "Could not listen to Wayland globals."
            )
        try:
            self.roundtrip()
        except Exception:
            self.close()
            raise


class KWinFakeInput:
    """Best-effort input client bound to one explicitly selected Wayland socket."""

    def __init__(
        self,
        socket_path: Path,
        *,
        application_name: str,
        reason: str,
    ) -> None:
        self.socket_path = socket_path
        self._connection = _WaylandConnection(socket_path)
        self._fake_input: int | None = None
        self._fake_input_version = 0
        try:
            self._bind_fake_input(application_name, reason)
        except Exception:
            self.close()
            raise

    @property
    def fake_input_version(self) -> int:
        return self._fake_input_version

    def send_key(self, chord: str) -> InputReceipt:
        transitions = parse_key_chord(chord)
        self._require_fake_input(minimum_version=6, operation="layout-aware keyboard input")
        for _name, keysym, state in transitions:
            self._request(12, ctypes.c_uint32(keysym), ctypes.c_uint32(state))
        return InputReceipt(
            action=f"key {chord.upper()}",
            events_submitted=len(transitions),
            server_roundtrip_completed=True,
        )

    def send_text(self, text: str) -> InputReceipt:
        keysyms = _text_keysyms(text)
        self._require_fake_input(minimum_version=6, operation="Unicode text input")
        for keysym in keysyms:
            self._request(12, ctypes.c_uint32(keysym), ctypes.c_uint32(_KEY_STATE_PRESSED))
            self._request(12, ctypes.c_uint32(keysym), ctypes.c_uint32(_KEY_STATE_RELEASED))
        return InputReceipt(
            action=f"type text ({len(text)} Unicode characters)",
            events_submitted=2 * len(keysyms),
            server_roundtrip_completed=True,
        )

    def click(self, x: int, y: int, button: str = "left") -> InputReceipt:
        self._require_fake_input(minimum_version=3, operation="pointer input")
        buttons = {"left": _BTN_LEFT, "middle": _BTN_MIDDLE, "right": _BTN_RIGHT}
        button_name = button.lower()
        if button_name not in buttons:
            raise InteractionError(
                "invalid_button",
                "Mouse button must be left, middle, or right.",
                details={"button": button},
            )
        self._request(9, ctypes.c_int32(_fixed(x)), ctypes.c_int32(_fixed(y)))
        self._request(2, ctypes.c_uint32(buttons[button_name]), ctypes.c_uint32(_KEY_STATE_PRESSED))
        self._request(
            2, ctypes.c_uint32(buttons[button_name]), ctypes.c_uint32(_KEY_STATE_RELEASED)
        )
        return InputReceipt(
            action=f"click {button_name} at ({x}, {y}) logical output pixels",
            events_submitted=3,
            server_roundtrip_completed=True,
        )

    def close(self) -> None:
        if self._fake_input is not None:
            # Local proxy destruction avoids the protocol's remote v5 destroy request.
            self._connection.lib.wl_proxy_destroy(self._fake_input)
            self._fake_input = None
        self._connection.close()

    def __enter__(self) -> KWinFakeInput:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def _bind_fake_input(self, application_name: str, reason: str) -> None:
        fake_global = self._connection.globals.get(_FAKE_INPUT_INTERFACE)
        if fake_global is None:
            self.close()
            raise InteractionError(
                "input_unavailable",
                "The selected KWin session does not advertise its fake-input protocol.",
                details={"socket": str(self.socket_path), "interface": _FAKE_INPUT_INTERFACE},
            )
        global_name, server_version = fake_global
        self._fake_input_version = min(server_version, _FAKE_INPUT_VERSION)
        self._fake_input = self._connection.lib.wl_proxy_marshal_flags(
            self._connection.registry,
            0,  # wl_registry.bind
            ctypes.addressof(_FAKE_INPUT_WL_INTERFACE),
            self._fake_input_version,
            0,
            ctypes.c_uint32(global_name),
            ctypes.c_char_p(_FAKE_INPUT_INTERFACE.encode()),
            ctypes.c_uint32(self._fake_input_version),
            None,
        )
        if not self._fake_input:
            self.close()
            raise InteractionError(
                "input_bind_failed", "Could not bind KWin's fake-input protocol."
            )
        self._request(
            0,
            ctypes.c_char_p(application_name.encode("utf-8")),
            ctypes.c_char_p(reason.encode("utf-8")),
        )

    def _require_fake_input(self, *, minimum_version: int, operation: str) -> None:
        if self._fake_input is None or self._fake_input_version < minimum_version:
            raise InteractionError(
                "input_capability_unavailable",
                f"The selected KWin protocol version cannot provide {operation}.",
                details={
                    "available_version": self._fake_input_version,
                    "required_version": minimum_version,
                    "operation": operation,
                },
            )

    def _request(self, opcode: int, *arguments: object) -> None:
        if self._fake_input is None:
            raise InteractionError(
                "input_connection_closed", "The KWin input connection is closed."
            )
        self._connection.lib.wl_proxy_marshal_flags(
            self._fake_input,
            opcode,
            None,
            self._fake_input_version,
            0,
            *arguments,
        )
        self._connection.roundtrip()


def fake_input_version(socket_path: Path) -> int | None:
    """Read the KWin fake-input global version without submitting input."""
    with _WaylandConnection(socket_path) as connection:
        advertised = connection.globals.get(_FAKE_INPUT_INTERFACE)
        return None if advertised is None else advertised[1]


def _wayland_library() -> Any:
    library_name = ctypes.util.find_library("wayland-client")
    if library_name is None:
        raise InteractionError(
            "input_backend_unavailable",
            "libwayland-client is required for session-bound KWin input.",
        )
    try:
        library = ctypes.CDLL(library_name, use_errno=True)
    except OSError as exc:
        raise InteractionError(
            "input_backend_unavailable",
            "Could not load libwayland-client for session-bound KWin input.",
            details={"library": library_name, "reason": str(exc)},
        ) from exc
    library.wl_display_connect_to_fd.argtypes = [ctypes.c_int]
    library.wl_display_connect_to_fd.restype = ctypes.c_void_p
    library.wl_display_disconnect.argtypes = [ctypes.c_void_p]
    library.wl_display_roundtrip.argtypes = [ctypes.c_void_p]
    library.wl_display_roundtrip.restype = ctypes.c_int
    library.wl_display_get_error.argtypes = [ctypes.c_void_p]
    library.wl_display_get_error.restype = ctypes.c_int
    library.wl_proxy_marshal_flags.argtypes = [
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
    ]
    library.wl_proxy_marshal_flags.restype = ctypes.c_void_p
    library.wl_proxy_add_listener.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.c_void_p,
    ]
    library.wl_proxy_add_listener.restype = ctypes.c_int
    library.wl_proxy_destroy.argtypes = [ctypes.c_void_p]
    library.wl_proxy_get_version.argtypes = [ctypes.c_void_p]
    library.wl_proxy_get_version.restype = ctypes.c_uint32
    return library


_NAMED_KEYSYMS = {
    "ENTER": 0xFF0D,
    "RETURN": 0xFF0D,
    "ESC": 0xFF1B,
    "ESCAPE": 0xFF1B,
    "TAB": 0xFF09,
    "SPACE": 0x20,
    "BACKSPACE": 0xFF08,
    "DELETE": 0xFFFF,
    "INSERT": 0xFF63,
    "HOME": 0xFF50,
    "END": 0xFF57,
    "PAGEUP": 0xFF55,
    "PAGEDOWN": 0xFF56,
    "LEFT": 0xFF51,
    "UP": 0xFF52,
    "RIGHT": 0xFF53,
    "DOWN": 0xFF54,
    "MINUS": 0x2D,
    "EQUALS": 0x3D,
    "LEFTBRACKET": 0x5B,
    "RIGHTBRACKET": 0x5D,
    "BACKSLASH": 0x5C,
    "SEMICOLON": 0x3B,
    "APOSTROPHE": 0x27,
    "GRAVE": 0x60,
    "COMMA": 0x2C,
    "PERIOD": 0x2E,
    "SLASH": 0x2F,
    "CTRL": 0xFFE3,
    "CONTROL": 0xFFE3,
    "SHIFT": 0xFFE1,
    "ALT": 0xFFE9,
    "META": 0xFFEB,
    "SUPER": 0xFFEB,
}

from __future__ import annotations

import ctypes
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from kde_desktop_automation.backends.kwin_input import _WaylandConnection, _WlInterface, _WlMessage
from kde_desktop_automation.errors import InteractionError

_WINDOW_MANAGEMENT = "org_kde_plasma_window_management"
_WINDOW = "org_kde_plasma_window"
_STACKING_ORDER = "org_kde_plasma_stacking_order"
_WINDOW_MANAGEMENT_VERSION = 22
_WINDOW_VERSION = 21
_STATE_ACTIVE = 0x1
_STATE_MINIMIZED = 0x2
_STATE_MAXIMIZED = 0x4
_STATE_FULLSCREEN = 0x8
_STATE_CLOSEABLE = 0x100
_STATE_MINIMIZABLE = 0x200
_STATE_MAXIMIZABLE = 0x400
_STATE_FULLSCREENABLE = 0x800
_STATE_MOVABLE = 0x8000
_STATE_RESIZABLE = 0x10000
_MINIMUM_UUID_VERSION = 13
_MAX_WINDOWS = 2048
_WINDOW_STATE_READBACK_TIMEOUT_SECONDS = 1.0
_WINDOW_STATE_READBACK_INTERVAL_SECONDS = 0.05


def _interface(name: bytes, version: int) -> _WlInterface:
    result = _WlInterface()
    result.name = name
    result.version = version
    result.method_count = 0
    result.methods = ctypes.POINTER(_WlMessage)()
    result.event_count = 0
    result.events = ctypes.POINTER(_WlMessage)()
    return result


def _message(
    name: bytes,
    signature: bytes,
    argument_interfaces: tuple[int | None, ...] = (),
) -> tuple[_WlMessage, ctypes.Array[ctypes.c_void_p] | None]:
    types: ctypes.Array[ctypes.c_void_p] | None = None
    type_pointer: int | None = None
    if argument_interfaces and any(item is not None for item in argument_interfaces):
        types = (ctypes.c_void_p * len(argument_interfaces))(
            *(ctypes.c_void_p(item) if item is not None else None for item in argument_interfaces)
        )
        type_pointer = ctypes.addressof(types)
    return _WlMessage(name, signature, type_pointer), types


def _fill_interface(
    interface: _WlInterface,
    *,
    requests: tuple[tuple[bytes, bytes, tuple[int | None, ...]], ...],
    events: tuple[tuple[bytes, bytes, tuple[int | None, ...]], ...],
) -> tuple[list[ctypes.Array[_WlMessage]], list[ctypes.Array[ctypes.c_void_p]]]:
    message_arrays: list[ctypes.Array[_WlMessage]] = []
    type_arrays: list[ctypes.Array[ctypes.c_void_p]] = []
    for definitions, field_name, count_field in (
        (requests, "methods", "method_count"),
        (events, "events", "event_count"),
    ):
        built = [_message(name, signature, types) for name, signature, types in definitions]
        messages = (_WlMessage * len(built))(*(item[0] for item in built))
        message_arrays.append(messages)
        type_arrays.extend(item[1] for item in built if item[1] is not None)
        setattr(interface, field_name, ctypes.cast(messages, ctypes.POINTER(_WlMessage)))
        setattr(interface, count_field, len(built))
    return message_arrays, type_arrays


_WINDOW_INTERFACE = _interface(_WINDOW.encode(), _WINDOW_VERSION)
_STACKING_INTERFACE = _interface(_STACKING_ORDER.encode(), 21)
_WINDOW_INTERFACE_KEEPALIVE = _fill_interface(
    _WINDOW_INTERFACE,
    requests=(
        (b"set_state", b"uu", (None, None)),
        (b"set_virtual_desktop", b"u", (None,)),
        (b"set_minimized_geometry", b"ouuuu", (None, None, None, None, None)),
        (b"unset_minimized_geometry", b"o", (None,)),
        (b"close", b"", ()),
        (b"request_move", b"3", ()),
        (b"request_resize", b"3", ()),
        (b"destroy", b"4", ()),
        (b"get_icon", b"7h", (None,)),
        (b"request_enter_virtual_desktop", b"8s", (None,)),
        (b"request_enter_new_virtual_desktop", b"8", ()),
        (b"request_leave_virtual_desktop", b"8s", (None,)),
        (b"request_enter_activity", b"14s", (None,)),
        (b"request_leave_activity", b"14s", (None,)),
        (b"send_to_output", b"15o", (None,)),
    ),
    events=(
        (b"title_changed", b"s", ()),
        (b"app_id_changed", b"s", ()),
        (b"state_changed", b"u", ()),
        (b"virtual_desktop_changed", b"i", ()),
        (b"themed_icon_name_changed", b"s", ()),
        (b"unmapped", b"", ()),
        (b"initial_state", b"4", ()),
        (
            b"parent_window",
            b"5?o",
            (ctypes.addressof(_WINDOW_INTERFACE),),
        ),
        (b"geometry", b"6iiuu", ()),
        (b"icon_changed", b"7", ()),
        (b"pid_changed", b"8u", ()),
        (b"virtual_desktop_entered", b"8s", ()),
        (b"virtual_desktop_left", b"8s", ()),
        (b"application_menu", b"10ss", ()),
        (b"activity_entered", b"14s", ()),
        (b"activity_left", b"14s", ()),
        (b"resource_name_changed", b"16s", ()),
        (b"client_geometry", b"18iiuu", ()),
        (b"mapped", b"21", ()),
    ),
)
_STACKING_INTERFACE_KEEPALIVE = _fill_interface(
    _STACKING_INTERFACE,
    requests=((b"destroy", b"", ()),),
    events=((b"window", b"s", ()), (b"done", b"1", ())),
)
_MANAGEMENT_INTERFACE = _interface(_WINDOW_MANAGEMENT.encode(), _WINDOW_MANAGEMENT_VERSION)
_MANAGEMENT_INTERFACE_KEEPALIVE = _fill_interface(
    _MANAGEMENT_INTERFACE,
    requests=(
        (b"show_desktop", b"u", (None,)),
        (b"get_window", b"nu", (ctypes.addressof(_WINDOW_INTERFACE), None)),
        (
            b"get_window_by_uuid",
            b"12ns",
            (ctypes.addressof(_WINDOW_INTERFACE), None),
        ),
        (
            b"get_stacking_order",
            b"17n",
            (ctypes.addressof(_STACKING_INTERFACE),),
        ),
    ),
    events=(
        (b"show_desktop_changed", b"u", ()),
        (b"window", b"u", ()),
        (b"stacking_order_changed", b"11a", ()),
        (b"stacking_order_uuid_changed", b"12s", ()),
        (b"window_with_uuid", b"13us", ()),
        (b"stacking_order_changed_2", b"17", ()),
    ),
)

_NO_ARGUMENTS = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_void_p)
_UINT_ARGUMENT = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32)
_INT_ARGUMENT = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int32)
_STRING_ARGUMENT = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_char_p)
_OBJECT_ARGUMENT = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p)
_ARRAY_ARGUMENT = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p)
_UINT_STRING_ARGUMENT = ctypes.CFUNCTYPE(
    None, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32, ctypes.c_char_p
)
_TWO_STRINGS_ARGUMENT = ctypes.CFUNCTYPE(
    None, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_char_p, ctypes.c_char_p
)
_GEOMETRY_ARGUMENT = ctypes.CFUNCTYPE(
    None,
    ctypes.c_void_p,
    ctypes.c_void_p,
    ctypes.c_int32,
    ctypes.c_int32,
    ctypes.c_uint32,
    ctypes.c_uint32,
)


@dataclass(slots=True)
class _Window:
    window_id: str
    proxy: int
    title: str = ""
    app_id: str = ""
    state: int = 0
    state_received: bool = False
    frame_geometry: tuple[int, int, int, int] | None = None
    client_geometry: tuple[int, int, int, int] | None = None
    pid: int | None = None
    resource_name: str = ""
    desktops: set[str] = field(default_factory=set)
    has_transient_parent: bool = False
    initialized: bool = False
    callback_refs: list[Any] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        state = self.state
        frame_geometry = _geometry_dict(self.frame_geometry)
        client_geometry = _geometry_dict(self.client_geometry)
        geometry = client_geometry or frame_geometry
        return {
            "window_id": self.window_id,
            "title": self.title,
            "app_id": self.app_id,
            "resource_name": self.resource_name,
            "role": "",
            "pid": self.pid,
            "fullscreen": bool(state & _STATE_FULLSCREEN),
            "minimized": bool(state & _STATE_MINIMIZED),
            "maximized": bool(state & _STATE_MAXIMIZED),
            "minimizable": bool(state & _STATE_MINIMIZABLE),
            "maximizable": bool(state & _STATE_MAXIMIZABLE),
            "fullscreenable": bool(state & _STATE_FULLSCREENABLE),
            "active": bool(state & _STATE_ACTIVE),
            "has_transient_parent": self.has_transient_parent,
            "desktops": sorted(self.desktops),
            "geometry": geometry,
            "frame_geometry": frame_geometry,
            "content_geometry": client_geometry,
            "geometry_basis": (
                "content"
                if client_geometry is not None
                else "frame"
                if frame_geometry is not None
                else None
            ),
        }


class KWinWindowManagementClient:
    """Read and request state for windows through KWin's Plasma window protocol."""

    def __init__(self, socket_path: Path) -> None:
        self._connection = _WaylandConnection(socket_path)
        self._manager: int | None = None
        self._manager_version = 0
        self._windows: dict[str, _Window] = {}
        self._callback_error: InteractionError | None = None
        self._manager_callbacks: list[Any] = []
        try:
            self._bind_manager()
            self._connection.roundtrip()
            self._connection.roundtrip()
            self._connection.roundtrip()
            if self._callback_error is not None:
                raise self._callback_error
        except Exception:
            self.close()
            raise

    def close(self) -> None:
        self._manager = None
        self._windows.clear()
        self._connection.close()

    def __enter__(self) -> KWinWindowManagementClient:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    @property
    def window_version(self) -> int:
        return min(self._manager_version, _WINDOW_VERSION)

    def windows(self) -> list[dict[str, Any]]:
        return [window.to_dict() for window in self._windows.values() if window.initialized]

    def refresh(self) -> None:
        """Complete another compositor roundtrip so asynchronous state changes arrive."""
        self._connection.roundtrip()
        if self._callback_error is not None:
            raise self._callback_error

    def activate(self, window_id: str) -> None:
        normalized = _normalize_uuid(window_id)
        window = self._windows.get(normalized)
        if window is None or not window.initialized:
            raise InteractionError(
                "stale_target",
                "The selected window no longer exists in the private KWin session.",
                details={"window_id": normalized},
            )
        if self.window_version < 1:
            raise InteractionError(
                "window_capability_unavailable",
                "The private KWin window protocol does not support activation requests.",
            )
        self._connection.lib.wl_proxy_marshal_flags(
            window.proxy,
            0,
            None,
            self.window_version,
            0,
            ctypes.c_uint32(_STATE_ACTIVE),
            ctypes.c_uint32(_STATE_ACTIVE),
        )
        self._connection.roundtrip()
        if self._callback_error is not None:
            raise self._callback_error

    def set_minimized(self, window_id: str, minimized: bool) -> None:
        normalized = _normalize_uuid(window_id)
        window = self._windows.get(normalized)
        if window is None or not window.initialized:
            raise InteractionError(
                "stale_target",
                "The selected window no longer exists in the private KWin session.",
                details={"window_id": normalized},
            )
        if not isinstance(minimized, bool):
            raise InteractionError(
                "invalid_window_state",
                "Minimized state must be explicitly true or false.",
                details={"minimized": minimized},
            )
        if self.window_version < 1:
            raise InteractionError(
                "window_capability_unavailable",
                "The private KWin window protocol does not support state requests.",
            )
        self._connection.lib.wl_proxy_marshal_flags(
            window.proxy,
            0,
            None,
            self.window_version,
            0,
            ctypes.c_uint32(_STATE_MINIMIZED),
            ctypes.c_uint32(_STATE_MINIMIZED if minimized else 0),
        )
        self._connection.roundtrip()
        if self._callback_error is not None:
            raise self._callback_error

    def set_fullscreen(self, window_id: str, fullscreen: bool) -> None:
        self._set_boolean_window_state(window_id, "fullscreen", _STATE_FULLSCREEN, fullscreen)

    def set_maximized(self, window_id: str, maximized: bool) -> None:
        self._set_boolean_window_state(window_id, "maximized", _STATE_MAXIMIZED, maximized)

    def _set_boolean_window_state(
        self,
        window_id: str,
        name: Literal["fullscreen", "maximized"],
        flag: int,
        enabled: bool,
    ) -> None:
        normalized = _normalize_uuid(window_id)
        window = self._windows.get(normalized)
        if window is None or not window.initialized:
            raise InteractionError(
                "stale_target",
                "The selected window no longer exists in the selected KWin session.",
                details={"window_id": normalized},
            )
        if not isinstance(enabled, bool):
            raise InteractionError(
                "invalid_window_state",
                f"{name.capitalize()} state must be explicitly true or false.",
                details={name: enabled},
            )
        if self.window_version < 1:
            raise InteractionError(
                "window_capability_unavailable",
                "The selected KWin session does not support window-state requests.",
            )
        self._connection.lib.wl_proxy_marshal_flags(
            window.proxy,
            0,
            None,
            self.window_version,
            0,
            ctypes.c_uint32(flag),
            ctypes.c_uint32(flag if enabled else 0),
        )
        self._connection.roundtrip()
        if self._callback_error is not None:
            raise self._callback_error

    def _bind_manager(self) -> None:
        manager_global = self._connection.globals.get(_WINDOW_MANAGEMENT)
        if manager_global is None:
            raise InteractionError(
                "window_inspection_unavailable",
                "The private KWin session does not advertise Plasma window management.",
                details={"interface": _WINDOW_MANAGEMENT},
            )
        global_name, server_version = manager_global
        if server_version < _MINIMUM_UUID_VERSION:
            raise InteractionError(
                "window_inspection_unavailable",
                "Private window UUIDs require Plasma window-management protocol version 13.",
                details={"available_version": server_version, "required_version": 13},
            )
        self._manager_version = min(server_version, _WINDOW_MANAGEMENT_VERSION)
        self._manager = self._connection.lib.wl_proxy_marshal_flags(
            self._connection.registry,
            0,
            ctypes.addressof(_MANAGEMENT_INTERFACE),
            self._manager_version,
            0,
            ctypes.c_uint32(global_name),
            ctypes.c_char_p(_WINDOW_MANAGEMENT.encode()),
            ctypes.c_uint32(self._manager_version),
            None,
        )
        if not self._manager:
            raise InteractionError(
                "window_management_bind_failed",
                "Could not bind private KWin's Plasma window-management protocol.",
            )
        self._add_manager_listener()

    def _add_manager_listener(self) -> None:
        assert self._manager is not None

        @_UINT_ARGUMENT  # type: ignore[untyped-decorator]
        def show_desktop_changed(_data: int, _proxy: int, _state: int) -> None:
            return

        @_UINT_ARGUMENT  # type: ignore[untyped-decorator]
        def legacy_window(_data: int, _proxy: int, _internal_id: int) -> None:
            return

        @_ARRAY_ARGUMENT  # type: ignore[untyped-decorator]
        def stacking_order_changed(_data: int, _proxy: int, _order: int) -> None:
            return

        @_STRING_ARGUMENT  # type: ignore[untyped-decorator]
        def stacking_order_uuid_changed(_data: int, _proxy: int, _uuids: bytes) -> None:
            return

        @_UINT_STRING_ARGUMENT  # type: ignore[untyped-decorator]
        def window_with_uuid(
            _data: int,
            proxy: int,
            _internal_id: int,
            raw_uuid: bytes,
        ) -> None:
            self._create_window(proxy, raw_uuid)

        @_NO_ARGUMENTS  # type: ignore[untyped-decorator]
        def stacking_order_changed_2(_data: int, _proxy: int) -> None:
            return

        callbacks = [
            show_desktop_changed,
            legacy_window,
            stacking_order_changed,
            stacking_order_uuid_changed,
            window_with_uuid,
            stacking_order_changed_2,
        ]
        self._manager_callbacks = callbacks
        _add_listener(self._connection, self._manager, callbacks)

    def _create_window(self, manager_proxy: int, raw_uuid: bytes | None) -> None:
        if raw_uuid is None:
            self._callback_error = InteractionError(
                "invalid_backend_data",
                "Private KWin announced a window without a UUID.",
            )
            return
        try:
            window_id = _normalize_uuid(raw_uuid.decode("utf-8", errors="strict"))
        except (UnicodeDecodeError, InteractionError):
            self._callback_error = InteractionError(
                "invalid_backend_data",
                "Private KWin announced an invalid window UUID.",
                details={"window_id": raw_uuid.decode("utf-8", errors="replace")},
            )
            return
        if window_id in self._windows:
            return
        if len(self._windows) >= _MAX_WINDOWS:
            self._callback_error = InteractionError(
                "window_limit_exceeded",
                "Private KWin announced more windows than this query safely supports.",
                details={"maximum": _MAX_WINDOWS},
            )
            return

        window_proxy = self._connection.lib.wl_proxy_marshal_flags(
            manager_proxy,
            2,
            ctypes.addressof(_WINDOW_INTERFACE),
            self.window_version,
            0,
            None,
            ctypes.c_char_p(raw_uuid),
        )
        if not window_proxy:
            self._callback_error = InteractionError(
                "window_proxy_creation_failed",
                "Could not create a private KWin window object.",
                details={"window_id": window_id},
            )
            return
        window = _Window(window_id=window_id, proxy=window_proxy)
        self._windows[window_id] = window
        self._add_window_listener(window)

    def _add_window_listener(self, window: _Window) -> None:
        @_STRING_ARGUMENT  # type: ignore[untyped-decorator]
        def title_changed(_data: int, _proxy: int, value: bytes | None) -> None:
            window.title = _decode(value)

        @_STRING_ARGUMENT  # type: ignore[untyped-decorator]
        def app_id_changed(_data: int, _proxy: int, value: bytes | None) -> None:
            window.app_id = _decode(value)

        @_UINT_ARGUMENT  # type: ignore[untyped-decorator]
        def state_changed(_data: int, _proxy: int, state: int) -> None:
            window.state = state
            window.state_received = True

        @_INT_ARGUMENT  # type: ignore[untyped-decorator]
        def virtual_desktop_changed(_data: int, _proxy: int, desktop: int) -> None:
            window.desktops = {str(desktop)}

        @_STRING_ARGUMENT  # type: ignore[untyped-decorator]
        def ignored_string(_data: int, _proxy: int, _value: bytes | None) -> None:
            return

        @_NO_ARGUMENTS  # type: ignore[untyped-decorator]
        def unmapped(_data: int, _proxy: int) -> None:
            window.initialized = False

        @_NO_ARGUMENTS  # type: ignore[untyped-decorator]
        def initial_state(_data: int, _proxy: int) -> None:
            window.initialized = True

        @_OBJECT_ARGUMENT  # type: ignore[untyped-decorator]
        def parent_window(_data: int, _proxy: int, parent: int) -> None:
            window.has_transient_parent = bool(parent)

        @_GEOMETRY_ARGUMENT  # type: ignore[untyped-decorator]
        def geometry(
            _data: int,
            _proxy: int,
            x: int,
            y: int,
            width: int,
            height: int,
        ) -> None:
            window.frame_geometry = (x, y, width, height)

        @_GEOMETRY_ARGUMENT  # type: ignore[untyped-decorator]
        def client_geometry(
            _data: int,
            _proxy: int,
            x: int,
            y: int,
            width: int,
            height: int,
        ) -> None:
            window.client_geometry = (x, y, width, height)

        @_NO_ARGUMENTS  # type: ignore[untyped-decorator]
        def icon_changed(_data: int, _proxy: int) -> None:
            return

        @_UINT_ARGUMENT  # type: ignore[untyped-decorator]
        def pid_changed(_data: int, _proxy: int, pid: int) -> None:
            window.pid = pid or None

        @_STRING_ARGUMENT  # type: ignore[untyped-decorator]
        def desktop_entered(_data: int, _proxy: int, desktop: bytes | None) -> None:
            value = _decode(desktop)
            if value:
                window.desktops.add(value)

        @_STRING_ARGUMENT  # type: ignore[untyped-decorator]
        def desktop_left(_data: int, _proxy: int, desktop: bytes | None) -> None:
            window.desktops.discard(_decode(desktop))

        @_TWO_STRINGS_ARGUMENT  # type: ignore[untyped-decorator]
        def application_menu(
            _data: int,
            _proxy: int,
            _service_name: bytes | None,
            _object_path: bytes | None,
        ) -> None:
            return

        @_STRING_ARGUMENT  # type: ignore[untyped-decorator]
        def resource_name_changed(_data: int, _proxy: int, value: bytes | None) -> None:
            window.resource_name = _decode(value)

        callbacks = [
            title_changed,
            app_id_changed,
            state_changed,
            virtual_desktop_changed,
            ignored_string,
            unmapped,
            initial_state,
            parent_window,
            geometry,
            icon_changed,
            pid_changed,
            desktop_entered,
            desktop_left,
            application_menu,
            desktop_entered,
            desktop_left,
            resource_name_changed,
            client_geometry,
            icon_changed,
        ]
        window.callback_refs = callbacks
        _add_listener(self._connection, window.proxy, callbacks)


def list_windows(socket_path: Path) -> dict[str, Any]:
    with KWinWindowManagementClient(socket_path) as client:
        windows = client.windows()
        return {
            "count": len(windows),
            "windows": windows,
            "window_management_protocol_version": client._manager_version,
        }


def window_management_version(socket_path: Path) -> int | None:
    """Return the advertised private window-management protocol version, if present."""
    with _WaylandConnection(socket_path) as connection:
        advertised = connection.globals.get(_WINDOW_MANAGEMENT)
        return None if advertised is None else advertised[1]


def normalize_window_id(value: str) -> str:
    return _normalize_uuid(value)


def activate_window(socket_path: Path, window_id: str) -> dict[str, Any]:
    normalized_id = _normalize_uuid(window_id)
    with KWinWindowManagementClient(socket_path) as client:
        before = {item["window_id"]: item for item in client.windows()}
        window = before.get(normalized_id)
        if window is None:
            raise InteractionError(
                "stale_target",
                "The selected window no longer exists in the private KWin session.",
                details={"window_id": normalized_id},
            )
        client.activate(normalized_id)
        after = {item["window_id"]: item for item in client.windows()}
        return {
            "window": after.get(normalized_id, window),
            "active_verified": bool(after.get(normalized_id, {}).get("active")),
        }


def set_minimized_window(
    socket_path: Path,
    window_id: str,
    minimized: bool,
) -> dict[str, Any]:
    normalized_id = _normalize_uuid(window_id)
    with KWinWindowManagementClient(socket_path) as client:
        before = {item["window_id"]: item for item in client.windows()}
        window = before.get(normalized_id)
        if window is None:
            raise InteractionError(
                "stale_target",
                "The selected window no longer exists in the private KWin session.",
                details={"window_id": normalized_id},
            )
        client.set_minimized(normalized_id, minimized)
        after = {item["window_id"]: item for item in client.windows()}
        result_window = after.get(normalized_id, window)
        return {
            "window": result_window,
            "minimized_verified": result_window.get("minimized") is minimized,
        }


def set_window_state(
    socket_path: Path,
    window_id: str,
    *,
    state: Literal["fullscreen", "maximized"],
    enabled: bool,
) -> dict[str, Any]:
    """Set one explicit fullscreen/maximized state and return its verified result."""
    if state not in {"fullscreen", "maximized"}:
        raise InteractionError(
            "invalid_window_state",
            "Only fullscreen and maximized states can be changed through this operation.",
            details={"state": state},
        )
    if not isinstance(enabled, bool):
        raise InteractionError(
            "invalid_window_state",
            f"{state.capitalize()} state must be explicitly true or false.",
            details={state: enabled},
        )

    normalized_id = _normalize_uuid(window_id)
    with KWinWindowManagementClient(socket_path) as client:
        before = {item["window_id"]: item for item in client.windows()}
        window = before.get(normalized_id)
        if window is None:
            raise InteractionError(
                "stale_target",
                "The selected window no longer exists in the selected KWin session.",
                details={"window_id": normalized_id},
            )
        if enabled:
            conflict_state = "maximized" if state == "fullscreen" else "fullscreen"
            if window.get("minimized") is True or window.get(conflict_state) is True:
                raise InteractionError(
                    "window_state_conflict",
                    f"Restore the minimized or {conflict_state} state before enabling {state}.",
                    details={
                        "window_id": normalized_id,
                        "requested_state": state,
                        "minimized": window.get("minimized"),
                        conflict_state: window.get(conflict_state),
                    },
                )
        if window.get(state) is not enabled:
            if state == "fullscreen":
                client.set_fullscreen(normalized_id, enabled)
            else:
                client.set_maximized(normalized_id, enabled)

        deadline = time.monotonic() + _WINDOW_STATE_READBACK_TIMEOUT_SECONDS
        result_window: dict[str, Any] | None = None
        while True:
            client.refresh()
            after = {item["window_id"]: item for item in client.windows()}
            result_window = after.get(normalized_id)
            if result_window is None or result_window.get(state) is enabled:
                break
            if time.monotonic() >= deadline:
                break
            time.sleep(_WINDOW_STATE_READBACK_INTERVAL_SECONDS)
        if result_window is None:
            raise InteractionError(
                "stale_target",
                "The selected window closed while KWin changed its state.",
                details={"window_id": normalized_id, "requested_state": state},
            )
        return {
            "window": result_window,
            "state": state,
            "enabled": enabled,
            "changed": window.get(state) is not result_window.get(state),
            "verified": result_window.get(state) is enabled,
            "focus_changed": window.get("active") is not result_window.get("active"),
        }


def _add_listener(connection: _WaylandConnection, proxy: int, callbacks: list[Any]) -> None:
    callback_array = (ctypes.c_void_p * len(callbacks))(
        *(ctypes.cast(callback, ctypes.c_void_p).value for callback in callbacks)
    )
    result = connection.lib.wl_proxy_add_listener(
        proxy,
        ctypes.cast(callback_array, ctypes.POINTER(ctypes.c_void_p)),
        None,
    )
    if result != 0:
        raise InteractionError(
            "wayland_listener_failed",
            "Could not register a KWin window-management event listener.",
        )
    # libwayland retains this array pointer and reads it on later roundtrips;
    # keeping only the callback objects alive does not keep the array storage.
    callbacks.append(callback_array)


def _decode(value: bytes | None) -> str:
    return "" if value is None else value.decode("utf-8", errors="replace")


def _geometry_dict(
    geometry: tuple[int, int, int, int] | None,
) -> dict[str, Any] | None:
    if geometry is None:
        return None
    x, y, width, height = geometry
    if width <= 0 or height <= 0:
        return None
    return {
        "x": x,
        "y": y,
        "width": width,
        "height": height,
        "coordinate_space": "logical-desktop-pixels",
    }


def _normalize_uuid(value: str) -> str:
    normalized = value.strip().strip("{}").lower()
    parts = normalized.split("-")
    if (
        len(parts) != 5
        or [len(part) for part in parts] != [8, 4, 4, 4, 12]
        or any(any(character not in "0123456789abcdef" for character in part) for part in parts)
    ):
        raise InteractionError(
            "invalid_window_id",
            "Window IDs must be UUIDs returned by the selected session's window list.",
            details={"window_id": value},
        )
    return normalized

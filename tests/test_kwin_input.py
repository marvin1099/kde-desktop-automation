import pytest

from kde_desktop_automation.backends.kwin_input import (
    _FAKE_INPUT_REQUESTS,
    _KEY_STATE_PRESSED,
    _KEY_STATE_RELEASED,
    InputReceipt,
    KWinFakeInput,
    _text_keysyms,
    parse_key_chord,
)
from kde_desktop_automation.errors import InteractionError


def test_parse_key_chord_orders_modifier_and_key_transitions() -> None:
    assert parse_key_chord("ctrl+shift+s") == (
        ("CTRL", 0xFFE3, _KEY_STATE_PRESSED),
        ("SHIFT", 0xFFE1, _KEY_STATE_PRESSED),
        ("S", ord("s"), _KEY_STATE_PRESSED),
        ("S", ord("s"), _KEY_STATE_RELEASED),
        ("SHIFT", 0xFFE1, _KEY_STATE_RELEASED),
        ("CTRL", 0xFFE3, _KEY_STATE_RELEASED),
    )


def test_letter_chords_use_layout_independent_keysyms() -> None:
    assert parse_key_chord("CTRL+A")[1] == ("A", ord("a"), _KEY_STATE_PRESSED)


@pytest.mark.parametrize(
    "value",
    ["", "+A", "CTRL+", "CTRL+LCTRL+A", "HYPER+A", "CTRL+NO_SUCH_KEY"],
)
def test_parse_key_chord_rejects_ambiguous_or_unsupported_names(value: str) -> None:
    with pytest.raises(InteractionError):
        parse_key_chord(value)


def test_text_keysyms_support_ascii_unicode_and_editor_controls() -> None:
    assert _text_keysyms("aéλ\r\n\t") == (97, 0xE9, 0x010003BB, 0xFF0D, 0xFF09)


@pytest.mark.parametrize("value", ["", "\x00", "\x7f", "\ud800", "x" * 4097])
def test_text_keysyms_reject_empty_oversized_or_invalid_input(value: str) -> None:
    with pytest.raises(InteractionError):
        _text_keysyms(value)


def test_input_receipt_reports_submission_without_claiming_effect() -> None:
    payload = InputReceipt("key CTRL+N", 4, True).to_dict(
        session_id="agent-0123456789ab",
        wayland_display="kde-desktop-0123456789ab",
    )

    assert payload["events_submitted"] == 4
    assert payload["server_roundtrip_completed"] is True
    assert payload["effect_verified"] is False
    assert "does not acknowledge" in payload["note"]

    live_desktop = InputReceipt("key CTRL+S", 4, True).to_dict(
        session_id=None,
        wayland_display="wayland-1",
        target_kind="user-desktop",
    )
    assert live_desktop["target"] == {
        "kind": "user-desktop",
        "wayland_display": "wayland-1",
        "keyboard_focus": "current KWin focus; not independently identified",
    }


class FakeProtocolLibrary:
    def __init__(self) -> None:
        self.calls: list[tuple[int, int, tuple[int, ...]]] = []

    def wl_proxy_marshal_flags(
        self,
        _proxy: object,
        opcode: int,
        _interface: object,
        _version: int,
        _flags: int,
        *arguments: object,
    ) -> None:
        self.calls.append(
            (
                opcode,
                _version,
                tuple(int(getattr(argument, "value", argument)) for argument in arguments),
            )
        )


class FakeWaylandConnection:
    def __init__(self) -> None:
        self.lib = FakeProtocolLibrary()
        self.roundtrips = 0

    def roundtrip(self) -> None:
        self.roundtrips += 1


def _fake_client(version: int = 6) -> tuple[KWinFakeInput, FakeWaylandConnection]:
    client = KWinFakeInput.__new__(KWinFakeInput)
    client.socket_path = None  # type: ignore[assignment]
    client._connection = FakeWaylandConnection()  # type: ignore[assignment]
    client._fake_input = object()  # type: ignore[assignment]
    client._fake_input_version = version
    return client, client._connection  # type: ignore[return-value]


def test_key_and_text_dispatch_v6_keysym_requests_and_roundtrip_each_event() -> None:
    client, connection = _fake_client()

    key_receipt = client.send_key("CTRL+S")
    text_receipt = client.send_text("Aλ")

    assert key_receipt.events_submitted == 4
    assert connection.lib.calls[:4] == [
        (12, 6, (0xFFE3, _KEY_STATE_PRESSED)),
        (12, 6, (ord("s"), _KEY_STATE_PRESSED)),
        (12, 6, (ord("s"), _KEY_STATE_RELEASED)),
        (12, 6, (0xFFE3, _KEY_STATE_RELEASED)),
    ]
    assert [call[2] for call in connection.lib.calls[4:]] == [
        (ord("A"), _KEY_STATE_PRESSED),
        (ord("A"), _KEY_STATE_RELEASED),
        (0x010003BB, _KEY_STATE_PRESSED),
        (0x010003BB, _KEY_STATE_RELEASED),
    ]
    assert connection.roundtrips == 8
    assert text_receipt.events_submitted == 4


def test_click_dispatches_absolute_motion_and_balanced_button_events() -> None:
    client, connection = _fake_client()

    receipt = client.click(12, 34, "right")

    assert receipt.events_submitted == 3
    assert connection.lib.calls == [
        (9, 6, (12 * 256, 34 * 256)),
        (2, 6, (0x111, _KEY_STATE_PRESSED)),
        (2, 6, (0x111, _KEY_STATE_RELEASED)),
    ]
    assert connection.roundtrips == 3


def test_drag_moves_with_button_held_and_releases_it_at_the_end() -> None:
    client, connection = _fake_client()

    receipt = client.drag(
        10,
        20,
        50,
        40,
        button="left",
        steps=2,
        duration_ms=0,
        hold_ms=0,
        end_pause_ms=0,
    )

    assert connection.lib.calls == [
        (9, 6, (10 * 256, 20 * 256)),
        (2, 6, (0x110, _KEY_STATE_PRESSED)),
        (9, 6, (30 * 256, 30 * 256)),
        (9, 6, (50 * 256, 40 * 256)),
        (2, 6, (0x110, _KEY_STATE_RELEASED)),
    ]
    assert receipt.action == "drag left from (10, 20) to (50, 40)"
    assert receipt.events_submitted == 5
    assert connection.roundtrips == 5


def test_drag_timing_waits_for_start_path_and_drop(monkeypatch: pytest.MonkeyPatch) -> None:
    client, connection = _fake_client()
    delays: list[float] = []
    monkeypatch.setattr("kde_desktop_automation.backends.kwin_input.time.sleep", delays.append)

    receipt = client.drag(
        10,
        20,
        50,
        40,
        steps=2,
        duration_ms=200,
        hold_ms=100,
        end_pause_ms=300,
    )

    assert delays == [0.1, 0.1, 0.1, 0.3]
    assert receipt.events_submitted == 5
    assert connection.lib.calls[1] == (2, 6, (0x110, _KEY_STATE_PRESSED))
    assert connection.lib.calls[-1] == (2, 6, (0x110, _KEY_STATE_RELEASED))


@pytest.mark.parametrize(
    "timing",
    [
        (True, 0, 0),
        (5_001, 0, 0),
        (0, -1, 0),
        (0, 0, 2_001),
        (5_000, 2_000, 2_000),
    ],
)
def test_drag_rejects_invalid_timing(timing: tuple[int, int, int]) -> None:
    client, _connection = _fake_client()

    with pytest.raises(InteractionError, match="(?i)drag"):
        client.drag(
            10,
            20,
            50,
            40,
            duration_ms=timing[0],
            hold_ms=timing[1],
            end_pause_ms=timing[2],
        )


def test_drag_releases_the_button_if_a_motion_request_fails() -> None:
    client, connection = _fake_client()
    original_request = connection.lib.wl_proxy_marshal_flags

    def fail_after_press(proxy, opcode, interface, version, flags, *arguments):
        if opcode == 9 and len(connection.lib.calls) >= 2:
            raise RuntimeError("simulated pointer motion failure")
        return original_request(proxy, opcode, interface, version, flags, *arguments)

    connection.lib.wl_proxy_marshal_flags = fail_after_press

    with pytest.raises(RuntimeError, match="simulated pointer motion failure"):
        client.drag(10, 20, 50, 40, steps=2)

    assert connection.lib.calls[-1] == (2, 6, (0x110, _KEY_STATE_RELEASED))


def test_drag_retries_button_release_if_the_initial_release_fails() -> None:
    client, connection = _fake_client()
    original_request = connection.lib.wl_proxy_marshal_flags
    release_attempts = 0

    def fail_first_release(proxy, opcode, interface, version, flags, *arguments):
        nonlocal release_attempts
        if opcode == 2 and getattr(arguments[1], "value", arguments[1]) == _KEY_STATE_RELEASED:
            release_attempts += 1
            if release_attempts == 1:
                raise RuntimeError("simulated button release failure")
        return original_request(proxy, opcode, interface, version, flags, *arguments)

    connection.lib.wl_proxy_marshal_flags = fail_first_release

    with pytest.raises(RuntimeError, match="simulated button release failure"):
        client.drag(10, 20, 50, 40, steps=1)

    assert release_attempts == 2
    assert connection.lib.calls[-1] == (2, 6, (0x110, _KEY_STATE_RELEASED))


def test_drag_rejects_unbounded_or_empty_step_count_before_input() -> None:
    client, connection = _fake_client()

    for steps in (0, 65):
        with pytest.raises(InteractionError, match="Drag steps"):
            client.drag(10, 20, 50, 40, steps=steps)

    assert connection.lib.calls == []


@pytest.mark.parametrize("version", [3, 4, 5])
def test_layout_aware_keys_and_text_require_fake_input_v6(version: int) -> None:
    client, connection = _fake_client(version)

    with pytest.raises(InteractionError, match="protocol version"):
        client.send_key("CTRL+S")
    with pytest.raises(InteractionError, match="protocol version"):
        client.send_text("hello")

    assert connection.lib.calls == []


def test_pointer_click_requires_fake_input_v3() -> None:
    client, connection = _fake_client(version=2)

    with pytest.raises(InteractionError, match="protocol version"):
        client.click(10, 20)

    assert connection.lib.calls == []


def test_fake_input_request_opcodes_and_version_signatures_match_protocol() -> None:
    requests = dict(_FAKE_INPUT_REQUESTS)

    assert requests["authenticate"] == "ss"
    assert requests["pointer_motion_absolute"] == "3ff"
    assert requests["keyboard_key"] == "4uu"
    assert requests["keyboard_keysym"] == "6uu"

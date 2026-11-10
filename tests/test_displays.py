import pytest

from kde_desktop_automation.backends.displays import parse_display_list
from kde_desktop_automation.errors import InteractionError


def test_display_parser_prefers_logical_geometry() -> None:
    displays = parse_display_list(
        '[{"name":"Virtual-0","x":0,"y":0,"width_px":2048,"height_px":1536,'
        '"logical_x":-1280,"logical_y":0,"logical_width":1280,"logical_height":960,'
        '"scale":1.5,"output_transform":"normal","refresh_hz":60.0}]'
    )

    assert len(displays) == 1
    assert displays[0].x == -1280
    assert displays[0].width == 1280
    assert displays[0].height == 960
    assert displays[0].to_dict()["coordinate_space"] == "logical-desktop-pixels"


@pytest.mark.parametrize("payload", ["not json", "{}", "[null]"])
def test_display_parser_rejects_invalid_payload(payload: str) -> None:
    with pytest.raises(InteractionError):
        parse_display_list(payload)


def test_display_parser_rejects_invalid_dimensions() -> None:
    with pytest.raises(InteractionError, match="invalid dimensions"):
        parse_display_list('[{"name":"broken","logical_width":0,"logical_height":800}]')

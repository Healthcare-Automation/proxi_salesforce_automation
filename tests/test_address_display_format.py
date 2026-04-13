"""US address display normalization (ALL CAPS → title-style)."""

from utils.address_display_format import format_us_address_line_for_display


def test_leaves_mixed_case_unchanged():
    s = "100 Main St, Springfield, IL"
    assert format_us_address_line_for_display(s) == s


def test_all_caps_street_city_state():
    assert (
        format_us_address_line_for_display("4625 VIRGINIA BEACH BLVD, VIRGINIA BEACH, VA")
        == "4625 Virginia Beach Blvd, Virginia Beach, VA"
    )


def test_state_zip_segment():
    assert format_us_address_line_for_display("MARSHALLTOWN, IA 50158") == "Marshalltown, IA 50158"


def test_directional_and_suffix():
    assert (
        format_us_address_line_for_display("1700 MAIN STREET SW, LOS LUNAS, NM")
        == "1700 Main Street SW, Los Lunas, NM"
    )


def test_idempotent_second_pass():
    once = format_us_address_line_for_display("101 IOWA AVENUE WEST, MARSHALLTOWN, IA")
    twice = format_us_address_line_for_display(once)
    assert once == twice


def test_po_box():
    assert format_us_address_line_for_display("P.O. BOX 12, AUSTIN, TX") == "PO Box 12, Austin, TX"


def test_empty():
    assert format_us_address_line_for_display("") == ""
    assert format_us_address_line_for_display(None) == ""


def test_strips_trailing_empty_comma_segments_all_caps():
    raw = "4625 VIRGINIA BEACH BLVD, VIRGINIA BCH VA, , "
    got = format_us_address_line_for_display(raw)
    assert not got.endswith(",")
    assert ", ," not in got
    assert got == "4625 Virginia Beach Blvd, Virginia Bch VA"


def test_strips_trailing_empty_segments_mixed_case_passthrough():
    """Comma cleanup runs even when we do not title-case the line."""
    assert (
        format_us_address_line_for_display("100 Main St, Springfield, , ")
        == "100 Main St, Springfield"
    )

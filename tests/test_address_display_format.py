"""US address display normalization (ALL CAPS → title-style)."""

from utils.address_display_format import (
    format_us_address_line_for_display,
    strip_redundant_city_state_from_shipping_street,
)


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


def test_strips_trailing_commas_city_state_run_no_zip():
    """Kimedics-style street + 'City ST' segment with empty trailing slots."""
    assert (
        format_us_address_line_for_display("6419 Reading Rd, Rosenberg TX, ,")
        == "6419 Reading Rd, Rosenberg TX"
    )


def test_fullwidth_comma_treated_as_separator():
    raw = "100 Main St，Springfield， ，"  # U+FF0C fullwidth commas
    assert format_us_address_line_for_display(raw) == "100 Main St, Springfield"


def test_strip_redundant_city_state_suffix_comma_city_abbr():
    s = "5101 N Belt Hwy, Saint Joseph MO"
    assert (
        strip_redundant_city_state_from_shipping_street(s, city="Saint Joseph", state="Missouri")
        == "5101 N Belt Hwy"
    )


def test_strip_redundant_city_state_suffix_state_abbr_input():
    s = "5101 N Belt Hwy, Saint Joseph MO"
    assert strip_redundant_city_state_from_shipping_street(s, city="Saint Joseph", state="MO") == "5101 N Belt Hwy"


def test_strip_redundant_city_state_comma_city_full_state():
    s = "5101 N Belt Hwy, Saint Joseph, Missouri"
    assert (
        strip_redundant_city_state_from_shipping_street(s, city="Saint Joseph", state="Missouri")
        == "5101 N Belt Hwy"
    )


def test_strip_redundant_city_state_no_digit_remainder_returns_none():
    assert strip_redundant_city_state_from_shipping_street("Saint Joseph MO", city="Saint Joseph", state="MO") is None


def test_strip_redundant_city_state_no_duplicate_returns_none():
    assert strip_redundant_city_state_from_shipping_street("5101 N Belt Hwy", city="Saint Joseph", state="MO") is None

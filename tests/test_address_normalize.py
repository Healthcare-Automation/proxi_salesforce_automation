"""Tests for utils.address_normalize."""

import sys
from pathlib import Path

REPO_SRC = Path(__file__).resolve().parents[1] / "src"
if str(REPO_SRC) not in sys.path:
    sys.path.insert(0, str(REPO_SRC))

from utils.address_normalize import (  # noqa: E402
    addresses_equivalent,
    location_key,
    normalize_address,
    normalize_city,
    normalize_state,
    normalize_street,
)


# ── normalize_state ──────────────────────────────────────────────────────────

def test_state_passthrough_abbrev():
    assert normalize_state("OK") == "OK"
    assert normalize_state("ok") == "OK"
    assert normalize_state("Ok") == "OK"
    assert normalize_state(" CA ") == "CA"


def test_state_full_name_to_abbrev():
    assert normalize_state("Oklahoma") == "OK"
    assert normalize_state("California") == "CA"
    assert normalize_state("oklahoma") == "OK"
    assert normalize_state("NEW YORK") == "NY"


def test_state_trailing_punctuation():
    assert normalize_state("OK.") == "OK"
    assert normalize_state("Oklahoma,") == "OK"


def test_state_short_aliases():
    assert normalize_state("Okla") == "OK"
    assert normalize_state("Calif") == "CA"
    assert normalize_state("tex") == "TX"


def test_state_empty_or_unknown():
    assert normalize_state("") == ""
    assert normalize_state(None) == ""
    assert normalize_state("Atlantis") == ""


def test_state_dc_and_territories():
    assert normalize_state("District of Columbia") == "DC"
    assert normalize_state("Puerto Rico") == "PR"
    assert normalize_state("U.S. Virgin Islands") == "VI"


# ── normalize_city ────────────────────────────────────────────────────────────

def test_city_basic():
    assert normalize_city("Muskogee") == "muskogee"
    assert normalize_city("  Muskogee  ") == "muskogee"
    assert normalize_city("ST. JOSEPH") == "st. joseph"
    assert normalize_city("New York") == "new york"


def test_city_collapses_internal_whitespace():
    assert normalize_city("New   York") == "new york"


def test_city_trailing_punctuation():
    assert normalize_city("Muskogee.") == "muskogee"
    assert normalize_city("Muskogee,") == "muskogee"


def test_city_empty():
    assert normalize_city("") == ""
    assert normalize_city(None) == ""


# ── normalize_street ─────────────────────────────────────────────────────────

def test_street_muskogee_real_case():
    """The actual case that produced the duplicate Aspen Dental account."""
    a = normalize_street("615 West Shawnee Bypass")
    b = normalize_street("615 W Shawnee Byp")
    assert a == b == "615 W SHAWNEE BYP"


def test_street_directional_variations():
    assert normalize_street("100 north 5th st") == "100 N 5TH ST"
    assert normalize_street("100 N 5th Street") == "100 N 5TH ST"
    assert normalize_street("100 North 5th Street") == "100 N 5TH ST"
    assert normalize_street("100 N. 5th St.") == "100 N 5TH ST"


def test_street_compass_compound():
    assert normalize_street("200 northeast park ave") == "200 NE PARK AVE"
    assert normalize_street("200 NE Park Avenue") == "200 NE PARK AVE"


def test_street_suffix_variants():
    assert normalize_street("1 boulevard") == "1 BLVD"
    assert normalize_street("1 Blvd") == "1 BLVD"
    assert normalize_street("1 Drive") == "1 DR"
    assert normalize_street("1 Parkway") == "1 PKWY"
    assert normalize_street("1 Highway") == "1 HWY"
    assert normalize_street("1 Trail") == "1 TRL"
    assert normalize_street("1 Square") == "1 SQ"
    assert normalize_street("1 Terrace") == "1 TER"


def test_street_collapses_whitespace_and_punctuation():
    assert normalize_street("  615   W   Shawnee   Byp.  ") == "615 W SHAWNEE BYP"
    assert normalize_street("100 South 5th St., Suite 200") == "100 S 5TH ST SUITE 200"


def test_street_keeps_unknown_words():
    """Unrecognized words pass through (uppercased) so we never silently drop info."""
    assert normalize_street("100 Vermont Avenue Northwest") == "100 VERMONT AVE NW"
    assert normalize_street("1 Pennsylvania Ave NW") == "1 PENNSYLVANIA AVE NW"


def test_street_empty():
    assert normalize_street("") == ""
    assert normalize_street(None) == ""


# ── normalize_address / addresses_equivalent ─────────────────────────────────

def test_address_tuple_normalization():
    assert normalize_address("615 West Shawnee Bypass", "Muskogee", "Oklahoma") == \
           ("615 W SHAWNEE BYP", "muskogee", "OK")


def test_addresses_equivalent_muskogee_case():
    """The actual Aspen Dental Muskogee duplicate that triggered this work."""
    a = ("615 West Shawnee Bypass", "Muskogee", "OK")
    b = ("615 W Shawnee Byp", "Muskogee", "Oklahoma")
    assert addresses_equivalent(a, b)


def test_addresses_equivalent_not_equal():
    a = ("100 N 5th St", "Anytown", "CA")
    b = ("200 N 5th St", "Anytown", "CA")
    assert not addresses_equivalent(a, b)


def test_addresses_equivalent_different_state():
    a = ("100 N 5th St", "Anytown", "CA")
    b = ("100 N 5th St", "Anytown", "NV")
    assert not addresses_equivalent(a, b)


# ── location_key ─────────────────────────────────────────────────────────────

def test_location_key_basic():
    assert location_key("Muskogee", "OK") == "muskogee|ok"
    assert location_key("Muskogee", "Oklahoma") == "muskogee|ok"
    assert location_key("  muskogee  ", "ok") == "muskogee|ok"


def test_location_key_missing_components():
    assert location_key("", "OK") == ""
    assert location_key("Muskogee", "") == ""
    assert location_key("Muskogee", "Atlantis") == ""

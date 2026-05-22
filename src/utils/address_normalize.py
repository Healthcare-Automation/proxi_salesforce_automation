"""
Address and US-state normalization helpers for comparing addresses
apples-to-apples across data sources (Kimedics scrape, Salesforce
Account.ShippingStreet, manually-entered records, etc.).

The single biggest cause of duplicate Salesforce worksite Accounts
historically has been format drift between the two sources:

    "615 West Shawnee Bypass" vs "615 W Shawnee Byp"
    "Oklahoma"                vs "OK"
    "St. Joseph, MO"          vs "St Joseph MO"

These helpers normalize to a canonical form so equality comparisons
work without false negatives.

Public API:
    normalize_state(value)   -> "OK"  (always 2-letter uppercase; passthrough if unknown)
    normalize_city(value)    -> "muskogee"
    normalize_street(value)  -> "615 W SHAWNEE BYP"
    normalize_address(...)   -> (norm_street, norm_city, norm_state) tuple
    location_key(city, state) -> "muskogee|ok"
    addresses_equivalent(...)-> bool

All helpers are pure functions with no I/O. Safe to call from anywhere.
"""

from __future__ import annotations

import re
from typing import Optional

# ── State name → 2-letter abbreviation ───────────────────────────────────────
# Covers all 50 states + DC + the four U.S. territories Salesforce commonly
# stores. Both directions of lookup are supported via _STATE_ABBREV_SET.

_STATE_NAME_TO_ABBREV: dict[str, str] = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR",
    "california": "CA", "colorado": "CO", "connecticut": "CT", "delaware": "DE",
    "district of columbia": "DC", "florida": "FL", "georgia": "GA",
    "hawaii": "HI", "idaho": "ID", "illinois": "IL", "indiana": "IN",
    "iowa": "IA", "kansas": "KS", "kentucky": "KY", "louisiana": "LA",
    "maine": "ME", "maryland": "MD", "massachusetts": "MA", "michigan": "MI",
    "minnesota": "MN", "mississippi": "MS", "missouri": "MO", "montana": "MT",
    "nebraska": "NE", "nevada": "NV", "new hampshire": "NH", "new jersey": "NJ",
    "new mexico": "NM", "new york": "NY", "north carolina": "NC",
    "north dakota": "ND", "ohio": "OH", "oklahoma": "OK", "oregon": "OR",
    "pennsylvania": "PA", "rhode island": "RI", "south carolina": "SC",
    "south dakota": "SD", "tennessee": "TN", "texas": "TX", "utah": "UT",
    "vermont": "VT", "virginia": "VA", "washington": "WA",
    "west virginia": "WV", "wisconsin": "WI", "wyoming": "WY",
    "american samoa": "AS", "guam": "GU", "northern mariana islands": "MP",
    "puerto rico": "PR", "u.s. virgin islands": "VI", "us virgin islands": "VI",
    "virgin islands": "VI",
}
_STATE_ABBREV_SET: set[str] = set(_STATE_NAME_TO_ABBREV.values())


def normalize_state(value: Optional[str]) -> str:
    """
    Return the 2-letter uppercase state abbreviation for any reasonable input.

    Accepts: "OK", "Ok", "ok", "Oklahoma", "  oklahoma ", "OKLA." -> "OK"
    Unknown/empty -> "" (caller can decide whether to skip).

    Passthrough for any 2-letter value already in our set, so we don't lose
    info on territories or odd cases we haven't mapped.
    """
    if not value:
        return ""
    s = value.strip()
    if not s:
        return ""

    # Drop trailing dot/comma noise (e.g. "OK.", "Okla.")
    s_clean = re.sub(r"[.,]+$", "", s).strip()

    # Already a known 2-letter abbreviation?
    up = s_clean.upper()
    if len(up) == 2 and up in _STATE_ABBREV_SET:
        return up

    # Full name? Look up case-insensitively.
    key = s_clean.lower()
    if key in _STATE_NAME_TO_ABBREV:
        return _STATE_NAME_TO_ABBREV[key]

    # Common short forms we want to absorb (postal abbreviations with trailing
    # dot or other minor variants).
    aliases = {
        "okla": "OK", "calif": "CA", "fla": "FL", "tex": "TX",
        "mass": "MA", "conn": "CT", "minn": "MN", "wash": "WA",
        "wisc": "WI", "wis": "WI", "tenn": "TN", "ark": "AR",
        "ariz": "AZ", "kan": "KS", "kans": "KS", "nebr": "NE",
        "neb": "NE", "ill": "IL", "ind": "IN", "mich": "MI",
        "miss": "MS",
    }
    if key in aliases:
        return aliases[key]

    return up if len(up) == 2 else ""


def normalize_city(value: Optional[str]) -> str:
    """Lowercase, collapse internal whitespace, strip surrounding noise."""
    if not value:
        return ""
    s = value.strip().lower()
    # Drop trailing comma/period
    s = re.sub(r"[.,]+$", "", s).strip()
    # Collapse internal whitespace
    s = re.sub(r"\s+", " ", s)
    return s


# ── Street normalization ─────────────────────────────────────────────────────
# USPS standard suffix abbreviations + common variants. Source: USPS Pub 28.
# We map each variant to a single canonical form (USPS-style abbreviation).

_DIRECTIONALS: dict[str, str] = {
    "north": "N", "south": "S", "east": "E", "west": "W",
    "northeast": "NE", "northwest": "NW",
    "southeast": "SE", "southwest": "SW",
}

_STREET_SUFFIXES: dict[str, str] = {
    # canonical -> canonical (identity)
    "ave": "AVE", "avenue": "AVE", "av": "AVE",
    "blvd": "BLVD", "boulevard": "BLVD", "boul": "BLVD", "boulv": "BLVD",
    "byp": "BYP", "bypass": "BYP", "bypa": "BYP", "bypas": "BYP",
    "cir": "CIR", "circle": "CIR", "circ": "CIR", "crcl": "CIR",
    "ct": "CT", "court": "CT", "crt": "CT",
    "dr": "DR", "drive": "DR", "drv": "DR", "driv": "DR",
    "expwy": "EXPY", "expy": "EXPY", "expressway": "EXPY",
    "hwy": "HWY", "highway": "HWY", "hiway": "HWY", "highwy": "HWY",
    "jct": "JCT", "junction": "JCT", "jction": "JCT",
    "ln": "LN", "lane": "LN", "lanes": "LN",
    "pkwy": "PKWY", "parkway": "PKWY", "pky": "PKWY", "pkwys": "PKWY",
    "pl": "PL", "place": "PL",
    "plz": "PLZ", "plaza": "PLZ", "plza": "PLZ",
    "rd": "RD", "road": "RD", "roads": "RD",
    "rdg": "RDG", "ridge": "RDG", "rdge": "RDG",
    "rte": "RTE", "route": "RTE",
    "sq": "SQ", "square": "SQ", "sqr": "SQ", "sqre": "SQ",
    "st": "ST", "street": "ST", "strt": "ST", "str": "ST",
    "ter": "TER", "terrace": "TER", "terr": "TER",
    "trl": "TRL", "trail": "TRL", "trails": "TRL", "tr": "TRL",
    "way": "WAY", "wy": "WAY",
}


def normalize_street(value: Optional[str]) -> str:
    """
    Return a canonical uppercase form of a street address line.

    Examples:
        "615 West Shawnee Bypass"      -> "615 W SHAWNEE BYP"
        "615 W. Shawnee Byp"           -> "615 W SHAWNEE BYP"
        "  615   W Shawnee  byp.  "    -> "615 W SHAWNEE BYP"
        "100 South 5th St., Suite 200" -> "100 S 5TH ST SUITE 200"

    Empty input -> "". Unknown words pass through unchanged (uppercased).
    """
    if not value:
        return ""
    s = value.strip()
    if not s:
        return ""

    # Strip punctuation except for digits/letters/spaces/&/-/#
    s = re.sub(r"[^\w\s&#\-/]", " ", s, flags=re.UNICODE)
    # Collapse whitespace
    s = re.sub(r"\s+", " ", s).strip()
    if not s:
        return ""

    tokens = s.split(" ")
    out: list[str] = []
    for tok in tokens:
        low = tok.lower()
        if low in _DIRECTIONALS:
            out.append(_DIRECTIONALS[low])
        elif low in _STREET_SUFFIXES:
            out.append(_STREET_SUFFIXES[low])
        else:
            out.append(tok.upper())
    return " ".join(out)


def normalize_address(
    street: Optional[str],
    city: Optional[str],
    state: Optional[str],
) -> tuple[str, str, str]:
    """Convenience: normalize an entire address tuple at once."""
    return normalize_street(street), normalize_city(city), normalize_state(state)


def location_key(city: Optional[str], state: Optional[str]) -> str:
    """
    Stable key used for sf_worksite_location_map lookups: ``"city|ST"``.
    Returns "" when either component is empty (caller skips lookup).
    """
    c = normalize_city(city)
    st = normalize_state(state)
    if not c or not st:
        return ""
    return f"{c}|{st.lower()}"


def addresses_equivalent(
    a: tuple[Optional[str], Optional[str], Optional[str]],
    b: tuple[Optional[str], Optional[str], Optional[str]],
) -> bool:
    """
    True iff two ``(street, city, state)`` triples normalize to the same form.
    Used to deduplicate worksite Accounts across data sources.
    """
    return normalize_address(*a) == normalize_address(*b)

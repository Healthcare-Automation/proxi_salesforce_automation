"""
Display-friendly US address strings: reduce ALL CAPS from Kimedics/Salesforce sources.

Applied at parse time and again at Salesforce push so existing DB rows still normalize on sync.
Idempotent on already mixed-case text (whitespace collapse only).
"""

from __future__ import annotations

import re
from typing import Optional

from utils.us_state_expand import US_STATE_CODE_TO_NAME, state_abbrev_for_job_title, state_name_for_salesforce

_US_STATE_CODES = frozenset(US_STATE_CODE_TO_NAME.keys())

# USPS-style street type abbreviations (Title Case output).
_STREET_TYPE_CANON: dict[str, str] = {
    "ST": "St",
    "ST.": "St.",
    "AVE": "Ave",
    "AVENUE": "Ave",
    "BLVD": "Blvd",
    "BOULEVARD": "Blvd",
    "RD": "Rd",
    "ROAD": "Rd",
    "DR": "Dr",
    "DRIVE": "Dr",
    "LN": "Ln",
    "LANE": "Ln",
    "CT": "Ct",
    "COURT": "Ct",
    "CIR": "Cir",
    "CIRCLE": "Cir",
    "HWY": "Hwy",
    "HIGHWAY": "Hwy",
    "PKWY": "Pkwy",
    "PARKWAY": "Pkwy",
    "PL": "Pl",
    "PLACE": "Pl",
    "WAY": "Way",
    "TRL": "Trl",
    "TRAIL": "Trl",
    "ROUTE": "Route",
    "RT": "Rt",
    "CR": "Cr",
    "COUNTY": "County",
}

_DIRECTIONS = frozenset(
    {
        "N",
        "S",
        "E",
        "W",
        "NE",
        "NW",
        "SE",
        "SW",
        "N.",
        "S.",
        "E.",
        "W.",
        "NE.",
        "NW.",
        "SE.",
        "SW.",
    }
)

_WS = re.compile(r"\s+")


def _collapse_ws(s: str) -> str:
    return _WS.sub(" ", (s or "").strip()).strip()


def _strip_empty_comma_segments(s: str) -> str:
    """Drop blank pieces from comma-separated address lines (e.g. trailing `, ,`)."""
    parts = [p.strip() for p in (s or "").split(",")]
    parts = [p for p in parts if p]
    return ", ".join(parts)


def _normalize_comma_separators(s: str) -> str:
    """Use ASCII commas so empty-segment cleanup runs on Kimedics/HTML pastes (fullwidth comma, etc.)."""
    if not s:
        return s
    return s.replace("\uFF0C", ",").replace("\u060C", ",")


def _strip_trailing_comma_junk(s: str) -> str:
    """Remove trailing `,` / `, ,` / whitespace after final content (defensive)."""
    t = (s or "").rstrip()
    while t:
        nxt = re.sub(r"(?:\s*,)+\s*$", "", t).rstrip()
        if nxt == t:
            break
        t = nxt
    return t


def _uppercase_letter_ratio(s: str) -> float:
    letters = [c for c in s if c.isalpha()]
    if not letters:
        return 0.0
    return sum(1 for c in letters if c.isupper()) / len(letters)


def _fix_token(tok: str) -> str:
    if not tok:
        return tok
    if re.match(r"^#?\d", tok) or re.match(r"^\d+[A-Za-z]$", tok) or re.match(r"^\d+-\d+$", tok):
        return tok
    m = re.match(r"^([^A-Za-z0-9#]*)([#]?[A-Za-z0-9][A-Za-z0-9'.-]*)([^A-Za-z0-9]*)$", tok)
    if not m:
        return tok
    lead, core, trail = m.group(1), m.group(2), m.group(3)
    u = core.upper().rstrip(".")
    dotted = core.endswith(".") and len(core) > 1
    if u in _STREET_TYPE_CANON or (u + ".") in _STREET_TYPE_CANON:
        canon = _STREET_TYPE_CANON.get(u) or _STREET_TYPE_CANON.get(u + ".", core)
        return lead + canon + trail
    if u in _DIRECTIONS or (u + ".") in _DIRECTIONS:
        base = u.rstrip(".")
        if dotted and len(base) <= 2:
            return lead + base + "." + trail
        return lead + base + trail
    if core.isdigit():
        return tok
    if len(u) == 2 and u.isalpha() and u in _US_STATE_CODES:
        return lead + u + trail
    if len(core) > 1:
        low = core.lower()
        return lead + low[0].upper() + low[1:] + trail
    return lead + core.upper() + trail


def _title_address_words(segment: str) -> str:
    seg = _collapse_ws(segment)
    if not seg:
        return seg
    if re.match(r"(?i)^P\.?\s*O\.?\s*BOX\s+", seg):
        m = re.match(r"(?i)^(P\.?\s*O\.?\s*BOX)\s+(.*)$", seg)
        if m:
            return "PO Box " + _title_address_words(m.group(2))
    return " ".join(_fix_token(p) for p in seg.split(" ") if p)


def _format_comma_segment(seg: str) -> str:
    part = _collapse_ws(seg)
    if not part:
        return part
    m_zip = re.fullmatch(r"([A-Za-z]{2})\s+(\d{5}(?:-\d{4})?)", part)
    if m_zip and m_zip.group(1).upper() in _US_STATE_CODES:
        return f"{m_zip.group(1).upper()} {m_zip.group(2)}"
    m_tail = re.match(r"^(.+?)\s+([A-Za-z]{2})$", part)
    if m_tail and m_tail.group(2).upper() in _US_STATE_CODES:
        body = m_tail.group(1).strip()
        st = m_tail.group(2).upper()
        if _uppercase_letter_ratio(body) >= 0.55:
            body = _title_address_words(body)
        return f"{body} {st}" if body else st
    if len(part) == 2 and part.isalpha() and part.upper() in _US_STATE_CODES:
        return part.upper()
    if _uppercase_letter_ratio(part) >= 0.55:
        return _title_address_words(part)
    return part


def format_us_address_line_for_display(value: Optional[str]) -> str:
    """
    Normalize a single-line US mailing-style address for display.

    - Collapses whitespace and removes **empty comma-separated segments** (trailing `, ,`, doubled
      commas from missing city/state slots, etc.).
    - If the text is mostly uppercase (typical Kimedics / county records), converts to title-style
      words with standard street-type and directional tokens (e.g. ``Blvd``, ``SW``).
    - Leaves already mixed-case strings unchanged aside from whitespace and comma cleanup.
    - Splits on commas, formats segments (state + ZIP, trailing state, city/street runs).
    """
    s = _normalize_comma_separators(_collapse_ws(value or ""))
    if not s:
        return ""
    s = _strip_empty_comma_segments(s)
    if not s:
        return ""
    letters = [c for c in s if c.isalpha()]
    if not letters:
        out = _strip_trailing_comma_junk(s)
        return _strip_empty_comma_segments(out)
    if _uppercase_letter_ratio(s) < 0.5:
        out = _strip_trailing_comma_junk(s)
        return _strip_empty_comma_segments(out)
    parts = [p.strip() for p in s.split(",")]
    parts = [p for p in parts if p]
    if not parts:
        out = _strip_trailing_comma_junk(s)
        return _strip_empty_comma_segments(out)
    formatted = [_format_comma_segment(p) for p in parts]
    formatted = [p for p in formatted if (p or "").strip()]
    out = ", ".join(formatted)
    out = _strip_trailing_comma_junk(out)
    return _strip_empty_comma_segments(out)


def looks_like_real_street(value: Optional[str]) -> bool:
    """
    Return True when ``value`` plausibly contains an actual street address
    (a number-prefixed line, or a known street-type / road-type token), as
    opposed to being just a city / city+state / empty placeholder.

    Used to gate ``ShippingStreet`` on new worksite Account records: when the
    only address info Kimedics gave us was ``"Freeport, IL"``, we want to skip
    ``ShippingStreet`` entirely rather than dump city+state into a Street
    field and leave the Account looking like the one in the user report.
    """
    s = _collapse_ws(value or "")
    if not s or len(s) < 4:
        return False
    # Number-prefixed lines (most US street addresses begin with a building #).
    if re.match(r"^\s*\d", s):
        return True
    # Common street-type / road-type tokens. Match as whole words, period-
    # tolerant. Keep this list focused on tokens that strongly imply a real
    # physical street, not generic place words.
    _STREET_TOKEN_RE = re.compile(
        r"\b("
        r"st|street|ave|av|avenue|blvd|boulevard|"
        r"rd|road|dr|drive|ln|lane|hwy|highway|"
        r"pkwy|parkway|way|ct|court|pl|place|"
        r"cir|circle|ter|terrace|trl|trail|"
        r"pike|loop|crossing|sq|square|"
        r"plaza|po\s*box|p\.o\.\s*box|suite|ste|"
        r"floor|fl|unit|apt|apartment|#|building|bldg"
        r")\.?\b",
        re.IGNORECASE,
    )
    return bool(_STREET_TOKEN_RE.search(s))


def strip_redundant_city_state_from_shipping_street(
    street: Optional[str],
    *,
    city: str,
    state: str,
) -> Optional[str]:
    """
    When ``ShippingStreet`` ends with the same city + state already represented in
    ``ShippingCity`` / ``ShippingState`` (typical Kimedics ``address_line`` packed into Street),
    return the street with that trailing duplicate removed so formula fields that concatenate
    Street + City + State do not repeat the location.

    Conservative: only strips when the remainder still contains a digit (street/building number)
    and the match is anchored at the end of the string.

    Returns ``None`` if there is no safe change.
    """
    s = _collapse_ws(street or "")
    city_c = _collapse_ws(city or "")
    state_raw = _collapse_ws(state or "")
    if not s or not city_c or not state_raw:
        return None

    abbr = state_abbrev_for_job_title(state_raw)
    if not abbr:
        return None
    full = state_name_for_salesforce(state_raw) if len(state_raw) == 2 else state_raw
    if not full:
        full = state_raw

    city_re = re.escape(city_c)
    abbr_re = re.escape(abbr)
    full_re = re.escape(full)

    # Longer / more specific patterns first (comma before duplicate tail is safest).
    patterns = [
        rf",\s*{city_re}\s*,\s*{full_re}\s*$",
        rf",\s*{city_re}\s*,\s*{abbr_re}\s*$",
        rf",\s*{city_re}\s+{abbr_re}\s*$",
        rf",\s*{city_re}\s+{full_re}\s*$",
        rf"\s+{city_re}\s+{abbr_re}\s*$",
        rf"\s+{city_re}\s+{full_re}\s*$",
    ]

    for pat in patterns:
        m = re.search(pat, s, flags=re.IGNORECASE)
        if not m:
            continue
        candidate = _collapse_ws(s[: m.start()])
        candidate = _strip_empty_comma_segments(_strip_trailing_comma_junk(candidate))
        if not candidate or len(candidate) < 3 or candidate.casefold() == s.casefold():
            continue
        if not any(ch.isdigit() for ch in candidate):
            continue
        return candidate

    return None

"""
Display-friendly US address strings: reduce ALL CAPS from Kimedics/Salesforce sources.

Applied at parse time and again at Salesforce push so existing DB rows still normalize on sync.
Idempotent on already mixed-case text (whitespace collapse only).
"""

from __future__ import annotations

import re
from typing import Optional

from utils.us_state_expand import US_STATE_CODE_TO_NAME

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
    s = _collapse_ws(value or "")
    if not s:
        return ""
    s = _strip_empty_comma_segments(s)
    if not s:
        return ""
    letters = [c for c in s if c.isalpha()]
    if not letters:
        return s
    if _uppercase_letter_ratio(s) < 0.5:
        return s
    parts = [p.strip() for p in s.split(",")]
    parts = [p for p in parts if p]
    if not parts:
        return s
    return ", ".join(_format_comma_segment(p) for p in parts)

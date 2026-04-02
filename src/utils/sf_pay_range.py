"""
Infer pay / salary range strings from Kimedics-style job description text.

Used only at Salesforce push time (not stored as a separate Supabase column).
"""

from __future__ import annotations

import re

# Examples: $125 – $145 per hour, $125-$145/hr, Starting at $125/hour, $125 per hour
_PAY_PATTERNS = [
    re.compile(
        r"\$\s*[\d,]+(?:\s*[-–—]\s*\$?\s*[\d,]+)?\s*(?:per\s*hour|/hr|/hour)",
        re.I,
    ),
    re.compile(
        r"(?:starting\s+at|from)\s+\$\s*[\d,]+(?:\s*[-–—]\s*\$?\s*[\d,]+)?(?:\s*(?:per\s*hour|/hr))?",
        re.I,
    ),
    re.compile(r"\$\s*[\d,]+\s*[-–—]\s*\$?\s*[\d,]+", re.I),
]


def extract_pay_range_from_description(text: str | None) -> str | None:
    """
    Return first plausible pay-range substring from free text, or None.

    Conservative: only returns non-empty strings that look like dollar amounts.
    """
    if not text or not isinstance(text, str):
        return None
    for pat in _PAY_PATTERNS:
        m = pat.search(text)
        if m:
            frag = m.group(0).strip()
            if "$" in frag and any(c.isdigit() for c in frag):
                return _normalize_dashes(frag)
    return None


def _normalize_dashes(s: str) -> str:
    return s.replace("—", "–").replace("-", "–")


DEFAULT_SALARY_PAY_RANGE = "Starting at $125/hour"

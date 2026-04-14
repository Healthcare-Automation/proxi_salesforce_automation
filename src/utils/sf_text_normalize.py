"""Salesforce-facing text cleanup (push-time hygiene)."""

from __future__ import annotations

from typing import Any


def strip_trailing_commas_from_sf_text(val: Any) -> str:
    """
    Remove trailing commas (and whitespace before each) from Kimedics-style list/sentence text.

    Examples: ``3 DAs, 2 Hygienists,`` → ``3 DAs, 2 Hygienists``; ``a, , ,`` → ``a``.
    """
    if val is None:
        return ""
    s = str(val).strip()
    while s.endswith(","):
        s = s[:-1].rstrip()
    return s

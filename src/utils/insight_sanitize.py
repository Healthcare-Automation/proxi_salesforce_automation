"""
Normalize Kimedics ``insight`` text for Salesforce (Insight__c) and templates.

Parses bullet segments marked with ``*`` / ``**`` and drops duplicate statements
(case- and whitespace-insensitive; light punctuation strip for comparison).
"""

from __future__ import annotations

import re
from typing import Optional


def _comparison_key(s: str) -> str:
    t = re.sub(r"\s+", " ", (s or "").strip().lower())
    t = re.sub(r"[*•]+", "", t)
    t = re.sub(r"[^\w\s/]", "", t)
    return t.strip()


def parse_insight_bullet_items(insight: str) -> list[str]:
    """
    Split Kimedics insight into bullet bodies (without leading asterisks).

    Handles single-line chunks like:
    ``*Must ... **Prefer ... *Must ...``
    and newline-separated ``*`` lines.
    """
    raw = (insight or "").strip()
    if not raw:
        return []
    # Inline * / ** segments (one or more lines).
    found = re.findall(r"\*+\s*([^*]+?)(?=\s*\*+|\Z)", raw, flags=re.DOTALL)
    if found:
        return [x.strip() for x in found if x.strip()]
    out: list[str] = []
    for line in raw.splitlines():
        t = line.strip()
        if t.startswith("*"):
            out.append(t.lstrip("*").strip())
    return [x for x in out if x]


def dedupe_insight_items(items: list[str]) -> list[str]:
    """Drop items whose comparison key was already seen (preserve first wording)."""
    seen: set[str] = set()
    out: list[str] = []
    for it in items:
        k = _comparison_key(it)
        if not k or k in seen:
            continue
        seen.add(k)
        out.append(it.strip())
    return out


def sanitize_insight_for_salesforce(insight: Optional[str]) -> Optional[str]:
    """
    Return multiline insight suitable for Insight__c: ``*line`` per deduped bullet, or None.
    """
    items = dedupe_insight_items(parse_insight_bullet_items(insight or ""))
    if not items:
        return None
    lines: list[str] = []
    for it in items:
        body = re.sub(r"^\*+\s*", "", it).strip()
        if body:
            lines.append(f"*{body}")
    if not lines:
        return None
    return "\n".join(lines)

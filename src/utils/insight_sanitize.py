"""
Normalize Kimedics ``insight`` text for Salesforce (Insight__c) and templates.

Parses bullet segments marked with ``*`` / ``**`` and drops duplicate statements
(case- and whitespace-insensitive; light punctuation strip for comparison).

The Salesforce ``Insight__c`` field is Text(255). When the deduped result still
exceeds 255 chars (~15% of jobs), we try AI summarization (gpt-4o-mini) to fit
the most important constraints; if AI is unavailable or returns oversized text,
we fall back to a hard truncate. This catches the length-exceeded PATCH errors
before they hit Salesforce instead of relying on the post-error quarantine path.
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from typing import Optional


SF_INSIGHT_MAX_LEN = 255  # Salesforce Insight__c is Text(255)
_AI_TARGET_LEN     = 240  # Aim slightly under the cap so we don't bounce again
_AI_TIMEOUT_SECS   = 10


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

    Enforces the Salesforce Text(255) length cap. When the natural sanitized
    output exceeds 255 chars, attempts an AI summarization down to ~240; if AI
    is unavailable or still produces oversized text, hard-truncates as a final
    safety net so the SF PATCH never bounces on length alone.
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
    result = "\n".join(lines)
    if len(result) <= SF_INSIGHT_MAX_LEN:
        return result
    return _fit_to_sf_insight_cap(result)


def _fit_to_sf_insight_cap(value: str) -> str:
    """
    Compress ``value`` to fit in Salesforce ``Insight__c`` (Text 255).

    Strategy:
      1. Ask gpt-4o-mini to summarize while preserving operationally-relevant
         constraints (DEA / CSR / schedules / max rates / required procedures).
      2. If AI is unavailable, errors, or produces text still over the cap,
         fall back to a hard truncate at 252 + "…" to guarantee the cap.

    Best-effort: never raises; the worst case is a clean truncation.
    """
    ai_out = _ai_summarize_insight(value)
    if ai_out and 0 < len(ai_out) <= SF_INSIGHT_MAX_LEN:
        return ai_out
    # Hard truncate fallback. Trim to 252 chars then append a single-char ellipsis.
    return value[: SF_INSIGHT_MAX_LEN - 1].rstrip() + "…"


def _ai_summarize_insight(value: str) -> Optional[str]:
    api_key = (os.environ.get("OPENAI_API_KEY") or "").strip()
    if not api_key:
        return None
    prompt = (
        "Compress the following candidate-requirement insight into ONE plain-text bullet line "
        f"of at most {_AI_TARGET_LEN} characters. Preserve operationally critical constraints "
        "(DEA / CSR licensure, required schedules, max bill / rate caps, mandatory procedures, "
        "travel or lodging constraints). Drop boilerplate and pleasantries. Output a JSON object "
        '{ "insight": "*..." } where the value starts with "*" and contains NO newlines.'
    )
    body = json.dumps({
        "model": "gpt-4o-mini",
        "messages": [
            {"role": "system", "content": prompt},
            {"role": "user", "content": value},
        ],
        "temperature": 0,
        "response_format": {"type": "json_object"},
        "max_tokens": 220,
    }).encode("utf-8")
    req = urllib.request.Request(
        "https://api.openai.com/v1/chat/completions",
        data=body, method="POST",
    )
    req.add_header("Content-Type", "application/json")
    req.add_header("Authorization", f"Bearer {api_key}")
    try:
        with urllib.request.urlopen(req, timeout=_AI_TIMEOUT_SECS) as resp:
            j = json.loads(resp.read().decode("utf-8"))
        text = j["choices"][0]["message"]["content"]
        obj = json.loads(text)
    except Exception as exc:
        print(f"[insight_sanitize] AI compression failed; falling back to truncate. err={exc}")
        return None
    out = (obj.get("insight") or "").strip()
    if not out:
        return None
    # Force single line + ensure "*" prefix.
    out = re.sub(r"\s+", " ", out).strip()
    if not out.startswith("*"):
        out = "*" + out.lstrip("*").strip()
    if len(out) > SF_INSIGHT_MAX_LEN:
        return None
    return out

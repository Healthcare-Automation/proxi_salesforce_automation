"""
OpenAI-powered copy generation + light QA for Proxi job descriptions.

This module is optional at runtime:
- If `openai` isn't installed or `OPENAI_API_KEY` is unset, callers should fall back to the
  deterministic template in `utils.job_description_proxi_template`.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Any, Optional


@dataclass(frozen=True)
class AIDescriptionResult:
    intro_html: str
    issues: list[str]
    model: str


@dataclass(frozen=True)
class DateResolution:
    """Resolved active dates plus the model's self-reported confidence (0-100).
    ``dates`` is None when the top line does not change the dates. ``confidence``
    is 100 when the result is fully certain; anything lower flags for human review."""
    dates: Optional[str]
    confidence: int = 100
    reason: str = ""


_DANGLING_DASH_RE = re.compile(r"\s*[-–—]+\s*$")


def strip_dangling_range_dash(dates: str) -> str:
    """Remove a trailing range dash with no end date ('July 15-' → 'July 15').
    Posters sometimes leave the dash in or forget the end date ('July 15- open
    to roster'); the dangling dash is poster error, not data — it reads as a
    typo in Salesforce and in the review alerts. Inner ranges are untouched."""
    return _DANGLING_DASH_RE.sub("", (dates or "").strip())


# ── Active-dates override (AI) ───────────────────────────────────────────────
#
# Kimedics posts carry a full ``Dates:`` line listing every date ever associated
# with the job. Their TOP line sometimes narrows that to the currently-active
# need ("Active need is June 26", "6/12 Dates added: June 29-30, …", and other
# wording that drifts over time). We let an LLM — not brittle precursor regex —
# decide whether the top line is such an override, so new phrasings keep working.
# The model's answer is trusted ONLY when the returned dates appear verbatim in
# the post (anti-hallucination guard). Callers fall back to the regex extractor
# in ``job_description_proxi_template`` when the AI is unavailable.

_MONTH_OR_MD = re.compile(
    r"(?i)(\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\b|\b\d{1,2}/\d{1,2}\b)"
)


def _has_top_line_date_signal(description_full_text: str) -> bool:
    """Cheap gate: does any of the first few non-empty lines mention a date?
    If not, there is nothing to override with — skip the LLM entirely."""
    seen = 0
    for line in (description_full_text or "").splitlines():
        s = line.strip()
        if not s:
            continue
        if _MONTH_OR_MD.search(s):
            return True
        seen += 1
        if seen >= 3:
            break
    return False


def _norm_for_match(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "")).strip().lower()


_MONTH_NUMBERS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}
_MONTH_WORD_RE = re.compile(r"(?i)\b(jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\b")
_M_SLASH_D_RE = re.compile(r"\b(\d{1,2})/(\d{1,2})\b")
_DAY_RANGE_RE = re.compile(r"\b(\d{1,2})\s*[-–—]\s*(\d{1,2})\b")
_DAY_RE = re.compile(r"\b(\d{1,2})\b")


def expand_date_tokens(dates: str) -> set[tuple[int, int]]:
    """Expand a dates string into ``{(month, day)}``.

    ``"July 13-17, 20-22, 24"`` → July 13,14,15,16,17,20,21,22,24. The month carries
    across commas the way posts write them (``"July 2, Aug 10-14, 17"`` → the trailing
    17 is August). Returns an empty set when no month can be established.
    """
    out: set[tuple[int, int]] = set()
    month: Optional[int] = None
    for chunk in re.split(r"[,;]", dates or ""):
        s = chunk.strip()
        if not s:
            continue
        for mm, dd in _M_SLASH_D_RE.findall(s):
            mi, di = int(mm), int(dd)
            if 1 <= mi <= 12 and 1 <= di <= 31:
                out.add((mi, di))
                month = mi
        s = _M_SLASH_D_RE.sub(" ", s)
        word = _MONTH_WORD_RE.search(s)
        if word:
            month = _MONTH_NUMBERS[word.group(1)[:3].lower()]
            s = f"{s[:word.start()]} {s[word.end():]}"
        if month is None:
            continue
        for a, b in _DAY_RANGE_RE.findall(s):
            start, end = int(a), int(b)
            if 1 <= start <= 31 and 1 <= end <= 31 and start <= end:
                out.update((month, d) for d in range(start, end + 1))
        for d in _DAY_RE.findall(_DAY_RANGE_RE.sub(" ", s)):
            if 1 <= int(d) <= 31:
                out.add((month, int(d)))
    return out


def description_top(description_full_text: str, n: int = 8) -> str:
    """The top portion of a post that the date resolver actually reads (first ``n``
    lines). Shared so the review alert can show reviewers the exact text the AI saw."""
    return "\n".join((description_full_text or "").strip().splitlines()[:n])


@dataclass(frozen=True)
class FieldExtraction:
    """Fields recovered by the AI safety-net, plus its confidence (0-100) that the
    recovered ``dates_needed`` truly represents the coverage dates (vs. a stray date
    in notes). < 100 flags the dates for human review."""
    fields: dict
    dates_confidence: int = 100
    dates_reason: str = ""


## Field hints for the AI extraction safety-net. Keys are job_content fields.
_DESC_FIELD_HINTS = {
    "dates_needed": "coverage/assignment dates, e.g. 'July 20-22, 28-29'",
    "standard_schedule": "daily hours / shift schedule, e.g. '8a-5p 1 hr unpaid lunch'",
    "required_procedures": "required clinical procedures",
    "support_staff": "clinical / support staff, e.g. '1 HYG, 3 DAs'",
    "additional_requirements": "additional requirements / info",
    "avg_patients_per_day": "average patients per day",
}


def ai_extract_description_fields(
    description_full_text: str,
    needed_fields,
    *,
    model: Optional[str] = None,
    timeout_s: float = 15.0,
) -> FieldExtraction:
    """Recover specific description fields the deterministic parser missed — for
    posts with human formatting errors (a label with no colon like "Dates July
    20-22", a misspelled label, a stray comma). Returns a :class:`FieldExtraction`
    whose ``fields`` holds only values found VERBATIM in the text (anti-hallucination);
    a field genuinely absent is omitted. ``dates_needed`` is returned only when it
    clearly represents the coverage dates, NOT a stray date in notes — and with a
    confidence so an ambiguous extraction can be flagged. Raises when the AI can't
    run so the caller keeps the regex result. Token-frugal: only the requested
    fields are asked for, and callers gate this so well-formed posts never reach here."""
    import json

    fields = [f for f in (needed_fields or []) if f in _DESC_FIELD_HINTS]
    desc = (description_full_text or "").strip()
    if not fields or not desc:
        return FieldExtraction({}, 100, "")

    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY is not set")
    try:
        from openai import OpenAI
    except Exception as e:  # pragma: no cover
        raise RuntimeError("openai package is required") from e

    target_model = (model or os.environ.get("PROXI_OPENAI_MODEL") or "gpt-4.1-mini").strip()
    field_lines = "\n".join(f"- {f}: {_DESC_FIELD_HINTS[f]}" for f in fields)
    want_dates = "dates_needed" in fields
    dates_rule = (
        "\nFor dates_needed: only return dates that clearly represent the assignment/COVERAGE dates "
        "(e.g. under a 'Dates' label, even with a missing colon or typo). If a date appears only in "
        "unrelated notes, or as a posting / credentialing / 'next update' date, return \"\" for it.\n"
        if want_dates else ""
    )
    conf_keys = (
        ', "dates_confidence": <0-100>, "dates_reason": "<brief; only if < 100>"'
        if want_dates else ""
    )
    conf_rule = (
        "\nAlso return dates_confidence (0-100): how certain dates_needed is the real coverage dates. "
        "Use 100 for a clear Dates context; lower it if the date's role is ambiguous.\n"
        if want_dates else ""
    )
    prompt = f"""Extract these fields from a dental staffing job description. The text may contain human formatting errors (a label with a missing colon, a misspelled label, a missing comma) — read past them. Return each field's value EXACTLY as written in the text (verbatim, no rephrasing), or "" if it is genuinely not present. Never invent.

Fields:
{field_lines}
{dates_rule}{conf_rule}
Description:
<<<
{desc}
>>>

Return STRICT JSON with these keys: {json.dumps(fields)}{conf_keys}.""".strip()

    client = OpenAI(api_key=api_key, timeout=timeout_s)
    resp = client.responses.create(
        model=target_model,
        temperature=0,
        input=[{"role": "user", "content": prompt}],
    )
    t = (resp.output_text or "").strip()
    if t.startswith("```"):
        t = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", t).strip()
    obj = json.loads(t)
    if not isinstance(obj, dict):
        raise ValueError("AI field extraction: JSON is not an object")
    hay = _norm_for_match(desc)
    out: dict = {}
    for f in fields:
        v = str(obj.get(f) or "").strip()
        # Trust only values that actually appear in the post (verbatim, normalized).
        if v and _norm_for_match(v) in hay:
            out[f] = v
    try:
        conf = int(round(float(obj.get("dates_confidence", 100))))
    except (TypeError, ValueError):
        conf = 0
    conf = max(0, min(100, conf))
    reason = str(obj.get("dates_reason") or "").strip()
    # Confidence only matters when dates were actually recovered.
    if "dates_needed" not in out:
        conf, reason = 100, ""
    return FieldExtraction(out, conf, reason)


def _validate_ai_dates(
    raw_text: str, description_full_text: str, structured_dates: str = ""
) -> DateResolution:
    """Parse the model's JSON into a :class:`DateResolution`. Anti-hallucination
    guard is token-level: every comma-separated date piece in the result must appear
    in the post (or the structured Dates list) — this allows cancellation results
    (full list minus removed dates), which are not a single verbatim substring, while
    still rejecting invented dates. Raises ``ValueError`` on anything unusable so the
    caller falls back to the regex extractor; returns dates=None for "no change"."""
    import json

    t = (raw_text or "").strip()
    if t.startswith("```"):
        t = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", t).strip()
    try:
        obj = json.loads(t)
    except Exception as e:
        raise ValueError(f"AI date override: unparseable JSON: {t!r}") from e
    if not isinstance(obj, dict):
        raise ValueError("AI date override: JSON is not an object")
    if not obj.get("override"):
        return DateResolution(None, 100, "")
    dates = strip_dangling_range_dash(str(obj.get("dates") or "").strip().rstrip(".").strip())
    if not dates:
        return DateResolution(None, 100, "")
    haystack = _norm_for_match(f"{description_full_text} {structured_dates or ''}")
    unmatched = [p for p in dates.split(",") if _norm_for_match(p) and _norm_for_match(p) not in haystack]
    if unmatched:
        # Excluding a date that sits inside a range has no verbatim form: dropping the
        # pending 7/17 from "July 13-17" yields "July 13-16", which appears nowhere in
        # the post, so the literal check would reject the one correct answer. Accept it
        # only when every returned day is already in the structured list — a narrowing
        # of dates the job actually has. Invented dates are still rejected.
        allowed = expand_date_tokens(structured_dates or "")
        returned = expand_date_tokens(dates)
        if not (allowed and returned and returned <= allowed):
            raise ValueError(f"AI date piece not present in post: {unmatched[0]!r}")
    try:
        confidence = int(round(float(obj.get("confidence", 100))))
    except (TypeError, ValueError):
        confidence = 0  # unparseable confidence → treat as needs-review
    confidence = max(0, min(100, confidence))
    return DateResolution(dates, confidence, str(obj.get("reason") or "").strip())


def ai_active_dates_override(
    description_full_text: str,
    structured_dates: Optional[str] = None,
    *,
    model: Optional[str] = None,
    timeout_s: float = 15.0,
) -> DateResolution:
    """Resolve the currently-active dates from a post's top line, applying any
    override (active need / dates added) OR cancellation (full list minus removed
    dates). Returns a :class:`DateResolution` (dates=None when the top line does not
    change the dates) with the model's self-reported confidence. Raises when the AI
    cannot run (no key / package / network / bad output) so callers fall back to
    regex. ``structured_dates`` is the parsed full "Dates:" list, used as the base
    for cancellation subtraction."""
    t = (description_full_text or "").strip()
    if not t or not _has_top_line_date_signal(t):
        return DateResolution(None, 100, "")

    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY is not set")
    try:
        from openai import OpenAI
    except Exception as e:  # pragma: no cover
        raise RuntimeError("openai package is required") from e

    target_model = (model or os.environ.get("PROXI_OPENAI_MODEL") or "gpt-4.1-mini").strip()
    top = description_top(t)
    structured = (structured_dates or "").strip()

    prompt = f"""
You determine the CURRENTLY ACTIVE dates needed for a dental staffing job post.

Every post has a structured full "Dates:" list (every date ever associated with the job). The TOP of the post sometimes CHANGES which dates are currently needed. Apply any such change and return the resulting active dates. Set "override" to true whenever the top changes the dates (override OR cancellation), false only when it does not.

1. OVERRIDE — top states the active need or newly-added dates (e.g. "Active need is ...", "6/12 Dates added: ...", "Updated dates: ...") → return THOSE dates exactly as written, KEEPING any weekday/day-of-week qualifiers that are part of them (e.g. "Fridays June 5, 12"). Drop only the leading date stamp (e.g. "6/12"), unrelated labels, and trailing sentences.
2. TENTATIVE vs CONFIRMED — only dates that are a CONFIRMED, currently-active need count. Dates the top marks as tentative / not-yet-firm — "pending confirmation", "tentative", "TBD", "may open up", "holding", "not yet confirmed", "if it opens" — are NOT active needs → EXCLUDE them. Dates marked "active need", "open to submittals", "confirmed", or "needed" ARE active → include them. When the top lists some of each, return ONLY the confirmed/active dates. This IS a change → override=true.
3. CANCELLATION — top says certain dates were cancelled, dropped, removed, no longer needed, or already filled (e.g. "the office cancelled the need for June 11/12", "June 8-9 have been cancelled") → return the structured "Dates:" list with EXACTLY those dates removed, keeping the rest in order. This IS a change → override=true.
4. NO CHANGE — top is only context, requirements, a location, or just restates the full list → override=false.

Never invent dates: every date you return must appear in the post or the structured list.

A leading date stamp is WHEN the note was written, not a date that is needed. Never return it as a date.

When rules 2 or 3 exclude a date that falls INSIDE a range, rewrite that range to cover only the remaining days — do not return it untouched. "July 13-17" minus July 17 → "July 13-16"; minus July 15 → "July 13-14, 16-17"; "July 13-14" minus July 14 → "July 13".

Also report "confidence": an integer 0-100 for how certain you are the returned dates are EXACTLY correct. Use 100 ONLY when the active dates are completely unambiguous and required no guessing. Lower it when the wording is ambiguous or conflicting, the cancellation scope is unclear, dates are malformed, or you had to interpret. When confidence < 100, give a brief "reason".

Examples (structured | top → output):
- "June 8-9, 17-19, 26, 29-30, July 1-2" | "6/12 Dates added: June 29-30, July 1-2" → {{"override": true, "dates": "June 29-30, July 1-2", "confidence": 100, "reason": ""}}
- "Monday only" | "4/8 Active needs are Fridays June 5, 12" → {{"override": true, "dates": "Fridays June 5, 12", "confidence": 100, "reason": ""}}
- "June 22-24" | "6/19 pending confirmation for June 22-24, open to submittals for June 25-26" → {{"override": true, "dates": "June 25-26", "confidence": 100, "reason": ""}}
- "July 13-17, 20-22, 24, 27-29, 31" | "7/17 pending confirmation" → {{"override": true, "dates": "July 13-16, 20-22, 24, 27-29, 31", "confidence": 100, "reason": ""}}
- "June 1-2, 11-12, 15-19, 22" | "6/10 the office cancelled the need for June 11/12" → {{"override": true, "dates": "June 1-2, 15-19, 22", "confidence": 100, "reason": ""}}
- "June 8-9, 17-19, 26" | "6/5 June 8-9 have been cancelled. Active need is June 26" → {{"override": true, "dates": "June 26", "confidence": 100, "reason": ""}}
- "June 8-9, 17-19, 26" | "**Aspen exp preferred" → {{"override": false, "dates": "", "confidence": 100, "reason": ""}}
- "July 20" | "6/29 date reopened" → {{"override": false, "dates": "", "confidence": 100, "reason": ""}}  (6/29 is the stamp saying when it reopened; the needed date is still July 20)

Structured "Dates:" list: {structured or "(none provided)"}

POST (top portion):
<<<
{top}
>>>

Respond with STRICT JSON and nothing else: {{"override": true or false, "dates": "<resulting active dates, or empty string>", "confidence": <0-100>, "reason": "<brief, only if confidence < 100>"}}
""".strip()

    client = OpenAI(api_key=api_key, timeout=timeout_s)
    resp = client.responses.create(
        model=target_model,
        temperature=0,
        input=[{"role": "user", "content": prompt}],
    )
    return _validate_ai_dates((resp.output_text or "").strip(), t, structured)


def _collapse_ws(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip()).strip()


def _guess_specialty(row: dict[str, Any]) -> str:
    # Prefer explicit job title fields when present; otherwise use the project-default.
    for k in ("job_title", "basics_job_title", "title_line"):
        v = _collapse_ws(str(row.get(k) or ""))
        if v:
            # Common Kimedics basics titles can be verbose; keep it compact for the lead sentence.
            if len(v) <= 80:
                return v
    return "General Dentist"


def _guess_job_type(row: dict[str, Any]) -> str:
    v = _collapse_ws(str(row.get("position_type") or row.get("Position_Type_DJC__c") or ""))
    if v:
        low = v.lower()
        if "locum" in low:
            return "locum tenens"
        return v
    return "locum tenens"


def _guess_key_procedures(row: dict[str, Any]) -> str:
    toc = _collapse_ws(str(row.get("types_of_cases") or ""))
    if not toc:
        return ""
    # Take first clause / first line and de-noise.
    first = toc.split("\n", 1)[0].strip()
    first = re.sub(r"\s+", " ", first).strip().strip(",")
    # Avoid huge lists; keep a short phrase.
    if len(first) > 90:
        first = first[:90].rsplit(" ", 1)[0].strip() + "…"
    return first


def merge_ai_intro_html_to_single_paragraph(intro_html: str) -> str:
    """
    Collapse multiple top-level ``<p>...</p>`` blocks into one paragraph (space-joined).

    Used so the opening job description reads as a single paragraph even when the model
    returns several ``<p>`` tags.
    """
    s = (intro_html or "").strip()
    if not s:
        return s
    parts = re.findall(r"<p[^>]*>(.*?)</p>", s, flags=re.I | re.DOTALL)
    if len(parts) <= 1:
        return s
    inner = " ".join(_collapse_ws(p) for p in parts if p and p.strip())
    return f"<p>{inner}</p>"


def _basic_readability_issues(text: str) -> list[str]:
    t = (text or "").strip()
    issues: list[str] = []
    if not t:
        return ["AI intro is empty"]
    if "  " in t:
        issues.append("Contains double spaces")
    if re.search(r"\b(a a|the the)\b", t, flags=re.I):
        issues.append("Contains repeated words")
    if re.search(r"\.\s*\.", t):
        issues.append("Contains repeated punctuation")
    if len(t) > 1200:
        issues.append("Intro is very long (over 1200 chars)")
    return issues


def generate_ai_intro_html(
    row: dict[str, Any],
    *,
    model: Optional[str] = None,
    timeout_s: float = 20.0,
) -> AIDescriptionResult:
    """
    Generate the top intro copy as a single HTML paragraph, and return any QA issues flagged by the model
    plus some deterministic checks.

    Requires `OPENAI_API_KEY` and the `openai` python package.
    """
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY is not set")

    try:
        from openai import OpenAI
    except Exception as e:  # pragma: no cover
        raise RuntimeError("openai package is required (pip install -r requirements.txt)") from e

    city = _collapse_ws(str(row.get("city") or ""))
    state = _collapse_ws(str(row.get("state") or ""))
    specialty = _guess_specialty(row)
    job_type = _guess_job_type(row)
    key_procedures = _guess_key_procedures(row)

    # Keep it client-safe: if procedures are unknown, omit that clause entirely.
    key_proc_clause = (
        f"The ideal candidate is comfortable with {key_procedures} and enjoys working in a collaborative environment."
        if key_procedures
        else "The ideal candidate enjoys working in a collaborative environment."
    )

    target_model = (model or os.environ.get("PROXI_OPENAI_MODEL") or "gpt-4.1-mini").strip()

    prompt = f"""
You are generating client-facing job posting copy for a dental staffing agency.

Write EXACTLY ONE short paragraph in clean HTML: a single <p>...</p> block containing only text (no lists, no headings, no nested <p>).
Tone: professional, clear, confident. Avoid hype and avoid phrases like "best-in-class".

Combine these ideas into that single paragraph (smooth sentences, normal punctuation; do not use line breaks inside the paragraph):

1) We are seeking a {{specialty}} for a {{job_type}} opportunity in {{city}}, {{state}}.

2) This opportunity allows a dentist to practice comprehensive general dentistry with a supportive clinical team and a steady patient workflow.

3) {key_proc_clause}

Hard rules:
- Do not include "Source notes", "Kimedics", or any internal sourcing language.
- Do not add pay, dates, schedule, requirements, or anything not in the template.
- If city or state is missing, rewrite the opening to avoid dangling commas and missing info.

Inputs:
- specialty: {specialty}
- job_type: {job_type}
- city: {city or "(missing)"}
- state: {state or "(missing)"}
""".strip()

    client = OpenAI(api_key=api_key, timeout=timeout_s)
    # Use Responses API (OpenAI python SDK v1+).
    resp = client.responses.create(
        model=target_model,
        input=[
            {
                "role": "user",
                "content": prompt,
            }
        ],
    )
    text = (resp.output_text or "").strip()

    # Basic validation: must be HTML with <p>.
    if "<p" not in text.lower():
        raise RuntimeError("AI intro did not return HTML <p> paragraphs")

    text = merge_ai_intro_html_to_single_paragraph(text)

    issues = _basic_readability_issues(text)

    # Light deterministic spot-check for dangling ", ," and " ,"
    if re.search(r",\s*,", text):
        issues.append("Contains duplicate commas")
    if re.search(r",\s*</p>", text, flags=re.I):
        issues.append("Paragraph ends with a comma (likely missing location piece)")

    return AIDescriptionResult(intro_html=text, issues=issues, model=target_model)


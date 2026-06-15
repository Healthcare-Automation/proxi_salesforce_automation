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


def _validate_ai_dates(
    raw_text: str, description_full_text: str, structured_dates: str = ""
) -> Optional[str]:
    """Parse the model's JSON and return the resolved active dates. Anti-hallucination
    guard is token-level: every comma-separated date piece in the result must appear
    in the post (or the structured Dates list) — this allows cancellation results
    (full list minus removed dates), which are not a single verbatim substring, while
    still rejecting invented dates. Raises ``ValueError`` on anything unusable so the
    caller falls back to the regex extractor; returns ``None`` for "no change"."""
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
        return None
    dates = str(obj.get("dates") or "").strip().rstrip(".").strip()
    if not dates:
        return None
    haystack = _norm_for_match(f"{description_full_text} {structured_dates or ''}")
    for piece in dates.split(","):
        p = _norm_for_match(piece)
        if p and p not in haystack:
            raise ValueError(f"AI date piece not present in post: {piece!r}")
    return dates


def ai_active_dates_override(
    description_full_text: str,
    structured_dates: Optional[str] = None,
    *,
    model: Optional[str] = None,
    timeout_s: float = 15.0,
) -> Optional[str]:
    """Resolve the currently-active dates from a post's top line, applying any
    override (active need / dates added) OR cancellation (full list minus removed
    dates). Returns the resolved date string, or ``None`` when the top line does not
    change the dates. Raises when the AI cannot run (no key / package / network /
    bad output) so callers fall back to regex. ``structured_dates`` is the parsed
    full "Dates:" list, used as the base for cancellation subtraction."""
    t = (description_full_text or "").strip()
    if not t or not _has_top_line_date_signal(t):
        return None

    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY is not set")
    try:
        from openai import OpenAI
    except Exception as e:  # pragma: no cover
        raise RuntimeError("openai package is required") from e

    target_model = (model or os.environ.get("PROXI_OPENAI_MODEL") or "gpt-4.1-mini").strip()
    top = "\n".join(t.splitlines()[:8])
    structured = (structured_dates or "").strip()

    prompt = f"""
You determine the CURRENTLY ACTIVE dates needed for a dental staffing job post.

Every post has a structured full "Dates:" list (every date ever associated with the job). The TOP of the post sometimes CHANGES which dates are currently needed. Apply any such change and return the resulting active dates. Set "override" to true whenever the top changes the dates (override OR cancellation), false only when it does not.

1. OVERRIDE — top states the active need or newly-added dates (e.g. "Active need is ...", "6/12 Dates added: ...", "Updated dates: ...") → return THOSE dates exactly as written, KEEPING any weekday/day-of-week qualifiers that are part of them (e.g. "Fridays June 5, 12"). Drop only the leading date stamp (e.g. "6/12"), unrelated labels, and trailing sentences.
2. CANCELLATION — top says certain dates were cancelled, dropped, removed, no longer needed, or already filled (e.g. "the office cancelled the need for June 11/12", "June 8-9 have been cancelled") → return the structured "Dates:" list with EXACTLY those dates removed, keeping the rest in order. This IS a change → override=true.
3. NO CHANGE — top is only context, requirements, a location, or just restates the full list → override=false.

Never invent dates: every date you return must appear in the post or the structured list.

Examples (structured | top → output):
- "June 8-9, 17-19, 26, 29-30, July 1-2" | "6/12 Dates added: June 29-30, July 1-2" → {{"override": true, "dates": "June 29-30, July 1-2"}}
- "Monday only" | "4/8 Active needs are Fridays June 5, 12" → {{"override": true, "dates": "Fridays June 5, 12"}}
- "June 1-2, 11-12, 15-19, 22" | "6/10 the office cancelled the need for June 11/12" → {{"override": true, "dates": "June 1-2, 15-19, 22"}}
- "June 8-9, 17-19, 26" | "6/5 June 8-9 have been cancelled. Active need is June 26" → {{"override": true, "dates": "June 26"}}
- "June 8-9, 17-19, 26" | "**Aspen exp preferred" → {{"override": false, "dates": ""}}

Structured "Dates:" list: {structured or "(none provided)"}

POST (top portion):
<<<
{top}
>>>

Respond with STRICT JSON and nothing else: {{"override": true or false, "dates": "<resulting active dates, or empty string>"}}
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


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
    Generate the top intro copy as HTML paragraphs, and return any QA issues flagged by the model
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

Write EXACTLY three short paragraphs in clean HTML, using only <p> and text (no lists, no headings).
Tone: professional, clear, confident. Avoid hype and avoid phrases like "best-in-class".

Fill this template, improving grammar and readability while keeping the meaning:

Paragraph 1:
Proxi Dental Staffing is seeking a {{specialty}} for a {{job_type}} opportunity in {{city}}, {{state}}.

Paragraph 2:
This opportunity allows a dentist to practice comprehensive general dentistry with a supportive clinical team and steady patient workflow.

Paragraph 3:
{key_proc_clause}

Hard rules:
- Do not include "Source notes", "Kimedics", or any internal sourcing language.
- Do not add pay, dates, schedule, requirements, or anything not in the template.
- If city or state is missing, rewrite the first paragraph to avoid dangling commas and missing info.

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

    issues = _basic_readability_issues(text)

    # Light deterministic spot-check for dangling ", ," and " ,"
    if re.search(r",\s*,", text):
        issues.append("Contains duplicate commas")
    if re.search(r",\s*</p>", text, flags=re.I):
        issues.append("Paragraph ends with a comma (likely missing location piece)")

    return AIDescriptionResult(intro_html=text, issues=issues, model=target_model)


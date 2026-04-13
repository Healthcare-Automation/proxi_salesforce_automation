"""
Canonical Proxi job posting body for Salesforce (Job_Client_Job_Description__c).

Built from the same structured row we map to other SF fields so the narrative stays in sync
with Status, City, State, Dates, Schedule, Types of Cases, Support Staff, etc.

**Formatting:** By default we emit **HTML** (``<p>``, ``<strong>``, ``<ul>``/``<li>``, ``<br>``) so
Lightning **Rich Text** fields show bold section titles and spacing. Use ``use_html=False`` if
your field is plain Long Text (raw tags would appear as text).

Template follows client direction (locum general dentist, Aspen-style minimal source → richer SEO copy).
"""

from __future__ import annotations

import html
import os
import re
from typing import Iterable, Optional

from utils.sf_pay_range import extract_pay_range_from_description
from utils.us_state_expand import state_name_for_salesforce

# Kimedics sometimes prepends ``M/D update: …`` without refreshing structured date/schedule fields.
_KIMEDICS_TOP_DATES_UPDATE = re.compile(
    r"^(?P<line>(?:\d{1,2}/\d{1,2})(?:/\d{2,4})?\s+update\s*:.*)$",
    re.IGNORECASE,
)

# Kimedics admin line may end with ``Active needs are …`` — that clause wins over structured ``dates_needed``.
_ACTIVE_NEEDS_ARE = re.compile(r"(?i)active\s+needs\s+are\s+")

# Kimedics internal phrasing for account managers — must not appear in candidate-facing copy.
_PRESENTATION_NOTATE = re.compile(
    r"(?i)(?:\s*[,;]+\s*)?(?:(?:\s+[-–—]\s+)|(?<=[\w\d])[-–—]\s*)?please\s+notate\s+any\s+limitations\s+in\s+presentation\.?",
)


def strip_internal_presentation_phrases(text: str) -> str:
    """
    Remove ``please notate any limitations in presentation`` (and the leading comma / dash / `` - ``),
    including forms like ``full mouth- please notate…``.
    """
    t = (text or "").strip()
    if not t:
        return ""
    prev = None
    while prev != t:
        prev = t
        t = _PRESENTATION_NOTATE.sub("", t)
    t = re.sub(r",\s*,+", ", ", t)
    # Collapse horizontal spaces only (preserve newlines in plain-text descriptions).
    t = re.sub(r"[ \t]{2,}", " ", t).strip()
    t = re.sub(r"[-–—]\s*$", "", t).strip()
    t = re.sub(r",\s*$", "", t).strip()
    return t


def extract_active_needs_dates(description_full_text: str) -> Optional[str]:
    """
    If any line in ``description_full_text`` contains ``Active needs are `` (case-insensitive),
    return the text after that phrase on the **same line** (trimmed). First match wins.
    """
    t = (description_full_text or "").strip()
    if not t:
        return None
    for line in t.splitlines():
        m = _ACTIVE_NEEDS_ARE.search(line)
        if m:
            rest = line[m.end() :].strip()
            return rest if rest else None
    return None


def effective_dates_needed(row: dict) -> str:
    """
    Dates for Job copy and ``Job_Dates_Needed__c``: ``Active needs are …`` from ``description_full_text``
    overrides ``dates_needed`` when present.
    """
    active = extract_active_needs_dates((row.get("description_full_text") or "").strip())
    if active:
        return active
    return (row.get("dates_needed") or "").strip()


def extract_kimedics_dates_update_preamble(description_full_text: str) -> Optional[str]:
    """
    If the description starts with a single-line ``4/8 update: …`` style note, return that line.

    Deterministic only (no AI): avoids inventing text; only matches this header pattern.
    """
    t = (description_full_text or "").strip()
    if not t:
        return None
    first = t.split("\n", 1)[0].strip()
    m = _KIMEDICS_TOP_DATES_UPDATE.match(first)
    return m.group("line").strip() if m else None


def _e(s: str) -> str:
    return html.escape((s or "").strip(), quote=False)


def _insight_bullet_items(insight: str) -> list[str]:
    """Lines starting with ``*``, or whole insight as one block if no such lines."""
    raw = (insight or "").strip()
    if not raw:
        return []
    from_lines: list[str] = []
    for line in raw.splitlines():
        t = line.strip()
        if t.startswith("*"):
            from_lines.append(t[1:].strip())
    if from_lines:
        return [x for x in from_lines if x]
    # Single line with multiple "* item" chunks: * foo * bar
    if "*" in raw:
        parts = re.split(r"\s*\*\s*", raw)
        return [p.strip() for p in parts if p.strip()]
    return [raw]


def _bullets_plain(lines: Iterable[str]) -> str:
    items = [ln.strip() for ln in lines if (ln or "").strip()]
    if not items:
        return ""
    return "\n".join(f"• {it}" for it in items)


def _bullets_html(lines: Iterable[str]) -> str:
    items = [_e(ln) for ln in lines if (ln or "").strip()]
    if not items:
        return ""
    # Use semantic list markup for proper bullet indentation/wrapping.
    # Add light inline styles to reduce Salesforce's default list spacing.
    ul_style = "margin-top:0;margin-bottom:0;padding-left:20px;"
    li_style = "margin:0 0 4px 0;"
    return f"<ul style=\"{ul_style}\">" + "".join(f"<li style=\"{li_style}\">{it}</li>" for it in items) + "</ul>"


def _capitalize_first_letter_bullet(s: str) -> str:
    """Ensure the first alphabetic character is uppercase (Kimedics lines often start lowercase)."""
    t = (s or "").strip()
    if not t:
        return t
    for i, ch in enumerate(t):
        if ch.isalpha():
            return t[:i] + ch.upper() + t[i + 1 :]
    return t


def _as_sentence(fragment: str) -> str:
    """Trim, capitalize first letter, end with punctuation when missing."""
    s = (fragment or "").strip()
    if not s:
        return ""
    s = s[0].upper() + s[1:] if len(s) > 1 else s.upper()
    if s[-1] not in ".!?":
        s += "."
    return s


def _source_posting_notes_html(insight_items: list[str]) -> str:
    """
    Kimedics ``insight`` / ``*`` lines — one tight prose line (no stacked paragraphs / double breaks).
    """
    if not insight_items:
        return ""
    sentences = [_as_sentence(x) for x in insight_items if (x or "").strip()]
    if not sentences:
        return ""
    prose = " ".join(sentences)
    return f"<p><strong>Source notes</strong> (from Kimedics): {_e(prose)}</p>"


def _source_posting_notes_plain(insight_items: list[str]) -> str:
    if not insight_items:
        return ""
    sentences = [_as_sentence(x) for x in insight_items if (x or "").strip()]
    if not sentences:
        return ""
    return "Source notes (from Kimedics): " + " ".join(sentences)


def build_proxi_job_posting_description(row: dict, *, use_html: bool = True) -> str:
    """
    Assemble multi-section posting text from ``job_current`` / parser-shaped columns.

    ``use_html=True`` (default): HTML for Salesforce Rich Text Area fields.
    ``use_html=False``: plain text with blank lines and ALL CAPS section labels (no bold).
    """
    city = (row.get("city") or "").strip()
    state_raw = (row.get("state") or "").strip()
    state_full = state_name_for_salesforce(state_raw)
    state_code = state_raw.upper() if len(state_raw) == 2 and state_raw.isalpha() else ""

    title_loc = ""
    if city and state_code:
        title_loc = f"{city}, {state_code}"
    elif city and state_full:
        title_loc = f"{city}, {state_full}"
    else:
        title_loc = city or state_code or state_full or ""

    prose_loc = ""
    if city and state_full:
        prose_loc = f"{city}, {state_full}"
    else:
        prose_loc = title_loc or "the listed location"

    dates = effective_dates_needed(row)
    schedule = (row.get("standard_schedule") or "").strip()

    raw_desc_for_pay = (row.get("description_full_text") or "").strip()
    pay_line = (extract_pay_range_from_description(raw_desc_for_pay) or "").strip()

    types_of_cases = strip_internal_presentation_phrases((row.get("types_of_cases") or "").strip())
    support_staff = (row.get("support_staff") or "").strip()
    # We intentionally do not include "source notes" in the client-facing description.
    # Keep parsing available in case we need it later.
    _ = _insight_bullet_items(row.get("insight") or "")

    state_license = (row.get("state_license_required") or "").strip()

    clinical_bullets: list[str] = []
    if types_of_cases:
        for segment in types_of_cases.split("\n"):
            seg = segment.strip()
            if seg:
                clinical_bullets.append(_capitalize_first_letter_bullet(seg))

    support_bullets: list[str] = []
    if support_staff:
        for segment in support_staff.replace(";", "\n").split("\n"):
            seg = segment.strip()
            if seg:
                support_bullets.append(seg)

    req_bullets: list[str] = []
    if state_license:
        req_bullets.append(state_license)
    req_bullets.extend(
        [
            f"Active {state_full} dental license" if state_full else "Active state dental license",
            "Active DEA with all schedules",
            "CSR if required by the state",
            "Experience in high volume general dentistry environments preferred",
        ]
    )

    # Patient mix is only emitted when explicitly available (avoid fabricating volumes).
    patient_lines: list[str] = []

    # Only claim specific "ideal for extractions/dentures" when supported by extracted text.
    _toc_low = types_of_cases.lower()
    mention_extractions = "extract" in _toc_low
    mention_dentures = "denture" in _toc_low
    include_ideal_para = mention_extractions and mention_dentures

    if use_html:
        def _spacer() -> str:
            # Rich Text: <p> adds consistent vertical rhythm; <br/> alone can collapse/stack oddly.
            return "<p><br/></p>"

        chunks: list[str] = []
        if title_loc:
            chunks.append(f"<p><strong>General Dentist Locum Tenens Opportunity in {_e(title_loc)}</strong></p>")
        else:
            chunks.append("<p><strong>General Dentist Locum Tenens Opportunity</strong></p>")

        chunks.append(_spacer())

        # Default on (unset = try AI); set PROXI_JOB_DESCRIPTION_USE_AI=false to skip API calls.
        use_ai_intro = (os.environ.get("PROXI_JOB_DESCRIPTION_USE_AI") or "true").strip().lower() in (
            "1",
            "true",
            "yes",
        )
        if use_ai_intro:
            try:
                from utils.job_description_ai import generate_ai_intro_html

                ai = generate_ai_intro_html(row)
                chunks.append(ai.intro_html)
            except Exception:
                # Safe fallback: never block writes on AI.
                use_ai_intro = False

        if not use_ai_intro:
            chunks.append(
                "<p>"
                f"Proxi Dental Staffing is seeking a General Dentist for a locum tenens opportunity in {_e(prose_loc)}."
                " This position offers the opportunity to practice comprehensive general dentistry with a supportive "
                "clinical team and steady patient flow."
                "</p>"
            )
            chunks.append(_spacer())

            if include_ideal_para:
                chunks.append(
                    "<p>"
                    "This role is ideal for a dentist comfortable with surgical extractions and dentures who enjoys "
                    "working in a collaborative environment."
                    "</p>"
                )
                chunks.append(_spacer())

            chunks.append("<p>Travel and lodging may be available for qualified candidates.</p>")

        meta_lines: list[str] = []
        if dates:
            meta_lines.append(f"<strong>Dates:</strong> {_e(dates)}")
        if schedule:
            meta_lines.append(f"<strong>Schedule:</strong> {_e(schedule)}")
        if meta_lines:
            chunks.append(_spacer())
            chunks.append("<p>" + "<br/>".join(meta_lines) + "</p>")

        if pay_line:
            chunks.append(f"<p><strong>Pay Range:</strong> {_e(pay_line)}</p>")

        if patient_lines:
            chunks.append(_spacer())
            chunks.append("<p><strong>Patient Mix</strong></p>")
            chunks.append(_bullets_html(patient_lines))

        if clinical_bullets:
            chunks.append(_spacer())
            chunks.append("<p><strong>Clinical Scope</strong></p>")
            chunks.append(_bullets_html(clinical_bullets))
            chunks.append(_spacer())
            chunks.append(
                "<p>"
                "Full mouth extractions may be required when clinically appropriate. Discuss any clinical "
                "limitations with the Proxi team before the assignment."
                "</p>"
            )

        if support_bullets:
            chunks.append(_spacer())
            chunks.append("<p><strong>Support Staff</strong></p>")
            chunks.append(_bullets_html(support_bullets))

        if req_bullets:
            chunks.append(_spacer())
            chunks.append("<p><strong>Requirements</strong></p>")
            chunks.append(_bullets_html(req_bullets))

        return strip_internal_presentation_phrases("".join(chunks).strip())

    # ----- Plain text (Long Text Area): spacing + section labels, no HTML -----
    lines: list[str] = []
    if title_loc:
        lines.append(f"General Dentist Locum Tenens Opportunity in {title_loc}")
    else:
        lines.append("General Dentist Locum Tenens Opportunity")
    lines.append("")
    lines.append(
        f"Proxi Dental Staffing is seeking a General Dentist for a locum tenens opportunity in {prose_loc}. "
        "This position offers the opportunity to practice comprehensive general dentistry with a supportive "
        "clinical team and steady patient flow."
    )
    if include_ideal_para:
        lines.append("")
        lines.append(
            "This role is ideal for a dentist comfortable with surgical extractions and dentures who enjoys working "
            "in a collaborative environment."
        )
    lines.append("")
    lines.append("Travel and lodging may be available for qualified candidates.")
    lines.append("")
    if dates:
        lines.append(f"Dates: {dates}")
    if schedule:
        lines.append(f"Schedule: {schedule}")
    if dates or schedule:
        lines.append("")
    if pay_line:
        lines.append(f"Pay Range: {pay_line}")
        lines.append("")
    # Do not include source notes in plain text output either.
    if patient_lines:
        lines.append("PATIENT MIX")
        lines.append(_bullets_plain(patient_lines))
        lines.append("")
    if clinical_bullets:
        lines.append("CLINICAL SCOPE")
        lines.append(_bullets_plain(clinical_bullets))
        lines.append("")
        lines.append(
            "Full mouth extractions may be required when clinically appropriate. Discuss any clinical "
            "limitations with the Proxi team before the assignment."
        )
        lines.append("")
    if support_bullets:
        lines.append("SUPPORT STAFF")
        lines.append(_bullets_plain(support_bullets))
        lines.append("")
    if req_bullets:
        lines.append("REQUIREMENTS")
        lines.append(_bullets_plain(req_bullets))

    return strip_internal_presentation_phrases("\n".join(lines).strip())

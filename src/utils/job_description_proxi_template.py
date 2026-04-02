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
import re
from typing import Iterable

from utils.sf_pay_range import DEFAULT_SALARY_PAY_RANGE, extract_pay_range_from_description
from utils.us_state_expand import state_name_for_salesforce


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
    return "<ul>" + "".join(f"<li>{it}</li>" for it in items) + "</ul>"


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
    state_full = state_name_for_salesforce(row.get("state") or "")
    loc_display = f"{city}, {state_full}" if city and state_full else (city or state_full or "the listed location")
    title_city = f"{city}, {state_full}" if city and state_full else loc_display

    dates = (row.get("dates_needed") or "").strip() or "See posting or contact recruiter"
    schedule = (row.get("standard_schedule") or "").strip() or "See posting or contact recruiter"

    raw_desc_for_pay = (row.get("description_full_text") or "").strip()
    pay_line = extract_pay_range_from_description(raw_desc_for_pay) or DEFAULT_SALARY_PAY_RANGE

    types_of_cases = (row.get("types_of_cases") or "").strip()
    support_staff = (row.get("support_staff") or "").strip()
    insight_items = _insight_bullet_items(row.get("insight") or "")

    state_license = (row.get("state_license_required") or "").strip()
    raw_desc = (row.get("description_full_text") or "").strip()

    clinical_bullets: list[str] = []
    if types_of_cases:
        for segment in types_of_cases.split("\n"):
            seg = segment.strip()
            if seg:
                clinical_bullets.append(seg)
    if not clinical_bullets:
        clinical_bullets = [
            "Preventive care and comprehensive exams",
            "Restorative dentistry including fillings and crowns",
            "Surgical and simple extractions as clinically appropriate",
        ]

    support_bullets: list[str] = []
    if support_staff:
        for segment in support_staff.replace(";", "\n").split("\n"):
            seg = segment.strip()
            if seg:
                support_bullets.append(seg)
    if not support_bullets:
        support_bullets = [
            "Clinical team per office staffing model",
            "Front office support",
        ]

    req_bullets = [
        f"Active {state_full} dental license" if state_full else "Active state dental license",
        "Active DEA (schedules per state requirement)",
        "CSR if required by the state",
        "Experience in high-volume general dentistry environments preferred",
    ]
    if state_license:
        req_bullets.insert(0, state_license)

    patient_lines = ["Primarily adult patients", "Volume: see office schedule and patient mix"]

    # One line break between major blocks (Rich Text margins on <p>/<ul> vary; this evens rhythm).
    _gap = "<br/>"

    if use_html:
        chunks: list[str] = []
        chunks.append(
            "<p>"
            f"Proxi Dental Staffing is seeking a General Dentist for a locum tenens opportunity in {_e(title_city)}."
            "</p>"
        )
        chunks.append(
            "<p>"
            "This position offers the opportunity to practice comprehensive general dentistry with a "
            "supportive clinical team and steady patient flow."
            "</p>"
        )
        chunks.append("<p><em>Travel and lodging may be available for qualified candidates.</em></p>")
        chunks.append(f"<p><strong>Dates</strong><br/>{_e(dates)}</p>")
        chunks.append(f"<p><strong>Schedule</strong><br/>{_e(schedule)}</p>")
        chunks.append(f"<p><strong>Pay range</strong><br/>{_e(pay_line)}</p>")
        notes_html = _source_posting_notes_html(insight_items)
        if notes_html:
            chunks.append(notes_html)
        chunks.append(_gap)
        chunks.append("<p><strong>Patient mix</strong></p>")
        chunks.append(_bullets_html(patient_lines))
        chunks.append(_gap)
        chunks.append("<p><strong>Clinical scope</strong></p>")
        chunks.append(_bullets_html(clinical_bullets))
        chunks.append(
            "<p>"
            "Full scope may vary by office. Any clinical limitations can be discussed during the "
            "presentation process."
            "</p>"
        )
        chunks.append(_gap)
        chunks.append("<p><strong>Support staff</strong></p>")
        chunks.append(_bullets_html(support_bullets))
        chunks.append(_gap)
        chunks.append("<p><strong>Requirements</strong></p>")
        chunks.append(_bullets_html(req_bullets))

        if raw_desc and len(raw_desc) > 200:
            excerpt = _e(raw_desc[:12000]) + ("…" if len(raw_desc) > 12000 else "")
            chunks.append(_gap)
            chunks.append("<hr/>")
            chunks.append("<p><strong>Source job post (Kimedics excerpt)</strong></p>")
            chunks.append(f"<p>{excerpt.replace(chr(10), '<br/>')}</p>")

        return "".join(chunks).strip()

    # ----- Plain text (Long Text Area): spacing + section labels, no HTML -----
    lines: list[str] = []
    lines.append(
        f"Proxi Dental Staffing is seeking a General Dentist for a locum tenens opportunity in {title_city}."
    )
    lines.append(
        "This position offers the opportunity to practice comprehensive general dentistry with a supportive "
        "clinical team and steady patient flow."
    )
    lines.append("")
    lines.append("Travel and lodging may be available for qualified candidates.")
    lines.append("")
    lines.append(f"Dates: {dates}")
    lines.append(f"Schedule: {schedule}")
    lines.append(f"Pay range: {pay_line}")
    lines.append("")
    notes_plain = _source_posting_notes_plain(insight_items)
    if notes_plain:
        lines.append(notes_plain)
        lines.append("")
    lines.append("PATIENT MIX")
    lines.append(_bullets_plain(patient_lines))
    lines.append("")
    lines.append("CLINICAL SCOPE")
    lines.append(_bullets_plain(clinical_bullets))
    lines.append("")
    lines.append(
        "Full scope may vary by office. Any clinical limitations can be discussed during the presentation process."
    )
    lines.append("")
    lines.append("SUPPORT STAFF")
    lines.append(_bullets_plain(support_bullets))
    lines.append("")
    lines.append("REQUIREMENTS")
    lines.append(_bullets_plain(req_bullets))

    if raw_desc and len(raw_desc) > 200:
        excerpt = raw_desc[:12000] + ("…" if len(raw_desc) > 12000 else "")
        lines.extend(["", "—" * 40, "SOURCE JOB POST (KIMEDICS EXCERPT)", "—" * 40, excerpt])

    return "\n".join(lines).strip()

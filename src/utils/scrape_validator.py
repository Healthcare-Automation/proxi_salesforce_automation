"""
Comprehensive validation for Kimedics scraped job content.

Calibrated against 554 real job_content rows (Apr 2026 export).

Three severity tiers
--------------------
CRITICAL  — the page structure itself likely changed (Kimedics redesign / login wall).
            These are signs that selectors are broken. Send alert IMMEDIATELY.
WARNING   — a specific field is missing or formatted wrong; data quality issue.
            An immediate alert is sent when 3+ warnings appear on a single job.
INFO      — soft quality check; recorded in the daily summary only.

Quick start::

    from utils.scrape_validator import validate_scraped_job, should_send_immediate_alert

    issues = validate_scraped_job(cleaned, job_post_id="12345")
    if should_send_immediate_alert(issues):
        # fire alert email
        ...

Validation layers (in order)
-----------------------------
1. Structural   — did the page render at all? (title_line, description_full_text)
2. Critical     — fields required for Salesforce submission
3. Important    — fields that must be present on every well-scraped job (WARNING)
                  and fields that are sometimes absent (INFO)
4. Format       — values present but in wrong shape (bad date, unknown status, etc.)
5. Anomaly      — cross-field logic

Data notes (from 554-row export)
---------------------------------
- status:              exactly 3 known values (case-sensitive)
- priority:            exactly 3 known values (case-sensitive)
- provider_start_date: primary format MM/DD/YY (2-digit year); "ASAP" is valid
- practice_value:      "XXXX - City, ST" or "XXXX – City, ST" (em-dash variant)
                       optional suffix like " - Closed"
- job_title:           always "#XXXXX: Specialty (Subspecialty (Credential))"
- description_full_text: always present; min 313 chars in real data
- posting_org:         always present (100% fill rate)
- point_of_contact:    always present (100% fill rate)
- location_line:       always present (100% fill rate)
- position_type:       never present in real data — not validated
- number_of_open_positions: never present in real data — not validated
- rates:               NOT validated — we don't scrape rates (fixed value pushed to SF)
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


# ── Severity ──────────────────────────────────────────────────────────────────

class Severity(str, Enum):
    CRITICAL = "CRITICAL"
    WARNING  = "WARNING"
    INFO     = "INFO"


@dataclass
class ValidationIssue:
    severity: Severity
    field:    str
    message:  str
    value:    Any = field(default=None, repr=False)


# ── Field lists ───────────────────────────────────────────────────────────────

# Must be non-empty for the automation to function correctly.
CRITICAL_FIELDS = [
    "title_line",          # always present when page loaded (.sections__container)
    "job_title",           # structured title; always present in real data
    "status",              # Active / Closed; always present in real data
    "provider_start_date", # when coverage starts; always present in real data
]

# 100% fill rate in real data — missing = something went wrong with the scrape.
IMPORTANT_FIELDS_WARNING = [
    "location_line",   # e.g. "HOBBS, NM"
    "state",           # e.g. "NM"
    "city",            # e.g. "Hobbs"
    "posting_org",     # e.g. "Aspen Dental"
    "practice_value",  # e.g. "4043 - Hobbs, NM"
    "point_of_contact", # e.g. "Christina Roth"
]

# Sometimes legitimately absent — flag INFO only (won't trigger immediate alert).
# NOTE: rates are intentionally NOT validated — we don't scrape rates (a fixed value
# is pushed to Salesforce), so they must never produce a warning/info anywhere.
IMPORTANT_FIELDS_INFO = [
    "posted_date",
]
# NOTE: position_type and number_of_open_positions are never populated in
# Kimedics exports — intentionally excluded from all validation checks.


# ── Known exact values (from real data) ───────────────────────────────────────

# Exact status strings used by Kimedics (case-sensitive).
# Any other value is a WARNING — these are the only 3 we've ever seen.
KNOWN_STATUSES = {
    "Closed",
    "Active, accepting new providers",
    "Active, not accepting new providers",
}

# Exact priority strings used by Kimedics (case-sensitive).
KNOWN_PRIORITIES = {"Normal", "High", "Critical"}

# All US state/territory abbreviations (2-letter).
US_STATE_ABBREVS = {
    "AL","AK","AZ","AR","CA","CO","CT","DE","FL","GA","HI","ID","IL","IN","IA",
    "KS","KY","LA","ME","MD","MA","MI","MN","MS","MO","MT","NE","NV","NH","NJ",
    "NM","NY","NC","ND","OH","OK","OR","PA","RI","SC","SD","TN","TX","UT","VT",
    "VA","WA","WV","WI","WY","DC","PR","GU","VI","AS","MP",
}


# ── Compiled patterns ─────────────────────────────────────────────────────────

# Primary Kimedics date format: MM/DD/YY (2-digit year).
# Also accept MM/DD/YYYY, YYYY-MM-DD, and spelled-out months as fallbacks.
# "ASAP" is explicitly valid.
_DATE_RE = re.compile(
    r"^ASAP$"                                  # literal "ASAP"
    r"|\d{1,2}/\d{1,2}/\d{2,4}"               # MM/DD/YY or MM/DD/YYYY  ← primary format
    r"|\d{4}-\d{1,2}-\d{1,2}"                 # YYYY-MM-DD
    r"|[A-Za-z]+ \d{1,2},?\s*\d{4}"           # January 5, 2025
    r"|\d{1,2} [A-Za-z]+ \d{4}",              # 5 January 2025
    re.IGNORECASE,
)

# Title line: "Specialty (Subspecialty (Credential)) (#XXXXX)"
# The job ID always appears as "(#XXXXX)" — use this as the structural marker.
_TITLE_JOB_ID_RE = re.compile(r"\(#\d{3,}\)")

# Job title field: "#XXXXX: Specialty ..." — always starts with "#NNNNN:"
_JOB_TITLE_RE = re.compile(r"^#\d{3,}:")

# Practice value: "XXXX - City, ST" or "XXXX – City, ST" (em-dash variant),
# with an optional trailing suffix like " - Closed".
# The separator can be hyphen (-) or em-dash (–).
_PRACTICE_VALUE_RE = re.compile(r"^\d{3,5}\s*[-\u2013]\s*.+,\s*[A-Z]{2}", re.UNICODE)

# Description length thresholds (calibrated from real data: min=313, median=633).
_DESC_SHORT_THRESHOLD = 200   # anything below this is suspicious


# ── Public API ────────────────────────────────────────────────────────────────

def validate_scraped_job(cleaned: dict, job_post_id: str = "") -> list[ValidationIssue]:
    """
    Run all validation checks on a parsed job dict (output of parse_job_content_txt).
    Returns a list of ValidationIssue objects (empty list = all checks passed).
    Never raises.
    """
    issues: list[ValidationIssue] = []
    try:
        c = {k: (v or "").strip() for k, v in (cleaned or {}).items()}
        _check_structural(c, issues)
        _check_critical_fields(c, issues)
        _check_important_fields(c, issues)
        _check_formats(c, issues)
        _check_anomalies(c, issues)
    except Exception as exc:
        issues.append(ValidationIssue(
            Severity.WARNING, "_validator",
            f"Validator itself raised an unexpected error: {exc}",
        ))
    return issues


def should_send_immediate_alert(issues: list[ValidationIssue]) -> bool:
    """
    Return True when issues are severe enough to warrant an immediate email.
    Rule: any CRITICAL issue, OR 3+ WARNINGs on a single job.
    """
    critical = sum(1 for i in issues if i.severity == Severity.CRITICAL)
    warnings = sum(1 for i in issues if i.severity == Severity.WARNING)
    return critical > 0 or warnings >= 3


def issues_summary(issues: list[ValidationIssue]) -> dict:
    """Return a dict with counts per severity."""
    return {
        "critical": sum(1 for i in issues if i.severity == Severity.CRITICAL),
        "warning":  sum(1 for i in issues if i.severity == Severity.WARNING),
        "info":     sum(1 for i in issues if i.severity == Severity.INFO),
        "total":    len(issues),
    }


def issues_as_text(issues: list[ValidationIssue], job_post_id: str = "") -> str:
    """Plain-text rendering of issues (for logs / plain-text email)."""
    if not issues:
        return "  ✓ All validation checks passed."
    lines = [f"  Job #{job_post_id}" if job_post_id else ""]
    for i in issues:
        lines.append(f"  [{i.severity.value}] {i.field}: {i.message}")
        if i.value is not None and str(i.value)[:120] not in i.message:
            lines.append(f"    → value: {str(i.value)[:120]}")
    return "\n".join(l for l in lines if l)


def issues_as_html(issues: list[ValidationIssue]) -> str:
    """HTML table rendering of issues (for rich email body)."""
    if not issues:
        return '<p style="color:#27ae60;">✓ All validation checks passed.</p>'
    _COLOR = {
        Severity.CRITICAL: "#c0392b",
        Severity.WARNING:  "#d35400",
        Severity.INFO:     "#2980b9",
    }
    rows = []
    for i in issues:
        color = _COLOR.get(i.severity, "#666")
        val_html = ""
        if i.value is not None:
            vs = str(i.value)[:150]
            if vs not in i.message:
                val_html = f'<br><span style="color:#666;font-size:11px;font-family:monospace;">→ {vs}</span>'
        rows.append(
            f'<tr style="border-bottom:1px solid #eee;">'
            f'<td style="color:{color};font-weight:bold;padding:5px 10px;white-space:nowrap;">'
            f'{i.severity.value}</td>'
            f'<td style="padding:5px 10px;font-family:monospace;white-space:nowrap;">{i.field}</td>'
            f'<td style="padding:5px 10px;">{i.message}{val_html}</td>'
            f'</tr>'
        )
    return (
        '<table style="border-collapse:collapse;width:100%;font-size:13px;'
        'border:1px solid #ddd;border-radius:4px;">'
        '<thead><tr style="background:#f5f5f5;">'
        '<th style="padding:6px 10px;text-align:left;font-size:12px;">Severity</th>'
        '<th style="padding:6px 10px;text-align:left;font-size:12px;">Field</th>'
        '<th style="padding:6px 10px;text-align:left;font-size:12px;">Issue</th>'
        '</tr></thead><tbody>'
        + "".join(rows)
        + '</tbody></table>'
    )


# ── Validation layers (private) ───────────────────────────────────────────────

def _check_structural(c: dict, issues: list) -> None:
    """
    Layer 1 — Structural integrity.
    Detects Kimedics page-layout changes or login walls.
    """
    title_line = c.get("title_line", "")
    desc_text  = c.get("description_full_text", "")

    # title_line is the first thing rendered by .sections__container.
    if not title_line:
        issues.append(ValidationIssue(
            Severity.CRITICAL, "title_line",
            "Title line is completely empty — .sections__container may not have rendered "
            "(possible login redirect or Kimedics page structure change)",
        ))
    elif not _TITLE_JOB_ID_RE.search(title_line):
        # Real format: "Specialty (Subspecialty) (#19406)". The (#XXXXX) is always present.
        issues.append(ValidationIssue(
            Severity.CRITICAL, "title_line",
            "Title line does not contain the expected job ID pattern '(#XXXXX)' — "
            "Kimedics may have restructured the job post header",
            value=title_line[:120],
        ))
    elif len(title_line) > 300:
        issues.append(ValidationIssue(
            Severity.CRITICAL, "title_line",
            f"Title line is {len(title_line)} chars — expected under 300. "
            "CSS selector may have captured the wrong container element",
            value=title_line[:120],
        ))

    # description_full_text comes from a separate textarea; empty = that selector broke.
    if not desc_text:
        issues.append(ValidationIssue(
            Severity.CRITICAL, "description_full_text",
            "Full job description is empty — the Kimedics textarea selector "
            "(section-builder.full-jobpost textarea) may have changed",
        ))
    elif len(desc_text) < _DESC_SHORT_THRESHOLD:
        # Real data minimum is 313 chars; anything below 200 is suspicious.
        issues.append(ValidationIssue(
            Severity.WARNING, "description_full_text",
            f"Full job description is only {len(desc_text)} chars "
            f"(real jobs average 633, minimum seen is 313) — "
            "selector may have partially broken or content was truncated",
            value=desc_text[:120],
        ))

    # 3+ critical fields all empty at once → total scrape failure.
    empty_critical = [f for f in CRITICAL_FIELDS if not c.get(f)]
    if len(empty_critical) >= 3:
        issues.append(ValidationIssue(
            Severity.CRITICAL, "multiple_fields",
            f"3+ critical fields are simultaneously empty: {empty_critical} — "
            "likely a complete scrape failure (login wall, timeout, or layout change)",
            value=empty_critical,
        ))


def _check_critical_fields(c: dict, issues: list) -> None:
    """
    Layer 2 — Critical field presence.
    title_line already handled in structural with richer context.
    """
    for f in CRITICAL_FIELDS:
        if f == "title_line":
            continue
        if not c.get(f):
            issues.append(ValidationIssue(
                Severity.WARNING, f,
                f"Critical field '{f}' is missing — this field is present on every "
                "real Kimedics job and is required for Salesforce submission",
            ))


def _check_important_fields(c: dict, issues: list) -> None:
    """
    Layer 3 — Important field presence.
    WARNING fields have 100% fill rate in real data — missing = scrape problem.
    INFO fields are sometimes legitimately absent.
    """
    for f in IMPORTANT_FIELDS_WARNING:
        if not c.get(f):
            issues.append(ValidationIssue(
                Severity.WARNING, f,
                f"Field '{f}' is missing — this field is present on every real Kimedics job",
            ))
    for f in IMPORTANT_FIELDS_INFO:
        if not c.get(f):
            issues.append(ValidationIssue(
                Severity.INFO, f,
                f"Field '{f}' is not populated (can be legitimately absent)",
            ))


def _check_formats(c: dict, issues: list) -> None:
    """
    Layer 4 — Format / content checks on fields that are present.
    Only fires when the field has a value (absence already flagged above).
    """
    # ── Status ────────────────────────────────────────────────────────────────
    # We know exactly 3 valid values from 554 real rows — flag anything else as WARNING.
    status = c.get("status", "")
    if status and status not in KNOWN_STATUSES:
        issues.append(ValidationIssue(
            Severity.WARNING, "status",
            f"Status '{status}' is not one of the 3 known Kimedics values. "
            f"Known: {sorted(KNOWN_STATUSES)}",
            value=status,
        ))

    # ── Priority ──────────────────────────────────────────────────────────────
    # We know exactly 3 valid values — unknown value is INFO (new value possible).
    priority = c.get("priority", "")
    if priority and priority not in KNOWN_PRIORITIES:
        issues.append(ValidationIssue(
            Severity.INFO, "priority",
            f"Priority '{priority}' is not one of the 3 known values: {sorted(KNOWN_PRIORITIES)}",
            value=priority,
        ))

    # ── State ─────────────────────────────────────────────────────────────────
    state = c.get("state", "")
    if state and state.upper() not in US_STATE_ABBREVS:
        issues.append(ValidationIssue(
            Severity.WARNING, "state",
            f"State '{state}' is not a recognised 2-letter US abbreviation",
            value=state,
        ))

    # ── Dates ─────────────────────────────────────────────────────────────────
    # Primary real-world format is MM/DD/YY. "ASAP" is also valid.
    for date_field in ("provider_start_date", "provider_end_date", "posted_date"):
        val = c.get(date_field, "")
        if val and not _DATE_RE.search(val):
            issues.append(ValidationIssue(
                Severity.WARNING, date_field,
                f"'{date_field}' value does not look like a date (expected MM/DD/YY or 'ASAP')",
                value=val[:80],
            ))

    # ── Job title format ──────────────────────────────────────────────────────
    # Real format is always "#XXXXX: Specialty (Subspecialty)".
    job_title = c.get("job_title", "")
    if job_title and not _JOB_TITLE_RE.match(job_title):
        issues.append(ValidationIssue(
            Severity.WARNING, "job_title",
            "job_title does not start with '#XXXXX:' — expected format is "
            "'#19406: Dentistry (Dentist (DMD/DDS))'",
            value=job_title[:100],
        ))

    # ── job_id ────────────────────────────────────────────────────────────────
    job_id = c.get("job_id", "")
    if job_id and not job_id.isdigit():
        issues.append(ValidationIssue(
            Severity.WARNING, "job_id",
            f"job_id '{job_id}' is not purely numeric — regex extraction may have failed",
            value=job_id,
        ))

def _check_anomalies(c: dict, issues: list) -> None:
    """
    Layer 5 — Cross-field logic anomalies.
    """
    # ── Practice value format ─────────────────────────────────────────────────
    # Real format: "XXXX - City, ST" or "XXXX – City, ST" (em-dash variant),
    # with optional suffix like " - Closed". 546/554 rows matched this pattern.
    pv = c.get("practice_value", "")
    if pv and not _PRACTICE_VALUE_RE.match(pv):
        issues.append(ValidationIssue(
            Severity.INFO, "practice_value",
            "practice_value does not match expected Kimedics format "
            "'XXXX - City, ST' (or em-dash variant) — may have captured wrong text",
            value=pv[:100],
        ))

    # ── POC should look like a person name, not a number or date ─────────────
    poc = c.get("point_of_contact", "")
    if poc and re.match(r"^\d", poc):
        issues.append(ValidationIssue(
            Severity.WARNING, "point_of_contact",
            "point_of_contact starts with a digit — likely captured wrong field",
            value=poc[:80],
        ))

    # ── City should not be a pure number ─────────────────────────────────────
    city = c.get("city", "")
    if city and city.isdigit():
        issues.append(ValidationIssue(
            Severity.WARNING, "city",
            f"City value '{city}' is purely numeric — likely a parse error",
            value=city,
        ))

"""
Parse raw job content text (from Kimedics job post page) into a structured row.
Designed to work 100% of the time: missing or malformed fields become empty string.

Extraction stages (see job_content_ai for validate/fix):
  1. Structured header (label/value pairs) — status, POC, dates, etc.
  2. Description block after ``--- Description (full text) ---``:
     - Labeled lines ``Address:``, ``City:``, ``Dates:``, ``Hours:``, ``Clinical Staff:``, etc.
     - Bullet insight lines starting with ``*``
     - Fallback section-style blocks (heading on its own line) when labels are absent
"""

from __future__ import annotations

import re
from typing import Optional
from pathlib import Path
from typing import Union, Optional

from utils.address_display_format import format_us_address_line_for_display
from utils.sf_text_normalize import strip_trailing_commas_from_sf_text
from utils.job_description_proxi_template import extract_active_needs_dates
from utils.us_state_expand import US_STATE_CODE_TO_NAME

# Invert for "Missouri" row + "… Independence MO" street (abbrev vs full name).
_STATE_FULL_NAME_UPPER_TO_CODE: dict[str, str] = {
    name.upper(): code for code, name in US_STATE_CODE_TO_NAME.items()
}

# Canonical columns we always output (for CSV): identity → location → org/status → scheduling →
# clinical detail → insight → remaining Kimedics header fields → full description.
JOB_CONTENT_COLUMNS = [
    "job_id",
    "title_line",
    "location_line",
    "practice_value",
    "city",
    "state",
    "address_line",
    "job_title",
    "posting_org",
    "priority",
    "status",
    "point_of_contact",
    "provider_start_date",
    "provider_end_date",
    "posted_date",
    "dates_needed",
    "standard_schedule",
    "required_procedures",
    "additional_requirements",
    "support_staff",
    "avg_patients_per_day",
    "roster_only",
    "insight",
    "basics_job_title",
    "number_of_open_positions",
    "shift_credential_accepted",
    "position_type",
    "time",
    "rates",
    "only_accept_providers_under_max_rates",
    "why_searching_for_providers",
    "shifts_available",
    "estimated_shifts_per_month",
    "state_license_required",
    "board_specialty_match",
    "privileges_available",
    "min_years_experience",
    "geographic_restriction",
    "description_full_text",
]

# Map exact label (as in file) -> our column name (first occurrence wins for duplicates)
LABEL_TO_COLUMN = {
    "Job Title": "job_title",
    "Posted Date": "posted_date",
    "Posting Org": "posting_org",
    "Priority": "priority",
    "Status": "status",
    "Full Job Post": "_skip",
    "Description": "_skip",
    "Basics": "_skip",
    "Job title": "basics_job_title",
    "Number of open positions": "number_of_open_positions",
    "Shift Credential Accepted": "shift_credential_accepted",
    "Position type": "position_type",
    "Time": "time",
    "Rates": "rates",
    "Billable = $0-$0/hr": "_skip",  # value of Rates sometimes
    "Only accept providers under the max rates": "only_accept_providers_under_max_rates",
    "No": "_skip",
    "Yes": "_skip",
    "Point Of Contact": "point_of_contact",
    "Search Details": "_skip",
    "Why are you searching for providers?": "why_searching_for_providers",
    "Provider start date": "provider_start_date",
    "Provider end date": "provider_end_date",
    "Which shifts are available for providers?": "shifts_available",
    "Estimated shifts per month": "estimated_shifts_per_month",
    "State License Required To Apply": "state_license_required",
    "Board specialty must match practice set up to apply": "board_specialty_match",
    "Privileges Available": "privileges_available",
    "Minimum Years of Experience": "min_years_experience",
    "Geographic Restriction": "geographic_restriction",
    "Other notes": "_skip",
    "Sharing": "_skip",
    "Edit": "_skip",
}

def _parse_city_state(text: str) -> tuple[str, str]:
    """
    Best-effort city/state from a Kimedics line like '6313 - Cheektowaga, NY' or 'Baxter, MN'.

    For lines with a **numeric office prefix** (``4190 - Gloucester``), the part after the dash may
    be city-only (no comma); state then comes from elsewhere (e.g. Description ``State:``). Plain
    single tokens without that prefix (practice names like ``Acme``) are not treated as cities so
    ``Location: …`` can still supply city/state.
    """
    s = (text or "").strip()
    if not s:
        return "", ""
    had_numeric_prefix = False
    m = re.match(r"^\d+\s*-\s*(.+)$", s)
    if m:
        had_numeric_prefix = True
        s = m.group(1).strip()
    s = re.sub(r"^location\s*:\s*", "", s, flags=re.IGNORECASE).strip()
    if "," in s:
        left, right = s.rsplit(",", 1)
        return left.strip(), right.strip()
    if had_numeric_prefix and s:
        return s, ""
    return "", ""


def _extract_insight_lines(description: str) -> str:
    """Lines in the description that start with '*'."""
    lines = []
    for ln in (description or "").splitlines():
        t = ln.strip()
        if t.startswith("*"):
            lines.append(t)
    return "\n".join(lines)


def _norm_heading(s: str) -> str:
    return (s or "").strip().lower().rstrip(":")


def _is_insight_bullet_line(line: str) -> bool:
    """Kimedics insight/footnote lines start with one or more ``*``. Break continuation on them."""
    t = (line or "").lstrip()
    return t.startswith("*")


def _is_following_section_start(line: str) -> bool:
    """Heuristic: next titled block in Kimedics free-text descriptions."""
    t = line.strip()
    if not t or len(t) > 100:
        return False
    low = _norm_heading(t)
    markers = (
        "required procedures",
        "additional requirements",
        "clinical staff",
        "support staff",
        "dates needed",
        "standard schedule",
        "types of cases",
        "volume",
        "avg patients",
        "insight",
        "point of contact",
        "facility:",
        "address:",
        "city:",
        "state:",
        "dates:",
        "hours:",
    )
    return any(low.startswith(m) or low == m for m in markers)


def _kimedics_inline_label_value(line: str) -> Optional[tuple[str, str]]:
    """If line is 'Short Label: rest', return (lowercased label head, value part)."""
    s = (line or "").strip()
    if ":" not in s:
        return None
    head, _, tail = s.partition(":")
    h = head.strip()
    if len(h) > 70 or not re.match(r"^[A-Za-z0-9]", h):
        return None
    return (h.lower(), tail.strip())


def _section_after_heading(desc_lines: list[str], headings: tuple[str, ...]) -> str:
    """Collect text for a section: heading on its own line, or 'Heading: value' on one line."""
    for i, ln in enumerate(desc_lines):
        low = _norm_heading(ln)
        if not low:
            continue
        buf: list[str] = []
        matched = False
        for h in headings:
            if low == h or low.startswith(h + " "):
                matched = True
                break
            if low.startswith(h + ":"):
                matched = True
                rest = ln.split(":", 1)[1].strip()
                if rest:
                    buf.append(rest)
                break
        if not matched:
            continue
        for j in range(i + 1, len(desc_lines)):
            raw = desc_lines[j]
            stripped = raw.strip()
            if not stripped:
                if buf:
                    break
                continue
            if _is_insight_bullet_line(stripped):
                break
            if _kimedics_inline_label_value(stripped) is not None:
                break
            if _is_following_section_start(stripped) and buf:
                break
            buf.append(stripped)
        return "\n".join(buf).strip()
    return ""


# (regex, field) — order matters; more specific patterns first.
_DESC_LABELED_LINE_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"^Additional\s+requirements?\s*/\s*info\s*:\s*(.*)$", re.I), "additional_requirements"),
    (re.compile(r"^Additional\s+requirements?\s*:\s*(.*)$", re.I), "additional_requirements"),
    (re.compile(r"^Required\s+procedures?\s*:\s*(.*)$", re.I), "required_procedures"),
    (re.compile(r"^Clinical\s+staff\s*:\s*(.*)$", re.I), "support_staff"),
    (re.compile(r"^Support\s+staff\s*:\s*(.*)$", re.I), "support_staff"),
    (re.compile(r"^Avg\.?\s*patients\s+per\s+day\s*:\s*(.*)$", re.I), "avg_patients_per_day"),
    (re.compile(r"^Average\s+patients\s+per\s+day\s*:\s*(.*)$", re.I), "avg_patients_per_day"),
    (re.compile(r"^Dates\s+needed\s*:\s*(.*)$", re.I), "dates_needed"),
    (re.compile(r"^Dates\s*:\s*(.*)$", re.I), "dates_needed"),
    (re.compile(r"^Standard\s+schedule\s*:\s*(.*)$", re.I), "standard_schedule"),
    (re.compile(r"^Hours\s*:\s*(.*)$", re.I), "standard_schedule"),
    (re.compile(r"^Schedule\s*:\s*(.*)$", re.I), "standard_schedule"),
    (re.compile(r"^Address\s*:\s*(.*)$", re.I), "address_line"),
    (re.compile(r"^City\s*:\s*(.*)$", re.I), "city"),
    (re.compile(r"^State\s*:\s*(.*)$", re.I), "state"),
]

def _desc_label_token_matcher() -> re.Pattern[str]:
    """
    Regex matcher for *known* Kimedics description label tokens (strict allowlist).

    Derived from ``_DESC_LABELED_LINE_PATTERNS`` by stripping the anchored ``^...:\s*(.*)$`` shape
    into a non-anchored "label token" matcher that can be found within a longer physical line.
    """
    label_token_parts: list[str] = []
    for pat, _field in _DESC_LABELED_LINE_PATTERNS:
        p = pat.pattern
        if not p.startswith("^"):
            continue
        if r"\s*:\s*(.*)$" not in p:
            continue
        token = p[1:].split(r"\s*:\s*(.*)$", 1)[0]
        token = token.strip()
        if token:
            label_token_parts.append(token)
    # Safety: if patterns change unexpectedly, return something that never matches.
    if not label_token_parts:
        return re.compile(r"a\A")
    alt = "|".join(f"(?:{part})" for part in label_token_parts)
    return re.compile(rf"(?P<label>(?:{alt}))\s*:\s*", re.I)


_DESC_LABEL_TOKEN_MATCHER = _desc_label_token_matcher()


def _split_chained_desc_labels_into_lines(description: str) -> str:
    """
    Normalize Kimedics description text so chained labels on one physical line become separate lines.

    Only splits on a strict allowlist of labels the parser already understands (see
    ``_DESC_LABELED_LINE_PATTERNS``). This avoids splitting arbitrary prose that happens to contain a ':'.
    """
    if not (description or "").strip():
        return description or ""
    out_lines: list[str] = []
    for raw in (description or "").splitlines():
        line = raw.rstrip("\r")
        matches = list(_DESC_LABEL_TOKEN_MATCHER.finditer(line))
        if len(matches) < 2:
            out_lines.append(line)
            continue

        # Preserve any non-label prefix as its own line (rare, but avoids dropping text).
        prefix = line[: matches[0].start()]
        if prefix.strip():
            out_lines.append(prefix.strip())

        for idx, m in enumerate(matches):
            start = m.start()
            end = matches[idx + 1].start() if idx + 1 < len(matches) else len(line)
            chunk = line[start:end].strip()
            if chunk:
                out_lines.append(chunk)
    return "\n".join(out_lines)


def _line_matches_any_desc_label(line: str) -> bool:
    s = (line or "").strip()
    if not s:
        return False
    return any(p.match(s) for p, _ in _DESC_LABELED_LINE_PATTERNS)


def _extract_labeled_description_fields(description: str) -> dict[str, str]:
    """
    Kimedics-style 'Key: value' lines in the free-text description (often after Facility/Address).
    Values may continue on following lines until a blank line or another label line.
    """
    desc = _split_chained_desc_labels_into_lines(description or "").strip()
    if not desc:
        return {}
    lines = desc.splitlines()
    out: dict[str, str] = {}
    i = 0
    while i < len(lines):
        raw = lines[i]
        s = raw.strip()
        if not s:
            i += 1
            continue
        matched_field = None
        m0 = None
        for pat, field in _DESC_LABELED_LINE_PATTERNS:
            m = pat.match(s)
            if m:
                matched_field = field
                m0 = m
                break
        if not matched_field or m0 is None:
            i += 1
            continue
        v0 = (m0.group(1) or "").strip()
        parts: list[str] = [v0] if v0 else []
        j = i + 1
        while j < len(lines):
            nxt = lines[j]
            t = nxt.strip()
            if not t:
                break
            if _is_insight_bullet_line(t):
                break
            if _line_matches_any_desc_label(nxt):
                break
            if _kimedics_inline_label_value(nxt) is not None:
                break
            parts.append(t)
            j += 1
        val = " ".join(parts).strip()
        if val:
            out[matched_field] = val
        i = j
    return out


def _fill_from_description_blocks(out: dict) -> None:
    """Best-effort extraction from description_full_text (non-AI)."""
    desc = (out.get("description_full_text") or "").strip()
    if not desc:
        return
    # Kimedics / scraping can concatenate multiple `Label:` segments on one physical line.
    # Normalize here as well so section-heading extraction doesn't treat a subsequent label as the
    # "value" for the heading (e.g. `Avg patients per day: Additional requirements: ...`).
    desc = _split_chained_desc_labels_into_lines(desc)
    lines = desc.splitlines()

    active_dates = extract_active_needs_dates(desc)
    if active_dates:
        out["dates_needed"] = active_dates

    if not (out.get("required_procedures") or "").strip():
        block = _section_after_heading(
            lines,
            ("required procedures", "required procedure"),
        )
        if block:
            out["required_procedures"] = block

    if not (out.get("additional_requirements") or "").strip():
        block = _section_after_heading(
            lines,
            (
                "additional requirements/ info",
                "additional requirements",
                "additional requirements/info",
                "additional requirement",
            ),
        )
        if block:
            out["additional_requirements"] = block

    if not (out.get("support_staff") or "").strip():
        block = _section_after_heading(lines, ("clinical staff", "support staff"))
        if block:
            out["support_staff"] = block

    if not (out.get("dates_needed") or "").strip():
        block = _section_after_heading(lines, ("dates needed", "dates required", "coverage dates"))
        if block:
            out["dates_needed"] = block

    if not (out.get("standard_schedule") or "").strip():
        # Avoid bare "schedule" — it often grabs the next non-label line (e.g. a person's name).
        block = _section_after_heading(
            lines,
            ("standard schedule", "shift hours", "hours", "weekly hours"),
        )
        if block:
            out["standard_schedule"] = block

    if not (out.get("avg_patients_per_day") or "").strip():
        block = _section_after_heading(
            lines,
            ("avg patients per day", "average patients per day", "avg. patients per day"),
        )
        if block:
            out["avg_patients_per_day"] = block


def _is_plausible_schedule_text(s: str) -> bool:
    """True if text looks like hours/days, not a person name or stray label."""
    t = (s or "").strip()
    if not t:
        return False
    low = t.lower()
    if re.search(r"\d", t):
        return True
    if re.search(r"\b(am|pm|a\.m\.|p\.m\.)\b", low):
        return True
    if re.search(
        r"\b(mon|tue|wed|thu|fri|sat|sun|monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b",
        low,
    ):
        return True
    if "hour" in low or "shift" in low:
        return True
    return False


def _sanitize_standard_schedule(out: dict) -> None:
    ss = (out.get("standard_schedule") or "").strip()
    if ss and not _is_plausible_schedule_text(ss):
        out["standard_schedule"] = ""


def _normalize_state_code(state: str) -> str:
    """
    Strip parenthetical qualifiers, e.g. ``IL (Rogers Park)`` → ``IL``, ``TX (NW Crossing)`` → ``TX``.
    """
    s = (state or "").strip()
    if not s:
        return ""
    s = re.sub(r"\s*\([^)]*\)", "", s)
    return s.strip()


_SINGLE_COUNT_SUPPORT_STAFF = re.compile(r"^\s*(\d+)\s*\.?\s*$")


def _normalize_support_staff(text: str) -> str:
    """If value is only a number (optional trailing period), append `` team members``."""
    s = (text or "").strip()
    if not s:
        return ""
    m = _SINGLE_COUNT_SUPPORT_STAFF.match(s)
    if m:
        return f"{int(m.group(1))} team members"
    return strip_trailing_commas_from_sf_text(s)


def _collapse_whitespace(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip()).strip()


def _city_appears_in_street(street: str, city: str) -> bool:
    if not city or not street:
        return False
    return city.lower() in street.lower()


def _us_state_row_match_tokens(state: str) -> list[str]:
    """
    Row ``state`` → uppercase strings to look for in the street line (2-letter and/or full name).

    Kimedics often ends Address with ``City ST`` while City:/State: use full state name — without
    both forms we duplicate (e.g. ``… Independence MO, Missouri``). Unknown / non-US values return
    the trimmed upper string only (legacy token behavior).
    """
    s = (state or "").strip()
    if not s:
        return []
    su = s.upper()
    if len(su) == 2 and su.isalpha():
        full = US_STATE_CODE_TO_NAME.get(su)
        return [su] + ([full.upper()] if full else [])
    code = _STATE_FULL_NAME_UPPER_TO_CODE.get(su)
    if code:
        return [code, su]
    return [su]


def _state_token_boundary_re(tok: str) -> re.Pattern[str]:
    """Comma/start/end or whitespace boundary; allow digit after 2-letter (``TX 75001``)."""
    if len(tok) == 2 and tok.isalpha():
        return re.compile(rf"(?:^|[\s,]){re.escape(tok)}(?=$|[\s,\.]|\d)", re.IGNORECASE)
    return re.compile(rf"(?:^|[\s,]){re.escape(tok)}(?=$|[\s,\.])", re.IGNORECASE)


def _state_appears_in_street(street: str, state: str) -> bool:
    """True if the row state (abbrev or full US name) already appears as a segment in ``street``."""
    if not state or not street:
        return False
    toks = _us_state_row_match_tokens(state)
    for tok in toks:
        if _state_token_boundary_re(tok).search(street):
            return True
    # Legacy: uncommon / non-canonical state text (still token-bounded on the raw upper value).
    raw = state.strip().upper()
    if raw and raw not in toks and _state_token_boundary_re(raw).search(street):
        return True
    return False


def _compose_full_address_line(out: dict) -> None:
    """
    Build one ``address_line``: street from ``Address:`` plus city/state when missing.
    If the street segment already includes city and state tokens, keep it (whitespace-normalized only).
    """
    street = _collapse_whitespace(out.get("address_line") or "")
    city = (out.get("city") or "").strip()
    state = (out.get("state") or "").strip()
    if not street and not city and not state:
        out["address_line"] = ""
        return
    if not street:
        if city and state:
            out["address_line"] = f"{city}, {state}"
        else:
            out["address_line"] = city or state
        return
    has_c = _city_appears_in_street(street, city)
    has_s = _state_appears_in_street(street, state)
    if has_c and has_s:
        out["address_line"] = street
    elif not has_c and city and state:
        out["address_line"] = f"{street}, {city}, {state}"
    elif not has_s and state:
        out["address_line"] = f"{street}, {state}"
    elif not has_c and city:
        out["address_line"] = f"{street}, {city}"
    else:
        out["address_line"] = street


def _normalize_city_display(city: str) -> str:
    """Title-style city for display (e.g. all-caps Kimedics labels → ``Los Angeles``)."""
    s = _collapse_whitespace(city)
    if not s:
        return ""
    return format_us_address_line_for_display(s)


def _finalize_field_normalizations(out: dict) -> None:
    """Post-parse cleanup for city, state, and support_staff."""
    c = (out.get("city") or "").strip()
    if c:
        out["city"] = _normalize_city_display(c)
    st = (out.get("state") or "").strip()
    if st:
        out["state"] = _normalize_state_code(st)
    ss = (out.get("support_staff") or "").strip()
    if ss:
        out["support_staff"] = _normalize_support_staff(ss)


def _normalize_key(key: str) -> str:
    key = (key or "").strip()
    if key in LABEL_TO_COLUMN:
        col = LABEL_TO_COLUMN[key]
        if col == "_skip":
            return "_skip"
        return col
    return ""


def _extract_practice_value_from_description(desc: str) -> str:
    """
    Kimedics often repeats the practice line in the description (``Facility:`` or a standalone
    ``#### - City, ST`` line) when the header omits it (e.g. line 3 is ``Job Title``).
    """
    d = (desc or "").strip()
    if not d:
        return ""
    m = re.search(r"(?im)^Facility\s*:\s*(.+)$", d)
    if m:
        return m.group(1).strip()
    for ln in d.splitlines()[:40]:
        t = ln.strip()
        if not t:
            continue
        # Skip ISO / numeric date-only lines so we do not treat them as practice ids.
        if re.match(r"^\d{4}-\d{2}-\d{2}\b", t) or re.match(r"^\d{1,2}/\d{1,2}/\d{2,4}\b", t):
            continue
        # Office id + location: "4361 - Rosenberg, TX" (digit chunk then hyphen; rest has letters).
        if re.match(r"^\d{3,5}\s*-\s*.+", t) and re.search(r"[A-Za-z]", t):
            return t
    return ""


# Common UI elements that should never be extracted as practice values
_UI_ELEMENT_BLOCKLIST = frozenset({
    "sign out",
    "signout",
    "log out",
    "logout",
    "sign in",
    "signin",
    "log in",
    "login",
    "home",
    "back",
    "menu",
    "settings",
    "profile",
    "dashboard",
    "help",
    "support",
    "close",
    "cancel",
    "submit",
    "save",
    "edit",
    "delete",
    "search",
    "filter",
    "sort",
    "export",
    "import",
    "download",
    "upload",
})


def _is_valid_practice_value(value: str) -> bool:
    """Check if a string is a valid practice value (not a UI element)."""
    v = (value or "").strip()
    if not v:
        return False

    # Check against UI element blocklist
    v_lower = v.lower()
    if v_lower in _UI_ELEMENT_BLOCKLIST:
        return False

    # Practice values should typically have letters/numbers and be more than 2 chars
    if len(v) < 3:
        return False

    # Check if it's just navigation text (all lowercase single/two words)
    if v_lower == v and len(v.split()) <= 2 and not any(c.isdigit() for c in v):
        # Likely a navigation element unless it contains location-like patterns
        if not any(marker in v_lower for marker in [",", " - ", "dental", "clinic", "practice", "office", "center"]):
            return False

    return True


def _backfill_practice_value(out: dict, main_block: str) -> None:
    if (out.get("practice_value") or "").strip():
        # Validate existing practice value
        if not _is_valid_practice_value(out["practice_value"]):
            out["practice_value"] = ""
        else:
            return

    v = _extract_practice_value_from_description(out.get("description_full_text") or "")
    if v and _is_valid_practice_value(v):
        out["practice_value"] = v
        return

    lines = [ln.strip() for ln in (main_block or "").splitlines()]
    for i, ln in enumerate(lines):
        if ln.lower() != "practice":
            continue
        if i + 1 >= len(lines):
            break
        nxt = lines[i + 1].strip()
        if nxt and not _normalize_key(nxt) and _is_valid_practice_value(nxt):
            out["practice_value"] = nxt
            return


def _extract_practice_value_from_sidebar(main_block: str) -> str:
    """
    Pull the structured "Practice" value from the Kimedics web sidebar (which is
    flattened into ``main_block`` via ``inner_text()`` during the Playwright scrape).

    Pattern: a line equal to ``Practice`` (case-insensitive) followed by a non-label
    line that looks like ``\\d{3,5}\\s*-\\s*City, ST``. This is a *standalone* lookup
    that doesn't share state with the line-3 heuristic, so we can use it as a
    high-trust candidate when reconciling.
    """
    if not main_block:
        return ""
    lines = [ln.strip() for ln in main_block.splitlines()]
    for i, ln in enumerate(lines):
        if ln.lower() != "practice":
            continue
        # Scan the next few non-empty lines (Kimedics sometimes inserts blanks).
        for j in range(i + 1, min(i + 5, len(lines))):
            nxt = lines[j]
            if not nxt:
                continue
            if _normalize_key(nxt):
                # Hit another label — abort this candidate.
                break
            if re.match(r"^\d{3,5}\s*-\s*.+", nxt) and re.search(r"[A-Za-z]", nxt) and _is_valid_practice_value(nxt):
                return nxt
            break
    return ""


def _practice_candidates(out: dict, main_block: str) -> list[tuple[str, str]]:
    """
    Build an ordered candidate list (most → least trustworthy) for the practice value.
    Each entry is ``(source_label, raw_value)``. Used by ``parse_job_content_txt`` when
    an SF practice map is provided so we can pick the candidate that already maps 1:1.
    Deduplicated downstream by normalized practice key.
    """
    candidates: list[tuple[str, str]] = []
    sidebar = _extract_practice_value_from_sidebar(main_block)
    if sidebar and _is_valid_practice_value(sidebar):
        candidates.append(("kimedics_sidebar", sidebar))
    # The "primary" current heuristic value, whatever it ended up being.
    primary = (out.get("practice_value") or "").strip()
    if primary and _is_valid_practice_value(primary):
        candidates.append(("header_or_existing", primary))
    # The "first \\d{3,5} - City, ST" line in the description body — least trusted.
    desc_line = _extract_practice_value_from_description(out.get("description_full_text") or "")
    if desc_line and _is_valid_practice_value(desc_line):
        candidates.append(("description_line", desc_line))
    return candidates


def _reconcile_practice_value_against_sf(
    out: dict,
    main_block: str,
    sf_practice_map: Optional[dict],
) -> None:
    """
    When ``sf_practice_map`` (``{practice_key → set[sf_job_id]}``) is provided, walk our
    candidate list and pick the first one with a 1:1 hit in Salesforce. This catches
    cases like a JD body that says "419 - Georgetown, KY" while the Kimedics sidebar
    shows "2419 - Georgetown, KY" (the real value). No-op if the map is missing or
    no candidate matches — we keep whatever the heuristics produced.
    """
    if not sf_practice_map:
        return
    try:
        # Lazy import: keep parser usable in test contexts that don't have SF utils.
        from utils.sf_practice_key import practice_key as _practice_key
    except Exception:
        return

    candidates = _practice_candidates(out, main_block)
    if len(candidates) < 2:
        return  # Nothing to reconcile — only one candidate.

    seen_keys: set[str] = set()
    chosen: Optional[tuple[str, str, str]] = None  # (source, raw, key)
    for source, raw in candidates:
        key = _practice_key(raw)
        if not key or key in seen_keys:
            continue
        seen_keys.add(key)
        hits = sf_practice_map.get(key)
        if hits and len(hits) == 1:
            chosen = (source, raw, key)
            break

    if not chosen:
        return

    source, raw, _key = chosen
    current = (out.get("practice_value") or "").strip()
    if raw == current:
        return  # Already on the right value.
    out["practice_value"] = raw
    # Visible signal in batch logs that reconciliation flipped the value. We
    # deliberately don't add a column for this — the right value is stored,
    # and the run output captures what happened.
    import sys as _sys
    print(
        f"[parser] practice_value reconciled via {source}: "
        f"{current!r} -> {raw!r} (1:1 SF hit)",
        file=_sys.stderr,
    )


# Kimedics job text (header + description): "roster only" / "roster-only" / "open to roster" → ``roster_only`` = true/false.
_ROSTER_ONLY_PHRASE = re.compile(r"roster\s*[-]?\s*only", re.IGNORECASE)
_OPEN_TO_ROSTER_PHRASE = re.compile(r"\bopen\s+to\s+roster\b", re.IGNORECASE)


def infer_roster_only_from_full_text(full_text: str) -> str:
    """Return ``\"true\"`` or ``\"false\"`` for Supabase / row payloads."""
    if not (full_text or "").strip():
        return "false"
    if _ROSTER_ONLY_PHRASE.search(full_text):
        return "true"
    if _OPEN_TO_ROSTER_PHRASE.search(full_text):
        return "true"
    return "false"


def repair_flat_jobpost_text_missing_posted_date(flat_text: str, posted_date: Optional[str]) -> str:
    """
    Kimedics job UI often lays out metadata in two columns. Playwright's ``inner_text()`` on
    ``.sections__container`` lists the *label* column first (``Posted Date`` then ``Posting Org``)
    while the posted date value lives in the other column—commonly an ``<input>``—so the
    flattened text has **no** line between those labels and ``parse_job_content_txt`` leaves
    ``posted_date`` empty.

    When the browser exposes a real posted date (see ``extract_posted_date_from_kimedics_page``),
    splice it between ``Posted Date`` and ``Posting Org`` so the alternating key/value parser works.
    """
    pd = (posted_date or "").strip()
    if not pd or not (flat_text or "").strip():
        return flat_text or ""
    t = (flat_text or "").replace("\r\n", "\n")
    for needle in ("Posted Date\nPosting Org", "Posted Date\n\nPosting Org"):
        if needle in t:
            return t.replace(needle, f"Posted Date\n{pd}\nPosting Org", 1)
    return flat_text


def parse_job_content_txt(text: str, sf_practice_map: Optional[dict] = None) -> dict:
    """
    Parse raw job post text into a single row dict with keys in JOB_CONTENT_COLUMNS.
    Never raises: missing/malformed data yields empty strings.

    When ``sf_practice_map`` is provided (``{practice_key → set[sf_job_id]}``), reconcile
    the chosen ``practice_value`` against Salesforce: collect all plausible candidates
    (Kimedics sidebar > current heuristic > description-line) and pick the first one
    that has a 1:1 hit in Salesforce. Catches typos like ``"419 - Georgetown, KY"``
    in the JD body when the real value is ``"2419 - Georgetown, KY"`` in the sidebar.
    Falls back to today's heuristic when the map isn't provided or no candidate matches.
    """
    out = {c: "" for c in JOB_CONTENT_COLUMNS}
    if not (text or "").strip():
        out["roster_only"] = "false"
        return out

    # Split description block
    desc_marker = "--- Description (full text) ---"
    parts = text.split(desc_marker, 1)
    main_block = (parts[0] or "").strip()
    out["description_full_text"] = (parts[1] or "").strip() if len(parts) > 1 else ""

    lines = [ln.strip() for ln in main_block.splitlines()]
    if not lines:
        out["roster_only"] = infer_roster_only_from_full_text(text)
        return out

    # First line: title and job_id
    out["title_line"] = lines[0]
    m = re.search(r"#(\d+)", lines[0])
    if m:
        out["job_id"] = m.group(1)

    if len(lines) >= 2:
        out["location_line"] = lines[1]
    # Line 2 is "Practice"; line 3 may be practice value (e.g. "6313 - Cheektowaga, NY") or missing (19476 has "Job Title" here)
    start_i = 4
    if len(lines) >= 4:
        if _normalize_key(lines[3]):
            # Line 3 is a known label (e.g. "Job Title") — practice value missing, start key-value from 3
            out["practice_value"] = ""
            start_i = 3
        else:
            # Validate that lines[3] is a legitimate practice value
            if _is_valid_practice_value(lines[3]):
                out["practice_value"] = lines[3]
            else:
                # Invalid practice value (likely UI element), treat as empty
                out["practice_value"] = ""
                start_i = 3

    # From start_i: alternating key, value. If the "value" is itself a known label, treat current key's value as "" and use that line as next key.
    i = start_i
    while i + 1 < len(lines):
        key = lines[i]
        value = lines[i + 1]
        col = _normalize_key(key)
        if col and col != "_skip" and col in out:
            # If value is a known label (e.g. "Provider start date" after "Other"), store empty for this key
            if _normalize_key(value):
                out[col] = ""
            else:
                out[col] = value
        if _normalize_key(value):
            i += 1  # value is next key
        else:
            i += 2

    _backfill_practice_value(out, main_block)

    # Reconcile the chosen practice_value against Salesforce when we have a map.
    # No-op when sf_practice_map is None or no candidate has a 1:1 hit.
    # Defensive: a bug in this step (e.g. a regex blowing the recursion limit on
    # an unusual sidebar) must not take out the whole parse. The heuristic
    # value already in `out["practice_value"]` is a fine fallback.
    try:
        _reconcile_practice_value_against_sf(out, main_block, sf_practice_map)
    except Exception as _exc:
        import sys as _sys, traceback as _tb
        print(
            f"[parser] _reconcile_practice_value_against_sf failed; keeping heuristic value "
            f"({out.get('practice_value')!r}). err={type(_exc).__name__}: {str(_exc)[:200]}",
            file=_sys.stderr,
        )
        _tb.print_exc(file=_sys.stderr)

    city, st = _parse_city_state(out.get("practice_value") or "")
    if not city and not st:
        city, st = _parse_city_state(out.get("location_line") or "")
    out["city"] = city
    out["state"] = st
    # Description labels (e.g. City:/State:/Dates:) override header-derived city/state when present.
    labeled = _extract_labeled_description_fields(out.get("description_full_text") or "")
    for key, val in labeled.items():
        v = (val or "").strip()
        if v:
            out[key] = v
    out["insight"] = _extract_insight_lines(out.get("description_full_text") or "")
    for k in (
        "dates_needed",
        "standard_schedule",
        "required_procedures",
        "additional_requirements",
        "support_staff",
        "avg_patients_per_day",
    ):
        if not out.get(k):
            out[k] = ""

    _fill_from_description_blocks(out)
    _sanitize_standard_schedule(out)
    _finalize_field_normalizations(out)
    _compose_full_address_line(out)
    out["address_line"] = format_us_address_line_for_display(out.get("address_line") or "")

    out["roster_only"] = infer_roster_only_from_full_text(text)
    return out


def parse_job_content_file(path: Union[Path, str]) -> dict:
    """Read a .txt or .csv path and return parsed row (for .txt). For .csv, read first data row and return as dict if needed; else treat as txt."""
    path = Path(path)
    if not path.exists():
        return parse_job_content_txt("")
    text = path.read_text(encoding="utf-8", errors="replace")
    return parse_job_content_txt(text)


def cleaned_row_to_flat_dict(row: dict) -> dict:
    """Ensure row has exactly JOB_CONTENT_COLUMNS with string values."""
    flat = {}
    for col in JOB_CONTENT_COLUMNS:
        flat[col] = str((row.get(col) or "")).strip()
    return flat

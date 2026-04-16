"""
Map Supabase ``job_current`` / parser-shaped rows → Salesforce Job__c field dict.

Push-time only:
  - Account / worksite Ids and fixed DJC defaults (not stored in Supabase).
  - Canonical Proxi job description (optional) so narrative matches structured fields.
  - ``Salary_Pay_Range__c`` always uses the fixed default (``Starting at $125/hour``); Kimedics pay lines are ignored for that field.
  - Full US state names on Job_State__c.

See ``docs/engineering/salesforce_job_push_rules.md`` for the full rule set.

The set ``CANONICAL_JOB_C_PUSH_FIELD_NAMES`` is the single allow-list of Job__c API names this module
may emit before the Salesforce describe filter; add new mappings there and in the doc together.
"""

from __future__ import annotations

import os
import re
import sys
from datetime import datetime
from typing import Any, Optional

from utils.insight_sanitize import sanitize_insight_for_salesforce
from utils.job_description_proxi_template import (
    build_proxi_job_posting_description,
    effective_dates_needed,
    strip_internal_presentation_phrases,
)
from utils.sf_pay_range import DEFAULT_SALARY_PAY_RANGE
from utils.address_display_format import format_us_address_line_for_display
from utils.job_content_parser import _parse_city_state, infer_roster_only_from_full_text
from utils.sf_push_defaults import SF_ACCOUNT_ASPEN_DENTAL_MANAGEMENT_ID
from utils.sf_text_normalize import strip_trailing_commas_from_sf_text
from utils.us_state_expand import state_abbrev_for_job_title, state_name_for_salesforce

# Display string in Job ``Name`` (must match org / formula expectations; same org as primary account).
SF_JOB_PRIMARY_ACCOUNT_DISPLAY_NAME = "Aspen Dental Management Inc."

# Salesforce ``Name`` on Job__c is often limited to 80 characters.
SF_JOB_NAME_MAX_LEN = 80

# Checkbox (or boolean) on Job__c — API name must match Setup → Job__c → Fields exactly.
ROSTER_ONLY_FIELD = "roster_only__c"

# Kimedics id → Salesforce External_Job_ID__c (org may enforce max length on Text field).
EXTERNAL_JOB_ID_MAX_LEN = 20

# External_Job_Link__c is often Text(255); fallback tracker URLs may need truncation.
EXTERNAL_JOB_LINK_MAX_LEN = 255

# Stable Kimedics deep link (avoids long email tracker URLs in Salesforce).
KIMEDICS_PORTAL_JOB_POST_URL_PREFIX = "https://portal.kimedics.com/app/workspace/job-posts/"

# Rich Text Area: HTML (default). Long Text Area: set PROXI_JOB_DESCRIPTION_HTML=false to avoid raw tags.
def _canonical_description_use_html() -> bool:
    return os.environ.get("PROXI_JOB_DESCRIPTION_HTML", "true").lower() in ("1", "true", "yes")

# Role + job-source picklists — always on **create**; on PATCH only when Salesforce value is empty
# (see :func:`merge_job_role_defaults_for_empty_sf_fields` and ``prepare_payload_for_write``).
# ``Job_Job_Source__c`` is coerced to the org’s API value via :func:`coerce_picklists_to_valid`.
SF_PUSH_JOB_ROLE_DEFAULTS: dict[str, str] = {
    "Job_Position_Type__c": "Locums",
    "Job_Specialty__c": "General Dentistry",
    "Occupation_DJC__c": "Dentist",
    "Job_Job_Source__c": "Shiftwise - Aspen Dental - AMN",
}

# Fixed at push — do not persist these in Supabase (see docs).
# ``Occupation_DJC__c`` lives only under ``SF_PUSH_JOB_ROLE_DEFAULTS`` (same as primary role defaults).
SF_PUSH_STATIC_DEFAULTS: dict[str, str] = {
    **SF_PUSH_JOB_ROLE_DEFAULTS,
    "Position_Type_DJC__c": "Locums",
    "Specialty_DJC__c": "General Dentistry",
    "Worksite_Parent__c": "Aspen Dental Management Inc.",
    "Job_Patient_Ages__c": "Mostly Adults",
}

# Exact Job__c API names automation may send (before Salesforce describe filters FLS).
# Must stay aligned with ``job_row_to_salesforce_fields`` + ``SF_PUSH_STATIC_DEFAULTS``
# and ``docs/engineering/salesforce_job_push_rules.md``.
# Free-text Job__c fields: strip trailing commas so SF matches clean Kimedics lists (e.g. support staff).
_SF_TEXT_FIELDS_STRIP_TRAILING_COMMAS: frozenset[str] = frozenset(
    {
        "Job_Support_Staff__c",
        "Job_Types_of_Cases__c",
        "Job_Point_of_Contact__c",
        "Job_Dates_Needed__c",
        "Job_Standard_Schedule__c",
        "Standard_Schedule_Hours__c",
        "Job_Facility_Display__c",
        "Job_Client_Job_Id__c",
        "Insight__c",
        "Job_Volume__c",
        "Job_Worksite_1_Address__c",
    }
)

CANONICAL_JOB_C_PUSH_FIELD_NAMES: frozenset[str] = frozenset(
    {
        "External_Job_ID__c",
        "Job_Account__c",
        "Job_Worksite_Location_1__c",
        "Job_Client_Job_Id__c",
        "Job_Client_Job_Description__c",
        "External_Job_Link__c",
        "Job_Status__c",
        "Job_State__c",
        "Job_City__c",
        "Insight__c",
        "Job_Facility_Display__c",
        "Job_Worksite_1_Address__c",
        "Job_Point_of_Contact__c",
        "Job_Dates_Needed__c",
        "Job_Standard_Schedule__c",
        "Standard_Schedule_Hours__c",
        "Job_Types_of_Cases__c",
        "Job_Support_Staff__c",
        "Job_Provider_Start_Date__c",
        "Job_Provider_End_Date__c",
        "Salary_Pay_Range__c",
        ROSTER_ONLY_FIELD,
        "Job_Ranking__c",
        "Job_Volume__c",
        *SF_PUSH_STATIC_DEFAULTS.keys(),
    }
)

# API names omitted from Salesforce push when unset env (org FLS / field not on Job__c).
_DEFAULT_SF_PUSH_OMITS: frozenset[str] = frozenset(
    {
        "Job_Facility_Display__c",
        "Job_Worksite_1_Address__c",
        "Job_Point_of_Contact__c",
        "Job_Standard_Schedule__c",
        "Job_Provider_Start_Date__c",
        "Job_Provider_End_Date__c",
        "Position_Type_DJC__c",
        "Specialty_DJC__c",
        "Worksite_Parent__c",
    }
)


def sf_push_omitted_field_names() -> frozenset[str]:
    """
    Fields not sent to Salesforce for this deployment.

    - Env unset → use :data:`_DEFAULT_SF_PUSH_OMITS` (Aspen org: not updateable / not on describe).
    - ``PROXI_SF_OMIT_JOB_FIELDS=`` (empty) → omit nothing (other orgs / full push).
    - Otherwise comma-separated API names to omit in addition to or instead of default — use only
      explicit list when set to non-empty.
    """
    raw = os.environ.get("PROXI_SF_OMIT_JOB_FIELDS")
    if raw is None:
        return _DEFAULT_SF_PUSH_OMITS
    s = raw.strip()
    if not s:
        return frozenset()
    return frozenset(x.strip() for x in s.split(",") if x.strip())


def _omit_not_provided_sentinel_strings(out: dict[str, Any]) -> None:
    """Remove any string field equal to ``Not Provided`` (case-insensitive); do not send that placeholder to SF."""
    for k in list(out.keys()):
        v = out.get(k)
        if isinstance(v, str) and v.strip().casefold() == "not provided":
            del out[k]


def _apply_trailing_comma_strip_to_sf_text_fields(out: dict[str, Any]) -> None:
    for k in _SF_TEXT_FIELDS_STRIP_TRAILING_COMMAS:
        v = out.get(k)
        if v is None:
            continue
        if not isinstance(v, str):
            v = str(v)
        cleaned = strip_trailing_commas_from_sf_text(v)
        out[k] = cleaned if cleaned else None


def _sf_field_nonempty(val: Any) -> bool:
    """True when Salesforce already has a value (non-null, non-blank string)."""
    if val is None:
        return False
    if isinstance(val, bool):
        return val
    return bool(str(val).strip())


def merge_job_role_defaults_for_empty_sf_fields(target: dict[str, Any], current: dict[str, Any]) -> None:
    """
    Set role fields and ``Job_Job_Source__c`` on ``target`` only when ``current`` has no value for
    that field (PATCH backfill without overwriting non-null SF).
    """
    for fname, default_v in SF_PUSH_JOB_ROLE_DEFAULTS.items():
        if _sf_field_nonempty(current.get(fname)):
            continue
        target[fname] = default_v


def _truncate_external_job_link(url: Optional[str]) -> Optional[str]:
    s = (url or "").strip()
    if not s:
        return None
    if len(s) <= EXTERNAL_JOB_LINK_MAX_LEN:
        return s
    print(
        f"Note: External_Job_Link__c truncated from {len(s)} to {EXTERNAL_JOB_LINK_MAX_LEN} chars "
        "(Salesforce field limit). Store full URL in Supabase or widen the SF field.",
        file=sys.stderr,
    )
    return s[:EXTERNAL_JOB_LINK_MAX_LEN]


def external_job_link_from_job_row(row: Optional[dict]) -> Optional[str]:
    """
    Value for ``External_Job_Link__c``: canonical Kimedics portal URL when ``job_id`` is numeric
    (e.g. ``https://portal.kimedics.com/app/workspace/job-posts/19448``); otherwise truncated
    ``view_job_link`` (e.g. synthetic / test rows).
    """
    r = row or {}
    jid = r.get("job_id")
    if jid is not None:
        s = str(jid).strip()
        if s.isdigit():
            return _truncate_external_job_link(f"{KIMEDICS_PORTAL_JOB_POST_URL_PREFIX}{s}")
    return _truncate_external_job_link(r.get("view_job_link"))


def _truncate_external_job_id(job_id: Optional[str]) -> Optional[str]:
    if not job_id:
        return None
    s = str(job_id).strip()
    if not s:
        return None
    if len(s) <= EXTERNAL_JOB_ID_MAX_LEN:
        return s
    return s[:EXTERNAL_JOB_ID_MAX_LEN]


def external_job_id_match_key(job_id: Optional[str]) -> Optional[str]:
    """Lowercase truncated key for matching Kimedics ``job_id`` ↔ ``External_Job_ID__c``."""
    t = _truncate_external_job_id(job_id)
    return (t or "").strip().lower() or None


def job_status_for_salesforce_push(raw_status: Any) -> Optional[str]:
    """
    Map ``job_current.status`` → ``Job_Status__c`` at Salesforce push time only (Supabase unchanged).

    Salesforce only receives **Open** or **Closed** (never raw Kimedics labels like Inactive).

      * **Active, accepting new providers** (any casing) → **Open**
      * Everything else non-empty (e.g. not accepting, Inactive, Closed) → **Closed**
    """
    s = str(raw_status or "").strip()
    if not s:
        return None
    low = re.sub(r"\s+", " ", s.lower())
    # Must run before the "accepting new provider" check ("not accepting" still contains "accepting").
    if "not accepting" in low:
        return "Closed"
    if "accepting new provider" in low:
        return "Open"
    if low == "open":
        return "Open"
    return "Closed"


def roster_only_string_from_row(row: Optional[dict]) -> str:
    """
    ``\"true\"`` / ``\"false\"`` for ``roster_only__c``.

    Uses Supabase ``roster_only`` when set; otherwise applies the same phrases as the parser
    (``roster only`` / ``open to roster``, etc.) against description/insight/status so pushes stay
    correct if the column was empty or stale.
    """
    r = row or {}
    s = str(r.get("roster_only") or "").strip().lower()
    if s == "true":
        return "true"
    blob = "\n".join(
        str(r.get(k) or "")
        for k in ("description_full_text", "insight", "status", "title_line")
    )
    if infer_roster_only_from_full_text(blob) == "true":
        return "true"
    return "false"


def _mdy_to_iso(d: Optional[str]) -> Optional[str]:
    if not d or not isinstance(d, str):
        return None
    try:
        return datetime.strptime(d.strip(), "%m/%d/%y").date().isoformat()
    except ValueError:
        return None


def coerce_picklists_to_valid(describe: dict, fields_map: dict) -> None:
    """
    Restricted picklists reject labels. Align each sent picklist field to a valid API value
    from describe (match by value, then by label, else first active value). Mutates in place.
    """
    by_name = {f["name"]: f for f in describe.get("fields", []) if f.get("name")}
    for fname in list(fields_map.keys()):
        raw = fields_map.get(fname)
        if raw is None or raw == "":
            continue
        cur = str(raw).strip()
        finfo = by_name.get(fname)
        if not finfo or finfo.get("type") != "picklist":
            continue
        allowed: list[str] = []
        label_for: dict[str, str] = {}
        for pv in finfo.get("picklistValues") or []:
            if pv.get("active") is False:
                continue
            v = pv.get("value")
            if v is None:
                continue
            vs = str(v)
            allowed.append(vs)
            label_for[vs] = str(pv.get("label") or vs)
        if not allowed:
            fields_map.pop(fname, None)
            print(f"Note: no active picklist values for {fname}; omitting.", file=sys.stderr)
            continue
        if cur in allowed:
            fields_map[fname] = cur
            continue
        matched = None
        for v in allowed:
            if label_for.get(v) == cur:
                matched = v
                break
        if matched is not None:
            print(f"Note: {fname} label {cur!r} -> API value {matched!r}", file=sys.stderr)
            fields_map[fname] = matched
            continue
        fallback = allowed[0]
        print(
            f"Note: {fname} value {cur!r} not allowed; using first active picklist value {fallback!r}",
            file=sys.stderr,
        )
        fields_map[fname] = fallback


def job_name_brand_display_for_row(row: dict) -> str:
    """
    Middle segment of Job ``Name`` (DSO / posting org) for manual parity.

    Kimedics ``posting_org`` drives Heartland vs Midwest vs Aspen; unknown non-empty values
    are passed through trimmed; blank falls back to the primary Aspen account display name.
    """
    po = (row.get("posting_org") or "").strip().lower()
    if "heartland" in po:
        return "Heartland Dental"
    if "midwest" in po:
        return "Midwest Dental"
    if "aspen" in po:
        return SF_JOB_PRIMARY_ACCOUNT_DISPLAY_NAME
    raw = (row.get("posting_org") or "").strip()
    return raw if raw else SF_JOB_PRIMARY_ACCOUNT_DISPLAY_NAME


def _row_with_job_name_location_fallback(
    row: dict,
    job_name_location_fallback: Optional[dict[str, Any]],
) -> dict:
    """Fill missing ``city`` / ``state`` on a copy from Salesforce GET (PATCH path)."""
    if not job_name_location_fallback:
        return row
    out = dict(row)
    if not (out.get("city") or "").strip():
        v = job_name_location_fallback.get("Job_City__c")
        if v is not None and str(v).strip():
            out["city"] = str(v).strip()
    if not (out.get("state") or "").strip():
        v = job_name_location_fallback.get("Job_State__c")
        if v is not None and str(v).strip():
            out["state"] = str(v).strip()
    return out


def _city_for_job_name(row: dict) -> str:
    """Prefer ``city``; else parse ``practice_value`` / ``location_line`` so we do not emit ``()``."""
    c = (row.get("city") or "").strip()
    if c:
        return c
    city2, _st2 = _parse_city_state(row.get("practice_value") or "")
    if city2:
        return city2.strip()
    loc = (row.get("location_line") or "").strip()
    if loc and "," in loc:
        left = loc.rsplit(",", 1)[0].strip()
        left = re.sub(r"\s*\([^)]*\)\s*$", "", left).strip()
        if left:
            return left
    return ""


def build_salesforce_job_name(
    row: dict,
    *,
    job_name_location_fallback: Optional[dict[str, Any]] = None,
) -> Optional[str]:
    """
    Job record header pattern (examples)::

        OH (Shelby) General Dentistry - Midwest Dental - Closed
        SC (Summerville) General Dentistry - Heartland Dental - Open

    Brand segment comes from :func:`job_name_brand_display_for_row` (``posting_org``).
    When city is missing everywhere, parentheses are omitted (no ``()``).

    ``job_name_location_fallback``: optional ``Job_City__c`` / ``Job_State__c`` from a Salesforce
    GET when the Supabase row has blank location — **reference / tooling only**; Job ``Name`` is a
    formula in this org and is not written by automation.
    """
    r = _row_with_job_name_location_fallback(row, job_name_location_fallback)
    abbr = state_abbrev_for_job_title(r.get("state"))
    city = _city_for_job_name(r)
    specialty = str(
        SF_PUSH_STATIC_DEFAULTS.get("Job_Specialty__c")
        or SF_PUSH_STATIC_DEFAULTS.get("Specialty_DJC__c")
        or "General Dentistry"
    ).strip()
    st = job_status_for_salesforce_push(r.get("status")) or "Open"
    brand = job_name_brand_display_for_row(r)
    if city:
        loc = f"{abbr} ({city})".strip() if abbr else f"({city})"
        name = f"{loc} {specialty} - {brand} - {st}".strip()
    else:
        loc = abbr.strip() if abbr else ""
        name = f"{loc} {specialty} - {brand} - {st}".strip() if loc else f"{specialty} - {brand} - {st}"
    name = re.sub(r"\s+", " ", name).strip()
    # Defensive: never leave a bare "() " from bad inputs / legacy rows.
    if name.startswith("()"):
        name = name[2:].strip()
    name = re.sub(r"\s+", " ", name).strip()
    if len(name) > SF_JOB_NAME_MAX_LEN:
        name = name[: SF_JOB_NAME_MAX_LEN - 1].rstrip() + "…"
    return name


def job_row_to_salesforce_fields(
    row: dict,
    *,
    use_canonical_description: bool = True,
    primary_account_id: Optional[str] = None,
    worksite_account_id: Optional[str] = None,
    description_use_html: Optional[bool] = None,
) -> dict[str, Any]:
    """
    Build a Job__c field dict from one job row (``job_current`` shape).

    Hyperlink / lookup fields:
      Job_Account__c → primary account Id (Aspen Dental Management Inc.).
      Job_Worksite_Location_1__c → worksite account Id from the row (or ``worksite_account_id``
      override only). Omitted when unknown — no placeholder Account Id.

    ``use_canonical_description``: when True, set Job_Client_Job_Description__c from
    ``build_proxi_job_posting_description`` so copy matches structured fields; when False,
    use ``description_full_text`` from the row only.
    """
    r = dict(row or {})
    pid = (r.get("sf_primary_account_id") or "").strip() or (primary_account_id or SF_ACCOUNT_ASPEN_DENTAL_MANAGEMENT_ID)
    w_from_row = (r.get("sf_worksite_account_id") or "").strip()
    w_override = (worksite_account_id or "").strip() if worksite_account_id is not None else ""
    wid = (w_override or w_from_row) or None

    desc_raw = (r.get("description_full_text") or "").strip()
    # Always push the standard rate line on Job__c (Kimedics ranges in description are not used here).
    salary_pay = DEFAULT_SALARY_PAY_RANGE

    state_sf = state_name_for_salesforce(r.get("state"))
    city = (r.get("city") or "").strip()

    if use_canonical_description:
        use_html = _canonical_description_use_html() if description_use_html is None else description_use_html
        body = build_proxi_job_posting_description(r, use_html=use_html)
    else:
        body = strip_internal_presentation_phrases(desc_raw)

    support = r.get("support_staff")
    toc_raw = (r.get("types_of_cases") or "").strip()
    types_clean = strip_internal_presentation_phrases(toc_raw) if toc_raw else ""
    addr_raw = (r.get("address_line") or "").strip()
    addr = format_us_address_line_for_display(addr_raw) if addr_raw else None
    if addr == "":
        addr = None
    # Job__c Name is a formula (worksite shipping city/state, specialty, account, status) — do not PATCH.
    out: dict[str, Any] = {
        "External_Job_ID__c": _truncate_external_job_id(r.get("job_id")),
        "Job_Account__c": pid,
        "Job_Worksite_Location_1__c": wid if wid else None,
        "Job_Client_Job_Id__c": (r.get("practice_value") or "").strip() or None,
        "Job_Client_Job_Description__c": body or None,
        "External_Job_Link__c": external_job_link_from_job_row(r),
        "Job_Status__c": job_status_for_salesforce_push(r.get("status")),
        "Job_State__c": state_sf or None,
        "Job_City__c": city or None,
        "Insight__c": sanitize_insight_for_salesforce(r.get("insight")),
        "Job_Facility_Display__c": (r.get("practice_value") or "").strip() or None,
        "Job_Worksite_1_Address__c": addr,
        "Job_Point_of_Contact__c": (r.get("point_of_contact") or "").strip() or None,
        "Job_Dates_Needed__c": effective_dates_needed(r) or None,
        "Job_Standard_Schedule__c": (r.get("standard_schedule") or "").strip() or None,
        "Standard_Schedule_Hours__c": (r.get("standard_schedule") or "").strip() or None,
        "Job_Types_of_Cases__c": types_clean or None,
        "Job_Support_Staff__c": support if (support and str(support).strip()) else None,
        "Job_Provider_Start_Date__c": _mdy_to_iso(r.get("provider_start_date")),
        "Job_Provider_End_Date__c": _mdy_to_iso(r.get("provider_end_date")),
        "Salary_Pay_Range__c": salary_pay,
        ROSTER_ONLY_FIELD: roster_only_string_from_row(r),
    }
    out.update(SF_PUSH_STATIC_DEFAULTS)
    vol = (r.get("avg_patients_per_day") or "").strip()
    if vol and vol.casefold() != "not provided":
        out["Job_Volume__c"] = vol
    out["Job_Ranking__c"] = str(r.get("job_ranking") or "B").strip() or "B"

    _apply_trailing_comma_strip_to_sf_text_fields(out)
    _omit_not_provided_sentinel_strings(out)

    omit = sf_push_omitted_field_names()
    for k in omit:
        out.pop(k, None)

    allowed = CANONICAL_JOB_C_PUSH_FIELD_NAMES - omit
    extra = set(out.keys()) - allowed
    if extra:
        raise RuntimeError(
            "job_row_to_salesforce_fields produced keys not in allowed canonical set: "
            f"{sorted(extra)} — update the frozenset and docs together."
        )

    return out


def prepare_payload_for_write(
    row: dict,
    describe: dict,
    *,
    use_canonical_description: bool = True,
    for_update: bool = False,
    primary_account_id: Optional[str] = None,
    worksite_account_id: Optional[str] = None,
    description_use_html: Optional[bool] = None,
) -> dict[str, Any]:
    """
    Full row → Salesforce fields → picklist coercion → createable/updateable filter.
    """
    from utils.sf_job_rest_minimal import filter_createable_fields, filter_updateable_fields

    raw = job_row_to_salesforce_fields(
        row,
        use_canonical_description=use_canonical_description,
        primary_account_id=primary_account_id,
        worksite_account_id=worksite_account_id,
        description_use_html=description_use_html,
    )
    if for_update:
        for k in SF_PUSH_JOB_ROLE_DEFAULTS:
            raw.pop(k, None)
    coerce_picklists_to_valid(describe, raw)
    if for_update:
        return filter_updateable_fields(describe, raw)
    return filter_createable_fields(describe, raw)

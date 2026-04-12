"""
Map Supabase ``job_current`` / parser-shaped rows → Salesforce Job__c field dict.

Push-time only:
  - Account / worksite Ids and fixed DJC defaults (not stored in Supabase).
  - Canonical Proxi job description (optional) so narrative matches structured fields.
  - Salary range default unless extracted from Kimedics description text.
  - Full US state names on Job_State__c.

See ``docs/salesforce_job_push_rules.md`` for the full rule set.
"""

from __future__ import annotations

import os
import re
import sys
from datetime import datetime
from typing import Any, Optional

from utils.insight_sanitize import sanitize_insight_for_salesforce
from utils.job_description_proxi_template import build_proxi_job_posting_description
from utils.sf_pay_range import DEFAULT_SALARY_PAY_RANGE, extract_pay_range_from_description
from utils.us_state_expand import state_name_for_salesforce

# Default Salesforce Account Ids (hyperlink targets in UI; REST sends Id only).
SF_ACCOUNT_ASPEN_DENTAL_MANAGEMENT_ID = "0015f00000HH63kAAD"

# Kimedics id → Salesforce External_Job_ID__c (org may enforce max length on Text field).
EXTERNAL_JOB_ID_MAX_LEN = 20

# External_Job_Link__c is often Text(255); fallback tracker URLs may need truncation.
EXTERNAL_JOB_LINK_MAX_LEN = 255

# Stable Kimedics deep link (avoids long email tracker URLs in Salesforce).
KIMEDICS_PORTAL_JOB_POST_URL_PREFIX = "https://portal.kimedics.com/app/workspace/job-posts/"

# Rich Text Area: HTML (default). Long Text Area: set PROXI_JOB_DESCRIPTION_HTML=false to avoid raw tags.
def _canonical_description_use_html() -> bool:
    return os.environ.get("PROXI_JOB_DESCRIPTION_HTML", "true").lower() in ("1", "true", "yes")

# Fixed at push — do not persist these in Supabase (see docs).
SF_PUSH_STATIC_DEFAULTS: dict[str, str] = {
    "Position_Type_DJC__c": "Locums",
    "Specialty_DJC__c": "General Dentistry",
    "Occupation_DJC__c": "Dentist",
    "Worksite_Parent__c": "Aspen Dental Management Inc.",
    "Job_Patient_Ages__c": "Mostly Adults",
    "Job_Volume__c": "Not Provided",
}


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
    inferred_pay = extract_pay_range_from_description(desc_raw)
    salary_pay = inferred_pay if inferred_pay else DEFAULT_SALARY_PAY_RANGE

    state_sf = state_name_for_salesforce(r.get("state"))
    city = (r.get("city") or "").strip()

    if use_canonical_description:
        use_html = _canonical_description_use_html() if description_use_html is None else description_use_html
        body = build_proxi_job_posting_description(r, use_html=use_html)
    else:
        body = desc_raw

    support = r.get("support_staff")
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
        "Job_Street_Address__c": (r.get("address_line") or "").strip() or None,
        "Job_Point_of_Contact__c": (r.get("point_of_contact") or "").strip() or None,
        "Job_Dates_Needed__c": (r.get("dates_needed") or "").strip() or None,
        "Job_Standard_Schedule__c": (r.get("standard_schedule") or "").strip() or None,
        "Standard_Schedule_Hours__c": (r.get("standard_schedule") or "").strip() or None,
        "Job_Types_of_Cases__c": (r.get("types_of_cases") or "").strip() or None,
        "Job_Support_Staff__c": support if (support and str(support).strip()) else None,
        "Job_Provider_Start_Date__c": _mdy_to_iso(r.get("provider_start_date")),
        "Job_Provider_End_Date__c": _mdy_to_iso(r.get("provider_end_date")),
        "Salary_Pay_Range__c": salary_pay,
    }
    out.update(SF_PUSH_STATIC_DEFAULTS)
    out["Job_Ranking__c"] = str(r.get("job_ranking") or "B").strip() or "B"

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
    coerce_picklists_to_valid(describe, raw)
    if for_update:
        return filter_updateable_fields(describe, raw)
    return filter_createable_fields(describe, raw)

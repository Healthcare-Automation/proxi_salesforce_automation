"""
Values applied only when pushing to Salesforce (not stored in Supabase).

Reference Account Ids live in sf_account_reference (seeded in supabase_db.seed_sf_account_reference_defaults).
Resolved worksite / Job Ids on rows come from scrape-time SF practice match + Supabase cache (see sf_job_supabase_resolve).
"""

import os
from typing import Any, Optional

from utils.us_state_expand import state_abbrev_for_job_title

# Hyperlink / Account lookups (see also sf_account_reference.reference_key)
SF_REFERENCE_KEY_PRIMARY = "primary_aspen_dental_management"
SF_REFERENCE_KEY_WORKSITE_DEFAULT = "worksite_location_default"

# Primary Aspen org Account Id — ``Job_Account__c`` default and worksite Account ``ParentId``.
SF_ACCOUNT_ASPEN_DENTAL_MANAGEMENT_ID = "0015f00000HH63kAAD"

# Salesforce custom object field name for worksite hyperlink (documented for mappers)
SF_FIELD_WORKSITE_LOCATION = "Job_Worksite_Location_1__c"

# Push-time defaults for Job__c live in ``utils.sf_job_payload.SF_PUSH_STATIC_DEFAULTS``
# (DJC fields, Job_Patient_Ages__c, etc.). See ``docs/engineering/salesforce_job_push_rules.md``.


def format_worksite_account_name(city: str, state: str) -> str:
    """
    Worksite **Account** ``Name``: ``Aspen Dental - Dunkirk, NY`` (2-letter state).

    ``state`` may be a code or full name; see ``state_abbrev_for_job_title``.
    """
    c, s = (city or "").strip(), (state or "").strip()
    abbr = state_abbrev_for_job_title(s) if s else ""
    if c and abbr:
        return f"Aspen Dental - {c}, {abbr}"
    if c:
        return f"Aspen Dental - {c}"
    if abbr:
        return f"Aspen Dental - {abbr}"
    return "Aspen Dental"


def format_worksite_display_label(city: str, state: str) -> str:
    """Same as :func:`format_worksite_account_name` (map label + Account Name)."""
    return format_worksite_account_name(city, state)


def worksite_account_record_type_id(
    describe_account: dict[str, Any],
    *,
    env_record_type_id: Optional[str] = None,
) -> Optional[str]:
    """
    Record type Id for new worksite Accounts.

    Prefer env ``PROXI_SF_ACCOUNT_WORKSITE_RECORD_TYPE_ID`` (18-char Id). Otherwise pick the
    Account describe entry whose **name** or **developerName** is ``Worksite`` (case-sensitive).
    """
    raw = (env_record_type_id or os.environ.get("PROXI_SF_ACCOUNT_WORKSITE_RECORD_TYPE_ID") or "").strip()
    if raw:
        return raw
    for rt in describe_account.get("recordTypeInfos") or []:
        if rt.get("available") is False:
            continue
        name = (rt.get("name") or "").strip()
        dev = (rt.get("developerName") or "").strip()
        if name == "Worksite" or dev == "Worksite":
            rid = rt.get("recordTypeId")
            if rid:
                return str(rid).strip()
    return None

"""
Values applied only when pushing to Salesforce (not stored in Supabase).

Reference Account Ids live in sf_account_reference (seeded in supabase_db.seed_sf_account_reference_defaults).
Resolved worksite / Job Ids on rows come from scrape-time SF practice match + Supabase cache (see sf_job_supabase_resolve).
"""

# Hyperlink / Account lookups (see also sf_account_reference.reference_key)
SF_REFERENCE_KEY_PRIMARY = "primary_aspen_dental_management"
SF_REFERENCE_KEY_WORKSITE_DEFAULT = "worksite_location_default"

# Salesforce custom object field name for worksite hyperlink (documented for mappers)
SF_FIELD_WORKSITE_LOCATION = "Job_Worksite_Location_1__c"

# Push-time defaults for Job__c live in ``utils.sf_job_payload.SF_PUSH_STATIC_DEFAULTS``
# (DJC fields, Job_Patient_Ages__c, Job_Volume__c, etc.). See ``docs/salesforce_job_push_rules.md``.


def format_worksite_display_label(city: str, state: str) -> str:
    """Display text for worksite hyperlink: Aspen Dental - {City}, {State}."""
    c, s = (city or "").strip(), (state or "").strip()
    if not c and not s:
        return ""
    if c and s:
        return f"Aspen Dental - {c}, {s}"
    return f"Aspen Dental - {c or s}"

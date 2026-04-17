"""CANONICAL_JOB_C_PUSH_FIELD_NAMES stays aligned with job_row_to_salesforce_fields."""

from utils.sf_job_payload import (
    CANONICAL_JOB_C_PUSH_FIELD_NAMES,
    ROSTER_ONLY_FIELD,
    job_row_to_salesforce_fields,
    sanitize_types_of_cases_for_salesforce,
)


def test_full_row_payload_keys_are_canonical():
    row = {
        "job_id": "19614",
        "city": "Virginia Beach",
        "state": "VA",
        "address_line": "123 Main St",
        "practice_value": "3190 - Virginia Beach, VA",
        "status": "Open",
        "insight": "Test insight",
        "dates_needed": "M-F",
        "standard_schedule": "8-5",
        "types_of_cases": "General",
        "support_staff": "Hygienist",
        "provider_start_date": "01/15/26",
        "provider_end_date": "12/31/26",
        "avg_patients_per_day": "12",
        "roster_only": "false",
        "job_ranking": "A",
        "description_full_text": "$100 – $120 per hour\n\nSome description.",
        "point_of_contact": "Dr. Who",
    }
    out = job_row_to_salesforce_fields(row)
    assert set(out.keys()) <= CANONICAL_JOB_C_PUSH_FIELD_NAMES


def test_minimal_row_payload_keys_are_canonical():
    out = job_row_to_salesforce_fields({"job_id": "1", "city": "X", "state": "TX"})
    assert set(out.keys()) <= CANONICAL_JOB_C_PUSH_FIELD_NAMES


def test_job_dates_needed_uses_active_needs_clause():
    row = {
        "job_id": "1",
        "city": "X",
        "state": "TX",
        "dates_needed": "Monday only",
        "description_full_text": "4/8 update: note. Active needs are Fridays June 5, 12.",
    }
    out = job_row_to_salesforce_fields(row, use_canonical_description=False)
    assert out.get("Job_Dates_Needed__c") == "Fridays June 5, 12."


def test_volume_override_still_canonical():
    row = {"job_id": "1", "city": "X", "state": "TX", "avg_patients_per_day": "10"}
    out = job_row_to_salesforce_fields(row)
    assert out.get("Job_Volume__c") == "10"
    assert set(out.keys()) <= CANONICAL_JOB_C_PUSH_FIELD_NAMES


def test_minimal_row_omits_job_volume_no_not_provided_default():
    out = job_row_to_salesforce_fields({"job_id": "1", "city": "X", "state": "TX"})
    assert "Job_Volume__c" not in out


def test_volume_not_provided_placeholder_omitted():
    row = {"job_id": "1", "city": "X", "state": "TX", "avg_patients_per_day": "  NOT PROVIDED "}
    out = job_row_to_salesforce_fields(row)
    assert "Job_Volume__c" not in out


def test_roster_only_true_from_open_to_roster_in_description_when_column_false():
    row = {
        "job_id": "1",
        "city": "X",
        "state": "TX",
        "roster_only": "false",
        "description_full_text": "Open to roster. Pay Range: $100 – $120 per hour.\n",
    }
    out = job_row_to_salesforce_fields(row, use_canonical_description=False)
    assert out.get(ROSTER_ONLY_FIELD) == "true"


def test_trailing_commas_stripped_from_list_style_text_fields(monkeypatch):
    monkeypatch.setenv("PROXI_SF_OMIT_JOB_FIELDS", "")
    row = {
        "job_id": "1",
        "city": "X",
        "state": "TX",
        "support_staff": "3 DAs, 2 Hygienists,",
        "types_of_cases": "Restorative, surgical,",
        "point_of_contact": "Jane Doe,",
        "insight": "*Note here,",
    }
    out = job_row_to_salesforce_fields(row, use_canonical_description=False)
    assert out.get("Job_Support_Staff__c") == "3 DAs, 2 Hygienists"
    assert out.get("Job_Types_of_Cases__c") == "Restorative, surgical"
    assert out.get("Job_Point_of_Contact__c") == "Jane Doe"
    assert out.get("Insight__c") == "*Note here"


def test_sanitize_types_of_cases_splits_semicolon_and_strips_instruction():
    s = "Surgical extractions; please notate any limitations in presentation"
    assert sanitize_types_of_cases_for_salesforce(s) == "Surgical extractions"


def test_job_row_types_of_cases_strips_non_ascii_hyphen_before_instruction():
    row = {
        "job_id": "1",
        "city": "X",
        "state": "TX",
        "types_of_cases": "Surgical extractions\u2011 please notate any limitations in presentation",
    }
    out = job_row_to_salesforce_fields(row, use_canonical_description=False)
    val = (out.get("Job_Types_of_Cases__c") or "").lower()
    assert "notate" not in val
    assert "presentation" not in val
    assert "surgical extractions" in val


def test_job_row_types_of_cases_re_sanitized_after_trailing_comma_strip():
    row = {
        "job_id": "1",
        "city": "X",
        "state": "TX",
        "types_of_cases": "Restorative, please notate any limitations in presentation,",
    }
    out = job_row_to_salesforce_fields(row, use_canonical_description=False)
    val = (out.get("Job_Types_of_Cases__c") or "").lower()
    assert "notate" not in val
    assert "presentation" not in val
    assert "restorative" in val

"""CANONICAL_JOB_C_PUSH_FIELD_NAMES stays aligned with job_row_to_salesforce_fields."""

from utils.sf_job_payload import (
    CANONICAL_JOB_C_PUSH_FIELD_NAMES,
    job_row_to_salesforce_fields,
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

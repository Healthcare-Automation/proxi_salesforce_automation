"""Small helpers for Salesforce push mapping."""

from datetime import date, datetime

from utils.sf_job_payload import (
    EXTERNAL_JOB_LINK_MAX_LEN,
    KIMEDICS_PORTAL_JOB_POST_URL_PREFIX,
    _canonical_description_use_html,
    _truncate_external_job_link,
    build_salesforce_job_name,
    external_job_id_match_key,
    external_job_link_from_job_row,
    job_row_to_salesforce_fields,
    job_status_for_salesforce_push,
    most_recent_open_date_from_history,
)
from utils.sf_pay_range import DEFAULT_SALARY_PAY_RANGE, extract_pay_range_from_description
from utils.sf_push_defaults import (
    format_worksite_account_name,
    worksite_account_record_type_id,
)
from utils.us_state_expand import state_abbrev_for_job_title, state_name_for_salesforce


def test_canonical_description_html_true_when_env_unset(monkeypatch):
    monkeypatch.delenv("PROXI_JOB_DESCRIPTION_HTML", raising=False)
    assert _canonical_description_use_html() is True


def test_canonical_description_html_false_when_env_false(monkeypatch):
    monkeypatch.setenv("PROXI_JOB_DESCRIPTION_HTML", "false")
    assert _canonical_description_use_html() is False


def test_job_status_accepting_new_providers_maps_to_open():
    assert job_status_for_salesforce_push("Active, accepting new providers") == "Open"
    assert job_status_for_salesforce_push("Active, Accepting New Providers") == "Open"


def test_job_status_not_accepting_maps_to_closed():
    assert job_status_for_salesforce_push("Active, not accepting new providers") == "Closed"
    assert job_status_for_salesforce_push("Active, Not Accepting New Providers") == "Closed"


def test_job_status_non_accepting_phrases_map_to_closed():
    assert job_status_for_salesforce_push("ACTIVE — open") == "Closed"
    assert job_status_for_salesforce_push("Inactive") == "Closed"
    assert job_status_for_salesforce_push("Closed") == "Closed"


def test_job_status_literal_open_label_maps_to_open():
    assert job_status_for_salesforce_push("Open") == "Open"


def test_job_status_empty_is_none():
    assert job_status_for_salesforce_push("") is None


def test_standard_schedule_hours_matches_job_standard_schedule(monkeypatch):
    monkeypatch.setenv("PROXI_SF_OMIT_JOB_FIELDS", "")
    row = {
        "job_id": "19999",
        "city": "Testville",
        "state": "TX",
        "standard_schedule": "Mon–Thu 8a–6p; Fri 8a–1p",
        "description_full_text": "Pay Range: $125 – $145 per hour",
    }
    out = job_row_to_salesforce_fields(row, use_canonical_description=False)
    assert out["Standard_Schedule_Hours__c"] == row["standard_schedule"]
    assert out["Job_Standard_Schedule__c"] == row["standard_schedule"]


def test_state_name_for_salesforce_abbrev():
    assert state_name_for_salesforce("TX") == "Texas"
    assert state_name_for_salesforce("tx") == "Texas"


def test_state_name_for_salesforce_full_name_passthrough():
    assert state_name_for_salesforce("Texas") == "Texas"


def test_format_worksite_account_name():
    assert format_worksite_account_name("Dunkirk", "NY") == "Aspen Dental - Dunkirk, NY"
    assert format_worksite_account_name("Marshalltown", "Iowa") == "Aspen Dental - Marshalltown, IA"


def test_worksite_account_record_type_id_from_describe(monkeypatch):
    monkeypatch.delenv("PROXI_SF_ACCOUNT_WORKSITE_RECORD_TYPE_ID", raising=False)
    d = {
        "recordTypeInfos": [
            {"name": "Master", "recordTypeId": "012BAD", "available": True},
            {"name": "Worksite", "recordTypeId": "012GOOD", "available": True},
        ]
    }
    assert worksite_account_record_type_id(d) == "012GOOD"


def test_worksite_account_record_type_id_env_over_describe(monkeypatch):
    monkeypatch.setenv("PROXI_SF_ACCOUNT_WORKSITE_RECORD_TYPE_ID", "012ENV")
    d = {"recordTypeInfos": [{"name": "Worksite", "recordTypeId": "012GOOD", "available": True}]}
    assert worksite_account_record_type_id(d) == "012ENV"


def test_state_abbrev_for_job_title_from_code_or_name():
    assert state_abbrev_for_job_title("NY") == "NY"
    assert state_abbrev_for_job_title("ny") == "NY"
    assert state_abbrev_for_job_title("Virginia") == "VA"
    assert state_abbrev_for_job_title("") == ""


def test_salesforce_job_name_pattern():
    row = {
        "job_id": "1",
        "city": "Dunkirk",
        "state": "NY",
        "status": "Active, accepting new providers",
        "description_full_text": "x",
    }
    out = job_row_to_salesforce_fields(row, use_canonical_description=False)
    assert "Name" not in out
    assert (
        build_salesforce_job_name(row)
        == "NY (Dunkirk) General Dentistry - Aspen Dental Management Inc. - Open"
    )


def test_salesforce_job_name_uses_abbrev_when_state_is_full_name():
    row = {
        "job_id": "1",
        "city": "Virginia Beach",
        "state": "Virginia",
        "status": "Open",
        "description_full_text": "x",
    }
    out = job_row_to_salesforce_fields(row, use_canonical_description=False)
    assert "Name" not in out
    assert (
        build_salesforce_job_name(row)
        == "VA (Virginia Beach) General Dentistry - Aspen Dental Management Inc. - Open"
    )


def test_salesforce_job_name_heartland_posting_org():
    row = {
        "job_id": "1",
        "city": "Summerville",
        "state": "SC",
        "posting_org": "Heartland Dental",
        "status": "Open",
        "description_full_text": "x",
    }
    out = job_row_to_salesforce_fields(row, use_canonical_description=False)
    assert "Name" not in out
    assert build_salesforce_job_name(row) == "SC (Summerville) General Dentistry - Heartland Dental - Open"


def test_salesforce_job_name_midwest_posting_org():
    row = {
        "job_id": "1",
        "city": "Shelby",
        "state": "OH",
        "posting_org": "Midwest Dental",
        "status": "Closed",
        "description_full_text": "x",
    }
    out = job_row_to_salesforce_fields(row, use_canonical_description=False)
    assert "Name" not in out
    assert build_salesforce_job_name(row) == "OH (Shelby) General Dentistry - Midwest Dental - Closed"


def test_salesforce_job_name_city_from_practice_value_when_city_blank():
    row = {
        "job_id": "1",
        "city": "",
        "state": "SC",
        "practice_value": "1234 - Summerville, SC",
        "posting_org": "Heartland Dental",
        "status": "Open",
        "description_full_text": "x",
    }
    out = job_row_to_salesforce_fields(row, use_canonical_description=False)
    assert "Name" not in out
    nm = build_salesforce_job_name(row)
    assert "SC (Summerville)" in nm
    assert "Heartland Dental" in nm


def test_salesforce_job_name_no_empty_parens_when_city_unknown():
    row = {
        "job_id": "1",
        "city": "",
        "state": "TX",
        "practice_value": "",
        "location_line": "",
        "posting_org": "Heartland Dental",
        "status": "Closed",
        "description_full_text": "x",
    }
    out = job_row_to_salesforce_fields(row, use_canonical_description=False)
    assert "Name" not in out
    nm = build_salesforce_job_name(row)
    assert "()" not in nm
    assert nm == "TX General Dentistry - Heartland Dental - Closed"


def test_extract_pay_range():
    text = "Something something Pay Range: $125 – $145 per hour\nfooter"
    got = extract_pay_range_from_description(text)
    assert got is not None
    assert "125" in got and "145" in got


def test_extract_pay_range_missing_uses_default_constant():
    assert extract_pay_range_from_description("no money here") is None
    assert "125" in DEFAULT_SALARY_PAY_RANGE


def test_salary_pay_range_field_always_default_despite_kimedics_range_in_description():
    row = {
        "job_id": "1",
        "city": "X",
        "state": "TX",
        "description_full_text": "Pay Range: $200 – $300 per hour\n",
    }
    out = job_row_to_salesforce_fields(row, use_canonical_description=False)
    assert out.get("Salary_Pay_Range__c") == DEFAULT_SALARY_PAY_RANGE
    assert "$200" not in (out.get("Salary_Pay_Range__c") or "")


def test_external_job_id_match_key_case_insensitive():
    assert external_job_id_match_key("19448") == "19448"
    assert external_job_id_match_key("AbC") == "abc"


def test_truncate_external_job_link_under_limit():
    u = "https://kimedics.example/job/123"
    assert _truncate_external_job_link(u) == u


def test_truncate_external_job_link_over_255():
    long_u = "https://x.com/" + "a" * 400
    got = _truncate_external_job_link(long_u)
    assert got is not None
    assert len(got) == EXTERNAL_JOB_LINK_MAX_LEN


def test_external_job_link_numeric_job_id_portal_url():
    want = f"{KIMEDICS_PORTAL_JOB_POST_URL_PREFIX}19448"
    assert external_job_link_from_job_row({"job_id": "19448"}) == want
    assert external_job_link_from_job_row({"job_id": 19448}) == want


def test_external_job_link_non_numeric_falls_back_to_view_job_link():
    long_tracker = "https://x.com/" + "a" * 400
    row = {"job_id": "TESTABC", "view_job_link": long_tracker}
    got = external_job_link_from_job_row(row)
    assert got is not None
    assert len(got) == EXTERNAL_JOB_LINK_MAX_LEN


def test_build_salesforce_job_name_uses_sf_location_fallback():
    """When Supabase row has no city/state, merge Job_City__c / Job_State__c from Salesforce GET."""
    row = {
        "city": "",
        "state": "",
        "status": "open",
        "posting_org": "Shiftwise - Aspen Dental - AMN",
    }
    fb = {"Job_City__c": "Gloucester", "Job_State__c": "Virginia"}
    got = build_salesforce_job_name(row, job_name_location_fallback=fb)
    assert got == (
        "VA (Gloucester) General Dentistry - Aspen Dental Management Inc. - Open"
    )


def test_build_salesforce_job_name_practice_line_city_only_numeric_prefix():
    """Kimedics ``4190 - Gloucester`` (no comma) still yields a city for the Job Name."""
    row = {
        "practice_value": "4190 - Gloucester",
        "city": "",
        "state": "VA",
        "status": "open",
        "posting_org": "Shiftwise - Aspen Dental - AMN",
    }
    got = build_salesforce_job_name(row)
    assert got == (
        "VA (Gloucester) General Dentistry - Aspen Dental Management Inc. - Open"
    )


def test_build_salesforce_job_name_strips_bare_empty_parens_prefix():
    row = {
        "city": "",
        "state": "",
        "status": "open",
        "posting_org": "Aspen",
    }
    # Malformed fallback: empty strings should not yield "() ..." after merge.
    got = build_salesforce_job_name(
        row,
        job_name_location_fallback={"Job_City__c": "", "Job_State__c": " "},
    )
    assert got == "General Dentistry - Aspen Dental Management Inc. - Open"
    assert not got.startswith("()")


# ── Job_Open_Date__c: latest transition into Open ────────────────────────────
# Rows are (email_date, status, posted_date) ordered NEWEST first, matching the
# SQL in get_most_recent_open_date.
OPEN = "Active, accepting new providers"
NOT_ACCEPTING = "Active, not accepting new providers"


def test_open_date_is_start_of_latest_open_run_not_its_end():
    """Real history of Kimedics job 20084 — must be 07-08, not 07-17 or 07-10."""
    rows = [
        (date(2026, 7, 27), "Closed", "07/06/26"),
        (date(2026, 7, 17), NOT_ACCEPTING, "07/06/26"),
        (date(2026, 7, 17), NOT_ACCEPTING, "07/06/26"),
        (date(2026, 7, 10), OPEN, "07/06/26"),
        (date(2026, 7, 9), OPEN, "07/06/26"),
        (date(2026, 7, 8), OPEN, "07/06/26"),
        (date(2026, 7, 8), OPEN, "07/06/26"),
        (date(2026, 7, 7), NOT_ACCEPTING, "07/06/26"),
        (date(2026, 7, 6), OPEN, "07/06/26"),
    ]
    assert most_recent_open_date_from_history(rows) == "2026-07-08"


def test_open_date_ignores_not_accepting_rows():
    """"not accepting" contains "accepting" — it must never stamp the open date."""
    rows = [
        (date(2026, 7, 17), NOT_ACCEPTING, "07/06/26"),
        (date(2026, 7, 6), OPEN, "07/06/26"),
    ]
    assert most_recent_open_date_from_history(rows) == "2026-07-06"


def test_open_date_uses_latest_run_after_a_reopen():
    rows = [
        (date(2026, 6, 20), OPEN, "05/01/26"),
        (date(2026, 6, 19), OPEN, "05/01/26"),
        (date(2026, 6, 10), "Closed", "05/01/26"),
        (date(2026, 5, 2), OPEN, "05/01/26"),
    ]
    assert most_recent_open_date_from_history(rows) == "2026-06-19"


def test_open_date_single_open_row():
    rows = [(date(2026, 7, 1), OPEN, "07/01/26")]
    assert most_recent_open_date_from_history(rows) == "2026-07-01"


def test_open_date_falls_back_to_posted_date_when_never_open():
    rows = [
        (date(2026, 7, 5), "Closed", "07/02/26"),
        (date(2026, 7, 3), NOT_ACCEPTING, "07/02/26"),
    ]
    assert most_recent_open_date_from_history(rows) == "2026-07-02"


def test_open_date_none_when_never_open_and_no_posted_date():
    assert most_recent_open_date_from_history([(date(2026, 7, 5), "Closed", None)]) is None
    assert most_recent_open_date_from_history([]) is None


def test_open_date_accepts_datetime_rows():
    rows = [(datetime(2026, 7, 8, 14, 16, 41), OPEN, "07/06/26")]
    assert most_recent_open_date_from_history(rows) == "2026-07-08"

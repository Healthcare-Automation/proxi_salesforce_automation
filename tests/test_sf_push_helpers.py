"""Small helpers for Salesforce push mapping."""

from utils.sf_job_payload import (
    EXTERNAL_JOB_LINK_MAX_LEN,
    KIMEDICS_PORTAL_JOB_POST_URL_PREFIX,
    _canonical_description_use_html,
    _truncate_external_job_link,
    external_job_id_match_key,
    external_job_link_from_job_row,
    job_row_to_salesforce_fields,
    job_status_for_salesforce_push,
)
from utils.sf_pay_range import DEFAULT_SALARY_PAY_RANGE, extract_pay_range_from_description
from utils.us_state_expand import state_name_for_salesforce


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


def test_standard_schedule_hours_matches_job_standard_schedule():
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


def test_extract_pay_range():
    text = "Something something Pay Range: $125 – $145 per hour\nfooter"
    got = extract_pay_range_from_description(text)
    assert got is not None
    assert "125" in got and "145" in got


def test_extract_pay_range_missing_uses_default_constant():
    assert extract_pay_range_from_description("no money here") is None
    assert "125" in DEFAULT_SALARY_PAY_RANGE


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

"""Small helpers for Salesforce push mapping."""

from utils.sf_job_payload import (
    EXTERNAL_JOB_LINK_MAX_LEN,
    KIMEDICS_PORTAL_JOB_POST_URL_PREFIX,
    _truncate_external_job_link,
    external_job_id_match_key,
    external_job_link_from_job_row,
)
from utils.sf_pay_range import DEFAULT_SALARY_PAY_RANGE, extract_pay_range_from_description
from utils.us_state_expand import state_name_for_salesforce


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

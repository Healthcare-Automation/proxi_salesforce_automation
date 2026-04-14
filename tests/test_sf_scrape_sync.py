from utils.sf_scrape_sync import (
    posted_date_to_salesforce_date,
    desired_scrape_sync_fields_from_job_row,
    _scrape_sync_audit_field_names,
    SF_FIELD_TEST_POSTED_DATE,
    SF_FIELD_TEST_STATUS,
)


def test_posted_date_iso_and_mdy():
    assert posted_date_to_salesforce_date("2025-01-02") == "2025-01-02"
    assert posted_date_to_salesforce_date("01/15/25") == "2025-01-15"


def test_desired_scrape_sync_fields():
    row = {
        "job_id": "19448",
        "status": "Open",
        "posted_date": "01/15/25",
        "view_job_link": "https://example.com/x",
    }
    d = desired_scrape_sync_fields_from_job_row(row)
    assert d["External_Job_ID__c"] == "19448"
    assert "portal.kimedics.com" in (d.get("External_Job_Link__c") or "")
    assert d["Job_Ranking__c"] == "B"
    assert d[SF_FIELD_TEST_STATUS] == "Open"
    assert d[SF_FIELD_TEST_POSTED_DATE] == "2025-01-15"


def test_scrape_sync_audit_includes_job_source_and_role_picklists():
    """Hub diffs use the canonical push set (not only non-empty PATCH body keys)."""
    base = _scrape_sync_audit_field_names(test_mode=False)
    assert "Job_Job_Source__c" in base
    assert "Job_Specialty__c" in base
    assert "Job_Position_Type__c" in base
    assert "Occupation_DJC__c" in base
    with_test = _scrape_sync_audit_field_names(test_mode=True)
    assert SF_FIELD_TEST_STATUS in with_test
    assert SF_FIELD_TEST_POSTED_DATE in with_test


def test_desired_scrape_sync_fields_job_ranking_from_row():
    row = {
        "job_id": "1",
        "view_job_link": "https://example.com/x",
        "job_ranking": "A",
    }
    d = desired_scrape_sync_fields_from_job_row(row)
    assert d["Job_Ranking__c"] == "A"

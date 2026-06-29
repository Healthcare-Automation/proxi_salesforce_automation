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


def test_field_sync_isolates_and_logs_per_job_error(monkeypatch):
    """#20046 regression: one job's field-sync crash must NOT abort the batch, and it
    MUST log sf_scrape_fields_error so the recovery loop sees it (never a silent success)."""
    import utils.sf_scrape_sync as s
    import utils.supabase_db as db
    calls, logged = [], []

    def fake_sync(row, **k):
        jid = k["job_id_for_log"]; calls.append(jid)
        if jid == "BAD":
            raise RuntimeError("transient SF 503")
        return {"patched": True}

    monkeypatch.setattr(s, "sync_missing_scrape_fields_to_salesforce", fake_sync)
    monkeypatch.setattr(s, "proxi_sf_writes_enabled", lambda: True)
    monkeypatch.setattr(db, "get_job_current",
                        lambda conn, job_ids, **k: [{"job_id": j, "sf_job_id": "sf_" + j} for j in job_ids])
    monkeypatch.setattr(db, "log_job_event", lambda conn, **k: logged.append((k["job_id"], k["event_type"])))

    att, patched = s.sync_missing_scrape_fields_for_job_ids(object(), ["BAD", "GOOD"], schema="public")
    assert att == 2 and patched == 1            # both attempted, the healthy one patched
    assert set(calls) == {"BAD", "GOOD"}        # BAD throwing did NOT abort GOOD
    assert ("BAD", "sf_scrape_fields_error") in logged  # the failure is logged, not swallowed

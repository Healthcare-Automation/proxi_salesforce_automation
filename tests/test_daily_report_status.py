"""
#20046 regression: a job mapped to Salesforce but whose fields were never written
(no field-sync resolution) must surface as a FAILURE in the daily report — never a
silent success. Asserts the NEEDS ATTENTION badge, the per-row 'not synced' status,
the 'Mapped, Not Synced' metric, the subject flag, and the plain-text 'NOT SYNCED'.
"""
import utils.alert_email as ae


def _row(jid, *, mapped, synced):
    return {
        "job_post_id": jid, "view_job_link": "http://k", "job_title": f"#{jid}",
        "posting_org": "Aspen Dental", "sf_job_id": "a01" if mapped else "",
        "et_time": "12:00 PM ET", "scrape_ok": True, "sf_mapped": mapped, "stuck": False,
        "created_sf_job": False, "created_sf_worksite": False, "ext_id_swap": False,
        "manual_rescraped": False, "auto_retried": False,
        "fields_changed": 2 if synced else 0, "fields_quarantined": 0, "push_recovered": 0,
        "push_errors": 0, "push_error_unresolved": False, "blocked_no_practice": 0,
        "silent_failures": 0, "field_sync_resolved": synced,
        "subject": "new", "action_or_change": "new",
    }


def test_daily_report_marks_mapped_but_unsynced_as_failure(monkeypatch):
    cap = {}
    monkeypatch.setattr(ae, "_send", lambda s, h, t="", recipients=None: cap.update(html=h, text=t, subj=s) or True)
    stats = {
        "period_label": "Jun 29, 2026", "emails_received": 2, "scraped_ok": 2, "sf_mapped": 2,
        "field_patches_total": 2, "mapped_not_synced": 1,
        "rows": [_row("20010", mapped=True, synced=True), _row("20046", mapped=True, synced=False)],
    }
    ae.send_daily_summary(stats)

    assert "NEEDS ATTENTION" in cap["html"]           # run is flagged, not healthy
    assert "not synced" in cap["html"]                # the 20046 row reads "not synced"
    assert "Mapped, Not Synced" in cap["html"]        # the metric card exists
    assert "mapped, not synced" in cap["subj"]        # surfaced in the subject for inbox triage
    assert "NOT SYNCED" in cap["text"]                # plain-text fallback too


def test_daily_report_fully_synced_job_is_clean(monkeypatch):
    cap = {}
    monkeypatch.setattr(ae, "_send", lambda s, h, t="", recipients=None: cap.update(html=h, subj=s) or True)
    stats = {
        "period_label": "Jun 29, 2026", "emails_received": 1, "scraped_ok": 1, "sf_mapped": 1,
        "field_patches_total": 2, "mapped_not_synced": 0,
        "rows": [_row("20010", mapped=True, synced=True)],
    }
    ae.send_daily_summary(stats)
    assert "not synced" not in cap["html"]            # a properly-synced job is not flagged
    assert "NEEDS ATTENTION" not in cap["html"]

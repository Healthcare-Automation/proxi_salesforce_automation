"""
#20046 regression: the daily report records what WENT WRONG during the period, not just
the current state. A job mapped but never synced is a FAILED (broken) row; a job that
synced LATE (didn't sync promptly but recovered) is still a failure that happened and
must be shown (recovered) — never hidden behind a clean success.
"""
import utils.alert_email as ae


def _row(jid, *, mapped, synced, prompt=None):
    # prompt defaults to `synced` — a normally-synced job synced promptly.
    prompt = synced if prompt is None else prompt
    return {
        "job_post_id": jid, "view_job_link": "http://k", "job_title": f"#{jid}",
        "posting_org": "Aspen Dental", "sf_job_id": "a01" if mapped else "",
        "et_time": "12:00 PM ET", "scrape_ok": True, "sf_mapped": mapped, "stuck": False,
        "created_sf_job": False, "created_sf_worksite": False, "ext_id_swap": False,
        "manual_rescraped": False, "auto_retried": False,
        "fields_changed": 2 if synced else 0, "fields_quarantined": 0, "push_recovered": 0,
        "push_errors": 0, "push_error_unresolved": False, "blocked_no_practice": 0,
        "silent_failures": 0, "field_sync_resolved": synced, "field_sync_prompt": prompt,
        "subject": "new", "action_or_change": "new",
    }


def _send(monkeypatch):
    cap = {}
    monkeypatch.setattr(ae, "_send", lambda s, h, t="", recipients=None: cap.update(html=h, text=t, subj=s) or True)
    return cap


def test_still_broken_sync_is_critical(monkeypatch):
    cap = _send(monkeypatch)
    ae.send_daily_summary({
        "period_label": "Jun 29, 2026", "emails_received": 2, "scraped_ok": 2, "sf_mapped": 2,
        "field_patches_total": 2, "mapped_not_synced": 1, "sync_failed_recovered": 0,
        "rows": [_row("20010", mapped=True, synced=True), _row("20046", mapped=True, synced=False)],
    })
    assert "NEEDS ATTENTION" in cap["html"]
    assert "not synced" in cap["html"]
    assert "Sync Failed (broken)" in cap["html"]
    assert "NOT SYNCED" in cap["text"]
    assert "auto-recovering" in cap["html"]   # row shows it is being fixed


def test_recovered_late_sync_is_shown(monkeypatch):
    cap = _send(monkeypatch)
    ae.send_daily_summary({
        "period_label": "Jun 29, 2026", "emails_received": 1, "scraped_ok": 1, "sf_mapped": 1,
        "field_patches_total": 2, "mapped_not_synced": 0, "sync_failed_recovered": 1,
        "rows": [_row("20046", mapped=True, synced=True, prompt=False)],  # synced, but late
    })
    assert "synced late" in cap["html"]                  # the failure that happened is shown
    assert "Sync Failed (recovered)" in cap["html"]
    assert "REVIEW AMENDMENTS" in cap["html"]            # surfaced, but not critical
    assert "SYNCED LATE" in cap["text"]
    assert "amended later" in cap["html"]    # the recovery is named in Notes


def test_promptly_synced_job_is_clean(monkeypatch):
    cap = _send(monkeypatch)
    ae.send_daily_summary({
        "period_label": "Jun 29, 2026", "emails_received": 1, "scraped_ok": 1, "sf_mapped": 1,
        "field_patches_total": 2, "mapped_not_synced": 0, "sync_failed_recovered": 0,
        "rows": [_row("20010", mapped=True, synced=True)],  # prompt sync
    })
    assert "not synced" not in cap["html"]
    assert "synced late" not in cap["html"]
    assert "NEEDS ATTENTION" not in cap["html"]

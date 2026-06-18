"""
Date alert dispatch — coverage dates must never be missing client-side:
  - dates resolved with < 100% confidence → "review" alert
  - no dates captured on a still-open job → "missing" alert
  - closed jobs / jobs with dates → no alert
"""
import utils.pipeline_link_scrape as pls


def test_dispatch_low_confidence(monkeypatch):
    calls = {}
    monkeypatch.setattr(pls, "_alert_low_confidence_dates", lambda *a, **k: calls.__setitem__("lc", True))
    monkeypatch.setattr(pls, "_alert_missing_dates", lambda *a, **k: calls.__setitem__("missing", True))
    cl = {"dates_needed": "July 1-2", "dates_confidence": 60, "status": "Active"}
    pls._review_job_dates(None, "1", cl, schema="public", run_id=1)
    assert calls.get("lc") and not calls.get("missing")


def test_dispatch_missing_when_open_and_empty(monkeypatch):
    calls = {}
    monkeypatch.setattr(pls, "_alert_low_confidence_dates", lambda *a, **k: calls.__setitem__("lc", True))
    monkeypatch.setattr(pls, "_alert_missing_dates", lambda *a, **k: calls.__setitem__("missing", True))
    cl = {"dates_needed": "", "status": "Active, accepting new providers"}
    pls._review_job_dates(None, "1", cl, schema="public", run_id=1)
    assert calls.get("missing") and not calls.get("lc")


def test_no_alert_when_closed(monkeypatch):
    calls = {}
    monkeypatch.setattr(pls, "_alert_missing_dates", lambda *a, **k: calls.__setitem__("missing", True))
    cl = {"dates_needed": "", "status": "Closed"}
    pls._review_job_dates(None, "1", cl, schema="public", run_id=1)
    assert not calls


def test_no_alert_when_dates_present(monkeypatch):
    calls = {}
    monkeypatch.setattr(pls, "_alert_missing_dates", lambda *a, **k: calls.__setitem__("missing", True))
    cl = {"dates_needed": "July 1-2", "status": "Active"}
    pls._review_job_dates(None, "1", cl, schema="public", run_id=1)
    assert not calls


def test_missing_alert_fires_with_hint(monkeypatch):
    sent = {}

    def fake(job_id, **k):
        sent.update(job_id=job_id, kind=k.get("kind"), structured=k.get("structured"), resolved=k.get("resolved"))
        return True

    monkeypatch.setattr("utils.alert_email.send_dates_review_alert", fake)
    cl = {"dates_needed": "", "status": "Active",
          "description_full_text": "Facility: X\nOther job post notes: April 7\nHours: 7am-5pm"}
    pls._alert_missing_dates(None, "19586", cl, schema="public", run_id=1)
    assert sent["kind"] == "missing"
    assert sent["resolved"] == ""
    assert "April 7" in sent["structured"]  # surfaces the likely-date line as a hint

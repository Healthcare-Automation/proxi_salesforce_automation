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


def test_description_top_returns_first_lines():
    from utils.job_description_ai import description_top
    txt = "\n".join(f"line {i}" for i in range(1, 13))
    assert description_top(txt, n=8).splitlines() == [f"line {i}" for i in range(1, 9)]


def test_low_confidence_alert_passes_top_text(monkeypatch):
    """The review alert must carry the exact top-of-post text the AI read (Sean's ask)."""
    sent = {}

    def fake(job_id, **k):
        sent.update(resolved=k.get("resolved"), top_text=k.get("top_text"))
        return True

    monkeypatch.setattr("utils.alert_email.send_dates_review_alert", fake)
    cl = {
        "dates_needed": "June 25-26", "dates_confidence": 90, "dates_reason": "tentative vs open",
        "dates_structured": "June 22-24",
        "description_full_text": "6/19 pending confirmation for June 22-24, open to submittals for June 25-26\n"
                                 "Facility: X\nDates: June 22-24\nHours: 8a-5p",
    }
    pls._alert_low_confidence_dates(None, "20002", cl, schema="public", run_id=1)
    assert sent["resolved"] == "June 25-26"
    assert "pending confirmation for June 22-24" in sent["top_text"]


def test_review_alert_renders_top_of_post(monkeypatch):
    import utils.alert_email as ae
    cap = {}
    monkeypatch.setattr(ae, "_send", lambda s, h, t, recipients=None: cap.update(html=h, text=t) or True)
    ae.send_dates_review_alert(
        "20002", kind="review", structured="June 22-24", resolved="June 25-26",
        confidence=90, reason="r",
        top_text="6/19 pending confirmation for June 22-24, open to submittals for June 25-26",
    )
    assert "Top of post" in cap["html"] and "pending confirmation for June 22-24" in cap["html"]
    assert "June 25-26" in cap["html"]  # corrected captured dates
    assert "Top of post" in cap["text"]

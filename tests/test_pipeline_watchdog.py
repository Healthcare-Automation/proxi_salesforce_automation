"""
Watchdog behavior: escalation rules for alert dedup, and the alert email itself
(the thing that guarantees "we must catch these early" — July 2 produced zero
alerts because everything alert-capable died with the killed task).
"""
import utils.alert_email as ae
from utils.pipeline_watchdog import _escalated


def test_escalation_rules():
    prior = {"orphan_count": 2, "unsynced_count": 1, "cron_dead": False}
    # No growth → suppressed within cooldown.
    assert not _escalated(prior, orphans=2, unsynced=1, cron_dead=False)
    assert not _escalated(prior, orphans=3, unsynced=2, cron_dead=False)
    # Cron newly dead → always escalate.
    assert _escalated(prior, orphans=0, unsynced=0, cron_dead=True)
    # Backlog grew by 3+ → escalate.
    assert _escalated(prior, orphans=5, unsynced=1, cron_dead=False)
    assert _escalated(prior, orphans=2, unsynced=4, cron_dead=False)
    # Cron already dead in prior alert → not an escalation by itself.
    assert not _escalated({**prior, "cron_dead": True}, orphans=2, unsynced=1, cron_dead=True)


def _capture_send(monkeypatch):
    cap = {}
    monkeypatch.setattr(
        ae, "_send",
        lambda s, h, t="", recipients=None: cap.update(subj=s, html=h, text=t) or True,
    )
    return cap


def test_watchdog_alert_lists_stuck_work(monkeypatch):
    cap = _capture_send(monkeypatch)
    ok = ae.send_pipeline_watchdog_alert(
        orphans=[{"job_id": "20066", "age_min": 92, "attempts": 4}],
        unsynced=[{"job_id": "20046", "age_min": 61}],
        cron_dead=False,
    )
    assert ok
    assert "watchdog" in cap["subj"].lower()
    assert "20066" in cap["html"] and "20046" in cap["html"]
    assert "4 auto-retries" in cap["html"]
    assert "NOT SCRAPED" in cap["text"] and "NOT SYNCED" in cap["text"]
    # Healthy cron → no dead-cron section.
    assert "cron down" not in cap["html"].lower()


def test_watchdog_alert_dead_cron(monkeypatch):
    cap = _capture_send(monkeypatch)
    ok = ae.send_pipeline_watchdog_alert(
        orphans=[], unsynced=[], cron_dead=True, beat_age_minutes=53,
    )
    assert ok
    assert "NOT running" in cap["subj"]
    assert "53 min" in cap["html"]
    assert "CRON DOWN" in cap["text"]


def test_escalation_inbox_locked():
    prior = {"orphan_count": 0, "unsynced_count": 0, "cron_dead": False}
    # Newly locked inbox → escalate; already-known lockout → suppressed.
    assert _escalated(prior, orphans=0, unsynced=0, cron_dead=False, inbox_locked=True)
    assert not _escalated(
        {**prior, "inbox_locked": True}, orphans=0, unsynced=0, cron_dead=False, inbox_locked=True
    )


def test_watchdog_alert_inbox_locked(monkeypatch):
    cap = _capture_send(monkeypatch)
    ok = ae.send_pipeline_watchdog_alert(
        orphans=[], unsynced=[], cron_dead=False,
        inbox_locked=True, fetch_age_minutes=112,
    )
    assert ok
    assert "unreadable" in cap["subj"].lower()
    assert "112 min" in cap["html"]
    assert "apppasswords" in cap["html"]
    assert "MAILBOX UNREADABLE" in cap["text"]
    assert "cron down" not in cap["html"].lower()

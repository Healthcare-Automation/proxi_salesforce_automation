"""Email-as-status-truth: Kimedics emails carry authoritative status signals.

Regression for job 20311 (2026-08-18): the "New job post" email arrived the minute
the job opened and status-update emails matched the Kimedics change log exactly,
but the pipeline relied only on the scraped page field.
"""
from datetime import datetime, timedelta, timezone

from utils.email_status_signal import (
    status_from_action_or_change,
    resolve_status_with_email,
)

NOW = datetime(2026, 8, 18, 17, 5, 0, tzinfo=timezone.utc)


def _dt(minutes_before_now: int) -> datetime:
    return NOW - timedelta(minutes=minutes_before_now)


def test_status_from_action_or_change():
    assert status_from_action_or_change("new") == "Active, accepting new providers"
    assert (
        status_from_action_or_change("status: Active, not accepting new providers")
        == "Active, not accepting new providers"
    )
    assert status_from_action_or_change("status: Closed") == "Closed"
    # Historical truncated rows ("status: Active") are ambiguous — never use them.
    assert status_from_action_or_change("status: Active") is None
    assert status_from_action_or_change("updated") is None
    assert status_from_action_or_change("") is None


def test_fill_when_page_status_missing():
    d = resolve_status_with_email(
        page_status="",
        action_or_change="new",
        email_date=_dt(5),
        newest_status_email_date=_dt(5),
        now=NOW,
    )
    assert d.status == "Active, accepting new providers"
    assert d.filled and not d.mismatch


def test_stale_email_never_fills_over_newer_signal():
    # Retry of the morning "new" email after a newer "Closed" status email exists.
    d = resolve_status_with_email(
        page_status="",
        action_or_change="new",
        email_date=_dt(300),
        newest_status_email_date=_dt(10),
        now=NOW,
    )
    assert d.status == "" and not d.filled


def test_fresh_explicit_status_email_overrides_page():
    d = resolve_status_with_email(
        page_status="Active, accepting new providers",
        action_or_change="status: Closed",
        email_date=_dt(4),
        newest_status_email_date=_dt(4),
        now=NOW,
    )
    assert d.status == "Closed"
    assert d.mismatch and d.overrode_page


def test_stale_explicit_status_email_does_not_override_live_page():
    # Retry hours later: the live page is fresher than the old email.
    d = resolve_status_with_email(
        page_status="Closed",
        action_or_change="status: Active, accepting new providers",
        email_date=_dt(240),
        newest_status_email_date=_dt(240),
        now=NOW,
    )
    assert d.status == "Closed"
    assert d.mismatch and not d.overrode_page


def test_new_email_never_overrides_valid_page_status():
    d = resolve_status_with_email(
        page_status="Active, not accepting new providers",
        action_or_change="new",
        email_date=_dt(2),
        newest_status_email_date=_dt(2),
        now=NOW,
    )
    assert d.status == "Active, not accepting new providers"
    assert not d.filled and not d.mismatch


def test_agreement_is_a_no_op():
    d = resolve_status_with_email(
        page_status="Closed",
        action_or_change="status: Closed",
        email_date=_dt(3),
        newest_status_email_date=_dt(3),
        now=NOW,
    )
    assert d.status == "Closed"
    assert not d.filled and not d.mismatch


def test_unknown_page_status_treated_as_missing():
    d = resolve_status_with_email(
        page_status="Some weird label",
        action_or_change="status: Closed",
        email_date=_dt(3),
        newest_status_email_date=_dt(3),
        now=NOW,
    )
    assert d.status == "Closed"
    assert d.filled

"""
July 7 regression: the IMAP FROM-only search matched every Kimedics email ever
(1,800+), and downloading them all each cron outgrew the 600s Modal timeout —
killing runs before the heartbeat. The search must be bounded server-side.
"""
from datetime import datetime, timezone

from utils.gmail import build_sender_search_criteria


def test_bounded_search_includes_since():
    cutoff = datetime(2026, 7, 6, 17, 30, tzinfo=timezone.utc)
    crit = build_sender_search_criteria("donotreply@kimedics.com", cutoff)
    # One-day pad: cutoff Jul 6 → SINCE 05-Jul-2026 (day-granular internal date).
    assert crit == '(FROM "donotreply@kimedics.com" SINCE "05-Jul-2026")'


def test_month_names_are_protocol_english_not_locale():
    cutoff = datetime(2026, 1, 1, tzinfo=timezone.utc)  # pad crosses year boundary
    crit = build_sender_search_criteria("x@y.com", cutoff)
    assert 'SINCE "31-Dec-2025"' in crit


def test_no_cutoff_keeps_plain_from_search():
    assert build_sender_search_criteria("x@y.com", None) == 'FROM "x@y.com"'

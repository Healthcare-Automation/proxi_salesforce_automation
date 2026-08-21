"""Kimedics emails as the status source of truth.

Job 20311 (2026-08-18) proved the emails mirror the Kimedics change log to the
minute while the scraped page field can be missing on partial loads. Rules:

- ``status: <label>`` emails carry an explicit, timestamped status. A FRESH one
  (first processing, within ``FRESH_WINDOW``) beats the page; a stale one (an
  auto-retry re-processing an old email hours later) never beats the live page —
  it only logs a mismatch.
- ``new`` (first-notify) emails imply "Active, accepting new providers" but only
  FILL a missing/unrecognized page status, never override a real one.
- No email status is ever applied unless that email is the newest status-bearing
  email for the job — a stale retry must not roll status back over a newer signal.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

from utils.scrape_validator import KNOWN_STATUSES

FRESH_WINDOW = timedelta(minutes=30)

NEW_POST_STATUS = "Active, accepting new providers"

_STATUS_PREFIX = re.compile(r"^status:\s*(.+)$", re.I)


def status_from_action_or_change(action_or_change: Optional[str]) -> Optional[str]:
    """Authoritative status carried by an email, or None.

    Only exact Kimedics labels are trusted — historical truncated rows like
    ``status: Active`` (pre-2026-08-19 parser) are ambiguous and ignored.
    """
    s = (action_or_change or "").strip()
    if not s:
        return None
    if s.lower() == "new":
        return NEW_POST_STATUS
    m = _STATUS_PREFIX.match(s)
    if m:
        label = m.group(1).strip().rstrip(".")
        if label in KNOWN_STATUSES:
            return label
    return None


@dataclass
class StatusDecision:
    status: str          # the value the pipeline should persist
    filled: bool = False        # page status was missing/unknown; email supplied it
    mismatch: bool = False      # page and an explicit status email disagreed
    overrode_page: bool = False  # the email won a mismatch (fresh explicit only)
    email_status: Optional[str] = None
    page_status: str = ""


def resolve_status_with_email(
    *,
    page_status: Optional[str],
    action_or_change: Optional[str],
    email_date: Optional[datetime],
    newest_status_email_date: Optional[datetime],
    now: Optional[datetime] = None,
) -> StatusDecision:
    """Combine the scraped page status with the triggering email's signal."""
    page = (page_status or "").strip()
    page_known = page in KNOWN_STATUSES
    email_status = status_from_action_or_change(action_or_change)
    d = StatusDecision(status=page if page_known else "", page_status=page, email_status=email_status)
    if page and not page_known:
        # Unrecognized label: keep it only if no email signal replaces it below.
        d.status = page

    if email_status is None or email_date is None:
        return d

    # A stale retry must never apply an older email's status over a newer signal.
    if newest_status_email_date is not None and email_date < newest_status_email_date:
        return d

    explicit = not (action_or_change or "").strip().lower() == "new"
    now = now or datetime.now(timezone.utc)

    if not page_known:
        d.status, d.filled = email_status, True
        return d

    if explicit and email_status != page:
        d.mismatch = True
        if (now - email_date) <= FRESH_WINDOW:
            # Kimedics just told us the status — the email wins.
            d.status, d.overrode_page = email_status, True
    return d

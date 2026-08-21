"""
Gmail helpers: decode subject/body and scrape emails from a sender.
Used by both the local scrape_gmail.py and the Modal job.

Fetch transport is Gmail API (OAuth, no connection cap) when the three
GMAIL_OAUTH_* env vars are set, with automatic fallback to IMAP — so removing
those env vars (or any API failure) reverts to the original IMAP behavior.
"""

import imaplib
import os
import re
import time
from datetime import datetime, timezone, timedelta
from email import message_from_bytes
from email.header import decode_header
from email.utils import parsedate_to_datetime
from typing import Optional

IMAP_SERVER = "imap.gmail.com"


def decode_subject(header_value):
    """Decode Subject header (handles encoded words)."""
    if not header_value:
        return ""
    parts = decode_header(header_value)
    result = []
    for part, encoding in parts:
        if isinstance(part, bytes):
            result.append(part.decode(encoding or "utf-8", errors="replace"))
        else:
            result.append(part or "")
    return "".join(result).strip()


def html_to_plain(html):
    """Strip HTML tags and collapse whitespace."""
    if not html or not html.strip():
        return ""
    text = re.sub(r"<[^>]+>", " ", html)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def get_body_from_message(msg):
    """Extract plain text body from email.message: prefer text/plain, else text/html stripped."""
    html_content = ""
    text_content = ""
    if msg.is_multipart():
        for part in msg.walk():
            content_type = part.get_content_type()
            if content_type == "text/html":
                try:
                    raw = part.get_payload(decode=True)
                    html_content = (raw or b"").decode("utf-8", errors="ignore")
                except Exception:
                    pass
            elif content_type == "text/plain":
                try:
                    raw = part.get_payload(decode=True)
                    text_content = (raw or b"").decode("utf-8", errors="ignore")
                except Exception:
                    pass
    else:
        ct = msg.get_content_type()
        try:
            raw = msg.get_payload(decode=True)
            decoded = (raw or b"").decode("utf-8", errors="ignore")
            if ct == "text/html":
                html_content = decoded
            else:
                text_content = decoded
        except Exception:
            pass
    if text_content and text_content.strip():
        return text_content.strip()
    if html_content:
        return html_to_plain(html_content)
    return ""


def get_html_from_message(msg):
    """Extract raw HTML body from email.message (for link extraction). Returns "" if no HTML part."""
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/html":
                try:
                    raw = part.get_payload(decode=True)
                    return (raw or b"").decode("utf-8", errors="ignore")
                except Exception:
                    pass
        return ""
    if msg.get_content_type() == "text/html":
        try:
            raw = msg.get_payload(decode=True)
            return (raw or b"").decode("utf-8", errors="ignore")
        except Exception:
            pass
    return ""


# Patterns for Kimedics job notification emails (donotreply@kimedics.com)
# Match "job post #", "job post: #", "post #" so we get the real job id, not stray "#2" etc.
_JOB_ID_MAIN_RE = re.compile(
    r"(?:job\s+post|post)\s*[:\s]*#\s*(\d+)",
    re.IGNORECASE,
)
# Fallback: prefer # followed by 4+ digits (job ids), else first #\d+
_JOB_ID_FALLBACK_RE = re.compile(r"#(\d{4,})")
_JOB_ID_FALLBACK_ANY_RE = re.compile(r"#(\d+)")
_VIEW_JOB_LINK_RE = re.compile(
    r'href=["\']([^"\']+)["\'][^>]*>[^<]*View\s+job\s+post',
    re.IGNORECASE,
)
# Some emails have the link on "Accept to submit providers" instead of "View job post"
_ACCEPT_TO_SUBMIT_LINK_RE = re.compile(
    r'href=["\']([^"\']+)["\'][^>]*>[^<]*Accept\s+to\s+submit\s+providers',
    re.IGNORECASE,
)
# Location: "at 3424 - Baxter, MN (BAXTER, MN)" -> strip parenthetical.
# Use \bat\b so "What it does: ..." in email <style> blocks does not match "at " inside "What ".
_LOCATION_RE = re.compile(r"\bat\s+([^(]+?)\s*\([^)]+\)", re.IGNORECASE)
_ACTION_PATTERNS = [
    (re.compile(r"updated the job post", re.I), "updated"),
    (re.compile(r"New job post from", re.I), "new"),
    # Full label up to the sentence period — "Active, not accepting new providers"
    # must not truncate to "Active" (ambiguous for the Open/Closed mapping).
    (re.compile(r"has been assigned a new status:\s*([^.\n<]+)", re.I), "status"),
    (re.compile(r"Accept to submit providers", re.I), "accept_to_submit"),
]


def parse_kimedics_job_email(record: dict) -> dict:
    """
    Parse a single Kimedics job notification email into structured columns.
    Expects record with keys: subject, body, html (optional), date, from_

    Returns dict with: job_post_id, location, action_or_change, view_job_link, subject, date, from_
    """
    subject = record.get("subject") or ""
    body = record.get("body") or ""
    html = record.get("html") or ""
    combined = f"{subject} {body}"

    # Job post # — "job post: #19413" or "job post #19413"; avoid stray "#2" from thread/HTML
    job_post_id = ""
    m = _JOB_ID_MAIN_RE.search(combined)
    if m:
        job_post_id = m.group(1)
    else:
        m2 = _JOB_ID_FALLBACK_RE.search(combined)  # 4+ digits first
        if m2:
            job_post_id = m2.group(1)
        else:
            m3 = _JOB_ID_FALLBACK_ANY_RE.search(combined)
            if m3:
                job_post_id = m3.group(1)

    # Location — e.g. "at 3424 - Baxter, MN (BAXTER, MN)" -> "3424 - Baxter, MN"
    location = ""
    loc_m = _LOCATION_RE.search(combined)
    if loc_m:
        location = loc_m.group(1).strip()

    # "View job post" or "Accept to submit providers" link from HTML
    view_job_link = ""
    if html:
        link_m = _VIEW_JOB_LINK_RE.search(html)
        if link_m:
            view_job_link = link_m.group(1).strip()
        if not view_job_link:
            link_m = _ACCEPT_TO_SUBMIT_LINK_RE.search(html)
            if link_m:
                view_job_link = link_m.group(1).strip()

    # What changed / action
    action_or_change = ""
    for pattern, label in _ACTION_PATTERNS:
        match = pattern.search(combined)
        if match:
            if label == "status" and match.lastindex:
                action_or_change = f"status: {match.group(1)}"
            else:
                action_or_change = label
            break

    return {
        "job_post_id": job_post_id,
        "location": location,
        "action_or_change": action_or_change,
        "view_job_link": view_job_link,
        "subject": subject,
        "date": record.get("date"),
        "from_": record.get("from_", ""),
    }


# IMAP month names are protocol-fixed (RFC 3501); never use strftime %b, which is
# locale-dependent and can emit non-English month names the server rejects.
_IMAP_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
                "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def build_sender_search_criteria(from_email: str, cutoff_date: Optional[datetime]) -> str:
    """
    IMAP search bounded server-side. Without SINCE, the FROM search matched every
    email the sender ever sent (1,800+ by July 2026) and the caller downloaded ALL
    of them each run just to keep the last-24h handful — that fetch alone outgrew
    the 10-min Modal timeout and silently killed the cron. SINCE is day-granular
    on the server's internal receive date, so pad one day back; the precise
    hour-level filter still happens on the Date header afterwards.
    """
    if cutoff_date is None:
        return f'FROM "{from_email}"'
    s = cutoff_date - timedelta(days=1)
    return f'(FROM "{from_email}" SINCE "{s.day:02d}-{_IMAP_MONTHS[s.month - 1]}-{s.year}")'


# Gmail caps an account at 15 simultaneous IMAP connections and rejects login
# with this ALERT while at the cap (seen 2026-07-10: a ~20-min external burst on
# the shared proxi@ inbox failed two cron ticks). The cap is transient by nature,
# so a short backoff-and-retry rides it out instead of failing the whole run.
_TRANSIENT_LOGIN_MARKER = "too many simultaneous connections"
_LOGIN_ATTEMPTS = 3
_LOGIN_BACKOFF_S = 30.0


def _login_with_retry(
    imap_server: str,
    email_account: str,
    email_password: str,
    attempts: int = _LOGIN_ATTEMPTS,
    backoff_s: float = _LOGIN_BACKOFF_S,
) -> imaplib.IMAP4_SSL:
    for attempt in range(attempts):
        mail = imaplib.IMAP4_SSL(imap_server)
        try:
            mail.login(email_account, email_password)
            return mail
        except imaplib.IMAP4.error as e:
            # Close the rejected connection's socket — a dangling pre-auth
            # session still counts against the very cap we're waiting out.
            try:
                mail.shutdown()
            except Exception:
                pass
            if _TRANSIENT_LOGIN_MARKER not in str(e).lower() or attempt == attempts - 1:
                raise
            time.sleep(backoff_s)
    raise RuntimeError("unreachable")  # loop always returns or raises


def _message_to_record(msg, cutoff_date: Optional[datetime]) -> Optional[dict]:
    """Convert a parsed email.message into the pipeline record dict.
    Returns None if the message is older than cutoff_date. Shared by the IMAP
    and Gmail API transports so both produce byte-identical records."""
    subject = decode_subject(msg.get("Subject"))
    from_header = msg.get("From", "")
    date_header = msg.get("Date")
    email_date = None
    if date_header:
        try:
            email_date = parsedate_to_datetime(date_header)
        except Exception:
            pass

    if cutoff_date and email_date and email_date < cutoff_date:
        return None

    return {
        "subject": subject,
        "body": get_body_from_message(msg),
        "html": get_html_from_message(msg),
        "date": email_date.isoformat() if email_date else None,
        "from_": from_header,
    }


def _gmail_oauth_env() -> Optional[dict]:
    """The three GMAIL_OAUTH_* env vars, or None if any is missing (→ IMAP)."""
    cfg = {
        "client_id": os.environ.get("GMAIL_OAUTH_CLIENT_ID", "").strip(),
        "client_secret": os.environ.get("GMAIL_OAUTH_CLIENT_SECRET", "").strip(),
        "refresh_token": os.environ.get("GMAIL_OAUTH_REFRESH_TOKEN", "").strip(),
    }
    return cfg if all(cfg.values()) else None


def _scrape_via_gmail_api(
    oauth_cfg: dict,
    from_email: str,
    cutoff_date: Optional[datetime],
    max_results: Optional[int],
) -> list:
    """Fetch matching INBOX messages via the Gmail API (no IMAP connection cap).

    Uses format=raw so each message goes through message_from_bytes — the exact
    same parse path as the IMAP RFC822 fetch. Query is bounded server-side with
    after:<epoch>; the precise cutoff still applies on the Date header in
    _message_to_record, mirroring the IMAP SINCE + post-filter behavior.
    """
    import base64

    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request
    from googleapiclient.discovery import build

    creds = Credentials(
        token=None,
        refresh_token=oauth_cfg["refresh_token"],
        client_id=oauth_cfg["client_id"],
        client_secret=oauth_cfg["client_secret"],
        token_uri="https://oauth2.googleapis.com/token",
        scopes=["https://www.googleapis.com/auth/gmail.readonly"],
    )
    creds.refresh(Request())
    svc = build("gmail", "v1", credentials=creds, cache_discovery=False)

    query = f"from:{from_email}"
    if cutoff_date is not None:
        query += f" after:{int(cutoff_date.timestamp())}"

    # messages.list returns newest-first, matching the IMAP loop's reversed()
    # iteration order. labelIds=INBOX mirrors mail.select("INBOX").
    msg_ids: list[str] = []
    page_token = None
    while True:
        resp = svc.users().messages().list(
            userId="me",
            q=query,
            labelIds=["INBOX"],
            maxResults=min(max_results or 500, 500),
            pageToken=page_token,
        ).execute()
        msg_ids.extend(m["id"] for m in resp.get("messages", []))
        page_token = resp.get("nextPageToken")
        if not page_token or (max_results and len(msg_ids) >= max_results):
            break

    email_list = []
    for mid in msg_ids:
        if max_results and len(email_list) >= max_results:
            break
        raw = svc.users().messages().get(userId="me", id=mid, format="raw").execute()
        msg = message_from_bytes(base64.urlsafe_b64decode(raw["raw"]))
        record = _message_to_record(msg, cutoff_date)
        if record is not None:
            email_list.append(record)
    return email_list


def scrape_emails_from_sender(
    email_account: str,
    email_password: str,
    from_email: str,
    imap_server: str = IMAP_SERVER,
    days: Optional[int] = None,
    hours: Optional[float] = None,
    max_results: Optional[int] = None,
):
    """
    Fetch emails from inbox that were received FROM a specific sender.

    Uses the Gmail API when GMAIL_OAUTH_CLIENT_ID / _CLIENT_SECRET /
    _REFRESH_TOKEN are set in the environment (immune to the 15-connection
    IMAP cap); any API failure falls back to the IMAP path below.

    Args:
        email_account: Your Gmail address (inbox we read)
        email_password: App password
        from_email: Only include emails from this sender
        imap_server: IMAP server host
        days: If set, only include emails from the last N days
        hours: If set, only include emails from the last N hours (overrides days if stricter)
        max_results: If set, stop after this many emails

    Returns:
        List of dicts with keys: subject, body, html, date, from_
    """
    cutoff_date = None
    if days is not None:
        cutoff_date = datetime.now(timezone.utc) - timedelta(days=days)
    if hours is not None:
        since = datetime.now(timezone.utc) - timedelta(hours=hours)
        cutoff_date = since if cutoff_date is None else max(cutoff_date, since)

    oauth_cfg = _gmail_oauth_env()
    if oauth_cfg is not None:
        try:
            return _scrape_via_gmail_api(oauth_cfg, from_email, cutoff_date, max_results)
        except Exception as e:
            print(f"Gmail API fetch failed ({type(e).__name__}: {e}) — falling back to IMAP")

    if not email_password:
        raise ValueError(
            "email_password (App Password) is required. "
            "Create one at https://myaccount.google.com/apppasswords"
        )

    mail = _login_with_retry(imap_server, email_account, email_password)
    email_list = []
    try:
        mail.select("INBOX")

        status, messages = mail.search(None, build_sender_search_criteria(from_email, cutoff_date))

        if status != "OK" or not messages[0]:
            return email_list

        email_ids = messages[0].split()
        for num in reversed(email_ids):
            if max_results and len(email_list) >= max_results:
                break
            status, msg_data = mail.fetch(num, "(RFC822)")
            if status != "OK" or not msg_data:
                continue
            msg = message_from_bytes(msg_data[0][1])
            record = _message_to_record(msg, cutoff_date)
            if record is not None:
                email_list.append(record)

        return email_list
    finally:
        # A leaked session lingers server-side and counts against Gmail's
        # 15-connection cap, starving the other automations on this inbox.
        try:
            mail.logout()
        except Exception:
            pass

"""
Gmail IMAP helpers: decode subject/body and scrape emails from a sender.
Used by both the local scrape_gmail.py and the Modal job.
"""

import imaplib
import re
from datetime import datetime, timezone, timedelta
from email import message_from_bytes
from email.header import decode_header
from email.utils import parsedate_to_datetime

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
    (re.compile(r"has been assigned a new status:\s*(\w+)", re.I), "status"),
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


def scrape_emails_from_sender(
    email_account: str,
    email_password: str,
    from_email: str,
    imap_server: str = IMAP_SERVER,
    days: int | None = None,
    hours: float | None = None,
    max_results: int | None = None,
):
    """
    Fetch emails from inbox that were received FROM a specific sender.

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
    if not email_password:
        raise ValueError(
            "email_password (App Password) is required. "
            "Create one at https://myaccount.google.com/apppasswords"
        )

    cutoff_date = None
    if days is not None:
        cutoff_date = datetime.now(timezone.utc) - timedelta(days=days)
    if hours is not None:
        since = datetime.now(timezone.utc) - timedelta(hours=hours)
        cutoff_date = since if cutoff_date is None else max(cutoff_date, since)

    mail = imaplib.IMAP4_SSL(imap_server)
    mail.login(email_account, email_password)
    mail.select("INBOX")

    status, messages = mail.search(None, f'FROM "{from_email}"')
    email_list = []

    if status != "OK" or not messages[0]:
        mail.logout()
        return email_list

    email_ids = messages[0].split()
    for num in reversed(email_ids):
        if max_results and len(email_list) >= max_results:
            break
        status, msg_data = mail.fetch(num, "(RFC822)")
        if status != "OK" or not msg_data:
            continue
        msg = message_from_bytes(msg_data[0][1])

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
            continue

        body = get_body_from_message(msg)
        html = get_html_from_message(msg)

        email_list.append({
            "subject": subject,
            "body": body,
            "html": html,
            "date": email_date.isoformat() if email_date else None,
            "from_": from_header,
        })

    mail.logout()
    return email_list

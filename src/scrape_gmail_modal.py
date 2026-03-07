"""
Modal job: scrape Kimedics job emails (donotreply@kimedics.com) every 30 minutes.
Parses job post #, action/change, and View job post link; stores parsed rows in a Modal Dict.
Uses secrets under "salesforce-automation" (GMAIL_APP_PASSWORD).

Deploy (from project root):
  modal deploy src/scrape_gmail_modal.py

Run scraper once:
  modal run src/scrape_gmail_modal.py::scrape_gmail_job

View stored data (sample from Dict):
  modal run src/scrape_gmail_modal.py::inspect_emails
  modal run src/scrape_gmail_modal.py::inspect_emails --sample-size 10
"""

import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import modal

# Include utils in the image (Modal 1.0+ uses Image.add_local_* instead of Mount)
_src = Path(__file__).resolve().parent
image = (
    modal.Image.debian_slim()
    .add_local_dir(_src / "utils", remote_path="/root/utils")
)

app = modal.App("salesforce-automation")

# Persistent Dict for scraped email data (Python objects)
EMAIL_DICT = modal.Dict.from_name("gmail-scraped-emails", create_if_missing=True)


@app.function(
    image=image,
    schedule=modal.Period(minutes=30),
    secrets=[modal.Secret.from_name("salesforce-automation")],
)
def scrape_gmail_job():
    # Make utils importable on the remote container
    sys.path.insert(0, "/root")
    from utils.gmail import parse_kimedics_job_email, scrape_emails_from_sender

    email_account = "anddy0622@gmail.com"
    email_password = os.environ["GMAIL_APP_PASSWORD"]
    from_email = "donotreply@kimedics.com"

    raw_emails = scrape_emails_from_sender(
        email_account=email_account,
        email_password=email_password,
        from_email=from_email,
        days=30,
        max_results=50,
    )
    parsed = [parse_kimedics_job_email(e) for e in raw_emails]

    # Store parsed job rows as Python objects in Modal Dict
    EMAIL_DICT["emails"] = parsed
    EMAIL_DICT["last_updated"] = datetime.now(timezone.utc).isoformat()
    EMAIL_DICT["count"] = len(parsed)

    print(f"Scraped {len(parsed)} job emails from {from_email}; saved to Dict")
    for i, row in enumerate(parsed[:5], 1):
        link = (row.get("view_job_link") or "")[:50]
        loc = row.get("location") or ""
        print(f"  {i}. #{row.get('job_post_id')} {loc} | {row.get('action_or_change')} | {link}{'...' if len(row.get('view_job_link') or '') > 50 else ''}")
    if len(parsed) > 5:
        print(f"  ... and {len(parsed) - 5} more")
    return len(parsed)


@app.function(image=image)
def get_stored_emails(sample_size: int = 5):
    """Read from the Dict and return metadata + a sample of emails (for inspection)."""
    last_updated = EMAIL_DICT.get("last_updated")
    count = EMAIL_DICT.get("count", 0)
    emails = EMAIL_DICT.get("emails") or []
    sample = emails[:sample_size] if emails else []
    return {
        "last_updated": last_updated,
        "count": count,
        "sample": sample,
    }


@app.local_entrypoint()
def inspect_emails(sample_size: int = 5):
    """Run locally to print a sample of stored Dict data (parsed job rows)."""
    data = get_stored_emails.remote(sample_size=sample_size)
    print("--- gmail-scraped-emails Dict (Kimedics job emails) ---")
    print(f"Last updated: {data['last_updated']}")
    print(f"Total count:  {data['count']}")
    print()
    for i, row in enumerate(data["sample"], 1):
        print(f"--- Job email {i} ---")
        print(f"  Job #:    {row.get('job_post_id')}")
        print(f"  Location: {row.get('location') or ''}")
        print(f"  Action:  {row.get('action_or_change')}")
        print(f"  Link:    {row.get('view_job_link') or '(none)'}")
        print(f"  Subject: {(row.get('subject') or '')[:70]}")
        print(f"  Date:    {row.get('date')}")
        print()

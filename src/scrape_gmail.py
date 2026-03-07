#!/usr/bin/env python3
"""
Scrape Kimedics job emails from Gmail inbox (donotreply@kimedics.com).
Parses job post #, action/change, and View job post link into CSV.
Get App Password at: https://myaccount.google.com/apppasswords
"""

import csv
import os
import sys
import time
from pathlib import Path

# Allow importing utils when run from project root (e.g. python src/scrape_gmail.py)
_src_dir = Path(__file__).resolve().parent
if str(_src_dir) not in sys.path:
    sys.path.insert(0, str(_src_dir))

from dotenv import load_dotenv

from utils.gmail import parse_kimedics_job_email, scrape_emails_from_sender

# Load .env from project root (parent of src/)
_env_path = Path(__file__).resolve().parent.parent / ".env"
load_dotenv(_env_path)

# -----------------------------------------------------------------------------
# Config
# -----------------------------------------------------------------------------
EMAIL_ACCOUNT = "anddy0622@gmail.com"
EMAIL_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD")
FROM_EMAIL = "donotreply@kimedics.com"

# CSV output: data/job_emails.csv (parsed columns)
DATA_DIR = Path(__file__).resolve().parent.parent / "data"
CSV_PATH = DATA_DIR / "job_emails.csv"

CSV_FIELDS = ["job_post_id", "location", "action_or_change", "view_job_link", "subject", "date", "from_"]


def save_job_emails_to_csv(rows: list[dict], path: Path = CSV_PATH) -> Path:
    """Write parsed job emails to CSV. Creates data dir if needed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        return path
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS, quoting=csv.QUOTE_NONNUMERIC)
        writer.writeheader()
        writer.writerows(rows)
    return path


def main():
    password = EMAIL_PASSWORD or os.environ.get("GMAIL_APP_PASSWORD", "")
    start = time.perf_counter()
    raw_emails = scrape_emails_from_sender(
        email_account=EMAIL_ACCOUNT,
        email_password=password,
        from_email=FROM_EMAIL,
        days=30,
        max_results=50,
    )
    parsed = [parse_kimedics_job_email(e) for e in raw_emails]
    elapsed = time.perf_counter() - start

    for i, row in enumerate(parsed, 1):
        print(f"--- Job email {i} ---")
        print(f"  Job #:    {row['job_post_id']}")
        print(f"  Location: {row.get('location') or ''}")
        print(f"  Action:   {row['action_or_change']}")
        print(f"  Link:     {row['view_job_link'] or '(none)'}")
        subj = row["subject"] or ""
        print(f"  Subject:  {subj[:70]}{'...' if len(subj) > 70 else ''}")
        print()
    print(f"Total: {len(parsed)} emails")
    if parsed:
        save_job_emails_to_csv(parsed)
        print(f"Saved to {CSV_PATH}")
    print(f"Completed in {elapsed:.2f}s")
    return parsed


if __name__ == "__main__":
    main()

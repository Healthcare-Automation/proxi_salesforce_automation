#!/usr/bin/env python3
"""
Scrape Kimedics job emails from Gmail inbox (donotreply@kimedics.com).
Parses job post #, action/change, and View job post link into CSV.
Get App Password at: https://myaccount.google.com/apppasswords

By default, Supabase logging **replaces** pipeline data: TRUNCATE scrape_runs CASCADE clears
email_scrapes, job_content, and job_current in the target schema, then one new run + rows.
Use ``--append`` to keep existing rows (legacy behavior).
"""

import argparse
import csv
import os
import sys
import time
from pathlib import Path

# src/ on path (this file lives in src/local/)
_SRC = Path(__file__).resolve().parent.parent
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from dotenv import load_dotenv

from utils.gmail import parse_kimedics_job_email, scrape_emails_from_sender

# Load .env from project root
_env_path = _SRC.parent / ".env"
load_dotenv(_env_path)

# -----------------------------------------------------------------------------
# Config
# -----------------------------------------------------------------------------
EMAIL_ACCOUNT = os.environ.get("GMAIL_EMAIL", "andy@uzu.studio")
EMAIL_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD")
FROM_EMAIL = "donotreply@kimedics.com"
MAX_EMAILS = 500  # Max emails to fetch per run (None = no cap)

# CSV output: data/job_emails.csv (parsed columns)
DATA_DIR = _SRC.parent / "data"
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
    p = argparse.ArgumentParser(
        description="Gmail Kimedics scrape → CSV + Supabase (default: replace scrape/job tables; use --append to add only).",
    )
    p.add_argument(
        "--pg-schema",
        default=None,
        metavar="NAME",
        help="Schema (e.g. staging, public). public requires --production-ok. Omit to type STAGING/PRODUCTION.",
    )
    p.add_argument(
        "--production-ok",
        action="store_true",
        help="Allow --pg-schema public.",
    )
    p.add_argument(
        "--append",
        action="store_true",
        help="Do not TRUNCATE before insert (default: replace scrape_runs + cascaded email/job tables).",
    )
    args = p.parse_args()

    from utils.run_target_prompt import resolve_pg_schema

    try:
        pg_schema = resolve_pg_schema(
            args.pg_schema,
            production_ok=args.production_ok,
            staging_only=False,
        )
    except ValueError as e:
        print(e, file=sys.stderr)
        sys.exit(1)
    print(f"Supabase writes will use schema: {pg_schema!r}")
    if not args.append:
        print("Replace mode: will TRUNCATE scrape_runs CASCADE (email_scrapes, job_content, job_current) before logging.")
    else:
        print("Append mode: existing scrape rows are kept; new run + rows are added.")

    password = EMAIL_PASSWORD or os.environ.get("GMAIL_APP_PASSWORD", "")
    start = time.perf_counter()
    raw_emails = scrape_emails_from_sender(
        email_account=EMAIL_ACCOUNT,
        email_password=password,
        from_email=FROM_EMAIL,
        days=30,
        max_results=MAX_EMAILS,
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
        try:
            from utils.supabase_db import (
                ensure_schema_for_writes,
                get_conn,
                log_email_scrapes,
                log_run_finish,
                log_run_start,
                truncate_email_scrape_tables,
            )
            with get_conn() as conn:
                if conn:
                    ensure_schema_for_writes(conn, pg_schema)
                    if not args.append:
                        truncate_email_scrape_tables(
                            conn,
                            pg_schema,
                            allow_public=pg_schema.strip().lower() == "public",
                        )
                    run_id = log_run_start(conn, "gmail", CSV_FIELDS, schema=pg_schema)
                    if run_id:
                        log_email_scrapes(conn, run_id, parsed, CSV_FIELDS, schema=pg_schema)
                        log_run_finish(conn, run_id, schema=pg_schema)
                        print(f"Logged to Supabase (scrape_runs + email_scrapes) schema={pg_schema!r}")
        except Exception as e:
            print(f"(Supabase logging skipped: {e})")
    print(f"Completed in {elapsed:.2f}s")
    return parsed


if __name__ == "__main__":
    main()

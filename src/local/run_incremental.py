"""
Incremental Gmail + Supabase run: last 1 hour only, skip already-logged emails.
Use for 10-min Modal cadence: fetch last 1h, check Supabase for (job_post_id, date), log only new.
Accuracy: we never skip an email that hasn't been logged (exact match by job_post_id + date).

Usage (from project root):
  python src/local/run_incremental.py           # fetch last 1h, filter, log only new to Supabase
  python src/local/run_incremental.py --scrape # same + run link scrape for new rows (temp CSV → batch script)

Requires: .env with GMAIL_APP_PASSWORD, DB_PASSWORD; optional KIMEDICS_* for --scrape.
"""

import argparse
import os
import subprocess
import sys
import tempfile
from pathlib import Path

_SRC = Path(__file__).resolve().parent.parent
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

# Config: same as Modal cadence
EMAIL_HOURS = 12.0  # fetch emails from last N hours
SUPABASE_LOOKBACK_HOURS = 13.0


def run_incremental_gmail_and_log(
    email_account: str,
    email_password: str,
    from_email: str = "donotreply@kimedics.com",
    hours: float = EMAIL_HOURS,
    since_hours_ago: float = SUPABASE_LOOKBACK_HOURS,
    *,
    pg_schema: str = "public",
):
    """
    Fetch emails from last `hours`, filter to only those not already in Supabase (by job_post_id + date),
    log new ones to Supabase (scrape_run + email_scrapes). Returns (run_id, new_rows).
    """
    from utils.gmail import parse_kimedics_job_email, scrape_emails_from_sender
    from utils.supabase_db import (
        ensure_schema_for_writes,
        filter_parsed_emails_not_logged,
        get_conn,
        get_existing_email_keys,
        log_email_scrapes,
        log_run_finish,
        log_run_start,
    )

    CSV_FIELDS = ["job_post_id", "location", "action_or_change", "view_job_link", "subject", "date", "from_"]

    raw = scrape_emails_from_sender(
        email_account=email_account,
        email_password=email_password,
        from_email=from_email,
        hours=hours,
        max_results=500,
    )
    parsed = [parse_kimedics_job_email(e) for e in raw]

    conn = None
    run_id = None
    with get_conn() as conn:
        if conn is None:
            return None, parsed  # no DB: treat all as "new" and return for caller to handle
        ensure_schema_for_writes(conn, pg_schema)
        existing = get_existing_email_keys(conn, since_hours_ago=since_hours_ago, schema=pg_schema)
        new_rows = filter_parsed_emails_not_logged(parsed, existing)
        if not new_rows:
            return None, []

        run_id = log_run_start(conn, "gmail", CSV_FIELDS, schema=pg_schema)
        if run_id:
            log_email_scrapes(conn, run_id, new_rows, CSV_FIELDS, schema=pg_schema)
            log_run_finish(conn, run_id, schema=pg_schema)
    return run_id, new_rows


def main():
    from dotenv import load_dotenv

    load_dotenv(_SRC.parent / ".env")

    p = argparse.ArgumentParser(
        description="Incremental Gmail (last ~1h) → new rows only in email_scrapes; optional Playwright scrape.",
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
        "--scrape",
        "-s",
        action="store_true",
        help="After new emails, run Playwright link batch for those rows.",
    )
    args = p.parse_args()

    from utils.run_target_prompt import resolve_pg_schema

    email_account = os.environ.get("GMAIL_EMAIL", "andy@uzu.studio")
    email_password = os.environ.get("GMAIL_APP_PASSWORD", "").strip()
    if not email_password:
        print("Set GMAIL_APP_PASSWORD in .env")
        sys.exit(1)

    try:
        pg_schema = resolve_pg_schema(
            args.pg_schema,
            production_ok=args.production_ok,
            staging_only=False,
        )
    except ValueError as e:
        print(e, file=sys.stderr)
        sys.exit(1)
    print(f"Supabase target schema: {pg_schema!r}")

    run_scrape = args.scrape

    run_id, new_rows = run_incremental_gmail_and_log(
        email_account=email_account,
        email_password=email_password,
        hours=EMAIL_HOURS,
        since_hours_ago=SUPABASE_LOOKBACK_HOURS,
        pg_schema=pg_schema,
    )

    if not new_rows:
        print("No new emails in the last hour (or all already logged).")
        return

    print(f"Logged {len(new_rows)} new email(s) to Supabase (run_id={run_id}, schema={pg_schema!r})")
    with_links = [r for r in new_rows if (r.get("view_job_link") or "").strip()]
    if not with_links:
        print("None have view_job_link; nothing to scrape.")
        return

    if run_scrape:
        import csv
        fd, path = tempfile.mkstemp(suffix=".csv", prefix="job_emails_")
        with open(fd, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=["job_post_id", "location", "action_or_change", "view_job_link", "subject", "date", "from_"])
            w.writeheader()
            w.writerows(new_rows)
        try:
            root = _SRC.parent
            batch_script = root / "tests" / "scrape_kimedics_batch_playwright.py"
            subprocess.run(
                [
                    sys.executable,
                    str(batch_script),
                    "--csv",
                    path,
                    "--pg-schema",
                    pg_schema,
                ],
                cwd=str(root),
                check=True,
            )
        finally:
            Path(path).unlink(missing_ok=True)
    else:
        print(f"{len(with_links)} row(s) have view_job_link. Run with --scrape to run link scrape.")


if __name__ == "__main__":
    main()

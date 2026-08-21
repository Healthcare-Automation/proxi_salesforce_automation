#!/usr/bin/env python3
"""
Full Gmail re-scrape into staging: mirrors public table names/columns under schema ``staging``.

1. Ensures staging has scrape_runs, email_scrapes, job_content, job_current, sf_account_reference
   (same layout as public), and seeds default SF Account reference rows.
2. TRUNCATE staging.scrape_runs RESTART IDENTITY CASCADE — also clears staging.email_scrapes and
   any staging.job_content rows linked via FK (and job_current if it references those rows).
3. Inserts one scrape_runs row + one email_scrapes row per parsed Kimedics email.

Does not modify the public schema.

Usage (project root):
  python src/local/staging_full_email_rescrape.py --dry-run
  python src/local/staging_full_email_rescrape.py --days 365 --max-emails 8000
  python src/local/staging_full_email_rescrape.py --skip-truncate   # append without clearing (advanced)

Override staging schema name with env ``SUPABASE_STAGING_SCHEMA`` (you still type STAGING at the prompt).

Requires .env: GMAIL_APP_PASSWORD, DB_PASSWORD. Optional: GMAIL_EMAIL, KIMEDICS_FROM_EMAIL.

You will be prompted to type STAGING (all caps) — this script only refills the staging mirror, not public.

To fill ``job_content`` / ``job_current`` in staging (Playwright), run next::

  python src/local/staging_run_link_batch.py --max 20
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parent.parent
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

CSV_FIELDS = ["job_post_id", "location", "action_or_change", "view_job_link", "subject", "date", "from_"]
DEFAULT_FROM = "donotreply@kimedics.com"


def main() -> int:
    from dotenv import load_dotenv

    load_dotenv(_SRC.parent / ".env")

    p = argparse.ArgumentParser(
        description="Truncate (optional) and reload staging scrape_runs + email_scrapes from Gmail."
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Fetch + parse only; print counts; no database writes.",
    )
    p.add_argument(
        "--skip-truncate",
        action="store_true",
        help="Do not TRUNCATE before insert (staging will accumulate duplicate-era rows if re-run).",
    )
    p.add_argument("--days", type=int, default=None, help="Only emails from the last N days (default: all).")
    p.add_argument("--max-emails", type=int, default=None, metavar="N", help="Cap IMAP results (default: no cap).")
    p.add_argument(
        "--pg-schema",
        default=None,
        metavar="NAME",
        help="Staging mirror schema (skip STAGING prompt). Cannot be public.",
    )
    args = p.parse_args()

    from utils.run_target_prompt import prompt_pg_schema
    from utils.supabase_db import _validate_pg_identifier

    if args.pg_schema:
        schema = _validate_pg_identifier(args.pg_schema.strip(), "schema")
        if schema == "public":
            print("This script only loads a staging mirror, not public.", file=sys.stderr)
            return 1
    else:
        schema = prompt_pg_schema(staging_only=True)
    print(f"Staging schema in use: {schema!r}")
    email_account = os.environ.get("GMAIL_EMAIL", "andy@uzu.studio").strip()
    email_password = os.environ.get("GMAIL_APP_PASSWORD", "").strip()
    from_email = os.environ.get("KIMEDICS_FROM_EMAIL", DEFAULT_FROM).strip() or DEFAULT_FROM

    if not email_password:
        print("Set GMAIL_APP_PASSWORD in .env", file=sys.stderr)
        return 1

    from utils.gmail import parse_kimedics_job_email, scrape_emails_from_sender
    from utils.supabase_db import (
        ensure_full_staging_schema,
        get_conn,
        log_email_scrapes,
        log_run_finish,
        log_run_start,
        truncate_email_scrape_tables,
    )

    print(
        f"Gmail: from={from_email!r} account={email_account!r} "
        f"days={args.days!r} max_emails={args.max_emails!r}"
    )
    raw = scrape_emails_from_sender(
        email_account=email_account,
        email_password=email_password,
        from_email=from_email,
        days=args.days,
        max_results=args.max_emails,
    )
    parsed = [parse_kimedics_job_email(e) for e in raw]
    with_job = [r for r in parsed if (r.get("job_post_id") or "").strip()]
    print(f"Fetched {len(raw)} message(s), {len(with_job)} with job_post_id.")

    if args.dry_run:
        print(f"[dry-run] Would write to schema={schema!r}: ensure tables, ", end="")
        if args.skip_truncate:
            print("skip truncate, ", end="")
        else:
            print("TRUNCATE scrape_runs CASCADE, ", end="")
        print(f"1 run + {len(parsed)} email_scrapes row(s).")
        return 0

    with get_conn() as conn:
        if conn is None:
            print("Database connection failed (psycopg2 / credentials).", file=sys.stderr)
            return 1
        ensure_full_staging_schema(conn, schema=schema, seed_sf_defaults=True)
        if not args.skip_truncate:
            truncate_email_scrape_tables(conn, schema)
        run_id = log_run_start(conn, "gmail", CSV_FIELDS, schema=schema)
        if not run_id:
            print("log_run_start failed.", file=sys.stderr)
            return 1
        ids = log_email_scrapes(conn, run_id, parsed, CSV_FIELDS, schema=schema)
        log_run_finish(conn, run_id, schema=schema)

    print(f"Schema {schema!r}: run_id={run_id}, inserted {len(ids)} email_scrapes row(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""
Populate staging ``job_content`` / ``job_current`` by scraping Kimedics links from staging ``email_scrapes``.

Prerequisites
-------------
1. Staging must already have rows in ``email_scrapes`` with ``view_job_link`` set. Load them with::

     python src/local/staging_full_email_rescrape.py --days 90 --max-emails 500

   (You will be prompted to type STAGING.)

2. ``.env``: ``DB_PASSWORD``, ``KIMEDICS_EMAIL``, ``KIMEDICS_PASSWORD``; Playwright installed
   (``pip install playwright && playwright install chromium``).

This script
-------------
- Prompts for **STAGING** (all caps), same as other staging-only tools.
- Ensures the staging schema has the expected tables.
- Reads links from staging, writes a temp CSV, runs ``tests/scrape_kimedics_batch_playwright.py``
  with ``--pg-schema <staging>`` so ``job_content`` / ``job_current`` land in staging only.

Usage (project root)::

  python src/local/staging_run_link_batch.py --max 15
  python src/local/staging_run_link_batch.py --only-missing --max 50
  python src/local/staging_run_link_batch.py --dry-run

**Refresh existing rows (step 3):** do **not** pass ``--only-missing``. Playwright re-visits each link and
``log_job_content`` inserts or updates the row for each ``email_scrape_id``, so ``view_job_link``, ``standard_schedule``, and
``raw_columns_json`` are rewritten.

**Refill bad scrapes** (empty / short ``description_full_text``, e.g. after session timeout): use
``--refill-sparse`` (optional ``--min-desc-chars 80``). Mutually exclusive with ``--only-missing``.

To target **public** (production), use ``--pg-schema public --production-ok`` or add ``--production-ok`` and type **PRODUCTION** at the prompt.

Override staging schema name with env ``SUPABASE_STAGING_SCHEMA``, or pass ``--pg-schema <name>`` to skip the prompt.
"""

from __future__ import annotations

import argparse
import csv
import os
import subprocess
import sys
import tempfile
from pathlib import Path

_SRC = Path(__file__).resolve().parent.parent
_ROOT = _SRC.parent
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

CSV_FIELDS = [
    "email_scrape_id",
    "job_post_id",
    "location",
    "action_or_change",
    "view_job_link",
    "subject",
    "date",
    "from_",
]


def main() -> int:
    from dotenv import load_dotenv

    load_dotenv(_ROOT / ".env")

    p = argparse.ArgumentParser(
        description="Run Playwright link batch against staging email_scrapes → job_content / job_current."
    )
    p.add_argument("--max", type=int, default=None, metavar="N", help="Max rows to scrape (default: all with links).")
    p.add_argument(
        "--only-missing",
        action="store_true",
        help="Only rows that do not yet have a job_content row linked by email_scrape_id.",
    )
    p.add_argument(
        "--refill-sparse",
        action="store_true",
        help="Only rows that already have job_content but description_full_text is null or shorter than --min-desc-chars.",
    )
    p.add_argument(
        "--min-desc-chars",
        type=int,
        default=80,
        metavar="N",
        help="With --refill-sparse, treat descriptions shorter than N as missing (default: 80).",
    )
    p.add_argument(
        "--production-ok",
        action="store_true",
        help="Allow typing PRODUCTION to refresh public.job_content (default: STAGING only).",
    )
    p.add_argument(
        "--pg-schema",
        default=None,
        metavar="NAME",
        help="PostgreSQL schema (e.g. staging, public). public requires --production-ok. Skips STAGING/PRODUCTION prompt.",
    )
    p.add_argument("--dry-run", action="store_true", help="Print counts and exit without Playwright.")
    args = p.parse_args()
    if args.only_missing and args.refill_sparse:
        print("Use only one of --only-missing and --refill-sparse.", file=sys.stderr)
        return 1

    from utils.run_target_prompt import resolve_pg_schema
    from utils.supabase_db import (
        ensure_schema_for_writes,
        fetch_email_scrapes_with_job_links,
        get_conn,
    )

    try:
        schema = resolve_pg_schema(
            args.pg_schema,
            production_ok=args.production_ok,
            staging_only=(args.pg_schema is None) and not args.production_ok,
        )
    except ValueError as e:
        print(str(e), file=sys.stderr)
        return 1
    print(f"Target schema: {schema!r}")

    with get_conn() as conn:
        if conn is None:
            print("Database connection failed (check DB_PASSWORD / DIRECT_URL).", file=sys.stderr)
            return 1
        ensure_schema_for_writes(conn, schema)
        try:
            rows = fetch_email_scrapes_with_job_links(
                conn,
                schema=schema,
                limit=args.max,
                only_without_job_content=args.only_missing,
                only_sparse_job_content=args.refill_sparse,
                min_description_length=args.min_desc_chars,
            )
        except ValueError as e:
            print(str(e), file=sys.stderr)
            return 1

    if not rows:
        hint = (
            "No rows matched. For --refill-sparse, ensure job_content exists but descriptions are short/empty.\n"
            "Otherwise load email_scrapes first, e.g.:\n"
            "  python src/local/staging_full_email_rescrape.py --days 90 --max-emails 500"
        )
        print(hint)
        return 1

    if args.only_missing:
        mode = "only new (no job_content yet)"
    elif args.refill_sparse:
        mode = f"refill sparse job_content (description < {args.min_desc_chars} chars)"
    else:
        mode = "all links (insert or update / refresh)"
    print(f"Selected {len(rows)} row(s) with view_job_link — {mode}.")
    if args.dry_run:
        for r in rows[:5]:
            eid = r.get("email_scrape_id", "")
            print(
                f"  email_scrape_id={eid} job {r.get('job_post_id')!r} — "
                f"{(r.get('view_job_link') or '')[:60]}..."
            )
        if len(rows) > 5:
            print(f"  ... and {len(rows) - 5} more")
        return 0

    batch_script = _ROOT / "tests" / "scrape_kimedics_batch_playwright.py"
    if not batch_script.exists():
        print(f"Missing {batch_script}", file=sys.stderr)
        return 1

    fd, path = tempfile.mkstemp(suffix=".csv", prefix="staging_links_")
    os.close(fd)
    try:
        def _csv_val(col: str, val):
            # DB drivers often return ``date`` as datetime; CSV must carry ISO text for find_email_scrape_id.
            if col == "email_scrape_id":
                if val is None:
                    return ""
                return str(int(val)) if isinstance(val, int) else str(val).strip()
            if col == "date" and val is not None and hasattr(val, "isoformat"):
                return val.isoformat()
            return val if val is not None else ""

        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=CSV_FIELDS, extrasaction="ignore")
            w.writeheader()
            for r in rows:
                w.writerow({k: _csv_val(k, r.get(k)) for k in CSV_FIELDS})
        cmd = [
            sys.executable,
            str(batch_script),
            "--csv",
            path,
            "--pg-schema",
            schema,
        ]
        print("Running Playwright batch...")
        subprocess.run(cmd, cwd=str(_ROOT), check=True)
    finally:
        Path(path).unlink(missing_ok=True)

    print(f"Done. Inspect `{schema}.job_content` and `{schema}.job_current` in Supabase SQL editor.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

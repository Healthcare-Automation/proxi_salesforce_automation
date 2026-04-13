#!/usr/bin/env python3
"""
Delete every Supabase row tied to one Kimedics ``job_id`` (pipeline + audit).

Removes:

- ``job_event_log`` (all events for that job)
- ``job_content`` (``job_id`` or ``job_post_id`` match)
- ``job_current`` (primary key ``job_id``)
- ``email_scrapes`` (``job_post_id`` match — same id as Kimedics post in mail)
- Orphan ``scrape_runs`` that no longer have any ``email_scrapes``, ``job_content``, or ``job_event_log`` rows

Does **not** delete ``sf_worksite_location_map`` or ``sf_account_reference`` (shared / not job-scoped).

Examples (from repo root)::

  python manual/triggers/delete_job_pipeline_data.py 19614 --pg-schema staging
  python manual/triggers/delete_job_pipeline_data.py 19614 --pg-schema public --production-ok --dry-run

Requires ``.env`` DB credentials (same as other local tools).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_SRC = _ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


def main() -> int:
    from dotenv import load_dotenv

    load_dotenv(_ROOT / ".env")

    p = argparse.ArgumentParser(
        description="Delete all pipeline + job_event_log rows for one Kimedics job_id."
    )
    p.add_argument("job_id", help="Kimedics job_id / job_post_id (e.g. 19614).")
    p.add_argument(
        "--pg-schema",
        default=None,
        metavar="NAME",
        help="PostgreSQL schema (e.g. staging, public). public requires --production-ok.",
    )
    p.add_argument(
        "--production-ok",
        action="store_true",
        help="Allow schema public when combined with --pg-schema public.",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Print counts only; no DELETE/UPDATE.",
    )
    args = p.parse_args()

    job_id = str(args.job_id or "").strip()
    if not job_id:
        print("job_id must be non-empty.", file=sys.stderr)
        return 1

    from utils.run_target_prompt import resolve_pg_schema
    from utils.supabase_db import delete_all_records_for_job_id, ensure_schema_for_writes, get_conn

    try:
        schema = resolve_pg_schema(
            args.pg_schema,
            production_ok=args.production_ok,
            staging_only=False,
        )
    except ValueError as e:
        print(e, file=sys.stderr)
        return 1

    with get_conn() as conn:
        if conn is None:
            print("Database connection failed.", file=sys.stderr)
            return 1

        ensure_schema_for_writes(conn, schema)

        print(f"Target schema: {schema!r}, job_id: {job_id!r}")
        if args.dry_run:
            print("Dry run — counting rows that would be affected.")

        stats = delete_all_records_for_job_id(
            conn,
            job_id,
            schema=schema,
            dry_run=args.dry_run,
        )

        print(
            f"  job_event_log rows:              {stats['job_event_log']}\n"
            f"  job_current (last_content set):  {stats['job_current_last_content_nulled']} rows would be nulled, then {stats['job_current']} PK row(s) removed\n"
            f"  job_content rows:                {stats['job_content']}\n"
            f"  email_scrapes rows:              {stats['email_scrapes']}\n"
            f"  scrape_runs orphans removed:     {stats['scrape_runs_orphans']}"
        )

    if args.dry_run:
        print("Dry run — no changes committed.")
    else:
        print("Done (committed).")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

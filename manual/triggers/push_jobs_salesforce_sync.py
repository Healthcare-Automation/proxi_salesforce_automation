#!/usr/bin/env python3
"""
Re-run Salesforce scrape-field sync for specific Kimedics ``job_id`` values (PATCH Job__c from
``job_current``) and write ``job_event_log`` rows the same way the link-batch pipeline does
(``sf_scrape_fields_patched``, ``sf_scrape_fields_skip``, ``sf_scrape_fields_error``, etc.).

Default job_ids match jobs that were healthy before a bad GET/PATCH (e.g. unknown field on describe).

Examples (from ``proxi_salesforce_automation`` repo root)::

  # Default four jobs
  python manual/triggers/push_jobs_salesforce_sync.py

  # Explicit list + staging schema
  python manual/triggers/push_jobs_salesforce_sync.py 19529 19487 --pg-schema staging

  # Preview PATCH bodies only (no Salesforce write; still uses GET for compare)
  python manual/triggers/push_jobs_salesforce_sync.py --dry-run

  # Attach events to a scrape run (optional ``job_event_log.run_id`` FK)
  python manual/triggers/push_jobs_salesforce_sync.py --run-id 300

Requires ``.env`` (``SALESFORCE_*``, ``DB_PASSWORD`` / connection) and
``PROXI_SF_UPDATE_JOBS`` not disabling writes.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_SRC = _ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

DEFAULT_JOB_IDS: tuple[str, ...] = ("19529", "19487", "19618", "19619")


def _env_truthy(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes")


def _print_sf_gates() -> None:
    from utils.sf_write_flags import proxi_sf_writes_enabled

    writes = proxi_sf_writes_enabled()
    print(
        "  Salesforce writes:",
        "enabled" if writes else "DISABLED (set PROXI_SF_UPDATE_JOBS=true or unset)",
    )
    if not writes:
        print("  → Sync will log sf_scrape_fields_skip / skip API calls.", file=sys.stderr)


def main() -> int:
    from dotenv import load_dotenv

    load_dotenv(_ROOT / ".env")

    p = argparse.ArgumentParser(
        description="PATCH Job__c from job_current for given job_ids; log to job_event_log.",
    )
    p.add_argument(
        "job_ids",
        nargs="*",
        metavar="JOB_ID",
        help=f"Kimedics job_id values (default: {' '.join(DEFAULT_JOB_IDS)})",
    )
    p.add_argument(
        "--pg-schema",
        default=None,
        metavar="NAME",
        help="PostgreSQL schema (e.g. staging, public). public requires --production-ok.",
    )
    p.add_argument(
        "--production-ok",
        action="store_true",
        help="Allow --pg-schema public.",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Pass dry_run to sync (no PATCH; GET/compare may still run).",
    )
    p.add_argument(
        "--run-id",
        type=int,
        default=None,
        metavar="N",
        help="Optional scrape_runs.id stored on job_event_log.run_id for these events.",
    )
    args = p.parse_args()

    job_ids = [str(x).strip() for x in (args.job_ids or []) if str(x).strip()]
    if not job_ids:
        job_ids = list(DEFAULT_JOB_IDS)

    from utils.run_target_prompt import resolve_pg_schema
    from utils.supabase_db import ensure_schema_for_writes, get_conn
    from utils.sf_scrape_sync import sync_missing_scrape_fields_for_job_ids

    try:
        schema = resolve_pg_schema(
            args.pg_schema,
            production_ok=args.production_ok,
            staging_only=False,
        )
    except ValueError as e:
        print(e, file=sys.stderr)
        return 1

    print(f"Schema: {schema!r}")
    print(f"job_ids ({len(job_ids)}): {', '.join(job_ids)}")
    print(f"dry_run={args.dry_run}  run_id={args.run_id!r}")
    _print_sf_gates()

    with get_conn() as conn:
        if conn is None:
            print("Database connection failed.", file=sys.stderr)
            return 1

        ensure_schema_for_writes(conn, schema)

        attempted, patched = sync_missing_scrape_fields_for_job_ids(
            conn,
            job_ids,
            schema=schema,
            dry_run=args.dry_run,
            run_id=args.run_id,
        )

    print(f"Done. attempted={attempted}  patched={patched}")
    if attempted == 0 and patched == 0:
        print(
            "  (No attempts usually means missing sf_job_id on job_current, or writes disabled.)",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

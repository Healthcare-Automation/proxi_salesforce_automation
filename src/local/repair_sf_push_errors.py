"""
Replay unresolved Salesforce push errors from the last N days.

Scans ``job_event_log`` for ``sf_scrape_fields_error`` and ``sf_mapping_pull_failed``
rows that have no subsequent success for the same ``job_id`` and runs the
recovery engine against each.

Follows repo convention: prompts for ``STAGING`` or ``PRODUCTION`` (all caps)
before any DB write. No prompt for ``--dry-run``.

Usage (from project root):
    python src/local/repair_sf_push_errors.py                  # last 2 days, prod
    python src/local/repair_sf_push_errors.py --since 2026-04-21T00:00:00Z
    python src/local/repair_sf_push_errors.py --job-id 19664 --dry-run
    python src/local/repair_sf_push_errors.py --limit 10
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

_here = Path(__file__).resolve()
_src_root = _here.parent.parent
sys.path.insert(0, str(_src_root))


def _parse_since(value: str) -> datetime:
    v = (value or "").strip()
    if not v:
        return datetime.now(timezone.utc) - timedelta(days=2)
    s = v.replace("Z", "+00:00")
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _prompt_schema() -> str:
    ans = input("Target schema — type STAGING or PRODUCTION (all caps): ").strip()
    if ans == "PRODUCTION":
        return "public"
    if ans == "STAGING":
        return "staging"
    print("Unrecognized input; aborting.")
    sys.exit(1)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Replay unresolved SF push errors using the recovery engine.",
    )
    parser.add_argument(
        "--since",
        default="",
        help="ISO8601 cutoff. Default: 2 days ago (UTC).",
    )
    parser.add_argument(
        "--job-id",
        action="append",
        default=[],
        help="One or more job_id values to replay (repeatable). Default: all candidates.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Cap the number of jobs processed in this run.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Classify + plan without calling Salesforce or writing events.",
    )
    parser.add_argument(
        "--schema",
        choices=["public", "staging"],
        default=None,
        help="Override schema (otherwise prompted). Only used when --dry-run is also set.",
    )
    args = parser.parse_args()

    since = _parse_since(args.since)

    if args.dry_run:
        schema = args.schema or "public"
    elif args.schema:
        schema = args.schema
    else:
        schema = _prompt_schema()

    from utils.sf_push_recovery import (
        recover_recent_failures,
        resolve_sf_credentials,
    )
    from utils.supabase_db import get_conn

    instance_url: str | None = None
    access_token: str | None = None
    if not args.dry_run:
        creds = resolve_sf_credentials()
        if not creds:
            print("ERROR: Salesforce credentials missing (set SALESFORCE_CONSUMER_KEY + _SECRET).")
            return 2
        instance_url, access_token = creds

    invoker = f"cli:{os.environ.get('USER','unknown')}@{os.uname().nodename}"

    with get_conn() as conn:
        if conn is None:
            print("ERROR: could not connect to Supabase.")
            return 2
        results = recover_recent_failures(
            conn,
            access_token=access_token,
            instance_url=instance_url,
            since=since,
            schema=schema,
            dry_run=args.dry_run,
            limit=args.limit,
            job_ids=args.job_id or None,
            invocation="manual_cli",
            invoker=invoker,
        )
        if not args.dry_run:
            conn.commit()

    print()
    print(f"Scanned since: {since.isoformat()}  schema={schema}  dry_run={args.dry_run}")
    print(f"Candidates replayed: {len(results)}")
    counts: dict[str, int] = {}
    for r in results:
        counts[r.action] = counts.get(r.action, 0) + 1
    for action in sorted(counts):
        print(f"  {action}: {counts[action]}")
    if not results:
        print("  (no unresolved SF push errors found in the window)")
        return 0

    print()
    print(f"{'job_id':<10}{'action':<22}{'sf_job_id':<24}{'pushed':>7}{'quar':>6}")
    for r in results:
        sfid = (r.sf_job_id or "")[:22]
        print(
            f"{r.job_id:<10}{r.action:<22}{sfid:<24}"
            f"{len(r.fields_pushed):>7}{len(r.fields_quarantined):>6}"
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""
Clear Salesforce-related state for one Kimedics ``job_id`` so mapping / create / PATCH can run again.

Writes to Supabase (``job_content``, ``job_current``, ``job_event_log``). Use STAGING or
``--production-ok`` for ``public``.

What it does
------------
- ``job_content``: NULL ``sf_primary_account_id``, ``sf_worksite_account_id``,
  ``sf_worksite_display_label``, ``sf_job_id`` for every row where ``job_id`` matches;
  removes those keys from ``raw_columns_json`` (JSONB) when present.
- ``job_current``: NULL the same four columns for that ``job_id``.
- ``job_event_log``: DELETE all rows for that ``job_id`` (optional ``--keep-event-log``).

After a reset, ``resolve_sf_ids_for_job_ids`` no longer short-circuits on a cache hit (both SF ids
were cleared). Either:

- Pass ``--run-resolve-and-sync`` to run resolver + scrape-field sync in this process, or
- Re-run the Playwright link batch so the pipeline runs resolve + sync after ``log_job_content``.

**New Job__c after no match:** requires ``PROXI_SF_CREATE_JOBS=true`` and Salesforce writes on
(``PROXI_SF_UPDATE_JOBS`` not false). You also need a worksite Account Id on the row or in
``sf_worksite_location_map``, or ``PROXI_SF_CREATE_WORKSITES=true`` so a map / Account can be created.

Examples (from repo root)::

  python manual/triggers/reset_job_salesforce_state.py 19614 --pg-schema staging
  python manual/triggers/reset_job_salesforce_state.py 19614 --pg-schema public --production-ok \\
      --run-resolve-and-sync

Requires ``.env`` DB credentials (same as other local tools).
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

SF_COLUMNS = (
    "sf_primary_account_id",
    "sf_worksite_account_id",
    "sf_worksite_display_label",
    "sf_job_id",
)


def _env_truthy(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes")


def _print_resolve_gates() -> None:
    """Warn when env will block auto-create or PATCH."""
    from utils.sf_write_flags import proxi_sf_writes_enabled

    create = _env_truthy("PROXI_SF_CREATE_JOBS")
    writes = proxi_sf_writes_enabled()
    sites = _env_truthy("PROXI_SF_CREATE_WORKSITES")
    print("  Env (Salesforce):", end="")
    print(f" PROXI_SF_CREATE_JOBS={'on' if create else 'off'}", end="")
    print(f" · PROXI_SF_UPDATE_JOBS/writes={'on' if writes else 'off'}", end="")
    print(f" · PROXI_SF_CREATE_WORKSITES={'on' if sites else 'off'}")
    if not create:
        print(
            "  → Auto-create Job__c after no match is OFF. Set PROXI_SF_CREATE_JOBS=true in .env.",
            file=sys.stderr,
        )
    if not writes:
        print(
            "  → Salesforce writes are OFF (PROXI_SF_UPDATE_JOBS false/0/no/off). Create/sync will skip.",
            file=sys.stderr,
        )


def _print_recent_job_events(conn, *, schema: str, job_id: str, limit: int = 12) -> None:
    import json

    from psycopg2 import sql as pg_sql

    from utils.supabase_db import _tbl

    jel = _tbl(schema, "job_event_log")
    _DETAIL_TYPES = frozenset(
        {
            "job_create_failed",
            "job_create_skipped",
            "worksite_create_failed",
            "worksite_stale_map_cleared",
            "mapping_ambiguous",
            "sf_mapping_pull_failed",
        }
    )
    with conn.cursor() as cur:
        cur.execute(
            pg_sql.SQL(
                """
                SELECT event_type, created_at, payload
                FROM {}
                WHERE job_id = %s
                ORDER BY created_at DESC
                LIMIT %s;
                """
            ).format(jel),
            (job_id, limit),
        )
        rows = cur.fetchall()
    if not rows:
        print("  Recent job_event_log: (none for this job_id)")
        return
    print("  Recent job_event_log (newest first):")
    for et, ts, payload in rows:
        line = f"    - {et}  @  {ts}"
        if et in _DETAIL_TYPES and payload is not None:
            try:
                blob = json.dumps(payload, default=str)
                if len(blob) > 800:
                    blob = blob[:800] + "…"
                line += f"\n      payload: {blob}"
            except Exception:
                line += f"\n      payload: {payload!r}"
        print(line)


def _reset_in_db(conn, *, schema: str, job_id: str, keep_event_log: bool, dry_run: bool) -> dict:
    from psycopg2 import sql as pg_sql

    from utils.supabase_db import _tbl

    jc = _tbl(schema, "job_content")
    jcur = _tbl(schema, "job_current")
    jel = _tbl(schema, "job_event_log")

    stats: dict[str, int] = {
        "job_content_rows": 0,
        "job_current_rows": 0,
        "job_event_log_deleted": 0,
    }

    with conn.cursor() as cur:
        cur.execute(
            pg_sql.SQL("SELECT COUNT(*) FROM {} WHERE job_id = %s;").format(jc),
            (job_id,),
        )
        stats["job_content_rows"] = int(cur.fetchone()[0])

        cur.execute(
            pg_sql.SQL("SELECT COUNT(*) FROM {} WHERE job_id = %s;").format(jcur),
            (job_id,),
        )
        stats["job_current_rows"] = int(cur.fetchone()[0])

        if not keep_event_log:
            cur.execute(
                pg_sql.SQL("SELECT COUNT(*) FROM {} WHERE job_id = %s;").format(jel),
                (job_id,),
            )
            stats["job_event_log_deleted"] = int(cur.fetchone()[0])

    if dry_run:
        return stats

    null_assign = pg_sql.SQL(", ").join(
        pg_sql.SQL("{} = NULL").format(pg_sql.Identifier(c)) for c in SF_COLUMNS
    )
    # Strip SF keys from JSON snapshot so a future scrape/enrichment does not resurrect stale Ids.
    json_chain = "COALESCE(raw_columns_json, '{}'::jsonb)" + "".join(
        f" - '{c}'" for c in SF_COLUMNS
    )
    json_strip = pg_sql.SQL(json_chain)

    with conn.cursor() as cur:
        cur.execute(
            pg_sql.SQL(
                """
                UPDATE {}
                SET {}, raw_columns_json = {}
                WHERE job_id = %s;
                """
            ).format(jc, null_assign, json_strip),
            (job_id,),
        )

        cur.execute(
            pg_sql.SQL(
                """
                UPDATE {}
                SET {}
                WHERE job_id = %s;
                """
            ).format(jcur, null_assign),
            (job_id,),
        )

        if not keep_event_log:
            cur.execute(
                pg_sql.SQL("DELETE FROM {} WHERE job_id = %s;").format(jel),
                (job_id,),
            )

    return stats


def main() -> int:
    from dotenv import load_dotenv

    load_dotenv(_ROOT / ".env")

    p = argparse.ArgumentParser(
        description="Clear SF columns and optional event log for one Kimedics job_id; optionally re-run resolve + sync."
    )
    p.add_argument("job_id", help="Kimedics job_id (e.g. 19614).")
    p.add_argument(
        "--pg-schema",
        default=None,
        metavar="NAME",
        help="PostgreSQL schema (e.g. staging, public). public requires --production-ok.",
    )
    p.add_argument(
        "--production-ok",
        action="store_true",
        help="Allow targeting schema public when combined with explicit --pg-schema public.",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Print row counts only; do not UPDATE/DELETE.",
    )
    p.add_argument(
        "--keep-event-log",
        action="store_true",
        help="Do not DELETE job_event_log rows for this job_id.",
    )
    p.add_argument(
        "--run-resolve-and-sync",
        action="store_true",
        help="After reset, call resolve_sf_ids_for_job_ids + sync_missing_scrape_fields_for_job_ids.",
    )
    args = p.parse_args()

    job_id = str(args.job_id or "").strip()
    if not job_id:
        print("job_id must be non-empty.", file=sys.stderr)
        return 1

    from utils.run_target_prompt import resolve_pg_schema
    from utils.supabase_db import ensure_schema_for_writes, get_conn

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
        stats = _reset_in_db(
            conn,
            schema=schema,
            job_id=job_id,
            keep_event_log=args.keep_event_log,
            dry_run=args.dry_run,
        )
        print(
            f"  job_content rows (matching job_id): {stats['job_content_rows']}\n"
            f"  job_current rows: {stats['job_current_rows']}\n"
            f"  job_event_log rows to delete: {0 if args.keep_event_log else stats['job_event_log_deleted']}"
        )

        if args.dry_run:
            print("Dry run — no changes committed.")
            return 0

        if args.run_resolve_and_sync:
            from utils.sf_job_supabase_resolve import resolve_sf_ids_for_job_ids
            from utils.sf_scrape_sync import sync_missing_scrape_fields_for_job_ids

            _print_resolve_gates()
            n = resolve_sf_ids_for_job_ids(conn, [job_id], schema=schema, run_id=None)
            print(f"resolve_sf_ids_for_job_ids updated count (resolver metric): {n}")
            _print_recent_job_events(conn, schema=schema, job_id=job_id)
            att, patched = sync_missing_scrape_fields_for_job_ids(
                conn, [job_id], schema=schema, run_id=None
            )
            print(f"sync_missing_scrape_fields_for_job_ids: attempted={att}, patched={patched}")

    print("Done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""
One-off: collapse Job__c records that share a practice key onto one canonical record.

Salesforce marks ``Job_Client_Job_Id__c`` unique, so one practice is meant to have one
Job__c. Seven legacy pairs slipped past that constraint on cosmetic drift alone —
``"4096- Statesville, NC"`` vs ``"4096 - Statesville, NC"`` — which ``practice_key``
correctly folds together. Because they coexisted, the resolver handed each new Kimedics
posting a *different* duplicate and kept both alive (job 20084 resurrected a 2022 row),
while every PATCH lost ``Job_Client_Job_Id__c`` to a unique-value collision.

Canonical winner: most recruiting activity (placements + submittals + applications),
tie-break newest ``CreatedDate``. Losers keep all their history and relationships — they
only release the unique practice value and get marked Closed, so they stop shadowing the
winner. Nothing is deleted.

DRY-RUN BY DEFAULT. Pass --apply to write. Run --apply only after the printed plan has
been approved.

    python scripts/consolidate_duplicate_practice_jobs.py            # plan only
    python scripts/consolidate_duplicate_practice_jobs.py --apply
"""

from __future__ import annotations

import argparse
import os
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")

from utils import salesforce as sf  # noqa: E402
from utils.sf_job_payload import _mdy_to_iso, get_most_recent_open_date  # noqa: E402
from utils.sf_job_rest_minimal import update_job_record  # noqa: E402
from utils.sf_job_supabase_resolve import (  # noqa: E402
    _pick_canonical_practice_record,
    _recruiting_activity,
)
from utils.sf_practice_key import practice_key  # noqa: E402
from utils.supabase_db import get_conn, update_sf_ids_for_job  # noqa: E402

FIELDS = (
    "Id, Name, Job_Client_Job_Id__c, External_Job_ID__c, Job_Status__c, CreatedDate, "
    "LastModifiedDate, Total_Placements__c, Total_Submittals__c, Total_Applications__c, "
    "Job_Worksite_Location_1__c"
)


def find_duplicate_groups(instance_url: str, access_token: str) -> list[tuple[str, list[dict]]]:
    rows = sf.query_all(
        instance_url,
        access_token,
        f"SELECT {FIELDS} FROM Job__c WHERE Job_Client_Job_Id__c != null",
    )
    grouped: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        grouped[practice_key(row.get("Job_Client_Job_Id__c"))].append(row)
    return sorted((k, v) for k, v in grouped.items() if len(v) > 1)


def kimedics_jobs_by_practice(conn) -> dict[str, list[dict]]:
    """Kimedics jobs per practice key, newest posting last."""
    out: dict[str, list[dict]] = defaultdict(list)
    if conn is None:
        return out
    with conn.cursor() as cur:
        cur.execute(
            "SELECT job_id, practice_value, posted_date, updated_at, sf_job_id "
            "FROM job_current WHERE practice_value IS NOT NULL"
        )
        for job_id, practice_value, posted_date, updated_at, sf_job_id in cur.fetchall():
            out[practice_key(practice_value)].append(
                {
                    "job_id": job_id,
                    "posted_date": posted_date,
                    "posted_iso": _mdy_to_iso(posted_date) or "",
                    "updated_at": updated_at,
                    "sf_job_id": sf_job_id,
                }
            )
    for jobs in out.values():
        jobs.sort(key=lambda r: (r["posted_iso"], r["updated_at"]))
    return out


def describe(record: dict) -> str:
    return (
        f"{record['Id']}  {str(record.get('Job_Client_Job_Id__c')):26}  "
        f"ext={str(record.get('External_Job_ID__c') or '-'):6}  "
        f"created={str(record.get('CreatedDate'))[:10]}  "
        f"activity={_recruiting_activity(record):.0f}  "
        f"worksite={record.get('Job_Worksite_Location_1__c')}"
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="perform the writes (default: plan only)")
    args = ap.parse_args()

    token = sf.get_token_auto(
        os.environ["SALESFORCE_CONSUMER_KEY"],
        os.environ["SALESFORCE_CONSUMER_SECRET"],
        token_url=os.getenv("SALESFORCE_TOKEN_URL"),
    )
    instance_url, access_token = token["instance_url"], token["access_token"]

    groups = find_duplicate_groups(instance_url, access_token)
    if not groups:
        print("No practice-key duplicates found.")
        return 0

    cm = get_conn()
    conn = cm.__enter__()
    try:
        kimedics = kimedics_jobs_by_practice(conn)

        mode = "APPLY" if args.apply else "DRY RUN"
        print(f"=== {mode} — {len(groups)} duplicate practice groups "
              f"({sum(len(v) for _, v in groups)} Job__c) ===\n")

        writes = 0
        failures = 0
        for key, records in groups:
            winner = _pick_canonical_practice_record(records)
            losers = [r for r in records if r["Id"] != winner["Id"]]
            canonical_value = winner.get("Job_Client_Job_Id__c")
            # Prefer the spaced "NNNN - City, ST" form Kimedics actually scrapes.
            for record in records:
                value = record.get("Job_Client_Job_Id__c") or ""
                if " - " in value:
                    canonical_value = value
                    break

            # External_Job_ID__c is unique too, and the resolver matches on it BEFORE the
            # practice key — so the newest Kimedics posting must move onto the winner or
            # it keeps pushing to the record we just retired.
            jobs_here = kimedics.get(key, [])
            newest = jobs_here[-1] if jobs_here else None
            winner_ext = (winner.get("External_Job_ID__c") or "").strip() or None
            target_ext = newest["job_id"] if newest else winner_ext

            print(f"{key}")
            print(f"   KEEP   {describe(winner)}")
            for loser in losers:
                print(f"   RELEASE{describe(loser)}")
            if not jobs_here:
                print("   (no Kimedics jobs at this practice — dormant legacy pair)")
            else:
                print(f"   Kimedics jobs: {', '.join(j['job_id'] + '@' + (j['posted_iso'] or '?') for j in jobs_here)}")
                print(f"   newest = {target_ext} -> winner")
            if canonical_value != winner.get("Job_Client_Job_Id__c"):
                print(f"   RENAME winner practice value -> {canonical_value!r}")
            # Re-pointing the winner to a different Kimedics posting means its open date
            # now describes that posting. Recompute it in the same PATCH — otherwise the
            # winner keeps the retired posting's date.
            new_open_date = None
            if target_ext != winner_ext:
                print(f"   REPOINT winner External_Job_ID__c {winner_ext} -> {target_ext}")
                new_open_date = get_most_recent_open_date(conn, target_ext)
                print(f"   OPENDATE winner Job_Open_Date__c -> {new_open_date} (job {target_ext})")
            repoint = [j["job_id"] for j in jobs_here if j["sf_job_id"] != winner["Id"]]
            if repoint:
                print(f"   SUPABASE re-point sf_job_id -> {winner['Id']} for jobs {repoint}")

            if not args.apply:
                print()
                continue

            # Losers must release BOTH unique fields before the winner can claim them.
            released_ok = True
            for loser in losers:
                try:
                    update_job_record(
                        instance_url,
                        access_token,
                        "Job__c",
                        loser["Id"],
                        {
                            "Job_Client_Job_Id__c": None,
                            "External_Job_ID__c": None,
                            "Job_Status__c": "Closed",
                        },
                    )
                    writes += 1
                    print(f"   ✓ released {loser['Id']}")
                except Exception as exc:
                    released_ok = False
                    failures += 1
                    print(f"   ✗ FAILED to release {loser['Id']}: {str(exc)[:300]}")

            if not released_ok:
                print("   ! skipping winner writes — a loser still holds the unique values\n")
                continue

            winner_fields = {}
            if canonical_value != winner.get("Job_Client_Job_Id__c"):
                winner_fields["Job_Client_Job_Id__c"] = canonical_value
            if target_ext != winner_ext:
                winner_fields["External_Job_ID__c"] = target_ext
                if new_open_date:
                    winner_fields["Job_Open_Date__c"] = new_open_date
            if winner_fields:
                try:
                    update_job_record(
                        instance_url, access_token, "Job__c", winner["Id"], winner_fields
                    )
                    writes += 1
                    print(f"   ✓ winner updated {sorted(winner_fields)}")
                except Exception as exc:
                    failures += 1
                    print(f"   ✗ FAILED to update winner: {str(exc)[:300]}")
                    print()
                    continue

            for job_id in repoint:
                try:
                    update_sf_ids_for_job(
                        conn,
                        job_id=job_id,
                        sf_job_id=winner["Id"],
                        sf_worksite_account_id=winner.get("Job_Worksite_Location_1__c") or None,
                        source="practice_duplicate_consolidation_script",
                        mapping_status="resolved",
                        mapping_detail=(
                            f"Consolidated practice_key={key!r} onto canonical {winner['Id']}"
                        ),
                    )
                    print(f"   ✓ supabase re-pointed job {job_id}")
                except Exception as exc:
                    failures += 1
                    print(f"   ✗ FAILED supabase re-point for {job_id}: {str(exc)[:300]}")
            print()

        if args.apply:
            print(f"Done. {writes} Salesforce writes, {failures} failures.")
        else:
            print("Dry run only — nothing was modified. Re-run with --apply to write.")
        return 1 if failures else 0
    finally:
        cm.__exit__(None, None, None)


if __name__ == "__main__":
    raise SystemExit(main())

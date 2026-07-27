"""
One-off: correct ``Job_Open_Date__c`` on existing Job__c after the open-date rule change.

The old rule stamped the *last* day a job looked open, and misread "Active, not accepting
new providers" as open (it contains the substring "accepting new provider"). The new rule
is the first Open after the latest close — see ``most_recent_open_date_from_history``.

Only ``Job_Open_Date__c`` is written. This deliberately does NOT rebuild the full push
payload: that would re-run the description/insight AI (token spend) and could overwrite
recruiter edits on unrelated fields. ``Days_Open__c`` is a formula and self-corrects.

When several Kimedics jobs point at one Job__c, the newest posting wins — one record,
one open date.

DRY-RUN BY DEFAULT. Pass --apply to write.

    python scripts/backfill_job_open_date.py             # plan only
    python scripts/backfill_job_open_date.py --apply
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")

from utils import salesforce as sf  # noqa: E402
from utils.sf_job_payload import _mdy_to_iso, get_most_recent_open_date  # noqa: E402
from utils.sf_job_rest_minimal import update_job_record  # noqa: E402
from utils.supabase_db import get_conn  # noqa: E402

# Abort if the org's daily API budget is lower than this before/while running. The whole
# backfill is <200 calls against a 127k/day limit, so tripping this means something else
# is burning the org's budget and we must not add to it.
MIN_API_REQUESTS_REMAINING = 20_000
API_CHECK_EVERY = 50


def api_requests_remaining(instance_url: str, access_token: str) -> int:
    req = urllib.request.Request(instance_url.rstrip("/") + "/services/data/v59.0/limits")
    req.add_header("Authorization", f"Bearer {access_token}")
    with urllib.request.urlopen(req) as resp:
        data = json.load(resp)
    return int(data.get("DailyApiRequests", {}).get("Remaining", 0))


def build_plan(conn, instance_url: str, access_token: str) -> list[dict]:
    """One row per SF Job__c whose open date needs correcting (newest posting wins)."""
    current = {
        r["Id"]: r.get("Job_Open_Date__c")
        for r in sf.query_all(
            instance_url, access_token, "SELECT Id, Job_Open_Date__c FROM Job__c"
        )
    }

    with conn.cursor() as cur:
        cur.execute(
            "SELECT job_id, sf_job_id, posted_date FROM job_current WHERE sf_job_id IS NOT NULL"
        )
        rows = cur.fetchall()

    by_record: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for job_id, sf_job_id, posted_date in rows:
        by_record[sf_job_id].append((_mdy_to_iso(posted_date) or "", job_id))

    plan = []
    for sf_job_id, jobs in by_record.items():
        if sf_job_id not in current:
            continue  # record deleted in Salesforce
        newest_job_id = sorted(jobs)[-1][1]
        new_date = get_most_recent_open_date(conn, newest_job_id)
        old_date = current[sf_job_id]
        if new_date and new_date != old_date:
            plan.append(
                {
                    "sf_job_id": sf_job_id,
                    "job_id": newest_job_id,
                    "old": old_date,
                    "new": new_date,
                    "postings": len(jobs),
                }
            )
    return sorted(plan, key=lambda r: r["job_id"])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="perform the writes (default: plan only)")
    ap.add_argument("--limit", type=int, default=0, help="cap records processed (0 = no cap)")
    args = ap.parse_args()

    token = sf.get_token_auto(
        os.environ["SALESFORCE_CONSUMER_KEY"],
        os.environ["SALESFORCE_CONSUMER_SECRET"],
        token_url=os.getenv("SALESFORCE_TOKEN_URL"),
    )
    instance_url, access_token = token["instance_url"], token["access_token"]

    remaining = api_requests_remaining(instance_url, access_token)
    print(f"Salesforce DailyApiRequests remaining: {remaining:,}")
    if remaining < MIN_API_REQUESTS_REMAINING:
        print(f"ABORT: below the {MIN_API_REQUESTS_REMAINING:,} tripwire. Nothing was written.")
        return 2

    cm = get_conn()
    conn = cm.__enter__()
    try:
        plan = build_plan(conn, instance_url, access_token)
        if args.limit:
            plan = plan[: args.limit]

        mode = "APPLY" if args.apply else "DRY RUN"
        print(f"=== {mode} — {len(plan)} Job__c need Job_Open_Date__c corrected ===\n")
        for row in plan[:20]:
            extra = f"  ({row['postings']} postings)" if row["postings"] > 1 else ""
            print(f"  {row['sf_job_id']}  job {row['job_id']}  {row['old']} -> {row['new']}{extra}")
        if len(plan) > 20:
            print(f"  … and {len(plan) - 20} more")
        print()

        if not args.apply:
            print("Dry run only — nothing was modified. Re-run with --apply to write.")
            return 0

        written = failures = 0
        for i, row in enumerate(plan, 1):
            if i % API_CHECK_EVERY == 0:
                remaining = api_requests_remaining(instance_url, access_token)
                if remaining < MIN_API_REQUESTS_REMAINING:
                    print(f"ABORT at {i}/{len(plan)}: API budget fell to {remaining:,}.")
                    print("Re-running later resumes safely — finished records are skipped.")
                    break
            try:
                update_job_record(
                    instance_url,
                    access_token,
                    "Job__c",
                    row["sf_job_id"],
                    {"Job_Open_Date__c": row["new"]},
                )
                written += 1
            except Exception as exc:
                failures += 1
                print(f"  ✗ {row['sf_job_id']} (job {row['job_id']}): {str(exc)[:250]}")

        print(f"\nDone. {written} records updated, {failures} failures.")
        print(f"DailyApiRequests remaining: {api_requests_remaining(instance_url, access_token):,}")
        return 1 if failures else 0
    finally:
        cm.__exit__(None, None, None)


if __name__ == "__main__":
    raise SystemExit(main())

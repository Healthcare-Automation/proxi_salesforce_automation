"""
One-off cleanup for the two duplicate-Job__c incidents found 2026-05-19:

  Crossville, TN (Kimedics 19816):
    - Auto-created duplicate a01UP00000fQoJKYA0 (worksite 001UP00000fQjgFYAS)
    - Legacy canonical record   a015f00000KxSGRAA3 (worksite 0015f00000cQelSAAS)
    Action: re-point Supabase 19816 to the legacy SF id + worksite, delete the dupe.

  Georgetown, KY (Kimedics 19806 and 19617):
    - a015f00000cHzqTAAS  ↔ Kimedics 19806  Job_Client_Job_Id__c='419 - Georgetown, KY'  (typo: missing leading 2)
    - a01UP00000drmstYAA  ↔ Kimedics 19617  Job_Client_Job_Id__c='2419 - Georgetown, KY'
    Both Kimedics jobs are LIVE and point to legitimate SF records — they're not
    orphans. The PATCH for 19806 fails because Salesforce treats Job_Client_Job_Id__c
    as unique, and 19617's record already holds '2419 - Georgetown, KY'.
    This is a data-model/semantics problem (Client_Job_Id is a facility id, not
    a per-job id) and needs an operator decision, NOT a script delete.
    Options to resolve manually:
      (a) Clear a015f00000cHzqTAAS.Job_Client_Job_Id__c so the PATCH can land.
      (b) Drop the SF uniqueness rule on Job_Client_Job_Id__c.
      (c) Merge the two Kimedics jobs if they're actually duplicates.
    Script will only REPORT for Georgetown — no writes.

Dry-run by default. Pass --apply to actually write. PRODUCTION schema only.

Usage (from project root):
    python src/local/cleanup_duplicate_sf_jobs_2026_05_19.py
    python src/local/cleanup_duplicate_sf_jobs_2026_05_19.py --apply
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

_here = Path(__file__).resolve()
_src_root = _here.parent.parent
sys.path.insert(0, str(_src_root))


CROSSVILLE = {
    "job_id": "19816",
    "dupe_sf_job_id": "a01UP00000fQoJKYA0",
    "canonical_sf_job_id": "a015f00000KxSGRAA3",
    "dupe_worksite_id": "001UP00000fQjgFYAS",
    "canonical_worksite_id": "0015f00000cQelSAAS",
}

GEORGETOWN = {
    "job_id": "19806",
    "orphan_sf_job_id": "a01UP00000drmstYAA",
    "orphan_ext_job_id": "19617",  # the External_Job_ID__c on the orphan
}


def _sf_token():
    from utils.salesforce import get_token_auto
    ck = os.environ["SALESFORCE_CONSUMER_KEY"].strip()
    cs = os.environ["SALESFORCE_CONSUMER_SECRET"].strip()
    use_cc = os.environ.get("SALESFORCE_USE_USERNAME_PASSWORD", "").lower() not in ("1", "true", "yes")
    tok = get_token_auto(
        ck, cs,
        os.environ.get("SALESFORCE_USERNAME") or None,
        os.environ.get("SALESFORCE_PASSWORD") or None,
        use_client_credentials=use_cc,
        token_url=os.environ.get("SALESFORCE_TOKEN_URL") or None,
        security_token=os.environ.get("SALESFORCE_SECURITY_TOKEN") or None,
        use_sandbox=(os.environ.get("SALESFORCE_USE_SANDBOX", "").lower() in ("1", "true", "yes")),
    )
    return tok["instance_url"], tok["access_token"]


def _sf_delete(instance_url: str, access_token: str, record_id: str) -> None:
    from utils.sf_job_rest_minimal import rest_json
    rest_json(instance_url, access_token, "DELETE", f"sobjects/Job__c/{record_id}")


def _check_orphan_safe_to_delete(conn, orphan_ext_id: str) -> tuple[bool, str]:
    """Return (safe, reason). Safe means no live Kimedics job references the orphan."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT job_id FROM job_current WHERE job_id = %s LIMIT 1",
            (orphan_ext_id,),
        )
        if cur.fetchone():
            return False, f"job_current has a row for job_id={orphan_ext_id}; orphan may be canonical for that job"
        cur.execute(
            "SELECT job_id FROM job_content WHERE job_id = %s LIMIT 1",
            (orphan_ext_id,),
        )
        if cur.fetchone():
            return False, f"job_content has rows for job_id={orphan_ext_id}; orphan may be canonical for that job"
    return True, "no Kimedics job references the orphan"


def _crossville_repoint(conn, apply: bool, instance_url: str, access_token: str) -> None:
    c = CROSSVILLE
    print(f"\n── Crossville (job_id={c['job_id']}) ──")
    with conn.cursor() as cur:
        cur.execute(
            "SELECT sf_job_id, sf_worksite_account_id FROM job_current WHERE job_id = %s",
            (c["job_id"],),
        )
        row = cur.fetchone()
        if not row:
            print(f"  job_current row missing for {c['job_id']} — abort")
            return
        cur_sf, cur_ws = row
        print(f"  job_current now:    sf_job_id={cur_sf!r} sf_worksite_account_id={cur_ws!r}")
        print(f"  will re-point to:   sf_job_id={c['canonical_sf_job_id']!r} sf_worksite_account_id={c['canonical_worksite_id']!r}")
        print(f"  will SF-delete:     {c['dupe_sf_job_id']!r}  (Job__c, the auto-created duplicate)")

        cur.execute(
            "SELECT count(*) FROM job_content WHERE job_id = %s AND sf_job_id = %s",
            (c["job_id"], c["dupe_sf_job_id"]),
        )
        n_history = cur.fetchone()[0]
        print(f"  history rows to rewrite (job_content): {n_history}")

    if not apply:
        print("  [dry-run] no changes written")
        return

    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE job_current
               SET sf_job_id = %s,
                   sf_worksite_account_id = %s
             WHERE job_id = %s
            """,
            (c["canonical_sf_job_id"], c["canonical_worksite_id"], c["job_id"]),
        )
        cur.execute(
            """
            UPDATE job_content
               SET sf_job_id = %s,
                   sf_worksite_account_id = %s
             WHERE job_id = %s AND sf_job_id = %s
            """,
            (c["canonical_sf_job_id"], c["canonical_worksite_id"], c["job_id"], c["dupe_sf_job_id"]),
        )
    conn.commit()
    print("  Supabase re-pointed.")

    try:
        _sf_delete(instance_url, access_token, c["dupe_sf_job_id"])
        print(f"  Deleted SF Job__c {c['dupe_sf_job_id']}.")
    except Exception as e:
        print(f"  WARNING: SF delete failed: {e}")
        print("           Supabase has been re-pointed; you can retry the SF delete manually.")


def _georgetown_report(conn, apply: bool, instance_url: str, access_token: str) -> None:
    g = GEORGETOWN
    print(f"\n── Georgetown (job_id={g['job_id']}) ──")
    safe, reason = _check_orphan_safe_to_delete(conn, g["orphan_ext_job_id"])
    print(f"  safety check: {reason}")
    if not safe:
        print(
            f"  {g['orphan_sf_job_id']} is NOT an orphan — Kimedics {g['orphan_ext_job_id']} "
            f"is a live job that maps to it. Do not delete."
        )
        print("  The PATCH for 19806 is colliding because two separate Kimedics jobs")
        print("  legitimately share the same client facility code. This is a schema/")
        print("  semantics decision (see this file's docstring for options). No script writes.")
        return
    # If we ever flipped to "safe", we still wouldn't auto-delete here — leave to operator.
    print(f"  (Safety would allow delete, but Georgetown path is report-only by design.)")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Actually write changes. Default is dry-run.")
    parser.add_argument("--skip-crossville", action="store_true")
    parser.add_argument("--skip-georgetown", action="store_true")
    args = parser.parse_args()

    from dotenv import load_dotenv
    load_dotenv(_src_root.parent / ".env")

    from utils.supabase_db import get_conn

    if args.apply:
        ans = input("Type PRODUCTION to confirm writes: ").strip()
        if ans != "PRODUCTION":
            print("Aborted.")
            return 1

    instance_url, access_token = _sf_token() if args.apply else ("", "")

    with get_conn() as conn:
        if conn is None:
            print("Could not connect to Supabase.")
            return 1
        if not args.skip_crossville:
            _crossville_repoint(conn, args.apply, instance_url, access_token)
        if not args.skip_georgetown:
            _georgetown_report(conn, args.apply, instance_url, access_token)

    print("\nDone." if args.apply else "\nDry-run complete. Pass --apply to write.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

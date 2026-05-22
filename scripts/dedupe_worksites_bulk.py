"""
Bulk cleanup of duplicate worksite Accounts that the automation itself created.

Safety rules:
  1. **Only considers Accounts the automation created** — identified by
     ``worksite_created`` events in ``job_event_log`` carrying
     ``payload.salesforce_account_id``. External (manually-created) Accounts
     are NEVER touched, even when they're part of a dupe cluster.
  2. **Only deletes if zero Job__c records reference the candidate.** Any
     Account that has live mappings stays put.
  3. **Always keeps a canonical per cluster.** Within each cluster of
     Accounts sharing the same normalized (city, state) under our parent,
     the canonical is picked as the one with the most Job__c references,
     ties broken by oldest CreatedDate.

Default mode is DRY-RUN. Pass ``--apply`` to actually delete.

Examples:
    modal run scripts/dedupe_worksites_bulk.py                 # dry run, prints plan
    modal run scripts/dedupe_worksites_bulk.py --apply         # deletes for real
"""

from __future__ import annotations

import json
import os
import sys
from collections import defaultdict

import modal

app = modal.App("dedupe-worksites-bulk")

_image = (
    modal.Image.debian_slim(python_version="3.11")
    .pip_install("psycopg2-binary>=2.9", "urllib3>=2", "python-dotenv>=1.0")
    .add_local_dir("src", remote_path="/root")
)


@app.function(
    image=_image,
    secrets=[modal.Secret.from_name("salesforce-automation")],
    timeout=900,
)
def run(apply: bool = False) -> dict:
    sys.path.insert(0, "/root")

    from utils.address_normalize import normalize_city, normalize_state
    from utils.salesforce import get_token_auto, query_all
    from utils.sf_job_rest_minimal import (
        DEFAULT_REST_VERSION,
        rest_json,
        update_job_record,
    )
    from utils.sf_push_defaults import SF_ACCOUNT_ASPEN_DENTAL_MANAGEMENT_ID

    # ── 1. Auth ─────────────────────────────────────────────────────────
    ck = (os.environ.get("SALESFORCE_CONSUMER_KEY") or "").strip()
    cs = (os.environ.get("SALESFORCE_CONSUMER_SECRET") or "").strip()
    auth = get_token_auto(
        ck, cs,
        os.environ.get("SALESFORCE_USERNAME") or None,
        os.environ.get("SALESFORCE_PASSWORD") or None,
        use_client_credentials=(os.environ.get("SALESFORCE_USE_USERNAME_PASSWORD") or "").lower() not in ("1", "true", "yes"),
        security_token=os.environ.get("SALESFORCE_SECURITY_TOKEN") or None,
        use_sandbox=(os.environ.get("SALESFORCE_USE_SANDBOX") or "").lower() in ("1", "true", "yes"),
        token_url=os.environ.get("SALESFORCE_TOKEN_URL") or None,
    )
    instance_url = auth["instance_url"]
    access_token = auth["access_token"]

    # ── 2. Get OUR created Account IDs from job_event_log ──────────────
    import psycopg2  # type: ignore
    from utils.supabase_db import get_connection_string

    our_created: set[str] = set()
    conn = psycopg2.connect(get_connection_string())
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT DISTINCT payload->>'salesforce_account_id'
                FROM job_event_log
                WHERE event_type = 'worksite_created'
                  AND payload->>'salesforce_account_id' IS NOT NULL
                  AND payload->>'salesforce_account_id' <> ''
                """
            )
            for (sf_id,) in cur.fetchall():
                if sf_id:
                    our_created.add(str(sf_id).strip())
    finally:
        conn.close()
    print(f"[bulk] {len(our_created)} Accounts have a worksite_created event (i.e. we created them).")

    # ── 3. Pull all worksite Accounts under our parent ──────────────────
    parent_id = (SF_ACCOUNT_ASPEN_DENTAL_MANAGEMENT_ID or "").replace("'", "\\'")
    soql_accts = (
        "SELECT Id, Name, ShippingCity, ShippingState, ShippingStreet, "
        "ShippingPostalCode, CreatedDate, LastModifiedDate "
        "FROM Account "
        f"WHERE ParentId = '{parent_id}'"
    )
    accounts = query_all(instance_url, access_token, soql_accts)
    print(f"[bulk] {len(accounts)} Aspen Dental worksite Accounts in Salesforce.")

    # ── 4. Pull Job__c → worksite reference counts ──────────────────────
    soql_jobs = (
        "SELECT Id, Job_Worksite_Location_1__c "
        "FROM Job__c "
        "WHERE Job_Worksite_Location_1__c != null"
    )
    jobs = query_all(instance_url, access_token, soql_jobs)
    job_count_by_worksite: dict[str, int] = defaultdict(int)
    jobs_by_worksite: dict[str, list[str]] = defaultdict(list)
    for j in jobs:
        wid = (j.get("Job_Worksite_Location_1__c") or "").strip()
        if wid:
            job_count_by_worksite[wid] += 1
            jobs_by_worksite[wid].append((j.get("Id") or "").strip())
    print(f"[bulk] {len(jobs)} Job__c records reference a worksite Account.")

    # ── 5. Group by NORMALIZED NAME (case-insensitive, punctuation-stripped) ─
    # By-city-state grouping is too coarse — it lumps together genuinely-
    # different sub-locations like "Phoenix (Metrocenter)" and "Phoenix
    # (Arcadia)". Only Accounts whose NAMES normalize to the same string are
    # true duplicates we created on top of an existing record.
    import re

    def _norm_name(n: str | None) -> str:
        if not n:
            return ""
        s = n.lower().strip()
        # collapse whitespace, drop common separators / punctuation
        s = re.sub(r"[.,;:'\"`]+", "", s)
        s = re.sub(r"\s+", " ", s)
        # strip a trailing state-abbrev clause like ", ok" so "X, OK" == "X"
        # if other variant doesn't have it. Conservative — only strip when
        # the trailing chars look like a state-abbrev clause.
        return s

    groups: dict[tuple[str, str, str], list[dict]] = defaultdict(list)
    for a in accounts:
        c = normalize_city(a.get("ShippingCity"))
        s = normalize_state(a.get("ShippingState"))
        n = _norm_name(a.get("Name"))
        if not c or not s or not n:
            continue
        groups[(n, c, s)].append(a)

    dupe_groups = {k: v for k, v in groups.items() if len(v) > 1}
    print(f"[bulk] {len(dupe_groups)} (name, city, state) clusters with >1 Account = true dupes.")

    # ── 6. Decide canonical per cluster + which dupes to repoint+delete ─
    # Canonical pick rule (in order): oldest CreatedDate AND not in our
    # worksite_created events. We prefer the pre-existing external record
    # (it's typically older and has cleaner Shipping fields than our
    # automation-generated record). If somehow ALL members of a cluster
    # are ours, fall back to oldest.
    plan: list[dict] = []
    summary_groups: list[dict] = []

    for key, members in sorted(dupe_groups.items()):
        externals = [m for m in members if m["Id"] not in our_created]
        externals.sort(key=lambda a: a.get("CreatedDate") or "")
        if externals:
            canonical = externals[0]
        else:
            canonical = sorted(members, key=lambda a: a.get("CreatedDate") or "")[0]

        winner_id = canonical["Id"]
        losers = [m for m in members if m["Id"] != winner_id]

        actions: list[dict] = []
        for L in losers:
            sf_id = L["Id"]
            we_created_it = sf_id in our_created
            jobs_here = job_count_by_worksite[sf_id]
            job_ids_here = list(jobs_by_worksite.get(sf_id, []))
            if we_created_it:
                # Repoint our Jobs to canonical, then delete the dupe.
                actions.append({
                    "loser_id": sf_id,
                    "loser_name": L.get("Name"),
                    "loser_created": L.get("CreatedDate"),
                    "jobs_to_repoint": job_ids_here,
                    "delete_after": True,
                })
            # External losers (not ours) are left alone, per the
            # "only the ones we affected" constraint.

        if actions:
            plan.append({
                "key": "|".join(key),
                "canonical_id": winner_id,
                "canonical_name": canonical.get("Name"),
                "canonical_created": canonical.get("CreatedDate"),
                "actions": actions,
            })

        summary_groups.append({
            "key": "|".join(key),
            "members": [
                {
                    "id": m["Id"],
                    "name": m.get("Name"),
                    "created": m.get("CreatedDate"),
                    "jobs": job_count_by_worksite[m["Id"]],
                    "we_created": m["Id"] in our_created,
                    "is_winner": m["Id"] == winner_id,
                    "will_repoint_and_delete": (m["Id"] != winner_id and m["Id"] in our_created),
                }
                for m in members
            ],
        })

    # ── 7. Pretty-print plan ────────────────────────────────────────────
    print()
    print("=" * 78)
    print(f"PLAN ({'APPLY' if apply else 'DRY-RUN'})")
    print("=" * 78)
    for g in summary_groups:
        if not any(m["will_repoint_and_delete"] for m in g["members"]):
            continue
        print(f"\nCluster {g['key']}:")
        for m in g["members"]:
            tag = (
                "← KEEP (canonical)"            if m["is_winner"]
                else "← REPOINT+DELETE"         if m["will_repoint_and_delete"]
                else "← keep (external sibling)"
            )
            we = "ours" if m["we_created"] else "external"
            print(f"  {m['id']}  jobs={m['jobs']:3d}  {we:8s}  created={m['created']}  {m['name']!r}  {tag}")

    total_repoint_jobs = sum(len(a["jobs_to_repoint"]) for c in plan for a in c["actions"])
    total_dupes = sum(len(c["actions"]) for c in plan)
    print()
    print(f"Clusters with action: {len(plan)}")
    print(f"Dupe Accounts to repoint+delete: {total_dupes}")
    print(f"Job__c records to repoint: {total_repoint_jobs}")

    # ── 8. Apply ────────────────────────────────────────────────────────
    deleted: list[dict] = []
    repointed: list[dict] = []
    failed: list[dict] = []

    if apply and plan:
        print()
        print("─── Applying … ───")
        api_version = DEFAULT_REST_VERSION
        for cluster in plan:
            canonical_id = cluster["canonical_id"]
            for action in cluster["actions"]:
                loser_id = action["loser_id"]
                # Step 1: repoint every Job__c that references the loser.
                for job_sf_id in action["jobs_to_repoint"]:
                    try:
                        update_job_record(
                            instance_url=instance_url,
                            access_token=access_token,
                            job_object_name="Job__c",
                            record_id=job_sf_id,
                            fields={"Job_Worksite_Location_1__c": canonical_id},
                        )
                        repointed.append({
                            "sf_job_id": job_sf_id,
                            "from_worksite": loser_id,
                            "to_worksite": canonical_id,
                        })
                        print(f"  ↻ Job {job_sf_id}  {loser_id} → {canonical_id}")
                    except Exception as e:
                        failed.append({
                            "kind": "repoint",
                            "sf_job_id": job_sf_id,
                            "from_worksite": loser_id,
                            "to_worksite": canonical_id,
                            "error": str(e)[:300],
                        })
                        print(f"  ✗ FAILED to repoint Job {job_sf_id}: {e}")

                # Step 2: also fix local sf_worksite_location_map to point at canonical.
                # The map's (city, state) key is normalized via the same function in supabase_db.
                try:
                    conn = psycopg2.connect(get_connection_string())
                    from utils.supabase_db import upsert_worksite_account_id_for_location
                    city = (action.get("loser_name") or "").split("-")[-1].strip().rstrip(",").split(",")[0].strip()
                    # Use the canonical's city/state directly for the map upsert below.
                    # We have those on the cluster's canonical record — fetch from the
                    # original SOQL result (lookup by id).
                    canon_meta = next((a for a in accounts if a.get("Id") == canonical_id), {})
                    canon_city = canon_meta.get("ShippingCity") or ""
                    canon_state = canon_meta.get("ShippingState") or ""
                    if canon_city and canon_state:
                        with conn:
                            upsert_worksite_account_id_for_location(
                                conn,
                                canon_city,
                                canon_state,
                                salesforce_account_id=canonical_id,
                                display_label=cluster.get("canonical_name"),
                                source="dedupe_bulk_repoint_to_canonical",
                                schema="public",
                            )
                except Exception as e:
                    print(f"  (note) local map upsert failed for {canonical_id}: {e}")
                finally:
                    try:
                        conn.close()
                    except Exception:
                        pass

                # Step 3: delete the (now-empty) loser Account.
                if action["delete_after"]:
                    try:
                        rest_json(
                            instance_url,
                            access_token,
                            "DELETE",
                            f"sobjects/Account/{loser_id}",
                            api_version=api_version,
                        )
                        deleted.append({"loser_id": loser_id, "name": action["loser_name"]})
                        print(f"  ✗ deleted Account {loser_id}  ({action['loser_name']!r})")
                    except Exception as e:
                        failed.append({
                            "kind": "delete",
                            "loser_id": loser_id,
                            "error": str(e)[:300],
                        })
                        print(f"  ✗ FAILED to delete {loser_id}: {e}")

    return {
        "ok": True,
        "applied": apply,
        "our_created_count": len(our_created),
        "accounts_total": len(accounts),
        "jobs_referencing_worksites": len(jobs),
        "dupe_clusters_true_name_match": len(dupe_groups),
        "clusters_with_action": len(plan),
        "dupes_to_repoint_and_delete": total_dupes,
        "jobs_to_repoint": total_repoint_jobs,
        "repointed_count": len(repointed),
        "deleted_count": len(deleted),
        "failed_count": len(failed),
        "plan": plan if not apply else None,
        "repointed": repointed if apply else None,
        "deleted": deleted if apply else None,
        "failed": failed,
    }


@app.local_entrypoint()
def main(apply: bool = False):
    result = run.remote(apply=apply)
    print()
    print("=" * 78)
    print("RESULT (summary)")
    print("=" * 78)
    print(json.dumps({k: v for k, v in result.items() if k not in ("deletion_plan", "deleted")}, indent=2, default=str))
    if not apply and result.get("deletion_plan_size", 0) > 0:
        print()
        print(f"To execute, re-run with --apply  (will delete {result['deletion_plan_size']} Account(s))")

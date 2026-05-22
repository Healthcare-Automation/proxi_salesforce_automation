"""
One-shot Modal script to clean up the duplicate Muskogee worksite Account
created on 2026-05-21 (run #1178 of the manual rescrape that happened before
the SOQL-probe fix landed).

Action:
    1. SOQL search for ALL Accounts under Aspen Dental Management whose Name
       starts with 'Aspen Dental - Muskogee'.
    2. Pick the canonical one = oldest CreatedDate (almost certainly the
       12/2/2025 account).
    3. For every Job__c that references any *other* matching Account in
       Job_Worksite_Location_1__c, PATCH it to point at the canonical.
    4. Update sf_worksite_location_map locally to point at the canonical.
    5. DO NOT delete the dupe Account in SF (we don't have a strong reason to
       trust we have delete permission, and an admin can clean up the empty
       record manually now that nothing references it).

Run with:
    modal run scripts/dedupe_muskogee_worksite.py

This is a one-off; the SOQL-probe fix already deployed prevents future
duplicates of this shape.
"""

from __future__ import annotations

import os
import sys

import modal

app = modal.App("dedupe-muskogee-worksite")

_image = (
    modal.Image.debian_slim(python_version="3.11")
    .pip_install("psycopg2-binary>=2.9", "urllib3>=2", "python-dotenv>=1.0")
    .add_local_dir("src", remote_path="/root")
)


@app.function(
    image=_image,
    secrets=[modal.Secret.from_name("salesforce-automation")],
    timeout=300,
)
def run() -> dict:
    sys.path.insert(0, "/root")

    from utils.address_normalize import normalize_city, normalize_state
    from utils.salesforce import get_token_auto, query_all
    from utils.sf_job_rest_minimal import update_job_record
    from utils.sf_push_defaults import SF_ACCOUNT_ASPEN_DENTAL_MANAGEMENT_ID

    ck = (os.environ.get("SALESFORCE_CONSUMER_KEY") or "").strip()
    cs = (os.environ.get("SALESFORCE_CONSUMER_SECRET") or "").strip()
    user = os.environ.get("SALESFORCE_USERNAME") or None
    pw = os.environ.get("SALESFORCE_PASSWORD") or None
    tok = os.environ.get("SALESFORCE_SECURITY_TOKEN") or None
    sandbox = (os.environ.get("SALESFORCE_USE_SANDBOX") or "").lower() in ("1", "true", "yes")
    token_url = os.environ.get("SALESFORCE_TOKEN_URL") or None
    use_cc = (os.environ.get("SALESFORCE_USE_USERNAME_PASSWORD") or "").lower() not in ("1", "true", "yes")

    auth = get_token_auto(
        ck, cs, user, pw,
        use_client_credentials=use_cc,
        security_token=tok,
        use_sandbox=sandbox,
        token_url=token_url,
    )
    instance_url = auth["instance_url"]
    access_token = auth["access_token"]

    # ── Step 1: list candidates ────────────────────────────────────────────
    parent_id = SF_ACCOUNT_ASPEN_DENTAL_MANAGEMENT_ID.replace("'", "\\'")
    soql = (
        "SELECT Id, Name, ShippingCity, ShippingState, ShippingStreet, "
        "ShippingPostalCode, CreatedDate, LastModifiedDate "
        "FROM Account "
        f"WHERE ParentId = '{parent_id}' "
        "AND Name LIKE 'Aspen Dental - Muskogee%' "
        "ORDER BY CreatedDate ASC"
    )
    candidates = query_all(instance_url, access_token, soql)
    print(f"[dedupe] found {len(candidates)} candidate Muskogee Accounts under parent:")
    for c in candidates:
        print(f"  - {c.get('Id')}  {c.get('Name')!r}  state={c.get('ShippingState')!r}  "
              f"city={c.get('ShippingCity')!r}  street={c.get('ShippingStreet')!r}  "
              f"created={c.get('CreatedDate')}")

    if len(candidates) < 2:
        return {"ok": True, "noop": True, "found": len(candidates), "message": "no duplicates"}

    # Keep the oldest by CreatedDate (the query is already ORDER BY ASC, so [0])
    canonical = candidates[0]
    dupes = candidates[1:]
    canonical_id = canonical["Id"]
    print(f"[dedupe] canonical = {canonical_id}  ({canonical['Name']!r})")
    print(f"[dedupe] dupes     = {[d['Id'] for d in dupes]}")

    # Sanity: all candidates should share the same normalized (city, state).
    canon_city = normalize_city(canonical.get("ShippingCity"))
    canon_state = normalize_state(canonical.get("ShippingState"))
    for d in dupes:
        d_city = normalize_city(d.get("ShippingCity"))
        d_state = normalize_state(d.get("ShippingState"))
        if d_city != canon_city or d_state != canon_state:
            print(f"[dedupe] WARNING: {d['Id']} city/state mismatch — skipping safety")
            return {"ok": False, "error": f"city/state mismatch on {d['Id']}", "candidates": candidates}

    # ── Step 2: find Job__c records pointing at any dupe ─────────────────
    dupe_ids = [d["Id"] for d in dupes]
    in_clause = ", ".join(f"'{i}'" for i in dupe_ids)
    jobs_soql = (
        "SELECT Id, Name, Job_Worksite_Location_1__c, External_Job_ID__c "
        "FROM Job__c "
        f"WHERE Job_Worksite_Location_1__c IN ({in_clause})"
    )
    jobs = query_all(instance_url, access_token, jobs_soql)
    print(f"[dedupe] {len(jobs)} Job__c record(s) reference dupe(s)")
    for j in jobs:
        print(f"  - {j['Id']}  ext_id={j.get('External_Job_ID__c')!r}  "
              f"current_worksite={j.get('Job_Worksite_Location_1__c')!r}")

    # ── Step 3: repoint Jobs to canonical ────────────────────────────────
    repointed: list[dict] = []
    for j in jobs:
        try:
            update_job_record(
                instance_url=instance_url,
                access_token=access_token,
                job_object_name="Job__c",
                record_id=j["Id"],
                fields={"Job_Worksite_Location_1__c": canonical_id},
            )
            repointed.append({"sf_job_id": j["Id"], "ext_id": j.get("External_Job_ID__c")})
            print(f"  ↳ repointed {j['Id']} → {canonical_id}")
        except Exception as e:
            print(f"  ↳ FAILED to repoint {j['Id']}: {e}")

    # ── Step 4: update local map ─────────────────────────────────────────
    import psycopg2  # type: ignore
    from utils.supabase_db import (
        get_connection_string,
        upsert_worksite_account_id_for_location,
    )
    conn = psycopg2.connect(get_connection_string())
    try:
        with conn:
            upsert_worksite_account_id_for_location(
                conn,
                canonical.get("ShippingCity") or "Muskogee",
                canonical.get("ShippingState") or "OK",
                salesforce_account_id=canonical_id,
                display_label=canonical.get("Name"),
                source="dedupe_repoint_to_canonical",
                schema="public",
            )
        print(f"[dedupe] updated sf_worksite_location_map → {canonical_id}")
    finally:
        conn.close()

    return {
        "ok": True,
        "canonical_id": canonical_id,
        "dupe_ids": dupe_ids,
        "jobs_repointed": len(repointed),
        "repointed": repointed,
        "note": (
            "Dupe Accounts left in SF (no delete attempted). Now that no Job__c "
            "references them, an admin can deactivate / delete in the SF UI."
        ),
    }


@app.local_entrypoint()
def main():
    result = run.remote()
    print()
    print("=" * 60)
    print("RESULT:")
    import json
    print(json.dumps(result, indent=2, default=str))

"""
Fill missing ``sf_job_id`` / ``sf_worksite_account_id`` on Supabase after a Kimedics scrape.

Order (matches manual resolver / agreed workflow):
1. Skip if ``job_current`` already has both ids.
2. Merge from newest ``job_content`` rows that have either id (carry forward partial cache).
3. If still missing either id, 1:1 Salesforce match on normalized ``practice_value`` ↔ ``Job_Client_Job_Id__c``;
   take ``Job__c.Id`` and ``Job_Worksite_Location_1__c``.
4. If practice is missing or practice match finds 0 / N hits, try **Kimedics ``job_id`` ↔ ``External_Job_ID__c``**
   (same truncation as pushes). Still 1:1 only.

Does not create Salesforce records (that stays in the “new job” path elsewhere).
"""

from __future__ import annotations

import os
from typing import Sequence, Optional

from utils.sf_practice_key import practice_key


def resolve_sf_ids_for_job_ids(
    conn,
    job_ids: Sequence[str],
    *,
    schema: str = "public",
    run_id: Optional[int] = None,
) -> int:
    """
    For each Kimedics ``job_id``, upsert missing SF ids via cache → history → practice match.

    Returns the number of jobs for which ``update_sf_ids_for_job`` was invoked (may include no-ops).
    """
    from utils.salesforce import pull_jobs_for_id_resolve
    from utils.supabase_db import get_job_current, get_job_content, log_job_event, update_sf_ids_for_job

    ids = sorted({str(x or "").strip() for x in job_ids if str(x or "").strip()})
    if not ids or conn is None:
        return 0

    ck = (os.environ.get("SALESFORCE_CONSUMER_KEY") or "").strip()
    cs = (os.environ.get("SALESFORCE_CONSUMER_SECRET") or "").strip()
    if not ck or not cs:
        for jid in ids:
            log_job_event(
                conn,
                job_id=jid,
                event_type="sf_mapping_skipped",
                schema=schema,
                run_id=run_id,
                payload={"reason": "missing_SALESFORCE_CONSUMER_KEY_or_SECRET"},
            )
        return 0

    token_url = os.environ.get("SALESFORCE_TOKEN_URL") or "https://proxi.my.salesforce.com"
    use_sandbox = os.environ.get("SALESFORCE_USE_SANDBOX", "").lower() in ("1", "true", "yes")
    use_cc = os.environ.get("SALESFORCE_USE_USERNAME_PASSWORD", "").lower() not in ("1", "true", "yes")

    try:
        sf_jobs = pull_jobs_for_id_resolve(
            consumer_key=ck,
            consumer_secret=cs,
            username=os.environ.get("SALESFORCE_USERNAME") or None,
            password=os.environ.get("SALESFORCE_PASSWORD") or None,
            use_client_credentials=use_cc,
            token_url=token_url,
            security_token=os.environ.get("SALESFORCE_SECURITY_TOKEN") or None,
            use_sandbox=use_sandbox,
        )
    except Exception as e:
        err = str(e)[:2000]
        for jid in ids:
            log_job_event(
                conn,
                job_id=jid,
                event_type="sf_mapping_pull_failed",
                schema=schema,
                run_id=run_id,
                payload={"error": err},
            )
        return 0

    sf_by_id = {j["Id"]: j for j in sf_jobs}
    sf_by_practice: dict[str, set[str]] = {}
    for j in sf_jobs:
        key = practice_key(j.get("Job_Client_Job_Id__c"))
        if key:
            sf_by_practice.setdefault(key, set()).add(j["Id"])

    cur_rows = get_job_current(conn, job_ids=ids, limit=None, schema=schema)
    cur_by_job = {str(r.get("job_id") or "").strip(): r for r in cur_rows}

    updated = 0
    for jid in ids:
        row = cur_by_job.get(jid) or {}
        w_cur = (row.get("sf_worksite_account_id") or "").strip()
        j_cur = (row.get("sf_job_id") or "").strip() if row.get("sf_job_id") is not None else ""
        if w_cur and j_cur:
            log_job_event(
                conn,
                job_id=jid,
                event_type="mapping_cache_hit",
                schema=schema,
                run_id=run_id,
                payload={"source": "job_current", "sf_job_id": j_cur, "sf_worksite_account_id": w_cur},
            )
            continue

        w, j = w_cur, j_cur
        hist = get_job_content(conn, limit=100, schema=schema, job_ids=[jid])
        for h in hist:
            hw = (h.get("sf_worksite_account_id") or "").strip()
            hj = (h.get("sf_job_id") or "").strip() if h.get("sf_job_id") is not None else ""
            if hw or hj:
                w = w or hw
                j = j or hj
                break

        if w and j:
            update_sf_ids_for_job(
                conn,
                job_id=jid,
                sf_job_id=j or None,
                sf_worksite_account_id=w or None,
                source="job_content_history",
                mapping_status="resolved",
                mapping_detail="filled from job_content history",
                run_id=run_id,
                schema=schema,
            )
            updated += 1
            continue

        practice_raw = (row.get("practice_value") or "").strip()
        if not practice_raw:
            for h in hist:
                pv = (h.get("practice_value") or "").strip()
                if pv:
                    practice_raw = pv
                    break

        p = practice_key(practice_raw)
        hits: list[str] = []
        match_source = ""
        if p:
            hits = sorted(sf_by_practice.get(p, set()))
            match_source = "sf_practice_match"

        if len(hits) == 1:
            sfid = hits[0]
            wid = (sf_by_id.get(sfid, {}).get("Job_Worksite_Location_1__c") or "").strip() or None
            j_final = j or sfid
            w_final = w or wid
            update_sf_ids_for_job(
                conn,
                job_id=jid,
                sf_job_id=j_final or None,
                sf_worksite_account_id=w_final or None,
                source=match_source,
                mapping_status="resolved",
                mapping_detail="1:1 practice match",
                run_id=run_id,
                schema=schema,
            )
            updated += 1
            continue

        if len(hits) > 1:
            log_job_event(
                conn,
                job_id=jid,
                event_type="mapping_ambiguous",
                schema=schema,
                run_id=run_id,
                payload={
                    "source": "sf_practice_match",
                    "practice_key": p or None,
                    "practice_raw": practice_raw[:500] if practice_raw else None,
                    "hits": len(hits),
                    "candidate_sf_job_ids": hits,
                },
            )
            continue

        # ── AI fallback (only when deterministic matching found 0 hits) ──────
        # Triggered for cases like apostrophes ("St. Joseph" vs "St. Joseph's")
        # or extra suffixes ("Suffolk, VA" vs "Suffolk, VA- Downtown").
        ai_result = None
        if practice_raw:
            try:
                from utils.sf_ai_matcher import ai_match_practice
                ai_result = ai_match_practice(practice_raw, sf_jobs)
            except Exception as ai_exc:
                print(f"[resolve] AI match error for job {jid}: {ai_exc}")

        if ai_result is not None:
            wid = (
                sf_by_id.get(ai_result.matched_sf_job_id, {})
                .get("Job_Worksite_Location_1__c") or ""
            ).strip() or None
            j_final = j or ai_result.matched_sf_job_id
            w_final = w or wid
            update_sf_ids_for_job(
                conn,
                job_id=jid,
                sf_job_id=j_final or None,
                sf_worksite_account_id=w_final or None,
                source="sf_ai_match",
                mapping_status="resolved",
                mapping_detail=(
                    f"AI matched (confidence={ai_result.confidence}): "
                    f"{practice_raw!r} → {ai_result.matched_sf_value!r}"
                ),
                run_id=run_id,
                schema=schema,
            )
            log_job_event(
                conn,
                job_id=jid,
                event_type="mapping_ai_match",
                schema=schema,
                run_id=run_id,
                payload={
                    "kimedics_practice": practice_raw[:200] if practice_raw else None,
                    "sf_matched_value":  ai_result.matched_sf_value,
                    "sf_job_id":         ai_result.matched_sf_job_id,
                    "confidence":        ai_result.confidence,
                    "candidates_seen":   ai_result.candidates_seen,
                },
            )
            updated += 1
            continue

        # True no-match — deterministic and AI both failed.
        log_job_event(
            conn,
            job_id=jid,
            event_type="mapping_no_match",
            schema=schema,
            run_id=run_id,
            payload={
                "practice_key": p or None,
                "practice_raw": practice_raw[:500] if practice_raw else None,
                "practice_hits": len(hits) if p else 0,
                "sf_jobs_indexed": len(sf_jobs),
                "ai_attempted": bool(practice_raw),
            },
        )

    return updated

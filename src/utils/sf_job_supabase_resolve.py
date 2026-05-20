"""
Fill missing ``sf_job_id`` / ``sf_worksite_account_id`` on Supabase after a Kimedics scrape.

Order:
1. Skip if ``job_current`` already has both ``sf_job_id`` and ``sf_worksite_account_id``.
2. Merge from newest ``job_content`` with both ids (history carry-forward).
3. **External Job ID** match: Kimedics ``job_id`` ↔ ``External_Job_ID__c`` (1:1 only; same truncation as push).
   Primary when many jobs already carry ``External_Job_ID__c`` in Salesforce (avoids wrong practice / Client Job Id typos).
4. **Practice** match: normalized ``practice_value`` ↔ ``Job_Client_Job_Id__c`` (1:1 only; N>1 → ambiguous, no update).
   Used when external id is absent or unmatched in the pulled SF snapshot.
5. **AI** fallback on practice string when practice key had 0 hits.
6. **No match**: log ``mapping_no_match``; if ``PROXI_SF_CREATE_JOBS=true`` and ``PROXI_SF_UPDATE_JOBS``
   is not false, **POST** a new ``Job__c`` (needs a worksite Account Id from ``sf_worksite_location_map``
   or ``PROXI_SF_CREATE_WORKSITES=true``).

Before POST, a **direct SOQL** by ``External_Job_ID__c`` runs so an existing ``Job__c`` (e.g. after
Supabase was reset but Salesforce was not) **re-links** instead of duplicate-create failing.

Does not create jobs when mapping is ambiguous (1:N).
"""

from __future__ import annotations

import os
from typing import Sequence, Optional

from utils.sf_practice_key import practice_key
from utils.sf_write_flags import proxi_sf_writes_enabled


def _env_truthy(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes")


def _sf_rest_token() -> Optional[tuple[str, str]]:
    ck = (os.environ.get("SALESFORCE_CONSUMER_KEY") or "").strip()
    cs = (os.environ.get("SALESFORCE_CONSUMER_SECRET") or "").strip()
    if not ck or not cs:
        return None
    from utils.salesforce import get_token_auto

    token_url = os.environ.get("SALESFORCE_TOKEN_URL") or None
    use_sandbox = os.environ.get("SALESFORCE_USE_SANDBOX", "").lower() in ("1", "true", "yes")
    use_cc = os.environ.get("SALESFORCE_USE_USERNAME_PASSWORD", "").lower() not in ("1", "true", "yes")
    token = get_token_auto(
        ck,
        cs,
        os.environ.get("SALESFORCE_USERNAME") or None,
        os.environ.get("SALESFORCE_PASSWORD") or None,
        use_client_credentials=use_cc,
        token_url=token_url,
        security_token=os.environ.get("SALESFORCE_SECURITY_TOKEN") or None,
        use_sandbox=use_sandbox,
    )
    instance_url = (token.get("instance_url") or "").strip()
    access_token = (token.get("access_token") or "").strip()
    if not instance_url or not access_token:
        return None
    return instance_url, access_token


def _try_create_sf_job_after_no_match(
    conn,
    *,
    job_id: str,
    row: dict,
    schema: str,
    run_id: Optional[int],
    sf_jobs: Optional[list[dict]] = None,
) -> bool:
    """POST new Job__c when unmapped; requires worksite Account Id (map or create). Returns True if created.

    When ``sf_jobs`` is provided, run a final safety check just before POST: if the
    resolved worksite already has an existing Job__c whose ``Job_Client_Job_Id__c``
    references the same city + state as ours, we are likely about to create a duplicate
    (this happens when our practice_value is malformed, e.g. JD body has ``"419 -
    Georgetown, KY"`` while the real worksite practice is ``"2419 - Georgetown, KY"``).
    In that case we emit ``mapping_review_required`` and skip the create.
    """
    from utils.supabase_db import (
        get_job_current,
        log_job_event,
        update_sf_ids_for_job,
        fetch_worksite_account_id_for_location,
    )
    from utils.sf_job_payload import _truncate_external_job_id, prepare_payload_for_write
    from utils.sf_job_rest_minimal import (
        create_job_record,
        describe_sobject,
        filter_createable_fields,
        is_salesforce_deleted_entity_error,
    )
    from utils.salesforce import query_jobs_by_external_id_exact
    from utils.sf_worksite_create import fetch_or_create_worksite_account_id

    jid = (job_id or "").strip()
    if not jid or conn is None:
        return False

    if not proxi_sf_writes_enabled():
        log_job_event(
            conn,
            job_id=jid,
            event_type="job_create_skipped",
            schema=schema,
            run_id=run_id,
            payload={
                "reason": "PROXI_SF_UPDATE_JOBS=false",
                "detail": "Job__c auto-create is disabled when Salesforce writes are off.",
            },
        )
        return False

    cur = get_job_current(conn, job_ids=[jid], limit=1, schema=schema)
    latest = dict(cur[0]) if cur else dict(row)
    if (latest.get("sf_job_id") or "").strip():
        return False

    # ── Refuse to auto-create without a practice_value. ──
    # Job_Client_Job_Id__c is what ties Kimedics to an existing Salesforce
    # Job__c (unique on the SF side). With no practice_value we cannot dedupe,
    # so creating risks a duplicate Job__c that later blocks PATCHes with
    # DUPLICATE_VALUE. Emit a quarantine-style event so the recovery loop
    # retries on the next scrape (once the parser produces a practice_value)
    # and the per-batch + daily alerts pick it up.
    practice_raw_guard = (latest.get("practice_value") or "").strip()
    if not practice_raw_guard:
        log_job_event(
            conn,
            job_id=jid,
            event_type="mapping_blocked_no_practice_value",
            schema=schema,
            run_id=run_id,
            payload={
                "reason": "empty_practice_value",
                "detail": (
                    "Refused to auto-create Job__c — practice_value is empty so "
                    "Job_Client_Job_Id__c-based dedup is impossible. Will retry on "
                    "next scrape once the parser fills practice_value."
                ),
                "city": (latest.get("city") or "").strip() or None,
                "state": (latest.get("state") or "").strip() or None,
                "automation_kind": "salesforce_job_create_blocked",
            },
        )
        return False

    tok = _sf_rest_token()
    if not tok:
        log_job_event(
            conn,
            job_id=jid,
            event_type="job_create_failed",
            schema=schema,
            run_id=run_id,
            payload={"reason": "no_salesforce_credentials"},
        )
        return False

    instance_url, access_token = tok
    job_object_name = os.environ.get("SALESFORCE_JOB_OBJECT", "Job__c").strip()

    city = (latest.get("city") or "").strip()
    state = (latest.get("state") or "").strip()
    w = (latest.get("sf_worksite_account_id") or "").strip()
    if not w and city and state:
        w = fetch_worksite_account_id_for_location(conn, city, state, schema=schema) or ""
    if not w and city and state:
        w = (
            fetch_or_create_worksite_account_id(
                conn,
                city,
                state,
                instance_url=instance_url,
                access_token=access_token,
                address_line=(latest.get("address_line") or "").strip() or None,
                schema=schema,
                run_id=run_id,
                job_id_for_log=jid,
            )
            or ""
        )
    if not w:
        log_job_event(
            conn,
            job_id=jid,
            event_type="job_create_failed",
            schema=schema,
            run_id=run_id,
            payload={"reason": "no_worksite_account_id"},
        )
        return False

    latest["sf_worksite_account_id"] = w

    # ── Pre-create safety: do NOT create a duplicate Job__c at the same worksite. ──
    # If the resolved worksite already hosts a Job__c whose Job_Client_Job_Id__c
    # references our city/state, the most likely explanation is that our
    # practice_value is malformed (e.g. "419 - Georgetown, KY" should be "2419 -
    # Georgetown, KY"). Skip the create and flag for manual review.
    if sf_jobs and w and city and state:
        practice_raw = (latest.get("practice_value") or "").strip()
        cl = city.lower()
        su = state.upper()
        suspicious: list[dict] = []
        for j in sf_jobs:
            j_w = (j.get("Job_Worksite_Location_1__c") or "").strip()
            if j_w != w:
                continue
            j_practice = (j.get("Job_Client_Job_Id__c") or "")
            if not j_practice:
                continue
            j_low = j_practice.lower()
            if cl in j_low and su in j_practice.upper():
                suspicious.append(j)
        if suspicious:
            log_job_event(
                conn,
                job_id=jid,
                event_type="mapping_review_required",
                schema=schema,
                run_id=run_id,
                payload={
                    "reason": "existing_job_at_resolved_worksite_with_matching_location",
                    "detail": (
                        "Skipped Job__c create — the resolved worksite already has "
                        "a Job__c with a Job_Client_Job_Id__c that matches our "
                        "city/state. Our practice_value may be malformed. Manual "
                        "review required."
                    ),
                    "candidate_practice_value": practice_raw[:200] or None,
                    "city": city,
                    "state": state,
                    "worksite_account_id": w,
                    "existing_sf_job_ids": [s.get("Id") for s in suspicious],
                    "existing_practice_values": [
                        (s.get("Job_Client_Job_Id__c") or "")[:200] for s in suspicious
                    ],
                },
            )
            return False

    eid_trim = _truncate_external_job_id(jid)
    if eid_trim:
        try:
            ex_hits = query_jobs_by_external_id_exact(
                instance_url, access_token, eid_trim, job_object_name=job_object_name
            )
        except Exception:
            ex_hits = []
        if len(ex_hits) == 1:
            rec = ex_hits[0]
            sfid = (rec.get("Id") or "").strip()
            wid_sf = (rec.get("Job_Worksite_Location_1__c") or "").strip()
            wid_use = wid_sf or w
            if sfid:
                update_sf_ids_for_job(
                    conn,
                    job_id=jid,
                    sf_job_id=sfid,
                    sf_worksite_account_id=wid_use,
                    source="sf_existing_by_external_id_query",
                    mapping_status="resolved",
                    mapping_detail=(
                        "Existing Job__c with same External_Job_ID__c (re-link; avoids duplicate POST "
                        "after Supabase SF ids cleared)"
                    ),
                    run_id=run_id,
                    schema=schema,
                )
                return True
        if len(ex_hits) > 1:
            log_job_event(
                conn,
                job_id=jid,
                event_type="mapping_ambiguous",
                schema=schema,
                run_id=run_id,
                payload={
                    "source": "sf_external_job_id_direct_query",
                    "external_id": eid_trim,
                    "hits": len(ex_hits),
                    "candidate_sf_job_ids": [(r.get("Id") or "").strip() for r in ex_hits],
                },
            )
            return False

    try:
        from utils.job_sf_enrichment import enrich_cleaned_row_salesforce_fields

        enrich_cleaned_row_salesforce_fields(conn, latest, schema=schema, run_id=run_id)
    except Exception:
        pass

    use_html = os.environ.get("PROXI_JOB_DESCRIPTION_HTML", "true").lower() in ("1", "true", "yes")
    describe = describe_sobject(instance_url, access_token, job_object_name)
    new_job_id = ""
    attempt = 0
    last_exc: Optional[BaseException] = None
    while attempt < 2:
        try:
            fields = prepare_payload_for_write(
                latest,
                describe,
                for_update=False,
                use_canonical_description=True,
                description_use_html=use_html,
                conn=conn,
                schema=schema,
            )
            fields = filter_createable_fields(describe, fields)
            if not fields:
                raise RuntimeError("create payload empty after createable filter")
            resp = create_job_record(instance_url, access_token, job_object_name, fields)
            new_job_id = (resp.get("id") or "").strip()
            if not new_job_id:
                raise RuntimeError(f"create Job__c returned no id: {resp!r}")
            last_exc = None
            break
        except Exception as e:
            last_exc = e
            if (
                attempt == 0
                and is_salesforce_deleted_entity_error(e)
                and city
                and state
            ):
                from utils.supabase_db import (
                    delete_worksite_location_map_by_salesforce_account_id,
                    delete_worksite_location_map_for_location,
                )

                n_map = 0
                if w:
                    n_map += delete_worksite_location_map_by_salesforce_account_id(
                        conn, w, schema=schema
                    )
                n_map += delete_worksite_location_map_for_location(conn, city, state, schema=schema)
                log_job_event(
                    conn,
                    job_id=jid,
                    event_type="worksite_stale_map_cleared",
                    schema=schema,
                    run_id=run_id,
                    payload={
                        "reason": "salesforce_entity_deleted_on_create",
                        "detail": str(e)[:1500],
                        "prior_sf_worksite_account_id": w or None,
                        "map_rows_deleted": n_map,
                    },
                )
                latest["sf_worksite_account_id"] = ""
                if _env_truthy("PROXI_SF_CREATE_WORKSITES"):
                    w = (
                        fetch_or_create_worksite_account_id(
                            conn,
                            city,
                            state,
                            instance_url=instance_url,
                            access_token=access_token,
                            address_line=(latest.get("address_line") or "").strip() or None,
                            schema=schema,
                            run_id=run_id,
                            job_id_for_log=jid,
                            skip_location_lookup=True,
                        )
                        or ""
                    )
                else:
                    w = ""
                if not w:
                    break
                latest["sf_worksite_account_id"] = w
                attempt += 1
                continue
            break

    if last_exc is not None or not new_job_id:
        log_job_event(
            conn,
            job_id=jid,
            event_type="job_create_failed",
            schema=schema,
            run_id=run_id,
            payload={
                "error": (str(last_exc)[:2000] if last_exc else "create returned no id"),
            },
        )
        return False

    update_sf_ids_for_job(
        conn,
        job_id=jid,
        sf_job_id=new_job_id,
        sf_worksite_account_id=w,
        source="sf_create_job",
        mapping_status="created",
        mapping_detail="Created Job__c after no practice/external/AI match",
        run_id=run_id,
        schema=schema,
    )
    log_job_event(
        conn,
        job_id=jid,
        event_type="job_created_in_salesforce",
        schema=schema,
        run_id=run_id,
        payload={
            "sf_job_id": new_job_id,
            "sf_worksite_account_id": w,
            # Stable keys for automation-hub (and other consumers) to detect auto-create vs match.
            "automation_kind": "salesforce_job_auto_created",
            "summary": (
                "POST created a new Job__c after no 1:1 practice / External_Job_ID__c / AI match "
                "(PROXI_SF_CREATE_JOBS)."
            ),
        },
    )
    return True


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
    from utils.sf_job_payload import external_job_id_match_key
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

    sf_by_external: dict[str, set[str]] = {}
    for j in sf_jobs:
        ek = external_job_id_match_key(j.get("External_Job_ID__c"))
        if ek:
            sf_by_external.setdefault(ek, set()).add(j["Id"])

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

        # ── External Job ID first (Kimedics job_id ↔ External_Job_ID__c), 1:1 only ──
        ext_key = external_job_id_match_key(jid)
        ext_hits: list[str] = []
        if ext_key:
            ext_hits = sorted(sf_by_external.get(ext_key, set()))
        if len(ext_hits) == 1:
            sfid = ext_hits[0]
            wid = (sf_by_id.get(sfid, {}).get("Job_Worksite_Location_1__c") or "").strip() or None
            j_final = j or sfid
            w_final = w or wid
            update_sf_ids_for_job(
                conn,
                job_id=jid,
                sf_job_id=j_final or None,
                sf_worksite_account_id=w_final or None,
                source="sf_external_job_id_match",
                mapping_status="resolved",
                mapping_detail="1:1 External_Job_ID__c match",
                run_id=run_id,
                schema=schema,
            )
            updated += 1
            continue
        if len(ext_hits) > 1:
            log_job_event(
                conn,
                job_id=jid,
                event_type="mapping_ambiguous",
                schema=schema,
                run_id=run_id,
                payload={
                    "source": "sf_external_job_id_match",
                    "external_key": ext_key,
                    "hits": len(ext_hits),
                    "candidate_sf_job_ids": ext_hits,
                },
            )
            continue

        # ── Practice match (fallback when external id absent / no 1:1 in SF snapshot) ──
        hits: list[str] = []
        if p:
            hits = sorted(sf_by_practice.get(p, set()))

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
                source="sf_practice_match",
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
                "external_key": ext_key or None,
                "external_hits": len(ext_hits) if ext_key else 0,
                "practice_key": p or None,
                "practice_raw": practice_raw[:500] if practice_raw else None,
                "practice_hits": len(hits) if p else 0,
                "sf_jobs_indexed": len(sf_jobs),
                "ai_attempted": bool(practice_raw),
            },
        )
        if _env_truthy("PROXI_SF_CREATE_JOBS"):
            if _try_create_sf_job_after_no_match(
                conn,
                job_id=jid,
                row=row,
                schema=schema,
                run_id=run_id,
                sf_jobs=sf_jobs,
            ):
                updated += 1

    return updated

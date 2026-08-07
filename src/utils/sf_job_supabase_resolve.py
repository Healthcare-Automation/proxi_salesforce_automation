"""
Fill missing ``sf_job_id`` / ``sf_worksite_account_id`` on Supabase after a Kimedics scrape.

Design rule
-----------
Every incoming Kimedics job either (a) links to an existing Salesforce Job__c,
or (b) creates a new Salesforce Job__c (and Worksite__c, when needed) within
the same pipeline run. There is **no human-in-the-loop step**. Earlier versions
of this resolver emitted ``mapping_review_required`` / ``mapping_ambiguous``
and stopped — those events still appear in the historical log but are no
longer generated.

Order
-----
1. Skip if ``job_current`` already has both ``sf_job_id`` and ``sf_worksite_account_id``.
2. Merge from newest ``job_content`` with both ids (history carry-forward).
3. **External Job ID** match: Kimedics ``job_id`` ↔ ``External_Job_ID__c``.
   1:1 → link. N>1 → deterministically pick the most recently modified candidate,
   ID-swap into it, and emit ``mapping_external_id_duplicate_resolved`` so the
   duplicates can be cleaned up out-of-band.
4. **Practice** match: normalized ``practice_value`` ↔ ``Job_Client_Job_Id__c``.
   1:1 → link. N>1 → deterministically pick (prefer no existing
   ``External_Job_ID__c``; break ties by ``LastModifiedDate``), ID-swap into it.
5. **AI** fallback on practice string when deterministic matching had 0 hits.
   Only acts on ``high`` confidence (``medium`` is intentionally dropped).
6. **No match**: log ``mapping_no_match``; then ``_try_create_sf_job_after_no_match``:
   - Resolve a worksite Account Id from ``sf_worksite_location_map`` (or
     create one when ``PROXI_SF_CREATE_WORKSITES=true``).
   - POST a brand-new ``Job__c`` at that worksite using Kimedics values.
   - Existing Job__c records at the same worksite (e.g. closed prior postings)
     are NOT touched — each Kimedics ``job_id`` gets its own SF Job__c.
     History is preserved.

The next field-sync cycle PATCHes every Kimedics-derived value onto the
newly-created (or, in the recovery cases above, re-linked) Job__c.
"""

from __future__ import annotations

import os
from typing import Sequence, Optional

from utils.sf_practice_key import practice_key
from utils.sf_write_flags import proxi_sf_writes_enabled

# New Job__c records are assigned to Cara Griffin (0055f000007qcxEAAQ) on CREATE only.
# Salesforce otherwise defaults a new record's owner to the integration run-as user
# (Sean Yang). This is never applied on update — re-asserting OwnerId on every 10-minute
# sync would yank ownership back from whoever a recruiter later assigns the job to.
# Mirrors the worksite (Account) owner default in sf_worksite_create.py.
JOB_DEFAULT_OWNER_ID = os.environ.get("PROXI_JOB_DEFAULT_OWNER_ID", "0055f000007qcxEAAQ")


def _env_truthy(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes")


def _pick_swap_candidate(candidates: Sequence[dict]) -> Optional[dict]:
    """Pick which existing Job__c the resolver should ID-swap into.

    Rule:
      1. Prefer candidates with NO existing ``External_Job_ID__c`` (so we don't
         stomp another Kimedics ↔ SF link).
      2. Break ties by most-recently modified.

    Returns None if the list is empty. Never raises.
    """
    if not candidates:
        return None

    def _mtime(c: dict) -> str:
        return (c.get("LastModifiedDate") or "")

    no_ext = [c for c in candidates if not (c.get("External_Job_ID__c") or "").strip()]
    pool = no_ext if no_ext else list(candidates)
    return sorted(pool, key=_mtime, reverse=True)[0]


def _recruiting_activity(candidate: dict) -> float:
    """Placements + submittals + applications on a Job__c (0.0 when absent/unparseable)."""
    total = 0.0
    for field in ("Total_Placements__c", "Total_Submittals__c", "Total_Applications__c"):
        try:
            total += float(candidate.get(field) or 0)
        except (TypeError, ValueError):
            continue
    return total


def _pick_canonical_practice_record(candidates: Sequence[dict]) -> Optional[dict]:
    """Pick the single Job__c that survives when several share one practice key.

    Salesforce marks ``Job_Client_Job_Id__c`` unique — one practice is meant to have
    one Job__c. Legacy pairs slipped past that constraint on cosmetic string drift
    (``"4096- X"`` vs ``"4096 - X"``), which ``practice_key`` correctly folds together.

    Rule: keep the record carrying the most recruiting history (placements +
    submittals + applications), tie-break on newest ``CreatedDate``.

    Deliberately NOT ``_pick_swap_candidate``'s "prefer no External_Job_ID__c" — against
    a duplicate pair that rule hands each new Kimedics posting a *different* record and
    keeps every duplicate alive forever, which is how job 20084 resurrected a 2022 row.

    Returns None if the list is empty. Never raises.
    """
    if not candidates:
        return None
    return sorted(
        candidates,
        key=lambda c: (_recruiting_activity(c), (c.get("CreatedDate") or "")),
        reverse=True,
    )[0]


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


def _consolidate_practice_duplicates(
    conn,
    *,
    job_id: str,
    practice_key_value: str,
    practice_raw: Optional[str],
    winner: Optional[dict],
    losers: Sequence[dict],
    run_id=None,
    schema: str = "public",
) -> None:
    """Collapse duplicate Job__c at one practice onto ``winner`` in Salesforce.

    Blanks each loser's ``Job_Client_Job_Id__c`` (the unique field that has been
    rejecting the winner's PATCH) and marks it Closed, then normalizes the winner's
    practice value to the Kimedics-scraped string. Losers are never deleted — their
    recruiting history stays intact and reachable, they just stop competing for the
    practice key.

    Order matters: the losers must release the unique value before the winner can
    take it, otherwise Salesforce rejects the winner's write.

    Always logs ``practice_duplicate_consolidated`` so the pair is visible even when
    writes are gated off (``PROXI_SF_CONSOLIDATE_DUPLICATE_JOBS`` unset → detect-only).
    """
    from utils.supabase_db import log_job_event

    winner_id = ((winner or {}).get("Id") or "").strip()
    loser_ids = [(c.get("Id") or "").strip() for c in losers if (c.get("Id") or "").strip()]
    if not winner_id or not loser_ids:
        return

    writes_on = proxi_sf_writes_enabled() and _env_truthy("PROXI_SF_CONSOLIDATE_DUPLICATE_JOBS")
    payload = {
        "practice_key": practice_key_value or None,
        "winner_sf_job_id": winner_id,
        "winner_activity": _recruiting_activity(winner or {}),
        "duplicate_sf_job_ids": loser_ids,
        "consolidated": False,
        "detail": (
            "Several Job__c share one practice key. Kept the record with the most "
            "recruiting history; the others release the unique practice value so they "
            "stop shadowing it. No record is deleted."
        ),
    }

    if not writes_on:
        payload["detail"] += " Detect-only: PROXI_SF_CONSOLIDATE_DUPLICATE_JOBS is not enabled."
        log_job_event(
            conn,
            job_id=job_id,
            event_type="practice_duplicate_consolidated",
            schema=schema,
            run_id=run_id,
            payload=payload,
        )
        return

    creds = _sf_rest_token()
    if not creds:
        payload["detail"] += " Skipped: no Salesforce credentials available."
        log_job_event(
            conn,
            job_id=job_id,
            event_type="practice_duplicate_consolidated",
            schema=schema,
            run_id=run_id,
            payload=payload,
        )
        return

    instance_url, access_token = creds
    from utils.sf_job_rest_minimal import update_job_record

    released: list[str] = []
    errors: list[dict] = []
    for loser_id in loser_ids:
        try:
            update_job_record(
                instance_url,
                access_token,
                "Job__c",
                loser_id,
                {"Job_Client_Job_Id__c": None, "Job_Status__c": "Closed"},
            )
            released.append(loser_id)
        except Exception as exc:
            errors.append({"sf_job_id": loser_id, "error": str(exc)[:500]})

    if released and (practice_raw or "").strip():
        try:
            update_job_record(
                instance_url,
                access_token,
                "Job__c",
                winner_id,
                {"Job_Client_Job_Id__c": (practice_raw or "").strip()},
            )
            payload["winner_practice_value"] = (practice_raw or "").strip()
        except Exception as exc:
            errors.append({"sf_job_id": winner_id, "error": str(exc)[:500]})

    payload["consolidated"] = bool(released) and not errors
    payload["released_sf_job_ids"] = released
    if errors:
        payload["errors"] = errors

    log_job_event(
        conn,
        job_id=job_id,
        event_type="practice_duplicate_consolidated",
        schema=schema,
        run_id=run_id,
        payload=payload,
    )


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
        update_job_record,
    )
    from utils.salesforce import query_jobs_by_external_id_exact, query_jobs_by_worksite_id_exact
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

    # Each new Kimedics posting that reaches this path gets its own fresh
    # Job__c at the resolved worksite. Historical Job__c records at the same
    # worksite (e.g. closed prior postings) stay where they are — they're
    # history, not duplicates. The previous "ID-swap if anything's at the
    # worksite" guard violated the design rule "no human in the loop AND
    # one Kimedics job_id == one Salesforce Job__c."

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
            # >1 SF Job__c with the same External_Job_ID__c is a data-integrity
            # violation (the field is supposed to be unique). ID-swap into the
            # most recently modified one and log the duplicates so they can be
            # cleaned up out-of-band — never block on this.
            pick = _pick_swap_candidate(ex_hits)
            sfid = (pick.get("Id") or "").strip() if pick else ""
            losers = [(r.get("Id") or "").strip() for r in ex_hits if (r.get("Id") or "").strip() != sfid]
            wid_pick = (pick.get("Job_Worksite_Location_1__c") or "").strip() if pick else ""
            wid_use = wid_pick or w
            if sfid:
                update_sf_ids_for_job(
                    conn,
                    job_id=jid,
                    sf_job_id=sfid,
                    sf_worksite_account_id=wid_use,
                    source="sf_external_job_id_duplicate_resolved",
                    mapping_status="resolved",
                    mapping_detail=(
                        f"Multiple SF Job__c share External_Job_ID__c={eid_trim!r}; "
                        f"picked most-recent ({sfid}). Duplicate ids: {losers}"
                    ),
                    run_id=run_id,
                    schema=schema,
                )
                log_job_event(
                    conn,
                    job_id=jid,
                    event_type="mapping_external_id_duplicate_resolved",
                    schema=schema,
                    run_id=run_id,
                    payload={
                        "external_id": eid_trim,
                        "winner_sf_job_id": sfid,
                        "duplicate_sf_job_ids": losers,
                    },
                )
                return True

    # ── Worksite-level safety net (the "one Job__c per practice" rule). ──
    # SF treats Job_Client_Job_Id__c as unique, so each worksite Account is
    # expected to have at most one Job__c. If we get here it means External_Job_ID
    # lookup found nothing and the in-memory snapshot's practice-key index missed
    # too (often because a single bad scrape produced a malformed practice_value
    # like "Covington, LA" instead of "3077 - Covington, LA"). Before POSTing,
    # SOQL-probe SF directly for any existing Job__c at this worksite and
    # re-link to it instead of creating a duplicate.
    try:
        worksite_hits = query_jobs_by_worksite_id_exact(
            instance_url, access_token, w, job_object_name=job_object_name
        )
    except Exception:
        worksite_hits = []
    if worksite_hits:
        # Prefer a candidate WITHOUT a conflicting External_Job_ID__c (so we
        # don't stomp another Kimedics ↔ SF link). Otherwise fall back to the
        # most recently modified candidate (the SOQL is ORDER BY LastModifiedDate
        # DESC, so worksite_hits[0] is the most-recent).
        no_ext = [r for r in worksite_hits if not (r.get("External_Job_ID__c") or "").strip()]
        pick = (no_ext[0] if no_ext else worksite_hits[0])
        sfid = (pick.get("Id") or "").strip()
        existing_ext = (pick.get("External_Job_ID__c") or "").strip() or None
        other_ids = [
            (r.get("Id") or "").strip()
            for r in worksite_hits
            if (r.get("Id") or "").strip() and (r.get("Id") or "").strip() != sfid
        ]
        if sfid:
            update_sf_ids_for_job(
                conn,
                job_id=jid,
                sf_job_id=sfid,
                sf_worksite_account_id=w,
                source="sf_existing_at_worksite",
                mapping_status="resolved",
                mapping_detail=(
                    "Existing Job__c at resolved worksite (re-link; "
                    "avoids duplicate POST when practice_value scrape is degraded)"
                ),
                run_id=run_id,
                schema=schema,
            )
            log_job_event(
                conn,
                job_id=jid,
                event_type="mapping_worksite_existing_job_relinked",
                schema=schema,
                run_id=run_id,
                payload={
                    "sf_worksite_account_id": w,
                    "winner_sf_job_id": sfid,
                    "winner_existing_external_job_id": existing_ext,
                    "other_sf_job_ids_at_worksite": other_ids,
                    "practice_raw": practice_raw_guard or None,
                    "automation_kind": "salesforce_job_relinked_to_worksite",
                    "summary": (
                        "Re-linked to an existing Job__c at the resolved worksite instead of "
                        "creating a duplicate. The kept record may already be linked to another "
                        "Kimedics job_id via External_Job_ID__c — that's expected and fine; "
                        "Job__c is one-per-practice in the SF model."
                    ),
                },
            )
            return True

    try:
        from utils.job_sf_enrichment import enrich_cleaned_row_salesforce_fields

        enrich_cleaned_row_salesforce_fields(conn, latest, schema=schema, run_id=run_id)
    except Exception:
        pass

    use_html = os.environ.get("PROXI_JOB_DESCRIPTION_HTML", "true").lower() in ("1", "true", "yes")
    describe = describe_sobject(instance_url, access_token, job_object_name)
    new_job_id = ""
    attempt = 0
    flip_to_open = False
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
            # The client's "Job Opened - Send Slack Alert" flow triggers on UPDATE into
            # Open only — a Job__c born Open never alerts. Create Closed, then PATCH to
            # Open right after (below) so the flow sees a real Closed→Open transition.
            flip_to_open = fields.get("Job_Status__c") == "Open"
            if flip_to_open:
                fields["Job_Status__c"] = "Closed"
            # Assign new Job__c to Cara on create (create-only — see JOB_DEFAULT_OWNER_ID).
            if JOB_DEFAULT_OWNER_ID:
                fields["OwnerId"] = JOB_DEFAULT_OWNER_ID
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
    if flip_to_open:
        flip_error: Optional[str] = None
        try:
            update_job_record(
                instance_url,
                access_token,
                job_object_name,
                new_job_id,
                {"Job_Status__c": "Open"},
            )
        except Exception as e:
            # Non-fatal: the scrape-fields PATCH later this run also sets Open and
            # still fires the client's alert flow.
            flip_error = str(e)[:1500]
        log_job_event(
            conn,
            job_id=jid,
            event_type="job_open_flip_after_create",
            schema=schema,
            run_id=run_id,
            payload={
                "sf_job_id": new_job_id,
                "ok": flip_error is None,
                "error": flip_error,
                "summary": (
                    "Created Closed then PATCHed to Open so the client's update-triggered "
                    "'Job Opened - Send Slack Alert' flow fires for brand-new Job__c records."
                ),
            },
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
            # Data-integrity violation: multiple SF jobs share the same
            # External_Job_ID__c. ID-swap into the most recently modified one
            # and log the duplicates for out-of-band cleanup.
            candidates_full = [sf_by_id[sid] for sid in ext_hits if sid in sf_by_id]
            pick = _pick_swap_candidate(candidates_full)
            sfid = (pick.get("Id") or "").strip() if pick else (ext_hits[0] if ext_hits else "")
            losers = [s for s in ext_hits if s != sfid]
            wid = (sf_by_id.get(sfid, {}).get("Job_Worksite_Location_1__c") or "").strip() or None
            j_final = j or sfid
            w_final = w or wid
            if sfid:
                update_sf_ids_for_job(
                    conn,
                    job_id=jid,
                    sf_job_id=j_final or None,
                    sf_worksite_account_id=w_final or None,
                    source="sf_external_job_id_duplicate_resolved",
                    mapping_status="resolved",
                    mapping_detail=(
                        f"Multiple SF Job__c share External_Job_ID__c key={ext_key!r}; "
                        f"picked most-recent ({sfid}). Duplicate ids: {losers}"
                    ),
                    run_id=run_id,
                    schema=schema,
                )
                log_job_event(
                    conn,
                    job_id=jid,
                    event_type="mapping_external_id_duplicate_resolved",
                    schema=schema,
                    run_id=run_id,
                    payload={
                        "external_key": ext_key,
                        "winner_sf_job_id": sfid,
                        "duplicate_sf_job_ids": losers,
                    },
                )
                updated += 1
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
            # Several SF Job__c share this practice key — they are duplicates of one
            # practice, not separate postings (SF marks Job_Client_Job_Id__c unique;
            # legacy pairs only slipped through on "4096-" vs "4096 - " string drift).
            # Pin EVERY Kimedics job at this practice to one canonical record so the
            # duplicates stop being kept alive, and consolidate them in Salesforce.
            candidates_full = [sf_by_id[sid] for sid in hits if sid in sf_by_id]
            pick = _pick_canonical_practice_record(candidates_full)
            sfid = (pick.get("Id") or "").strip() if pick else (hits[0] if hits else "")
            losers = [s for s in hits if s != sfid]
            wid = (sf_by_id.get(sfid, {}).get("Job_Worksite_Location_1__c") or "").strip() or None
            j_final = j or sfid
            w_final = w or wid
            if sfid:
                update_sf_ids_for_job(
                    conn,
                    job_id=jid,
                    sf_job_id=j_final or None,
                    sf_worksite_account_id=w_final or None,
                    source="sf_practice_match_canonical_pick",
                    mapping_status="resolved",
                    mapping_detail=(
                        f"{len(hits)} SF Job__c share practice_key={p!r}; "
                        f"kept canonical ({sfid}). Duplicates: {losers}"
                    ),
                    run_id=run_id,
                    schema=schema,
                )
                _consolidate_practice_duplicates(
                    conn,
                    job_id=jid,
                    practice_key_value=p,
                    practice_raw=practice_raw,
                    winner=pick,
                    losers=[sf_by_id[s] for s in losers if s in sf_by_id],
                    run_id=run_id,
                    schema=schema,
                )
                updated += 1
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

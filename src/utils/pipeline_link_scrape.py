"""
Link-batch phase of the Kimedics pipeline: persist Playwright rows, resolve Salesforce
mapping, then PATCH Job__c — all in the same process / Modal invocation (no queue).
"""

from __future__ import annotations

from typing import Any, Set


def process_link_scrape_batch(
    conn,
    *,
    link_run_id: int,
    scrape_results: list[dict[str, Any]],
    schema: str = "public",
) -> Set[str]:
    """
    For each Playwright result: validate (alerts), ``log_job_content``; then
    ``resolve_sf_ids_for_job_ids`` and ``sync_missing_scrape_fields_for_job_ids``
    for touched Kimedics ``job_id`` values.

    Returns the set of touched job_ids (from cleaned ``job_id`` or fallback ``job_post_id``).
    """
    from utils.alert_email import send_scrape_alert
    from utils.scrape_validator import (
        issues_as_text,
        issues_summary,
        should_send_immediate_alert,
        validate_scraped_job,
    )
    from utils.supabase_db import log_job_content

    sf_cache: dict = {}
    touched_job_ids: set[str] = set()

    # ── Pass 1: validate + persist every row. We DON'T fire alerts here any
    # more — the alert decision is made in Pass 2 after SF push completes,
    # so it can consult sibling-scrape outcomes, downstream SF events, and
    # recent history (same signal set the daily summary tracks). This kills
    # the false-alarm class where Job A's empty scrape sends an email even
    # though Job A's sibling scrape in the same batch succeeded.
    per_row_state: list[dict] = []
    for r in scrape_results:
        cl = r.get("cleaned") or {}
        jid = str(cl.get("job_id") or r.get("job_post_id") or "").strip()
        job_post_id = str(r.get("job_post_id") or "").strip()
        view_link = r.get("view_job_link", "")
        scrape_err = r.get("error", "")

        if jid:
            touched_job_ids.add(jid)

        issues = validate_scraped_job(cl, job_post_id=job_post_id)
        summary = issues_summary(issues)

        # Per-row console signal stays so Modal logs still tell the story.
        if scrape_err:
            print(f"  [SCRAPE ERROR] Job #{job_post_id}: {scrape_err}")
        elif should_send_immediate_alert(issues):
            print(
                f"  [VALIDATION] Job #{job_post_id}: "
                f"{summary['critical']} critical, {summary['warning']} warnings"
            )
        elif issues:
            print(f"  [VALIDATION] Job #{job_post_id}: {summary['info']} info — logged")
        else:
            print(f"  [VALIDATION] Job #{job_post_id}: all checks passed ✓")
        if issues:
            print(issues_as_text(issues, job_post_id))

        parse_ok = bool((cl.get("title_line") or "").strip()
                        and (cl.get("description_full_text") or "").strip())
        per_row_state.append({
            "canonical_job_id": jid,
            "job_post_id":      job_post_id,
            "issues":           issues,
            "summary":          summary,
            "cleaned":          cl,
            "view_job_link":    view_link,
            "scrape_err":       scrape_err,
            "parse_ok":         parse_ok,
            "would_alert":      bool(scrape_err) or should_send_immediate_alert(issues),
        })

        log_job_content(
            conn,
            link_run_id,
            r["job_post_id"],
            r["email_received_date"],
            cl,
            email_scrape_id=r.get("email_scrape_id"),
            sf_lookup_cache=sf_cache,
            view_job_link=view_link,
        )

        # Flag AI date resolutions made with < 100% confidence for human review
        # (set by the parser only when a top-line override/cancellation fired and
        # the model was unsure). Best-effort, deduped per (job, resolved value).
        if cl.get("dates_confidence") is not None:
            try:
                _alert_low_confidence_dates(conn, job_post_id, cl, schema=schema, run_id=link_run_id)
            except Exception as _e:
                print(f"  [alert_email] dates review alert skipped for #{job_post_id}: {_e}")

    # ── SF resolution + scrape-field sync (existing) ──────────────────────────
    if touched_job_ids:
        try:
            from utils.sf_job_supabase_resolve import resolve_sf_ids_for_job_ids

            updated = resolve_sf_ids_for_job_ids(
                conn, sorted(touched_job_ids), schema=schema, run_id=link_run_id
            )
            if updated:
                print(f"Resolved sf ids for {updated}/{len(touched_job_ids)} touched job(s).")
        except Exception as e:
            print(f"SF id resolution step skipped (error): {e}")

        try:
            from utils.sf_scrape_sync import sync_missing_scrape_fields_for_job_ids

            att, patched = sync_missing_scrape_fields_for_job_ids(
                conn,
                sorted(touched_job_ids),
                schema=schema,
                run_id=link_run_id,
            )
            if att:
                print(
                    f"Salesforce scrape-field sync: attempted={att}, patched={patched} "
                    f"(touched job_ids={len(touched_job_ids)})."
                )
        except Exception as e:
            print(f"Salesforce scrape-field sync skipped (error): {e}")

    # ── Pass 2: consolidated alerting with full pipeline context ──────────────
    _decide_and_send_alerts(conn, per_row_state, link_run_id, schema)

    return touched_job_ids


def _alert_low_confidence_dates(conn, job_post_id, cl: dict, *, schema: str, run_id) -> None:
    """Email the team when AI resolved a job's active dates with < 100% confidence,
    so a human can verify before the value is relied on in Salesforce. Deduped on
    (job_id, resolved value) so a re-scrape of the same content stays silent."""
    from utils.supabase_db import _validate_pg_identifier, log_job_event

    resolved = (cl.get("dates_needed") or "").strip()
    confidence = int(cl.get("dates_confidence") or 0)
    reason = (cl.get("dates_reason") or "").strip()
    structured = (cl.get("dates_structured") or "").strip()
    jid = str(job_post_id or "").strip()
    if not jid:
        return
    sch = _validate_pg_identifier(schema, "schema")

    sf_job_id = ""
    if conn is not None:
        with conn.cursor() as cur:
            cur.execute(
                f'SELECT 1 FROM "{sch}".job_event_log WHERE job_id=%s '
                f"AND event_type='sf_dates_low_confidence' AND payload->>'resolved'=%s LIMIT 1",
                (jid, resolved),
            )
            if cur.fetchone():
                return  # already alerted for this job + resolved value
            cur.execute(f'SELECT sf_job_id FROM "{sch}".job_current WHERE job_id=%s', (jid,))
            row = cur.fetchone()
            sf_job_id = ((row[0] or "").strip() if row else "")

    from utils.alert_email import send_dates_review_alert

    sent = send_dates_review_alert(
        jid,
        kimedics_url=f"https://portal.kimedics.com/app/workspace/job-posts/{jid}",
        sf_job_id=sf_job_id,
        structured=structured,
        resolved=resolved,
        confidence=confidence,
        reason=reason,
    )
    print(f"  [alert_email] dates review alert for #{jid} ({confidence}% conf) sent={sent}")
    log_job_event(
        conn,
        job_id=jid,
        event_type="sf_dates_low_confidence",
        run_id=run_id,
        schema=schema,
        payload={
            "resolved": resolved,
            "structured": structured,
            "confidence": confidence,
            "reason": reason,
            "email_sent": bool(sent),
        },
    )


def _decide_and_send_alerts(conn, per_row_state, link_run_id: int, schema: str) -> None:
    """
    Consolidated alert decision — runs once after Pass 1 + SF push.

    For each canonical job_id touched in this batch, decide whether to alert by
    looking at the union of:
      - validator output (Pass 1)
      - sibling-scrape outcomes in the same batch (suppresses false alarms)
      - this run's SF events (sf_scrape_fields_error, job_create_failed,
        worksite_create_failed, sf_field_quarantined, sf_sync_skipped_no_mapping)
      - recent history (last 24h) of unresolved failures / retries / rescrapes
      - empty job_content rows written for this batch

    This matches (and slightly exceeds) the daily-summary error-detection
    surface, so per-job alerts now catch everything the daily summary catches.
    """
    from utils.alert_email import send_scrape_alert
    from utils.scrape_validator import issues_summary

    if not per_row_state:
        return

    # Group rows by canonical job_id (so two emails for the same job in one
    # batch produce a single decision and a single email).
    by_job: dict[str, list[dict]] = {}
    for st in per_row_state:
        key = st["canonical_job_id"] or st["job_post_id"] or "(unknown)"
        by_job.setdefault(key, []).append(st)

    # Query SF/event signals for this batch in one shot.
    job_ids = [k for k in by_job.keys() if k and k != "(unknown)"]
    sf_signals = _query_alerting_signals(conn, job_ids, link_run_id, schema) if job_ids else {}

    for jid, rows in by_job.items():
        # Pick the "worst" row for snapshot purposes — most severe issues first.
        worst = max(
            rows,
            key=lambda s: (s["summary"]["critical"], s["summary"]["warning"], 1 if s["scrape_err"] else 0),
        )
        any_success_in_batch = any(s["parse_ok"] and not s["scrape_err"] for s in rows)
        signals = sf_signals.get(jid, {}) if jid else {}

        # Build downstream-event count list for body + decision.
        downstream_events = [
            ("sf_scrape_fields_error",            signals.get("sf_error",            0)),
            ("job_create_failed",                 signals.get("job_failed",          0)),
            ("worksite_create_failed",            signals.get("worksite_failed",     0)),
            ("sf_field_quarantined",              signals.get("quarantined",         0)),
            ("sf_sync_skipped_no_mapping",        signals.get("skipped",             0)),
            ("mapping_blocked_no_practice_value", signals.get("blocked_no_practice", 0)),
            ("scrape_silent_failure",             signals.get("silent_failure",      0)),
        ]
        downstream_total = sum(n for _, n in downstream_events)
        history_total = sum([
            signals.get("history_failed",  0),
            signals.get("history_auto",    0),
            signals.get("history_manual",  0),
        ])

        # Decide.
        validator_says_alert = worst["would_alert"]
        downstream_says_alert = downstream_total > 0

        # ★ Suppression: if a sibling row in THIS batch succeeded AND there are
        # no downstream errors for this job in this run AND no recent unresolved
        # failures in DB, this is a transient blip — suppress.
        if validator_says_alert and any_success_in_batch and not downstream_says_alert \
                and signals.get("recent_unresolved", 0) == 0:
            print(
                f"  [VALIDATION] Job #{worst['job_post_id']}: paired scrape in same batch "
                f"succeeded → suppressing alert (transient blip)"
            )
            continue

        # Empty-row signal (we wrote a job_content with job_id NULL or empty title).
        empty_row = signals.get("empty_row_this_run", False)

        # When the scrape silently failed (job_content row written but every
        # critical field empty — auth wall, layout change, login redirect),
        # log a scrape_silent_failure event so the auto-retry / quarantine
        # surface tracks it the same way it tracks mapping_blocked_no_practice_value.
        # Without this event, the empty-row signal lived only inside the alert
        # decision and operators had no per-run counter to reason about.
        if empty_row:
            try:
                from utils.supabase_db import log_job_event
                log_job_event(
                    conn,
                    job_id=worst["job_post_id"],
                    event_type="scrape_silent_failure",
                    run_id=link_run_id,
                    schema=schema,
                    payload={
                        "reason": "empty_critical_fields",
                        "detail": (
                            "Scrape wrote a job_content row with empty critical fields "
                            "(title_line / description_full_text / job_title). Auto-retry "
                            "will pick this up on the next cron tick."
                        ),
                        "view_job_link": worst.get("view_job_link"),
                        "batch_size": len(rows),
                        "sibling_success": any_success_in_batch,
                        "automation_kind": "kimedics_scrape_silent_failure",
                    },
                )
            except Exception as ev_err:
                print(f"  [alert_email] failed to log scrape_silent_failure for #{worst['job_post_id']}: {ev_err}")

        if not (validator_says_alert or downstream_says_alert or empty_row):
            continue  # nothing to alert on for this job

        # Send a single consolidated alert per job_id.
        try:
            send_scrape_alert(
                job_post_id=worst["job_post_id"],
                issues=worst["issues"],
                cleaned=worst["cleaned"],
                view_job_link=worst["view_job_link"],
                # New richer context:
                sibling_success=any_success_in_batch,
                downstream_events=[(t, n) for t, n in downstream_events if n > 0],
                recent_history={
                    "unresolved_failed": signals.get("recent_unresolved", 0),
                    "auto_retries_24h":  signals.get("history_auto", 0),
                    "manual_rescr_24h":  signals.get("history_manual", 0),
                    "failures_24h":      signals.get("history_failed", 0),
                },
                sf_mapping={
                    "sf_job_id":     signals.get("sf_job_id"),
                    "patched_fields": signals.get("patched_fields", 0),
                    "created_record": signals.get("created_record", False),
                },
                empty_row=empty_row,
                batch_size=len(rows),
            )
        except Exception as mail_err:
            print(f"  [alert_email] Could not send consolidated alert for #{worst['job_post_id']}: {mail_err}")


def _query_alerting_signals(conn, job_ids: list[str], link_run_id: int, schema: str) -> dict:
    """
    Pull all alert-relevant signals for the given job_ids in a small set of
    queries. Returns ``{job_id: signal_dict}``.
    """
    if conn is None or not job_ids:
        return {}
    out: dict[str, dict] = {jid: {} for jid in job_ids}

    with conn.cursor() as cur:
        # 1. This run's per-job event counts (failures + push outcomes + mapping skips).
        cur.execute(
            """
            SELECT
              job_id,
              count(*) FILTER (WHERE event_type='sf_scrape_fields_error')            AS sf_error,
              count(*) FILTER (WHERE event_type='job_create_failed')                 AS job_failed,
              count(*) FILTER (WHERE event_type='worksite_create_failed')            AS worksite_failed,
              count(*) FILTER (WHERE event_type='sf_field_quarantined')              AS quarantined,
              count(*) FILTER (WHERE event_type='sf_sync_skipped_no_mapping')        AS skipped,
              count(*) FILTER (WHERE event_type='mapping_blocked_no_practice_value') AS blocked_no_practice,
              count(*) FILTER (WHERE event_type='scrape_silent_failure')             AS silent_failure,
              count(*) FILTER (WHERE event_type='sf_scrape_fields_patched')          AS patched_events,
              bool_or(event_type='job_created_in_salesforce')                 AS created_record,
              SUM(jsonb_array_length(COALESCE(payload->'fields_changed','[]'::jsonb)))
                FILTER (WHERE event_type='sf_scrape_fields_patched')          AS patched_fields
            FROM job_event_log
            WHERE run_id = %s AND job_id = ANY(%s)
            GROUP BY job_id
            """,
            (link_run_id, job_ids),
        )
        for row in cur.fetchall():
            jid, sf_err, jf, wf, q, sk, bnp, sf_sil, p_evt, created, p_fld = row
            out.setdefault(jid, {}).update({
                "sf_error":            int(sf_err or 0),
                "job_failed":          int(jf or 0),
                "worksite_failed":     int(wf or 0),
                "quarantined":         int(q or 0),
                "skipped":             int(sk or 0),
                "blocked_no_practice": int(bnp or 0),
                "silent_failure":      int(sf_sil or 0),
                "patched_events":      int(p_evt or 0),
                "patched_fields":      int(p_fld or 0),
                "created_record":      bool(created),
            })

        # 2. Recent unresolved failures (same rule as the Stuck list).
        cur.execute(
            """
            SELECT jel.job_id, count(*)::int AS n
            FROM job_event_log jel
            WHERE jel.job_id = ANY(%s)
              AND jel.event_type IN ('job_create_failed', 'worksite_create_failed')
              AND jel.created_at >= NOW() - INTERVAL '24 hours'
              AND NOT EXISTS (
                SELECT 1 FROM job_event_log ok
                WHERE ok.job_id = jel.job_id
                  AND ok.event_type = 'job_created_in_salesforce'
                  AND ok.created_at >= jel.created_at
              )
              AND NOT EXISTS (
                SELECT 1 FROM job_event_log rs
                WHERE rs.job_id = jel.job_id
                  AND rs.event_type IN ('manual_rescrape_completed','auto_retry_completed')
                  AND rs.created_at >= jel.created_at
                  AND COALESCE(rs.payload->>'action','') IN ('re_scraped','re_scraped_with_warning')
              )
            GROUP BY jel.job_id
            """,
            (job_ids,),
        )
        for jid, n in cur.fetchall():
            out.setdefault(jid, {})["recent_unresolved"] = int(n)

        # 3. Recent history (auto retries, manual rescrapes, failures — last 24h).
        cur.execute(
            """
            SELECT job_id,
              count(*) FILTER (WHERE event_type='auto_retry_completed')      AS auto_n,
              count(*) FILTER (WHERE event_type='manual_rescrape_completed') AS manual_n,
              count(*) FILTER (WHERE event_type IN ('job_create_failed','worksite_create_failed','sf_scrape_fields_error')) AS fail_n
            FROM job_event_log
            WHERE job_id = ANY(%s)
              AND created_at >= NOW() - INTERVAL '24 hours'
            GROUP BY job_id
            """,
            (job_ids,),
        )
        for jid, a, m, f in cur.fetchall():
            out.setdefault(jid, {}).update({
                "history_auto":   int(a or 0),
                "history_manual": int(m or 0),
                "history_failed": int(f or 0),
            })

        # 4. Latest job_content state (sf_job_id, and "empty row written in this run" detection).
        cur.execute(
            """
            SELECT DISTINCT ON (job_id)
              job_id,
              sf_job_id,
              run_id,
              COALESCE(title_line,'') = '' AS empty_title,
              COALESCE(description_full_text,'') = '' AS empty_desc
            FROM job_content
            WHERE job_id = ANY(%s) OR job_post_id = ANY(%s)
            ORDER BY job_id, created_at DESC
            """,
            (job_ids, job_ids),
        )
        for jid, sfid, rid, et, ed in cur.fetchall():
            if jid:
                out.setdefault(jid, {})["sf_job_id"] = sfid
        # Empty-row detection in THIS run only (catches the 19782 case: job_id=NULL, run_id=this).
        cur.execute(
            """
            SELECT job_post_id,
              bool_or(job_id IS NULL OR COALESCE(title_line,'') = '')  AS has_empty
            FROM job_content
            WHERE run_id = %s AND job_post_id = ANY(%s)
            GROUP BY job_post_id
            """,
            (link_run_id, job_ids),
        )
        for jpost, has_empty in cur.fetchall():
            if jpost and has_empty:
                out.setdefault(jpost, {})["empty_row_this_run"] = True

    return out

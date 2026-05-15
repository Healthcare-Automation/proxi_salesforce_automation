"""
Modal jobs: scraping pipeline (every 10 min) + daily summary report.

Functions
---------
scrape_gmail_job   — Gmail → Playwright → Supabase → Salesforce (every 10 min)
daily_summary_job  — Query Supabase, validate quality, send digest email (daily 9 AM ET)

Secrets (salesforce-automation): GMAIL_APP_PASSWORD, GMAIL_EMAIL, DB_PASSWORD,
KIMEDICS_EMAIL, KIMEDICS_PASSWORD, OPENAI_API_KEY, plus Salesforce env vars.

Deploy (from project root) — registers **both** scheduled functions on app ``salesforce-automation``:
  modal deploy src/production/scrape_gmail_modal.py

  - ``scrape_gmail_job`` — every 10 min; Gmail → Kimedics → Supabase → SF; per-job validation + alert emails
  - ``daily_summary_job`` — daily cron (13:00 UTC ≈ 9 AM Eastern); 24 h Supabase stats + validation rollup digest email

Run once (manual):
  modal run src/production/scrape_gmail_modal.py::run_once
  modal run src/production/scrape_gmail_modal.py::run_daily_summary_once
"""

import os
import sys
from pathlib import Path
from typing import Optional

import modal

# Band-aid for parser regex paths that recurse on unusual Kimedics body_text
# (e.g. login-wall HTML interleaved with the real page). Pairs with the
# try/except around _reconcile_practice_value_against_sf and the traceback
# capture in playwright_job_scrape.py so we get a full stack on the next
# occurrence rather than just losing the row.
sys.setrecursionlimit(5000)

_modal_dir = Path(__file__).resolve().parent
_src_root  = _modal_dir.parent

# Full image: Playwright + psycopg2 (for scrape runs)
_full_image = (
    modal.Image.debian_slim()
    .pip_install("psycopg2-binary", "playwright", "python-dotenv", "openai>=1.0.0")
    .run_commands("playwright install chromium", "playwright install-deps chromium")
    .add_local_dir(_src_root / "utils", remote_path="/root/utils")
)

# Light image: psycopg2 only (for daily summary — no browser needed)
_light_image = (
    modal.Image.debian_slim()
    .pip_install("psycopg2-binary", "python-dotenv")
    .add_local_dir(_src_root / "utils", remote_path="/root/utils")
)

# Endpoint image: adds FastAPI so Modal's fastapi_endpoint decorator works.
_endpoint_image = (
    modal.Image.debian_slim()
    .pip_install("psycopg2-binary", "python-dotenv", "fastapi")
    .add_local_dir(_src_root / "utils", remote_path="/root/utils")
)

# Rescrape endpoint image: Playwright + FastAPI (needs the browser to actually rescrape).
_rescrape_image = (
    modal.Image.debian_slim()
    .pip_install("psycopg2-binary", "playwright", "python-dotenv", "openai>=1.0.0", "fastapi")
    .run_commands("playwright install chromium", "playwright install-deps chromium")
    .add_local_dir(_src_root / "utils", remote_path="/root/utils")
)

app = modal.App("salesforce-automation")

# Gmail fetch window. Wide on purpose: every 10-min cron tick re-checks the
# last day's emails, so anything we missed (Modal preempted before
# log_email_scrapes committed, IMAP blip, a skipped cron tick) gets logged
# within ~10 min instead of being lost forever. Dedup against email_scrapes
# (job_post_id, date) prevents reprocessing. SUPABASE_LOOKBACK_HOURS must be
# >= EMAIL_HOURS or the dedup set misses edge-of-window rows.
EMAIL_HOURS             = 24.0
SUPABASE_LOOKBACK_HOURS = 26.0

# Immediately alert when a job triggers either of these thresholds.
# (Matching logic lives in scrape_validator.should_send_immediate_alert)
_WARN_THRESHOLD = 3   # 3+ warnings → send alert even without CRITICAL


# ── Scrape pipeline (every 10 min) ────────────────────────────────────────────

@app.function(
    image=_full_image,
    schedule=modal.Period(minutes=10),
    secrets=[modal.Secret.from_name("salesforce-automation")],
    timeout=600,
)
def scrape_gmail_job():
    sys.path.insert(0, "/root")
    from utils.gmail import parse_kimedics_job_email, scrape_emails_from_sender
    from utils.playwright_job_scrape import scrape_job_pages
    from utils.supabase_db import (
        ensure_tables,
        filter_parsed_emails_not_logged,
        get_conn,
        get_connection_string,
        get_existing_email_keys,
        log_email_scrapes,
        log_run_finish,
        log_run_start,
    )
    from utils.pipeline_link_scrape import process_link_scrape_batch

    email_account    = os.environ.get("GMAIL_EMAIL", "proxi@scrubnetwork.com")
    email_password   = os.environ.get("GMAIL_APP_PASSWORD", "")
    kimedics_email   = os.environ.get("KIMEDICS_EMAIL", "").strip()
    kimedics_password = os.environ.get("KIMEDICS_PASSWORD", "").strip()
    from_email = "donotreply@kimedics.com"

    if not email_password:
        print("GMAIL_APP_PASSWORD not set in secret")
        return 0

    raw_emails = scrape_emails_from_sender(
        email_account=email_account,
        email_password=email_password,
        from_email=from_email,
        hours=EMAIL_HOURS,
        max_results=500,
    )
    parsed = [parse_kimedics_job_email(e) for e in raw_emails]

    conn = None
    try:
        import psycopg2
        conn_str = get_connection_string()
        has_pwd  = bool(os.environ.get("DB_PASSWORD", "").strip())
        print(f"DB_PASSWORD set: {has_pwd}, connecting to Supabase...")
        conn = psycopg2.connect(conn_str)
    except Exception as e:
        err_msg = str(e)
        if "password" in err_msg.lower() or "auth" in err_msg.lower():
            print("Supabase auth failed: check DB_PASSWORD in Modal secret")
        else:
            print(f"Supabase connection failed: {err_msg}")
        return 0

    try:
        ensure_tables(conn)
        existing  = get_existing_email_keys(conn, since_hours_ago=SUPABASE_LOOKBACK_HOURS)
        new_rows  = filter_parsed_emails_not_logged(parsed, existing)

        if not new_rows:
            print("No new emails in the window (all already logged)")
            return 0

        csv_fields = ["job_post_id", "location", "action_or_change", "view_job_link", "subject", "date", "from_"]
        run_id = log_run_start(conn, "gmail", csv_fields)
        if not run_id:
            return 0
        email_scrape_ids = log_email_scrapes(conn, run_id, new_rows, csv_fields)
        log_run_finish(conn, run_id)
        conn.commit()
    finally:
        conn.close()

    print(f"Logged {len(new_rows)} new email(s) to Supabase (run_id={run_id})")

    with_links = [
        (new_rows[i], email_scrape_ids[i])
        for i in range(len(new_rows))
        if (new_rows[i].get("view_job_link") or "").strip() and i < len(email_scrape_ids)
    ]
    if not with_links:
        return len(new_rows)

    if not kimedics_email or not kimedics_password:
        print("KIMEDICS_EMAIL / KIMEDICS_PASSWORD not set; skipping link scrape")
        return len(new_rows)

    print(f"Scraping {len(with_links)} job page(s) with Playwright...")
    scrape_results = scrape_job_pages(with_links, kimedics_email, kimedics_password)

    link_run_id = None
    with get_conn() as conn:
        if conn:
            link_run_id = log_run_start(conn, "link_batch", ["job_post_id", "error"])
            process_link_scrape_batch(
                conn,
                link_run_id=link_run_id,
                scrape_results=scrape_results,
                schema="public",
            )
            if link_run_id:
                log_run_finish(conn, link_run_id)

    # Check for authentication failures and send immediate alert
    auth_failures = [r for r in scrape_results if r.get("authentication_failed")]
    if auth_failures:
        print(f"\n⚠️ AUTHENTICATION FAILURES DETECTED: {len(auth_failures)} job(s) failed due to login issues!")
        print("Failed jobs:")
        for failure in auth_failures:
            print(f"  - Job #{failure['job_post_id']}: {failure.get('error', 'Unknown error')}")

        # Send immediate alert email
        try:
            from utils.alert_email import send_authentication_failure_alert
            alert_sent = send_authentication_failure_alert(
                failed_jobs=auth_failures,
                total_jobs=len(scrape_results),
            )
            if alert_sent:
                print("Authentication failure alert email sent!")
            else:
                print("Failed to send authentication failure alert email")
        except ImportError:
            # If the alert function doesn't exist yet, at least log it prominently
            print("\n" + "=" * 80)
            print("CRITICAL: Authentication failures detected but alert function not available!")
            print("Manual intervention required - Kimedics login may be broken!")
            print("=" * 80 + "\n")

    ok = sum(1 for r in scrape_results if not r.get("error"))
    failed = len(scrape_results) - ok
    print(f"\nLink scrape done: {ok}/{len(scrape_results)} successful, {failed} failed")

    if failed > 0:
        print(f"⚠️ {failed} job(s) had errors - check logs for details")

    # Tail: auto-recover unresolved SF push errors from the last 3 h. Best-effort;
    # swallow exceptions so a recovery bug never breaks the primary scrape loop.
    try:
        from utils.sf_push_recovery import recover_recent_failures, resolve_sf_credentials
        creds = resolve_sf_credentials()
        if creds is None:
            print("Recovery: SF credentials unavailable; skipping auto-recovery")
        else:
            instance_url, access_token = creds
            with get_conn() as rec_conn:
                if rec_conn:
                    results = recover_recent_failures(
                        rec_conn,
                        access_token=access_token,
                        instance_url=instance_url,
                        hours=3.0,
                        recovery_run_id=link_run_id,
                        invocation="modal_auto",
                        invoker=f"modal:scrape_gmail_job:{link_run_id or 'unknown'}",
                    )
                    rec_conn.commit()
                    recovered = sum(1 for r in results if r.action == "re_parsed")
                    dropped   = sum(1 for r in results if r.action == "field_dropped")
                    transient = sum(1 for r in results if r.action == "transient_retried")
                    quarantined = sum(1 for r in results if r.action == "quarantined")
                    unhandled = sum(1 for r in results if r.action == "unhandled")
                    print(
                        f"Recovery: {len(results)} candidates · re_parsed={recovered} "
                        f"field_dropped={dropped} transient_retried={transient} "
                        f"quarantined={quarantined} unhandled={unhandled}"
                    )
    except Exception as e:
        print(f"Recovery failed: {e}")

    # Tail: auto-retry orphaned email_scrapes (failed/never-processed). Picks
    # up jobs that earlier runs couldn't scrape (Modal preemption, broken auth,
    # AUTH_BROKEN_SKIP_THRESHOLD, parser crashes) once Kimedics is healthy
    # again. Same anchor as the original — uses the existing email_scrape_id,
    # so the resulting job_content links back to the original gmail run and
    # the validation popup remains coherent.
    try:
        _auto_retry_orphaned_scrapes(
            kimedics_email=kimedics_email,
            kimedics_password=kimedics_password,
        )
    except Exception as e:
        print(f"Auto-retry tail failed (non-fatal): {e}")

    return len(new_rows)


# Per-cron caps. Keep the auto-retry tail bounded so it never threatens the
# primary 10-min Modal timeout, even with the new circuit breaker.
_AUTO_RETRY_MAX_PER_CRON = 3        # at most N orphaned jobs picked per run
_AUTO_RETRY_MAX_ATTEMPTS = 6        # give up after this many auto-retries
# Exponential backoff: 5min, 10, 20, 40, 80, 160 → ~5.3h total before giveup.
_AUTO_RETRY_BACKOFF_BASE_MIN = 5
# Don't retry until the original cron has had a chance to run + finish.
_AUTO_RETRY_GRACE_MIN = 5
# Don't bother with email_scrapes older than this — let the operator decide.
_AUTO_RETRY_LOOKBACK_HOURS = 24


def _auto_retry_orphaned_scrapes(*, kimedics_email: str, kimedics_password: str) -> None:
    """
    Find email_scrapes rows with no matching job_content (orphaned by an
    earlier failure) and re-run the scrape pipeline against them, with
    exponential backoff so we don't thrash a known-broken job. Each attempt
    emits an ``auto_retry_completed`` event mirroring the manual_rescrape
    audit shape — the hub surfaces these in the Manual push log and uses
    them as a resolution path on the Stuck job creation list.
    """
    sys.path.insert(0, "/root")
    from utils.playwright_job_scrape import scrape_job_pages
    from utils.pipeline_link_scrape import process_link_scrape_batch
    from utils.supabase_db import get_conn, log_job_event, log_run_finish, log_run_start

    if not kimedics_email or not kimedics_password:
        print("Auto-retry: KIMEDICS credentials missing; skipping")
        return

    candidates: list[dict] = []
    with get_conn() as conn:
        if conn is None:
            return
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                  es.id            AS email_scrape_id,
                  es.job_post_id   AS job_id,
                  es.run_id        AS gmail_run_id,
                  es.view_job_link AS view_job_link,
                  es.subject       AS subject,
                  es.action_or_change AS action_or_change,
                  es."date"        AS email_date,
                  es.created_at    AS email_created_at,
                  COALESCE(att.n, 0)            AS prior_attempts,
                  att.last_attempt_at           AS last_attempt_at
                FROM email_scrapes es
                LEFT JOIN job_content jc ON jc.email_scrape_id = es.id
                LEFT JOIN LATERAL (
                  SELECT count(*)::int AS n, MAX(jel.created_at) AS last_attempt_at
                  FROM job_event_log jel
                  WHERE jel.event_type = 'auto_retry_completed'
                    AND (jel.payload->>'email_scrape_id') = es.id::text
                ) att ON true
                WHERE jc.id IS NULL
                  AND es.job_post_id IS NOT NULL
                  AND es.job_post_id <> ''
                  AND es.created_at >= NOW() - (%s::text || ' hours')::interval
                  AND es.created_at <  NOW() - (%s::text || ' minutes')::interval
                  AND COALESCE(att.n, 0) < %s
                  AND (
                    att.last_attempt_at IS NULL
                    OR att.last_attempt_at <
                       NOW() - ((%s * POWER(2, COALESCE(att.n, 0)))::text || ' minutes')::interval
                  )
                ORDER BY COALESCE(att.n, 0) ASC, es.created_at DESC
                LIMIT %s
                """,
                (
                    _AUTO_RETRY_LOOKBACK_HOURS,
                    _AUTO_RETRY_GRACE_MIN,
                    _AUTO_RETRY_MAX_ATTEMPTS,
                    _AUTO_RETRY_BACKOFF_BASE_MIN,
                    _AUTO_RETRY_MAX_PER_CRON,
                ),
            )
            for row in cur.fetchall():
                candidates.append({
                    "email_scrape_id": int(row[0]),
                    "job_id": str(row[1] or ""),
                    "gmail_run_id": int(row[2]) if row[2] is not None else None,
                    "view_job_link": (row[3] or "").strip(),
                    "subject": row[4] or "",
                    "action_or_change": row[5] or "",
                    "email_date": row[6],
                    "email_created_at": row[7],
                    "prior_attempts": int(row[8] or 0),
                })

    if not candidates:
        return

    print(f"Auto-retry: picking up {len(candidates)} orphaned scrape(s)")

    # Build the with_links tuples the way the cron does. Use the canonical
    # Kimedics URL (rebuilt from job_post_id) rather than the SendGrid redirect
    # — those tokens expire and we've already failed once on this URL.
    with_links: list[tuple[dict, int]] = []
    for c in candidates:
        synthetic_row = {
            "job_post_id": c["job_id"],
            "view_job_link": f"https://portal.kimedics.com/app/workspace/job-posts/{c['job_id']}",
            "subject": c["subject"],
            "action_or_change": c["action_or_change"],
            "date": c["email_date"] or c["email_created_at"],
        }
        with_links.append((synthetic_row, c["email_scrape_id"]))

    # Re-scrape using the same Playwright pipeline (now retry-capped + circuit
    # broken). If auth is broken, the circuit breaker fast-fails the rest and
    # the next cron will pick them up.
    scrape_results = scrape_job_pages(with_links, kimedics_email, kimedics_password)

    retry_run_id: Optional[int] = None
    touched: set[str] = set()
    with get_conn() as conn:
        if conn is None:
            return
        retry_run_id = log_run_start(conn, "link_batch", ["job_post_id", "error", "auto_retry"])
        try:
            touched = process_link_scrape_batch(
                conn,
                link_run_id=retry_run_id,
                scrape_results=scrape_results,
                schema="public",
            )
            if retry_run_id:
                log_run_finish(conn, retry_run_id)
            conn.commit()
        except Exception as e:
            try: conn.rollback()
            except Exception: pass
            print(f"Auto-retry: pipeline write failed: {e}")
            return

    # Audit: emit one auto_retry_completed event per scraped job. Same payload
    # shape as manual_rescrape_completed so the hub's Manual push log query
    # can surface it with no extra logic.
    try:
        with get_conn() as ev_conn:
            if ev_conn:
                for c, r in zip(candidates, scrape_results):
                    cl = r.get("cleaned") or {}
                    jid = (cl.get("job_id") or r.get("job_post_id") or c["job_id"]).strip()
                    if not jid:
                        continue
                    parse_ok = bool((cl.get("title_line") or "").strip())
                    err = r.get("error") or ""
                    if err == "auth_broken_skipped":
                        action = "auth_broken_skipped"
                    elif parse_ok and not err:
                        action = "re_scraped"
                    elif parse_ok and err:
                        action = "re_scraped_with_warning"
                    else:
                        action = "rescrape_parse_failed"
                    log_job_event(
                        ev_conn,
                        job_id=jid,
                        event_type="auto_retry_completed",
                        run_id=retry_run_id,
                        schema="public",
                        payload={
                            "invocation": "auto_retry",
                            "invoker": f"modal:scrape_gmail_job:{retry_run_id or 'unknown'}",
                            "action": action,
                            "link_run_id": retry_run_id,
                            "email_scrape_id": str(c["email_scrape_id"]),
                            "original_run_id": c["gmail_run_id"],
                            "attempt_n": c["prior_attempts"] + 1,
                            "parse_ok": parse_ok,
                            "title_line": cl.get("title_line") or "",
                            "description_present": bool((cl.get("description_full_text") or "").strip()),
                            "sf_job_id": cl.get("sf_job_id") or None,
                            "error": err[:500] if err else None,
                        },
                    )
                ev_conn.commit()
    except Exception as e:
        print(f"Auto-retry: audit log failed (non-fatal): {e}")

    ok = sum(1 for r in scrape_results if not r.get("error") and (r.get("cleaned") or {}).get("title_line"))
    skipped = sum(1 for r in scrape_results if (r.get("error") or "") == "auth_broken_skipped")
    print(
        f"Auto-retry done: {ok}/{len(scrape_results)} succeeded "
        f"(skipped={skipped}, touched={sorted(touched)}, retry_run_id={retry_run_id})"
    )


# ── Daily summary (00:00 ET = 04:00 UTC during EDT, ≈1 PM KST) ──────────────
# We fire at 04:00 UTC so the report lands at midnight Eastern (the user's
# preferred slot) and ≈13:00 KST for the Korea-based operator. Under EST
# (winter) this becomes 23:00 ET — still "end of the previous calendar day"
# which is what the digest reports on, so the semantics stay the same.

@app.function(
    image=_light_image,
    schedule=modal.Cron("0 4 * * *"),
    secrets=[modal.Secret.from_name("salesforce-automation")],
    timeout=120,
)
def daily_summary_job():
    """
    Query the last 24 h of activity from Supabase, run quality checks across all
    scraped jobs, and send a digest email to the alert recipients.
    """
    sys.path.insert(0, "/root")
    from utils.supabase_db import get_conn
    from utils.scrape_validator import (
        validate_scraped_job,
        should_send_immediate_alert,
        issues_as_text,
        issues_summary,
    )
    from utils.alert_email import send_daily_summary

    print("daily_summary_job: starting...")

    stats = _build_daily_stats(get_conn, validate_scraped_job, issues_as_text, issues_summary)
    ok = send_daily_summary(stats)
    print(f"daily_summary_job: email sent={ok}, stats={stats.get('scrape_attempts')} attempts")
    return ok


# ── Weekly client-facing pulse (Monday 9 AM ET = 13:00 UTC) ──────────────────

@app.function(
    image=_light_image,
    schedule=modal.Cron("0 13 * * 1"),  # Monday 13:00 UTC ≈ 9 AM ET
    secrets=[modal.Secret.from_name("salesforce-automation")],
    timeout=180,
)
def weekly_summary_job():
    """
    Build and send the C-level weekly pulse: a single email summarizing the
    previous calendar week's automation activity with throughput, ROI, and
    reliability framed for a non-technical executive audience.
    """
    sys.path.insert(0, "/root")
    from utils.supabase_db import get_conn
    from utils.alert_email import send_weekly_summary

    print("weekly_summary_job: starting...")
    stats = _build_weekly_stats(get_conn)
    ok = send_weekly_summary(stats)
    print(f"weekly_summary_job: email sent={ok}, emails={stats.get('emails_received')}")
    return ok


# ── Admin-triggered rescrape web endpoint (called by automation-hub) ──────────
#
# Re-runs the Kimedics scrape for an explicit list of job_ids — used when a
# scheduled scrape produced an empty job_content row (login wall, page-structure
# change) and the job sits stuck without an SF Job__c record. Payload:
#   { "token": "...", "jobIds": ["19722", ...], "dryRun": false, "invoker": "..." }
# Response: { "ok": true, "results": [...], "linkRunId": N, "counts": {...} }

@app.function(
    image=_rescrape_image,
    secrets=[modal.Secret.from_name("salesforce-automation")],
    timeout=600,
)
@modal.fastapi_endpoint(method="POST")
def manual_rescrape_endpoint(payload: dict):
    sys.path.insert(0, "/root")
    from fastapi import HTTPException

    # Accept either env-var name so the secret can be added under whichever key
    # the operator already configured on the hub side.
    expected = (
        os.environ.get("RESCRAPE_ENDPOINT_TOKEN")
        or os.environ.get("MODAL_RESCRAPE_TOKEN")
        or ""
    ).strip()
    supplied = (payload.get("token") or "").strip() if isinstance(payload, dict) else ""
    if not expected or supplied != expected:
        raise HTTPException(status_code=401, detail="unauthorized")

    job_ids = [str(x).strip() for x in (payload.get("jobIds") or []) if str(x).strip()]
    if not job_ids:
        raise HTTPException(status_code=400, detail="jobIds required")
    dry_run = bool(payload.get("dryRun"))
    invoker = str(payload.get("invoker") or "admin_ui")

    from utils.playwright_job_scrape import scrape_job_pages
    from utils.pipeline_link_scrape import process_link_scrape_batch
    from utils.supabase_db import get_conn, log_run_finish, log_run_start

    kimedics_email = (os.environ.get("KIMEDICS_EMAIL") or "").strip()
    kimedics_password = (os.environ.get("KIMEDICS_PASSWORD") or "").strip()
    if not kimedics_email or not kimedics_password:
        raise HTTPException(status_code=503, detail="KIMEDICS credentials not configured")

    # For each requested job_id, look up the most recent email_scrapes row so the
    # rescrape is anchored to the original email (so the resulting job_content
    # row links back through email_scrape_id, matching the cron pipeline).
    # Construct the Kimedics URL directly from the job_post_id rather than using
    # the SendGrid redirect from the email — those tokens expire.
    with_links: list[tuple[dict, int]] = []
    skipped: list[dict] = []
    seen_email_scrape_ids: set[int] = set()

    with get_conn() as conn_lookup:
        if conn_lookup is None:
            raise HTTPException(status_code=503, detail="DB connection unavailable")
        with conn_lookup.cursor() as cur:
            for jid in job_ids:
                cur.execute(
                    """
                    SELECT id, job_post_id, view_job_link, subject, action_or_change, created_at, "date"
                    FROM email_scrapes
                    WHERE job_post_id = %s
                    ORDER BY created_at DESC
                    LIMIT 1
                    """,
                    (jid,),
                )
                row = cur.fetchone()
                if not row:
                    skipped.append({"job_id": jid, "reason": "no_email_scrape_found"})
                    continue
                es_id, job_post_id, _link, subject, action, created_at, email_date = row
                if int(es_id) in seen_email_scrape_ids:
                    continue
                seen_email_scrape_ids.add(int(es_id))
                synthetic_row = {
                    "job_post_id": str(job_post_id or jid),
                    "view_job_link": f"https://portal.kimedics.com/app/workspace/job-posts/{job_post_id or jid}",
                    "subject": subject or "",
                    "action_or_change": action or "",
                    "date": email_date or created_at,
                }
                with_links.append((synthetic_row, int(es_id)))

    if not with_links:
        return {
            "ok": True,
            "linkRunId": None,
            "counts": {},
            "results": [],
            "skipped": skipped,
            "dryRun": dry_run,
            "invoker": invoker,
        }

    print(f"manual_rescrape: scraping {len(with_links)} job page(s) (invoker={invoker})")
    if dry_run:
        return {
            "ok": True,
            "linkRunId": None,
            "counts": {},
            "results": [{"job_id": r[0]["job_post_id"], "action": "would_rescrape"} for r in with_links],
            "skipped": skipped,
            "dryRun": True,
            "invoker": invoker,
        }

    scrape_results = scrape_job_pages(with_links, kimedics_email, kimedics_password)

    link_run_id = None
    touched: set[str] = set()
    with get_conn() as conn:
        if conn is None:
            raise HTTPException(status_code=503, detail="DB connection unavailable")
        link_run_id = log_run_start(conn, "link_batch", ["job_post_id", "error"])
        try:
            touched = process_link_scrape_batch(
                conn,
                link_run_id=link_run_id,
                scrape_results=scrape_results,
                schema="public",
            )
            if link_run_id:
                log_run_finish(conn, link_run_id)
            conn.commit()
        except Exception as e:
            try:
                conn.rollback()
            except Exception:
                pass
            raise HTTPException(status_code=500, detail=f"rescrape pipeline failed: {e}")

    # Best-effort SF push retry on the just-touched job_ids (mirrors the cron's
    # tail-recovery step) so a freshly written job_content row that hits a
    # transient SF error gets a second chance immediately.
    counts: dict[str, int] = {}
    try:
        from utils.sf_push_recovery import recover_recent_failures, resolve_sf_credentials

        creds = resolve_sf_credentials()
        if creds and touched:
            instance_url, access_token = creds
            with get_conn() as rec_conn:
                if rec_conn:
                    rec_results = recover_recent_failures(
                        rec_conn,
                        access_token=access_token,
                        instance_url=instance_url,
                        hours=1.0,
                        recovery_run_id=link_run_id,
                        invocation="manual_admin_ui",
                        invoker=f"manual_rescrape:{invoker}",
                        job_ids=list(touched),
                    )
                    rec_conn.commit()
                    for rr in rec_results:
                        counts[rr.action] = counts.get(rr.action, 0) + 1
    except Exception as e:
        print(f"manual_rescrape: SF retry tail failed (non-fatal): {e}")

    # Audit: emit one ``manual_rescrape_completed`` event per scraped job so
    # this rescrape shows up in the admin "Manual push log" alongside the
    # existing recovery actions. Best-effort — must not fail the response.
    try:
        from utils.supabase_db import log_job_event

        with get_conn() as ev_conn:
            if ev_conn:
                for r in scrape_results:
                    cl = r.get("cleaned") or {}
                    jid = (cl.get("job_id") or r.get("job_post_id") or "").strip()
                    if not jid:
                        continue
                    parse_ok = bool((cl.get("title_line") or "").strip())
                    err = r.get("error") or ""
                    if parse_ok and not err:
                        action = "re_scraped"
                    elif parse_ok and err:
                        action = "re_scraped_with_warning"
                    else:
                        action = "rescrape_parse_failed"
                    log_job_event(
                        ev_conn,
                        job_id=jid,
                        event_type="manual_rescrape_completed",
                        run_id=link_run_id,
                        schema="public",
                        payload={
                            "invocation": "manual_admin_ui",
                            "invoker": invoker,
                            "action": action,
                            "link_run_id": link_run_id,
                            "parse_ok": parse_ok,
                            "title_line": cl.get("title_line") or "",
                            "description_present": bool((cl.get("description_full_text") or "").strip()),
                            "sf_job_id": cl.get("sf_job_id") or None,
                            "error": err[:500] if err else None,
                        },
                    )
                ev_conn.commit()
    except Exception as e:
        print(f"manual_rescrape: event audit log failed (non-fatal): {e}")

    return {
        "ok": True,
        "linkRunId": link_run_id,
        "counts": counts,
        "results": [
            {
                "job_id": r.get("cleaned", {}).get("job_id") or r.get("job_post_id"),
                "job_post_id": r.get("job_post_id"),
                "error": r.get("error"),
                "title_line": (r.get("cleaned") or {}).get("title_line"),
                "description_present": bool((r.get("cleaned") or {}).get("description_full_text")),
            }
            for r in scrape_results
        ],
        "skipped": skipped,
        "touchedJobIds": sorted(touched),
        "dryRun": False,
        "invoker": invoker,
    }


# ── Admin-triggered recovery web endpoint (called by automation-hub) ──────────
#
# Exposes the recovery engine as a token-auth'd HTTP POST. Payload:
#   { "jobIds": ["19664", ...], "sinceHours": 48, "dryRun": false }
# Response: { "results": [...], "counts": { "re_parsed": N, ... } }

@app.function(
    image=_endpoint_image,
    secrets=[modal.Secret.from_name("salesforce-automation")],
    timeout=300,
)
@modal.fastapi_endpoint(method="POST")
def recovery_endpoint(payload: dict):
    sys.path.insert(0, "/root")
    from fastapi import HTTPException, Header, Request
    from utils.sf_push_recovery import recover_recent_failures, resolve_sf_credentials
    from utils.supabase_db import get_conn
    from datetime import datetime as _dt, timedelta as _td, timezone as _tz

    expected = (os.environ.get("RECOVERY_ENDPOINT_TOKEN") or "").strip()
    supplied = (payload.get("token") or "").strip() if isinstance(payload, dict) else ""
    if not expected or supplied != expected:
        raise HTTPException(status_code=401, detail="unauthorized")

    job_ids    = [str(x).strip() for x in (payload.get("jobIds") or []) if str(x).strip()]
    since_hrs  = float(payload.get("sinceHours") or 48.0)
    dry_run    = bool(payload.get("dryRun"))
    invoker    = str(payload.get("invoker") or "admin_ui")

    since = _dt.now(_tz.utc) - _td(hours=since_hrs)

    creds = None if dry_run else resolve_sf_credentials()
    instance_url = creds[0] if creds else None
    access_token = creds[1] if creds else None
    if not dry_run and not creds:
        raise HTTPException(status_code=503, detail="SF credentials not configured")

    with get_conn() as conn:
        if conn is None:
            raise HTTPException(status_code=503, detail="DB connection unavailable")
        results = recover_recent_failures(
            conn,
            access_token=access_token,
            instance_url=instance_url,
            since=since,
            schema="public",
            dry_run=dry_run,
            job_ids=job_ids or None,
            invocation="manual_admin_ui",
            invoker=invoker,
        )
        if not dry_run:
            conn.commit()

    counts: dict[str, int] = {}
    for r in results:
        counts[r.action] = counts.get(r.action, 0) + 1
    return {
        "ok": True,
        "dryRun": dry_run,
        "counts": counts,
        "results": [r.to_dict() for r in results],
    }


def _pair_runs(runs: list[dict]) -> list[dict]:
    """
    Pair consecutive gmail + link_batch runs into single combined entries.
    A gmail run and the next link_batch whose ``started_at`` is within
    ``_GMAIL_LINK_BATCH_PAIR_WINDOW_SEC`` of the gmail run's ``started_at`` are
    treated as one pipeline (covers Gmail + Playwright + DB within Modal).
    Returns list ordered most-recent first.
    """
    # Keep in sync with automation-hub ``PAIRED_CTE`` pairing window (gmail started_at → link_batch).
    _GMAIL_LINK_BATCH_PAIR_WINDOW_SEC = 15 * 60

    def _secs(r: dict) -> float | None:
        s = r.get("started_at")
        f = r.get("finished_at")
        if s and f:
            try:
                return (f - s).total_seconds()
            except Exception:
                pass
        return None

    def _fmt_dur(seconds: float | None) -> str:
        if seconds is None:
            return "—"
        s = int(seconds)
        if s < 60:
            return f"{s}s"
        return f"{s // 60}m {s % 60}s"

    paired: list[dict] = []
    used: set[int] = set()

    for i, r in enumerate(runs):
        if r["run_id"] in used:
            continue
        if r["run_type"] == "gmail":
            partner = None
            for j in range(i + 1, len(runs)):
                nxt = runs[j]
                if nxt["run_id"] in used:
                    continue
                if nxt["run_type"] != "link_batch":
                    continue
                gap = (nxt["started_at"] - r["started_at"]).total_seconds()
                if 0 <= gap <= _GMAIL_LINK_BATCH_PAIR_WINDOW_SEC:
                    partner = nxt
                    break
            if partner:
                used.add(r["run_id"])
                used.add(partner["run_id"])
                g_start = r["started_at"]
                lb_end  = partner.get("finished_at")
                total_secs = (lb_end - g_start).total_seconds() if lb_end else None
                paired.append({
                    "gmail_run_id":      r["run_id"],
                    "link_batch_run_id": partner["run_id"],
                    "started_at":        g_start,
                    "finished_at":       lb_end,
                    "duration":          _fmt_dur(total_secs),
                    "gmail_dur":         _fmt_dur(_secs(r)),
                    "link_batch_dur":    _fmt_dur(_secs(partner)),
                    "paired":            True,
                })
            else:
                # Unpaired gmail (no link_batch followed)
                used.add(r["run_id"])
                paired.append({
                    "gmail_run_id":      r["run_id"],
                    "link_batch_run_id": None,
                    "started_at":        r["started_at"],
                    "finished_at":       r.get("finished_at"),
                    "duration":          _fmt_dur(_secs(r)),
                    "gmail_dur":         _fmt_dur(_secs(r)),
                    "link_batch_dur":    "—",
                    "paired":            False,
                })
        elif r["run_type"] == "link_batch" and r["run_id"] not in used:
            # Orphaned link_batch (shouldn't happen, but handle gracefully)
            used.add(r["run_id"])
            paired.append({
                "gmail_run_id":      None,
                "link_batch_run_id": r["run_id"],
                "started_at":        r["started_at"],
                "finished_at":       r.get("finished_at"),
                "duration":          _fmt_dur(_secs(r)),
                "gmail_dur":         "—",
                "link_batch_dur":    _fmt_dur(_secs(r)),
                "paired":            False,
            })

    # Most-recent first
    paired.sort(key=lambda x: x["started_at"] or "", reverse=True)
    return paired


def _build_daily_stats(get_conn, validate_scraped_job, issues_as_text, issues_summary) -> dict:
    """
    Build the daily report dataset: one row per email_scrape in the day window
    (5:00 AM – 11:59 PM ET of the previous calendar day, DST-aware), with all
    downstream pipeline outcomes joined in (scrape result, SF mapping, field
    patches, new-Job creation, External_Job_ID swap, manual rescrape, auto-
    retry, stuck status). The email-row is the unit of reporting.
    """
    import psycopg2.extras
    from datetime import datetime, time, timezone, timedelta
    try:
        from zoneinfo import ZoneInfo
        ET = ZoneInfo("America/New_York")
    except Exception:
        # Fallback for environments without IANA tzdata: fixed -5 (EST). DST
        # will be off by an hour in the summer — best effort.
        ET = timezone(timedelta(hours=-5))

    # Yesterday 00:00 ET → 23:59:59 ET (full calendar day, ET-based), converted
    # to UTC for the SQL. Full-day window so we never miss anything that
    # arrived between midnight and 5 AM.
    now_et       = datetime.now(ET)
    report_date  = (now_et - timedelta(days=1)).date()
    start_et     = datetime.combine(report_date, time(0, 0, 0), tzinfo=ET)
    end_et       = datetime.combine(report_date, time(23, 59, 59), tzinfo=ET)
    start_utc    = start_et.astimezone(timezone.utc)
    end_utc      = end_et.astimezone(timezone.utc)
    period_label = f"{start_et.strftime('%b %-d, %Y')} · 12:00 AM – 11:59 PM ET"

    stats: dict = {
        "period_label":         period_label,
        "window_start_utc":     start_utc,
        "window_end_utc":       end_utc,
        "emails_received":      0,
        "scraped_ok":           0,
        "sf_mapped":            0,
        "sf_jobs_created":      0,
        "field_patches_total":  0,
        "ext_id_swaps":         0,
        "manual_rescrapes":     0,
        "auto_retries":         0,
        "stuck_jobs":           0,
        "scrape_failures":      0,
        "rows":                 [],
    }

    with get_conn() as conn:
        if conn is None:
            print("daily_summary_job: Supabase connection unavailable")
            return stats

        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            # One row per email_scrape with everything we need joined in.
            # Anchor events on jel.job_id = email_scrape.job_post_id within a
            # generous window after the email (matches the validation popup's
            # behavior).
            cur.execute(
                """
                WITH window_emails AS (
                  -- Window on the *email-received* time (Gmail header ``date``),
                  -- not when our cron logged it. Falls back to created_at if
                  -- date is missing on a historical row.
                  SELECT id, job_post_id, run_id, view_job_link, subject,
                         action_or_change, created_at,
                         COALESCE("date", created_at) AS received_at
                  FROM email_scrapes
                  WHERE COALESCE("date", created_at) >= %(start)s
                    AND COALESCE("date", created_at) <= %(end)s
                ),
                jc_by_email AS (
                  SELECT email_scrape_id,
                    bool_or(COALESCE(title_line, '') <> ''
                            AND COALESCE(description_full_text, '') <> '') AS scrape_ok,
                    bool_or(COALESCE(sf_job_id, '') <> '')                 AS sf_mapped,
                    (array_agg(DISTINCT sf_job_id) FILTER (WHERE COALESCE(sf_job_id,'') <> ''))[1] AS sf_job_id,
                    (array_agg(job_title  ORDER BY created_at DESC) FILTER (WHERE COALESCE(job_title,'') <> ''))[1] AS job_title,
                    (array_agg(posting_org ORDER BY created_at DESC) FILTER (WHERE COALESCE(posting_org,'') <> ''))[1] AS posting_org,
                    (array_agg(practice_value ORDER BY created_at DESC) FILTER (WHERE COALESCE(practice_value,'') <> ''))[1] AS practice_value
                  FROM job_content
                  WHERE email_scrape_id IN (SELECT id FROM window_emails)
                  GROUP BY email_scrape_id
                ),
                events AS (
                  -- Per-email aggregates of downstream events. We anchor by
                  -- job_id (Kimedics) + a generous 7-day window after the
                  -- email arrived. This is the same logic the validation
                  -- popup uses.
                  SELECT we.id AS email_scrape_id,
                    count(*) FILTER (WHERE jel.event_type = 'sf_scrape_fields_patched') AS patch_events,
                    COALESCE(SUM(jsonb_array_length(COALESCE(jel.payload->'fields_changed','[]'::jsonb)))
                             FILTER (WHERE jel.event_type = 'sf_scrape_fields_patched'), 0) AS fields_changed,
                    bool_or(jel.event_type = 'job_created_in_salesforce')              AS created_sf_job,
                    bool_or(
                      jel.event_type = 'sf_scrape_fields_patched'
                      AND jel.payload->'fields_changed' ? 'External_Job_ID__c'
                      AND COALESCE(jel.payload->'prev'->>'External_Job_ID__c','') <> ''
                      AND COALESCE(jel.payload->'prev'->>'External_Job_ID__c','')
                          <> COALESCE(jel.payload->'next'->>'External_Job_ID__c','')
                    ) AS ext_id_swap,
                    bool_or(jel.event_type = 'manual_rescrape_completed')              AS manual_rescraped,
                    bool_or(jel.event_type = 'auto_retry_completed')                   AS auto_retried,
                    -- Amendments / push errors that we want surfaced in the
                    -- daily report (in addition to the existing failure flags).
                    count(*) FILTER (WHERE jel.event_type = 'sf_field_quarantined')        AS fields_quarantined,
                    count(*) FILTER (WHERE jel.event_type = 'sf_scrape_fields_recovered')  AS push_recovered,
                    count(*) FILTER (WHERE jel.event_type = 'sf_scrape_fields_error')      AS push_errors,
                    -- Unresolved field-update errors: a push error with no later
                    -- recovered / patched event. This is the "still broken" signal.
                    bool_or(
                      jel.event_type = 'sf_scrape_fields_error'
                      AND NOT EXISTS (
                        SELECT 1 FROM job_event_log ok2
                        WHERE ok2.job_id = jel.job_id
                          AND ok2.event_type IN ('sf_scrape_fields_recovered','sf_scrape_fields_patched')
                          AND ok2.created_at >= jel.created_at
                      )
                    ) AS push_error_unresolved,
                    -- Unresolved failure (same logic as Stuck job creation list).
                    bool_or(
                      jel.event_type IN ('job_create_failed', 'worksite_create_failed')
                      AND NOT EXISTS (
                        SELECT 1 FROM job_event_log ok
                        WHERE ok.job_id = jel.job_id
                          AND ok.event_type = 'job_created_in_salesforce'
                          AND ok.created_at >= jel.created_at
                      )
                      AND NOT EXISTS (
                        SELECT 1 FROM job_event_log rs
                        WHERE rs.job_id = jel.job_id
                          AND rs.event_type IN ('manual_rescrape_completed', 'auto_retry_completed')
                          AND rs.created_at >= jel.created_at
                          AND COALESCE(rs.payload->>'action','') IN ('re_scraped','re_scraped_with_warning')
                      )
                    ) AS stuck
                  FROM window_emails we
                  LEFT JOIN job_event_log jel
                    ON jel.job_id = we.job_post_id
                   AND jel.created_at >= we.created_at - INTERVAL '5 minutes'
                   AND jel.created_at <= we.created_at + INTERVAL '7 days'
                  GROUP BY we.id
                )
                SELECT
                  we.id, we.job_post_id, we.subject, we.action_or_change,
                  we.view_job_link, we.received_at, we.created_at,
                  COALESCE(jc.scrape_ok, false)    AS scrape_ok,
                  COALESCE(jc.sf_mapped, false)    AS sf_mapped,
                  jc.sf_job_id, jc.job_title, jc.posting_org, jc.practice_value,
                  COALESCE(ev.patch_events, 0)     AS patch_events,
                  COALESCE(ev.fields_changed, 0)   AS fields_changed,
                  COALESCE(ev.created_sf_job, false) AS created_sf_job,
                  COALESCE(ev.ext_id_swap, false)  AS ext_id_swap,
                  COALESCE(ev.manual_rescraped, false) AS manual_rescraped,
                  COALESCE(ev.auto_retried, false) AS auto_retried,
                  COALESCE(ev.fields_quarantined, 0) AS fields_quarantined,
                  COALESCE(ev.push_recovered, 0)   AS push_recovered,
                  COALESCE(ev.push_errors, 0)      AS push_errors,
                  COALESCE(ev.push_error_unresolved, false) AS push_error_unresolved,
                  COALESCE(ev.stuck, false)        AS stuck
                FROM window_emails we
                LEFT JOIN jc_by_email jc ON jc.email_scrape_id = we.id
                LEFT JOIN events      ev ON ev.email_scrape_id = we.id
                -- Sort by Gmail received time (same as the Time column) so
                -- the table reads chronologically by what the operator sees.
                ORDER BY we.received_at ASC
                """,
                {"start": start_utc, "end": end_utc},
            )
            for r in cur.fetchall():
                d = dict(r)
                # "Time" column shows when the email was *received*
                # (Gmail header date), not when our cron logged it.
                d["et_time"] = d["received_at"].astimezone(ET).strftime("%-I:%M %p ET")
                stats["rows"].append(d)

    rows = stats["rows"]
    stats["emails_received"]     = len(rows)
    stats["scraped_ok"]          = sum(1 for r in rows if r["scrape_ok"])
    stats["sf_mapped"]           = sum(1 for r in rows if r["sf_mapped"])
    stats["sf_jobs_created"]     = sum(1 for r in rows if r["created_sf_job"])
    stats["field_patches_total"] = sum(int(r["fields_changed"] or 0) for r in rows)
    stats["ext_id_swaps"]        = sum(1 for r in rows if r["ext_id_swap"])
    stats["manual_rescrapes"]    = sum(1 for r in rows if r["manual_rescraped"])
    stats["auto_retries"]        = sum(1 for r in rows if r["auto_retried"])
    stats["stuck_jobs"]          = sum(1 for r in rows if r["stuck"])
    stats["scrape_failures"]     = sum(1 for r in rows if not r["scrape_ok"] and not r["stuck"])
    # Amendments — SF push errors that recovered (field dropped), fields
    # quarantined by SF, and any push errors still unresolved. These are
    # rows where SF was patched but with edits, or rows where SF rejected
    # some/all of the update — operators wanted these surfaced in the report.
    stats["fields_quarantined"]   = sum(int(r["fields_quarantined"] or 0) for r in rows)
    stats["pushes_recovered"]     = sum(1 for r in rows if int(r["push_recovered"] or 0) > 0)
    stats["push_errors_total"]    = sum(int(r["push_errors"] or 0) for r in rows)
    stats["push_errors_unresolved"] = sum(1 for r in rows if r["push_error_unresolved"])

    return stats


def _build_weekly_stats(get_conn) -> dict:
    """
    Build the C-level weekly pulse dataset for ``send_weekly_summary``.
    Reports on the previous calendar week (Mon 00:00 → Sun 23:59:59 ET), with
    week-over-week deltas computed against the week before that.
    """
    import psycopg2.extras
    from datetime import datetime, time, timezone, timedelta
    try:
        from zoneinfo import ZoneInfo
        ET = ZoneInfo("America/New_York")
    except Exception:
        ET = timezone(timedelta(hours=-5))

    now_et = datetime.now(ET)
    # Previous calendar week (Mon-Sun in ET).
    weekday_today = now_et.weekday()  # Mon=0
    last_sunday   = (now_et - timedelta(days=weekday_today + 1)).date()
    last_monday   = last_sunday - timedelta(days=6)
    prior_sunday  = last_monday - timedelta(days=1)
    prior_monday  = prior_sunday - timedelta(days=6)

    def window_utc(d_start, d_end):
        s = datetime.combine(d_start, time(0, 0, 0), tzinfo=ET).astimezone(timezone.utc)
        e = datetime.combine(d_end,   time(23, 59, 59), tzinfo=ET).astimezone(timezone.utc)
        return s, e

    cur_start_utc, cur_end_utc = window_utc(last_monday,  last_sunday)
    prv_start_utc, prv_end_utc = window_utc(prior_monday, prior_sunday)

    period_label = f"{last_monday.strftime('%b %-d')} – {last_sunday.strftime('%b %-d, %Y')}"

    def _aggregate(get_conn, start_utc, end_utc):
        agg = {
            "emails_received":      0,
            "scraped_ok":           0,
            "sf_mapped":            0,
            "sf_jobs_created":      0,
            "field_patches_total":  0,
            "median_latency_min":   None,
            "daily_counts":         {},   # date_iso → emails_count
            "top_practices":        [],   # [(practice_value, count), ...]
            "top_jobs":             [],   # [{job_id, title, org, sf_job_id, patches}, ...]
            "needs_attention":      0,    # stuck jobs (unresolved) — flagged subtly
        }
        with get_conn() as conn:
            if conn is None:
                return agg
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                # Aggregate via the same email-anchored shape as the daily report.
                cur.execute(
                    """
                    WITH window_emails AS (
                      SELECT id, job_post_id, COALESCE("date", created_at) AS received_at
                      FROM email_scrapes
                      WHERE COALESCE("date", created_at) >= %(start)s
                        AND COALESCE("date", created_at) <= %(end)s
                        AND job_post_id IS NOT NULL AND job_post_id <> ''
                    ),
                    jc AS (
                      SELECT email_scrape_id,
                        bool_or(COALESCE(title_line,'') <> '' AND COALESCE(description_full_text,'') <> '') AS scrape_ok,
                        bool_or(COALESCE(sf_job_id,'')   <> '')                                            AS sf_mapped,
                        (array_agg(job_title    ORDER BY created_at DESC) FILTER (WHERE COALESCE(job_title,'')<>''))[1]   AS job_title,
                        (array_agg(posting_org  ORDER BY created_at DESC) FILTER (WHERE COALESCE(posting_org,'')<>''))[1] AS posting_org,
                        (array_agg(practice_value ORDER BY created_at DESC) FILTER (WHERE COALESCE(practice_value,'')<>''))[1] AS practice_value,
                        (array_agg(sf_job_id    ORDER BY created_at DESC) FILTER (WHERE COALESCE(sf_job_id,'')<>''))[1]   AS sf_job_id
                      FROM job_content
                      WHERE email_scrape_id IN (SELECT id FROM window_emails)
                      GROUP BY email_scrape_id
                    ),
                    ev AS (
                      SELECT we.id AS email_scrape_id, we.job_post_id, we.received_at,
                        count(*) FILTER (WHERE jel.event_type = 'sf_scrape_fields_patched') AS patch_events,
                        COALESCE(SUM(jsonb_array_length(COALESCE(jel.payload->'fields_changed','[]'::jsonb)))
                                 FILTER (WHERE jel.event_type = 'sf_scrape_fields_patched'), 0) AS fields_changed,
                        bool_or(jel.event_type = 'job_created_in_salesforce') AS created_sf_job,
                        MIN(jel.created_at) FILTER (WHERE jel.event_type IN ('sf_scrape_fields_patched','job_created_in_salesforce')) AS first_sf_action_at,
                        bool_or(
                          jel.event_type IN ('job_create_failed', 'worksite_create_failed')
                          AND NOT EXISTS (
                            SELECT 1 FROM job_event_log ok
                            WHERE ok.job_id = jel.job_id
                              AND ok.event_type = 'job_created_in_salesforce'
                              AND ok.created_at >= jel.created_at
                          )
                          AND NOT EXISTS (
                            SELECT 1 FROM job_event_log rs
                            WHERE rs.job_id = jel.job_id
                              AND rs.event_type IN ('manual_rescrape_completed', 'auto_retry_completed')
                              AND rs.created_at >= jel.created_at
                              AND COALESCE(rs.payload->>'action','') IN ('re_scraped','re_scraped_with_warning')
                          )
                        ) AS stuck
                      FROM window_emails we
                      LEFT JOIN job_event_log jel ON jel.job_id = we.job_post_id
                        AND jel.created_at >= we.received_at - INTERVAL '5 minutes'
                        AND jel.created_at <= we.received_at + INTERVAL '7 days'
                      GROUP BY we.id, we.job_post_id, we.received_at
                    )
                    SELECT
                      ev.email_scrape_id, ev.job_post_id, ev.received_at,
                      COALESCE(jc.scrape_ok, false) AS scrape_ok,
                      COALESCE(jc.sf_mapped, false) AS sf_mapped,
                      jc.job_title, jc.posting_org, jc.practice_value, jc.sf_job_id,
                      COALESCE(ev.fields_changed, 0)    AS fields_changed,
                      COALESCE(ev.created_sf_job, false) AS created_sf_job,
                      COALESCE(ev.stuck, false)         AS stuck,
                      ev.first_sf_action_at
                    FROM ev
                    LEFT JOIN jc ON jc.email_scrape_id = ev.email_scrape_id
                    """,
                    {"start": start_utc, "end": end_utc},
                )
                rows = [dict(r) for r in cur.fetchall()]

        agg["emails_received"]     = len(rows)
        agg["scraped_ok"]          = sum(1 for r in rows if r["scrape_ok"])
        agg["sf_mapped"]           = sum(1 for r in rows if r["sf_mapped"])
        agg["sf_jobs_created"]     = sum(1 for r in rows if r["created_sf_job"])
        agg["field_patches_total"] = sum(int(r["fields_changed"] or 0) for r in rows)
        agg["needs_attention"]     = sum(1 for r in rows if r["stuck"])

        # Latency (email → first SF action) in minutes.
        latencies: list[float] = []
        for r in rows:
            ra = r["received_at"]
            fa = r["first_sf_action_at"]
            if ra is None or fa is None:
                continue
            try:
                delta = (fa - ra).total_seconds() / 60.0
                if delta >= 0:
                    latencies.append(delta)
            except Exception:
                pass
        if latencies:
            latencies.sort()
            mid = len(latencies) // 2
            med = latencies[mid] if len(latencies) % 2 else (latencies[mid-1] + latencies[mid]) / 2
            agg["median_latency_min"] = round(med, 1)

        # Daily counts (ET date).
        daily: dict = {}
        for r in rows:
            d = r["received_at"].astimezone(ET).date().isoformat()
            daily[d] = daily.get(d, 0) + 1
        agg["daily_counts"] = daily

        # Top practices.
        prac: dict = {}
        for r in rows:
            p = (r.get("practice_value") or "").strip()
            if not p:
                continue
            prac[p] = prac.get(p, 0) + 1
        agg["top_practices"] = sorted(prac.items(), key=lambda x: x[1], reverse=True)[:5]

        # Top job records by patch volume.
        jobs: dict = {}
        for r in rows:
            jid = r.get("job_post_id") or ""
            if not jid:
                continue
            entry = jobs.setdefault(jid, {
                "job_id":      jid,
                "title":       (r.get("job_title") or "").strip(),
                "org":         (r.get("posting_org") or "").strip(),
                "sf_job_id":   r.get("sf_job_id") or "",
                "patches":     0,
                "emails":      0,
            })
            entry["patches"] += int(r.get("fields_changed") or 0)
            entry["emails"]  += 1
        agg["top_jobs"] = sorted(jobs.values(), key=lambda j: j["patches"], reverse=True)[:5]

        return agg

    cur_agg = _aggregate(get_conn, cur_start_utc, cur_end_utc)
    prv_agg = _aggregate(get_conn, prv_start_utc, prv_end_utc)

    # ── "New roles added to SF" — client-facing meaning ─────────────────────
    # The internal event ``job_created_in_salesforce`` only fires when Proxi
    # itself POSTs a new Job__c — but most "new job post" emails match to an
    # already-existing SF record. The number a recruiting executive cares
    # about is: distinct first-time job_post_ids this week that ended up
    # mapped to an SF Job__c (whether Proxi created the record or matched
    # to an existing one).
    def _new_roles_in_sf(get_conn, start_utc, end_utc):
        with get_conn() as conn:
            if conn is None:
                return 0
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT COUNT(DISTINCT es.job_post_id)::int
                    FROM email_scrapes es
                    JOIN job_content   jc ON jc.email_scrape_id = es.id
                    WHERE COALESCE(es."date", es.created_at) >= %s
                      AND COALESCE(es."date", es.created_at) <= %s
                      AND es.action_or_change = 'new'
                      AND COALESCE(jc.sf_job_id, '') <> ''
                    """,
                    (start_utc, end_utc),
                )
                return int(cur.fetchone()[0])
    cur_agg["sf_jobs_created"] = _new_roles_in_sf(get_conn, cur_start_utc, cur_end_utc)
    prv_agg["sf_jobs_created"] = _new_roles_in_sf(get_conn, prv_start_utc, prv_end_utc)

    # ROI: conservative per-task seconds for a recruiter doing the work by
    # hand (open Kimedics → find job → copy → switch → open SF → paste → save).
    SEC_PER_FIELD       = 30
    SEC_PER_NEW_RECORD  = 180
    SEC_PER_EMAIL_AWARE = 45
    # Loaded recruiter cost — conservative; client can override.
    HOURLY_RATE_USD     = 80
    # Manual baseline: typical "next time someone gets to it" lag, in minutes.
    # Used to compute the "Nx faster than manual" headline.
    MANUAL_BASELINE_MIN = 120  # 2 hours

    def _hours_saved(agg):
        return round(
            (agg["field_patches_total"] * SEC_PER_FIELD
             + agg["sf_jobs_created"]   * SEC_PER_NEW_RECORD
             + agg["emails_received"]   * SEC_PER_EMAIL_AWARE) / 3600.0,
            1,
        )

    hours_saved     = _hours_saved(cur_agg)
    hours_saved_prv = _hours_saved(prv_agg)
    dollars_saved   = round(hours_saved * HOURLY_RATE_USD)

    # ── States touched (top 5 + total distinct states) ──────────────────────
    top_states: list[tuple[str, int]] = []
    states_total = 0
    with get_conn() as conn:
        if conn is not None:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT jc.state, count(*)::int AS n
                    FROM job_content jc
                    JOIN email_scrapes es ON es.id = jc.email_scrape_id
                    WHERE COALESCE(es."date", es.created_at) >= %s
                      AND COALESCE(es."date", es.created_at) <= %s
                      AND COALESCE(jc.state, '') <> ''
                    GROUP BY jc.state
                    ORDER BY n DESC
                    """,
                    (cur_start_utc, cur_end_utc),
                )
                for row in cur.fetchall():
                    top_states.append((row[0], int(row[1])))
            states_total = len(top_states)

    # ── Cadence: peak day-of-week + peak 2-hour window over the period ──────
    # Use the daily_counts already computed for day-of-week.
    weekday_totals = {i: 0 for i in range(7)}
    for d_iso, n in cur_agg["daily_counts"].items():
        try:
            d = datetime.strptime(d_iso, "%Y-%m-%d").date()
            weekday_totals[d.weekday()] += n
        except Exception:
            pass
    weekday_names = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
    peak_day_idx = max(weekday_totals, key=weekday_totals.get)
    peak_day = weekday_names[peak_day_idx]
    peak_day_count = weekday_totals[peak_day_idx]

    # Hour-of-day from DB.
    peak_hour, peak_hour_count = None, 0
    with get_conn() as conn:
        if conn is not None:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT EXTRACT(HOUR FROM (COALESCE("date", created_at) AT TIME ZONE 'America/New_York'))::int AS hr,
                           count(*)::int AS n
                    FROM email_scrapes
                    WHERE COALESCE("date", created_at) >= %s AND COALESCE("date", created_at) <= %s
                      AND job_post_id IS NOT NULL AND job_post_id <> ''
                    GROUP BY hr ORDER BY n DESC LIMIT 1
                    """,
                    (cur_start_utc, cur_end_utc),
                )
                row = cur.fetchone()
                if row:
                    peak_hour, peak_hour_count = int(row[0]), int(row[1])

    # ── 4-week throughput trend (emails per week, ending current week) ──────
    trend: list[tuple[str, int]] = []
    with get_conn() as conn:
        if conn is not None:
            with conn.cursor() as cur:
                for back in range(3, -1, -1):
                    wk_end_date = last_sunday - timedelta(days=back * 7)
                    wk_start_date = wk_end_date - timedelta(days=6)
                    ws, we = window_utc(wk_start_date, wk_end_date)
                    cur.execute(
                        """
                        SELECT count(*)::int FROM email_scrapes
                        WHERE COALESCE("date", created_at) >= %s
                          AND COALESCE("date", created_at) <= %s
                          AND job_post_id IS NOT NULL AND job_post_id <> ''
                        """,
                        (ws, we),
                    )
                    n = int(cur.fetchone()[0])
                    trend.append((wk_end_date.strftime("%b %-d"), n))

    # ── Cumulative since launch ─────────────────────────────────────────────
    cum = {
        "emails":       0,
        "patches":      0,
        "fields":       0,
        "new_jobs":     0,
        "launch_iso":   None,
        "hours_saved":  0.0,
    }
    with get_conn() as conn:
        if conn is not None:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT
                      MIN(COALESCE("date", created_at)) AS launch,
                      count(*)::int AS emails
                    FROM email_scrapes
                    WHERE job_post_id IS NOT NULL AND job_post_id <> ''
                      AND COALESCE("date", created_at) <= %s
                    """,
                    (cur_end_utc,),
                )
                row = cur.fetchone()
                if row:
                    cum["launch_iso"] = row[0].astimezone(ET).date().isoformat() if row[0] else None
                    cum["emails"]     = int(row[1] or 0)
                cur.execute(
                    """
                    SELECT
                      count(*)::int AS n,
                      COALESCE(SUM(jsonb_array_length(COALESCE(payload->'fields_changed','[]'::jsonb))), 0)::int AS f
                    FROM job_event_log
                    WHERE event_type = 'sf_scrape_fields_patched'
                      AND created_at <= %s
                    """,
                    (cur_end_utc,),
                )
                row = cur.fetchone()
                if row:
                    cum["patches"] = int(row[0] or 0)
                    cum["fields"]  = int(row[1] or 0)
                cur.execute(
                    """
                    SELECT count(*)::int FROM job_event_log
                    WHERE event_type = 'job_created_in_salesforce' AND created_at <= %s
                    """,
                    (cur_end_utc,),
                )
                cum["new_jobs"] = int(cur.fetchone()[0])
    cum["hours_saved"] = round(
        (cum["fields"] * SEC_PER_FIELD
         + cum["new_jobs"] * SEC_PER_NEW_RECORD
         + cum["emails"]   * SEC_PER_EMAIL_AWARE) / 3600.0,
        1,
    )
    cum["dollars_saved"] = round(cum["hours_saved"] * HOURLY_RATE_USD)

    # ── Speed multiplier ────────────────────────────────────────────────────
    speed_x = None
    median_lat = cur_agg.get("median_latency_min")
    if median_lat and median_lat > 0:
        speed_x = round(MANUAL_BASELINE_MIN / median_lat)

    # ── Narrative paragraph ─────────────────────────────────────────────────
    parts: list[str] = []
    # Volume trend sentence
    if prv_agg["emails_received"] > 0:
        pct = round((cur_agg["emails_received"] - prv_agg["emails_received"])
                    / prv_agg["emails_received"] * 100)
        if abs(pct) >= 10:
            verb = "climbed" if pct > 0 else "eased"
            parts.append(f"Activity {verb} {abs(pct)}% this week — {cur_agg['emails_received']} job updates flowed in")
        else:
            parts.append(f"Steady week — {cur_agg['emails_received']} job updates flowed in")
    else:
        parts.append(f"{cur_agg['emails_received']} job updates flowed in this week")
    if top_states:
        if len(top_states) >= 2:
            parts[-1] += f" across {states_total} states, with {top_states[0][0]} and {top_states[1][0]} leading"
        else:
            parts[-1] += f" across {states_total} state{'s' if states_total != 1 else ''}, led by {top_states[0][0]}"
    parts[-1] += "."
    # Speed sentence
    if median_lat is not None and speed_x:
        parts.append(f"Median time from email to Salesforce was {median_lat:.0f} minutes — roughly {speed_x}× faster than a typical manual workflow.")
    # Spotlight sentence
    top_jobs = cur_agg.get("top_jobs", [])
    if top_jobs and top_jobs[0]["patches"] >= 5:
        j0 = top_jobs[0]
        parts.append(f"Hottest record: job #{j0['job_id']} received {j0['patches']} field updates this week alone.")
    narrative = " ".join(parts)

    return {
        "period_label":        period_label,
        "window_start_utc":    cur_start_utc,
        "window_end_utc":      cur_end_utc,
        "current":             cur_agg,
        "previous":            prv_agg,
        "hours_saved_estimate":     hours_saved,
        "hours_saved_prev":         hours_saved_prv,
        "dollars_saved_estimate":   dollars_saved,
        "hourly_rate_usd":          HOURLY_RATE_USD,
        "manual_baseline_min":      MANUAL_BASELINE_MIN,
        "speed_multiplier_x":       speed_x,
        "top_states":               top_states[:8],
        "states_total":             states_total,
        "peak_day":                 peak_day if peak_day_count > 0 else None,
        "peak_day_count":           peak_day_count,
        "peak_hour":                peak_hour,
        "peak_hour_count":          peak_hour_count,
        "trend_weekly":             trend,   # last 4 weeks
        "cumulative":               cum,
        "narrative":                narrative,
        "daily_series":        [
            (
                (last_monday + timedelta(days=i)).isoformat(),
                cur_agg["daily_counts"].get((last_monday + timedelta(days=i)).isoformat(), 0),
            )
            for i in range(7)
        ],
    }


@app.local_entrypoint()
def run_once():
    """Run the incremental scrape once (same as scheduled ``scrape_gmail_job``)."""
    n = scrape_gmail_job.remote()
    print(f"Done: {n} new email(s) logged")


@app.local_entrypoint()
def run_daily_summary_once():
    """Run the daily summary + validation digest once (same as scheduled ``daily_summary_job``)."""
    ok = daily_summary_job.remote()
    print(f"Done: daily summary email sent={ok}")


@app.function(
    image=_light_image,
    secrets=[modal.Secret.from_name("salesforce-automation")],
    timeout=180,
)
def daily_summary_for_date(date_iso: str):
    """One-off: run the daily summary reporting on a specific ET calendar day.

    ``date_iso`` is YYYY-MM-DD interpreted as the ET calendar day to report on.
    Patches ``datetime.now`` inside ``_build_daily_stats`` so its "yesterday"
    derivation lands on the supplied date.
    """
    sys.path.insert(0, "/root")
    from utils.supabase_db import get_conn
    from utils.scrape_validator import (
        validate_scraped_job, issues_as_text, issues_summary,
    )
    from utils.alert_email import send_daily_summary
    import datetime as _dt
    try:
        from zoneinfo import ZoneInfo
        ET = ZoneInfo("America/New_York")
    except Exception:
        ET = _dt.timezone(_dt.timedelta(hours=-5))

    target_date = _dt.datetime.strptime(date_iso, "%Y-%m-%d").date()
    target = _dt.datetime.combine(target_date + _dt.timedelta(days=1),
                                  _dt.time(12, 0), tzinfo=ET)
    real = _dt.datetime
    class _P(real):
        @classmethod
        def now(cls, tz=None):
            return target if tz is None else target.astimezone(tz)
    _dt.datetime = _P
    try:
        stats = _build_daily_stats(get_conn, validate_scraped_job, issues_as_text, issues_summary)
    finally:
        _dt.datetime = real
    ok = send_daily_summary(stats)
    print(f"daily_summary_for_date: date={date_iso} sent={ok} emails={stats.get('emails_received')}")
    return ok


@app.local_entrypoint()
def run_daily_summary_for_date(date: str):
    """``modal run … :: run_daily_summary_for_date --date 2026-05-13``"""
    ok = daily_summary_for_date.remote(date)
    print(f"Done: daily summary for {date} sent={ok}")


@app.local_entrypoint()
def run_weekly_summary_once():
    """Run the C-level weekly pulse once (same as scheduled ``weekly_summary_job``)."""
    ok = weekly_summary_job.remote()
    print(f"Done: weekly pulse email sent={ok}")

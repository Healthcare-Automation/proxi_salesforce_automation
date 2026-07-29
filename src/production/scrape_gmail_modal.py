"""
Modal jobs: scraping pipeline (every 10 min) + daily summary report.

Functions
---------
scrape_gmail_job   — Gmail → Playwright → Supabase → Salesforce (every 10 min)
daily_summary_job  — Query Supabase, validate quality, send digest email (daily 9 AM ET)

Secrets (salesforce-automation): GMAIL_APP_PASSWORD, GMAIL_EMAIL, DB_PASSWORD,
KIMEDICS_EMAIL, KIMEDICS_PASSWORD, OPENAI_API_KEY, plus Salesforce env vars.
Secrets (gmail-oauth, scrape job only): GMAIL_OAUTH_CLIENT_ID / _CLIENT_SECRET /
_REFRESH_TOKEN — enables the Gmail API fetch path (no 15-connection IMAP cap);
remove these three keys to revert the fetch to pure IMAP, no redeploy needed.

Deploy (from project root) — registers **both** scheduled functions on app ``salesforce-automation``:
  modal deploy src/production/scrape_gmail_modal.py

  - ``scrape_gmail_job`` — every 10 min; Gmail → Kimedics → Supabase → SF; per-job validation + alert emails
  - ``daily_summary_job`` — daily cron (13:00 UTC ≈ 9 AM Eastern); 24 h Supabase stats + validation rollup digest email

Run once (manual):
  modal run src/production/scrape_gmail_modal.py::run_once
  modal run src/production/scrape_gmail_modal.py::run_daily_summary_once
"""

import imaplib
import os
import sys
import time
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
    .pip_install(
        "psycopg2-binary", "playwright", "python-dotenv", "openai>=1.0.0",
        "google-api-python-client", "google-auth",
    )
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

# Soft time budgets inside the 600s Modal hard timeout. A hard kill mid-run is
# what dropped the July 2 burst: job_content was only written after the whole
# batch, so the kill discarded every page already scraped. Results are now
# persisted per page (see on_result below), and each Playwright loop stops at
# its soft deadline and defers the rest to the auto-retry tail — so the hard
# kill should never fire, and even if it does, at most one in-flight page is lost.
_MODAL_TIMEOUT_S = 600
_PRIMARY_SCRAPE_DEADLINE_S = 330   # primary scrape loop stops by t0+330s
_RUN_SOFT_DEADLINE_S = 510         # everything (incl. auto-retry tail) stops by t0+510s
_AUTO_RETRY_MIN_BUDGET_S = 60      # don't even start the retry scrape with less than this


def _record_heartbeats(*, cron: bool, fetch: bool) -> None:
    """Best-effort watchdog beats on a short-lived connection. ``cron`` = the tick
    ran at all; ``fetch`` = the Gmail read actually succeeded. The gap between the
    two is what lets the watchdog distinguish a dead cron from a locked inbox."""
    try:
        from utils.pipeline_watchdog import (
            HEARTBEAT_GMAIL_CRON,
            HEARTBEAT_GMAIL_FETCH,
            record_heartbeat,
        )
        from utils.supabase_db import get_conn

        with get_conn() as conn:
            if conn is None:
                return
            if cron:
                record_heartbeat(conn, HEARTBEAT_GMAIL_CRON)
            if fetch:
                record_heartbeat(conn, HEARTBEAT_GMAIL_FETCH)
            conn.commit()
    except Exception as e_hb:
        print(f"warn: heartbeat write failed (non-fatal): {e_hb}")


@app.function(
    image=_full_image,
    schedule=modal.Period(minutes=10),
    secrets=[
        modal.Secret.from_name("salesforce-automation"),
        # Separate secret so the existing one is never recreated/clobbered.
        modal.Secret.from_name("gmail-oauth"),
    ],
    timeout=_MODAL_TIMEOUT_S,
)
def scrape_gmail_job():
    t0 = time.monotonic()
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

    try:
        raw_emails = scrape_emails_from_sender(
            email_account=email_account,
            email_password=email_password,
            from_email=from_email,
            hours=EMAIL_HOURS,
            max_results=500,
        )
    except imaplib.IMAP4.error as e:
        if "too many simultaneous connections" not in str(e).lower():
            raise
        # Known daily flood (~08:46 UTC): an external client holds all 15 of the
        # shared inbox's IMAP slots for ~20-40 min, outlasting the login retries.
        # Skip quietly — the next tick re-reads a 24h window so nothing is lost,
        # and the watchdog pages a human via the fetch-staleness signal if the
        # lockout ever persists. Failing loudly here just produced Modal failure
        # emails for a condition that self-heals.
        print("Inbox at its connection cap even after retries — skipping this tick "
              "(next tick catches up; watchdog alerts if the lockout persists)")
        _record_heartbeats(cron=True, fetch=False)
        return 0
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

    run_id = None
    try:
        ensure_tables(conn)
        # Liveness beats for the independent watchdog — committed immediately so
        # they survive even if everything after this point dies. CRON beats every
        # run; FETCH beats only here, after a successful Gmail read (the capped-
        # inbox skip path above beats CRON alone), so the watchdog can tell a
        # locked inbox from a dead cron.
        try:
            from utils.pipeline_watchdog import (
                HEARTBEAT_GMAIL_CRON,
                HEARTBEAT_GMAIL_FETCH,
                record_heartbeat,
            )
            record_heartbeat(conn, HEARTBEAT_GMAIL_CRON)
            record_heartbeat(conn, HEARTBEAT_GMAIL_FETCH)
            conn.commit()
        except Exception as e_hb:
            print(f"warn: heartbeat write failed (non-fatal): {e_hb}")
        existing  = get_existing_email_keys(conn, since_hours_ago=SUPABASE_LOOKBACK_HOURS)
        new_rows  = filter_parsed_emails_not_logged(parsed, existing)

        if new_rows:
            csv_fields = ["job_post_id", "location", "action_or_change", "view_job_link", "subject", "date", "from_"]
            run_id = log_run_start(conn, "gmail", csv_fields)
            if not run_id:
                return 0
            email_scrape_ids = log_email_scrapes(conn, run_id, new_rows, csv_fields)
            conn.commit()
    finally:
        # Mark the run as finished even if anything above raised after
        # log_run_start. See the link_batch block below for the same pattern
        # and rationale (orphaned scrape_runs rows show as "Failed" in the
        # dashboard otherwise).
        if run_id:
            try:
                log_run_finish(conn, run_id)
            except Exception as e_fin:
                print(f"warn: log_run_finish failed for gmail run_id={run_id}: {e_fin}")
        conn.close()

    if not new_rows:
        # Nothing new this cron — but still drain any orphaned email_scrapes that
        # earlier runs logged and never scraped (e.g. a burst that hit the 600s
        # Modal timeout). Previously we returned here BEFORE the retry tail below,
        # so on the common "no new emails" cron the orphans never recovered.
        print("No new emails in the window (all already logged)")
        try:
            _auto_retry_orphaned_scrapes(
                kimedics_email=kimedics_email,
                kimedics_password=kimedics_password,
                deadline_ts=t0 + _RUN_SOFT_DEADLINE_S,
            )
        except Exception as e:
            print(f"Auto-retry tail failed (non-fatal): {e}")
        return 0

    print(f"Logged {len(new_rows)} new email(s) to Supabase (run_id={run_id})")

    # Cap the primary batch so a burst of new emails can't run the Playwright
    # scrape past Modal's 600s timeout — a timeout cancels the whole task and
    # discards every scraped page, since job_content is only written after the
    # full batch completes. Overflow emails are already logged as email_scrapes;
    # the auto-retry tail / subsequent crons pick them up a few at a time.
    _PRIMARY_SCRAPE_MAX = 8
    with_links = [
        (new_rows[i], email_scrape_ids[i])
        for i in range(len(new_rows))
        if (new_rows[i].get("view_job_link") or "").strip() and i < len(email_scrape_ids)
    ]
    if len(with_links) > _PRIMARY_SCRAPE_MAX:
        deferred = len(with_links) - _PRIMARY_SCRAPE_MAX
        with_links = with_links[:_PRIMARY_SCRAPE_MAX]
        print(f"Deferring {deferred} job(s) beyond the per-cron cap of "
              f"{_PRIMARY_SCRAPE_MAX} — auto-retry tail / next crons will drain them")
    if not with_links:
        return len(new_rows)

    if not kimedics_email or not kimedics_password:
        print("KIMEDICS_EMAIL / KIMEDICS_PASSWORD not set; skipping link scrape")
        return len(new_rows)

    print(f"Scraping {len(with_links)} job page(s) with Playwright (incremental saves)...")
    scrape_results: list = []
    link_run_id = None
    with get_conn() as conn:
        if conn:
            # The link_batch run is created BEFORE scraping and each page's result
            # is persisted + pushed the moment it's scraped (on_result). A hard
            # kill mid-batch can then lose at most the page in flight — never the
            # whole batch, which is how the July 2 burst silently dropped 19 jobs.
            link_run_id = log_run_start(conn, "link_batch", ["job_post_id", "error"])
            conn.commit()

            def _persist_result(r: dict) -> None:
                try:
                    process_link_scrape_batch(
                        conn,
                        link_run_id=link_run_id,
                        scrape_results=[r],
                        schema="public",
                    )
                    conn.commit()
                except Exception as e_row:
                    try:
                        conn.rollback()
                    except Exception:
                        pass
                    print(
                        f"  [incremental-save] job {r.get('job_post_id', '?')} failed to "
                        f"persist: {e_row} — email stays orphaned; auto-retry will pick it up"
                    )

            try:
                scrape_results = scrape_job_pages(
                    with_links,
                    kimedics_email,
                    kimedics_password,
                    on_result=_persist_result,
                    deadline_ts=t0 + _PRIMARY_SCRAPE_DEADLINE_S,
                )
            finally:
                # Always mark the run as finished, even if the scrape raised.
                # Without this guarantee, the scrape_runs row stays orphaned
                # (finished_at = NULL) and the dashboard reports "Failed" even when
                # the work succeeded up to the exception. Best-effort: if the
                # log_run_finish write itself fails, swallow it so we still let the
                # original exception propagate.
                if link_run_id:
                    try:
                        log_run_finish(conn, link_run_id)
                    except Exception as e_fin:
                        print(f"warn: log_run_finish failed for link_run_id={link_run_id}: {e_fin}")

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
            deadline_ts=t0 + _RUN_SOFT_DEADLINE_S,
        )
    except Exception as e:
        print(f"Auto-retry tail failed (non-fatal): {e}")

    return len(new_rows)


# Per-cron caps. Keep the auto-retry tail bounded so it never threatens the
# primary 10-min Modal timeout, even with the new circuit breaker.
_AUTO_RETRY_MAX_PER_CRON = 6        # at most N orphaned jobs picked per run
_AUTO_RETRY_MAX_ATTEMPTS = 6        # give up after this many auto-retries
# Exponential backoff: 5min, 10, 20, 40, 80, 160 → ~5.3h total before giveup.
_AUTO_RETRY_BACKOFF_BASE_MIN = 5
# Don't retry until the original cron has had a chance to run + finish.
_AUTO_RETRY_GRACE_MIN = 5
# Don't bother with email_scrapes older than this — let the operator decide.
_AUTO_RETRY_LOOKBACK_HOURS = 24


def _alert_stuck_practice_jobs(get_conn, log_job_event) -> None:
    """
    Layer-3 backstop: a job blocked on empty ``practice_value`` that has exhausted
    auto-retries (the self-heal couldn't recover it) is GENUINELY stuck — it can't be
    created in Salesforce and would otherwise sit silent in the validation UI. Email
    one alert per such job so a human is told immediately. Deduped via a
    ``practice_stuck_alerted`` event so it fires once per job.
    """
    with get_conn() as conn:
        if conn is None:
            return
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT jc.job_id, jc.city, jc.state, jc.view_job_link, jc.email_scrape_id,
                       (SELECT count(*) FROM job_event_log r
                          WHERE r.event_type = 'auto_retry_completed'
                            AND (r.payload->>'email_scrape_id') = jc.email_scrape_id::text) AS attempts
                FROM job_content jc
                WHERE jc.id IN (SELECT MAX(id) FROM job_content GROUP BY job_id)   -- latest row per job
                  AND COALESCE(NULLIF(jc.title_line, ''), '') <> ''
                  AND COALESCE(NULLIF(jc.practice_value, ''), '') = ''
                  AND COALESCE(NULLIF(jc.sf_job_id, ''), '') = ''
                  AND COALESCE(jc.status, '') NOT ILIKE %s
                  AND EXISTS (SELECT 1 FROM job_event_log b WHERE b.job_id = jc.job_id
                                AND b.event_type = 'mapping_blocked_no_practice_value'
                                AND b.created_at >= NOW() - INTERVAL '48 hours')
                  AND (SELECT count(*) FROM job_event_log r
                         WHERE r.event_type = 'auto_retry_completed'
                           AND (r.payload->>'email_scrape_id') = jc.email_scrape_id::text) >= %s
                  AND NOT EXISTS (SELECT 1 FROM job_event_log a WHERE a.job_id = jc.job_id
                                    AND a.event_type = 'practice_stuck_alerted')
                """,
                ("%closed%", _AUTO_RETRY_MAX_ATTEMPTS),
            )
            stuck = cur.fetchall()
        if not stuck:
            return
        from utils.alert_email import send_practice_block_alert
        for job_id, city, state, link, _escrape_id, attempts in stuck:
            jid = str(job_id or "").strip()
            if not jid:
                continue
            kimedics_url = (link or "").strip() or f"https://portal.kimedics.com/app/workspace/job-posts/{jid}"
            sent = send_practice_block_alert(
                jid, kimedics_url=kimedics_url, city=city or "", state=state or "",
                attempts=int(attempts or 0),
            )
            print(f"  [alert_email] practice-stuck alert for #{jid} (attempts={attempts}) sent={sent}")
            log_job_event(
                conn, job_id=jid, event_type="practice_stuck_alerted", run_id=None,
                schema="public", payload={"email_sent": bool(sent), "attempts": int(attempts or 0)},
            )
        conn.commit()


_RESYNC_SWEEP_MAX = 25  # cap mapped-but-unsynced jobs re-synced per cron


def _resync_unsynced_mapped_jobs(get_conn) -> None:
    """
    The "never again" backstop. Any job mapped to Salesforce (sf_job_id is set) that has
    NO field-sync event of ANY kind — patched, recovered, skip, error, or no-mapping —
    since its latest scrape never had its details written and left no trail. That is the
    exact #20046 silent-failure shape, and it can be produced by ANY future bug, not just
    the one we fixed. So instead of trusting every code path to log correctly, we sweep
    the DB every cron and re-run the field sync for these jobs: it pushes the values, or
    (now that errors are isolated + logged) records an error the recovery loop will retry.
    Self-limiting — once a job has any field-sync event it drops out of this set.
    """
    with get_conn() as conn:
        if conn is None:
            return
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT jc.job_id
                FROM job_content jc
                WHERE jc.id IN (SELECT MAX(id) FROM job_content GROUP BY job_id)   -- latest row per job
                  AND COALESCE(NULLIF(jc.sf_job_id, ''), '') <> ''                 -- mapped to Salesforce
                  -- NOTE: closed jobs are INCLUDED. A job that closes while unsynced (e.g. a
                  -- 'status: Closed' update whose field sync silently didn't run — #20076) must
                  -- still have its final state recorded in Salesforce; the sweep is the only
                  -- thing that recovers it, since it never gets re-scraped after closing.
                  AND jc.created_at <  NOW() - INTERVAL '15 minutes'               -- not mid-pipeline
                  AND jc.created_at >  NOW() - INTERVAL '21 days'                  -- bounded window
                  AND NOT EXISTS (
                    SELECT 1 FROM job_event_log e
                    WHERE e.job_id = jc.job_id
                      AND e.event_type IN (
                            'sf_scrape_fields_patched', 'sf_scrape_fields_recovered',
                            'sf_scrape_fields_skip', 'sf_scrape_fields_error',
                            'sf_sync_skipped_no_mapping')
                      AND e.created_at >= jc.created_at - INTERVAL '5 minutes'      -- since this scrape
                  )
                ORDER BY jc.created_at DESC
                LIMIT %s
                """,
                (_RESYNC_SWEEP_MAX,),
            )
            ids = [str(r[0]) for r in cur.fetchall() if str(r[0] or "").strip()]
        if not ids:
            return
        print(f"[resync-sweep] {len(ids)} mapped-but-unsynced job(s) — re-running field sync: {ids}")
        try:
            from utils.sf_scrape_sync import sync_missing_scrape_fields_for_job_ids
            att, patched = sync_missing_scrape_fields_for_job_ids(conn, ids, schema="public", run_id=None)
            conn.commit()
            print(f"[resync-sweep] attempted={att} patched={patched}")
        except Exception as e:
            print(f"[resync-sweep] field sync raised (per-job errors logged inside): {e}")


def _auto_retry_orphaned_scrapes(
    *, kimedics_email: str, kimedics_password: str, deadline_ts: Optional[float] = None
) -> None:
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

    # Backstops that run every cron, independent of retry candidates or credentials.
    # Non-fatal — never block the retry path.
    try:
        _alert_stuck_practice_jobs(get_conn, log_job_event)
    except Exception as e:
        print(f"Auto-retry: stuck-practice alert check failed (non-fatal): {e}")
    try:
        # The "never again" sweep: re-sync any mapped-but-unsynced job (the #20046 shape).
        _resync_unsynced_mapped_jobs(get_conn)
    except Exception as e:
        print(f"Auto-retry: mapped-but-unsynced resync sweep failed (non-fatal): {e}")

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
                LEFT JOIN LATERAL (
                  SELECT count(*)::int AS n, MAX(jel.created_at) AS last_attempt_at
                  FROM job_event_log jel
                  WHERE jel.event_type = 'auto_retry_completed'
                    AND (jel.payload->>'email_scrape_id') = es.id::text
                ) att ON true
                -- Treat both true orphans (no job_content row) AND silent
                -- failures (job_content row exists but every critical field
                -- is empty — auth wall, layout change, login redirect) as
                -- "not yet successfully scraped." Without this, the alert's
                -- "Auto-retry will pick this up within ~5 min" promise was
                -- broken because the empty row made the email_scrape look
                -- complete.
                --
                -- ALSO self-heal "blocked on a missing scrape field": a row that
                -- scraped content fine but is stuck unmapped because practice_value
                -- came back empty (e.g. an unlinked Kimedics practice rendered in a
                -- field the text scrape missed). A mapping retry re-runs on the same
                -- stale row forever; only a FRESH page re-scrape can recover it. Job
                -- #20038 sat blocked here until a manual rescrape — this closes that.
                WHERE (
                        NOT EXISTS (
                          SELECT 1 FROM job_content j2
                          WHERE j2.email_scrape_id = es.id
                            AND (
                              COALESCE(NULLIF(j2.title_line, ''), '') <> ''
                              OR COALESCE(NULLIF(j2.description_full_text, ''), '') <> ''
                              OR COALESCE(NULLIF(j2.job_title, ''), '') <> ''
                            )
                        )
                        OR EXISTS (
                          SELECT 1 FROM job_content j3
                          WHERE j3.email_scrape_id = es.id
                            AND COALESCE(NULLIF(j3.title_line, ''), '') <> ''        -- DID scrape content
                            AND COALESCE(NULLIF(j3.practice_value, ''), '') = ''     -- but practice missing
                            AND COALESCE(NULLIF(j3.sf_job_id, ''), '') = ''          -- and not yet in Salesforce
                            AND COALESCE(j3.status, '') NOT ILIKE '%%closed%%'       -- and still open
                            AND EXISTS (
                              SELECT 1 FROM job_event_log b
                              WHERE b.job_id = es.job_post_id
                                AND b.event_type = 'mapping_blocked_no_practice_value'
                                AND b.created_at >= NOW() - INTERVAL '24 hours'
                            )
                        )
                      )
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

    if deadline_ts is not None and time.monotonic() >= deadline_ts - _AUTO_RETRY_MIN_BUDGET_S:
        print(
            f"Auto-retry: {len(candidates)} candidate(s) but <{_AUTO_RETRY_MIN_BUDGET_S}s "
            "of soft budget left — deferring to the next cron"
        )
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
    # the next cron will pick them up. The soft deadline stops the loop before
    # Modal's hard kill; unscraped candidates stay orphaned for the next cron.
    scrape_results = scrape_job_pages(
        with_links, kimedics_email, kimedics_password, deadline_ts=deadline_ts
    )

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


# ── Independent pipeline watchdog (every 30 min) ──────────────────────────────
# Runs on its OWN schedule so a hard-killed scrape task can never suppress the
# alarm — the July 2 failure mode. Reads only the DB; alerts on stale orphans,
# mapped-but-unsynced jobs, and a dead scrape cron (stale heartbeat).

@app.function(
    image=_light_image,
    schedule=modal.Period(minutes=30),
    secrets=[modal.Secret.from_name("salesforce-automation")],
    timeout=120,
)
def pipeline_watchdog_job():
    sys.path.insert(0, "/root")
    from utils.pipeline_watchdog import run_watchdog
    from utils.supabase_db import get_conn

    with get_conn() as conn:
        if conn is None:
            print("watchdog: no DB connection — cannot check pipeline health")
            return False
        return run_watchdog(conn)


@app.function(
    image=_light_image,
    secrets=[modal.Secret.from_name("salesforce-automation")],
    timeout=120,
)
def run_watchdog_once():
    """Manual trigger: modal run …::run_watchdog_once"""
    return pipeline_watchdog_job.local()


# ── Weekly client-facing pulse (Monday 9 AM ET = 13:00 UTC) ──────────────────

# NOTE: weekly + monthly pulses share ONE cron slot via pulse_dispatcher_job
# below (Modal's plan caps scheduled functions at 5 workspace-wide, and the
# independent watchdog needs a slot). The dispatcher fires daily at the same
# 13:00 UTC and calls these; both remain directly runnable via `modal run`.

@app.function(
    image=_light_image,
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


# ── Monthly client-facing pulse (1st of month, 9 AM ET = 13:00 UTC) ──────────

@app.function(
    image=_light_image,
    secrets=[modal.Secret.from_name("salesforce-automation")],
    timeout=180,
)
def monthly_summary_job():
    """
    Build and send the comprehensive monthly pulse: the prior calendar month's
    activity with month-over-month deltas and the full chart set.
    """
    sys.path.insert(0, "/root")
    from utils.supabase_db import get_conn
    from utils.alert_email import send_monthly_summary

    print("monthly_summary_job: starting...")
    stats = _build_monthly_stats(get_conn)
    ok = send_monthly_summary(stats)
    print(f"monthly_summary_job: email sent={ok}, emails={stats.get('current', {}).get('emails_received')}")
    return ok


@app.function(
    image=_light_image,
    schedule=modal.Cron("0 13 * * *"),  # daily 13:00 UTC ≈ 9 AM ET
    secrets=[modal.Secret.from_name("salesforce-automation")],
    timeout=400,
)
def pulse_dispatcher_job():
    """
    One cron slot for both client pulses: fires daily at the same 13:00 UTC the
    old dedicated crons used, sends the weekly pulse on Mondays and the monthly
    pulse on the 1st. Cadence and send times are identical to before.
    """
    from datetime import datetime, timezone

    today = datetime.now(timezone.utc)
    ran = []
    if today.weekday() == 0:  # Monday → weekly pulse
        weekly_summary_job.local()
        ran.append("weekly")
    if today.day == 1:  # 1st of month → monthly pulse
        monthly_summary_job.local()
        ran.append("monthly")
    print(f"pulse_dispatcher_job: ran={ran or 'nothing scheduled today'}")
    return ran


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
            try:
                touched = process_link_scrape_batch(
                    conn,
                    link_run_id=link_run_id,
                    scrape_results=scrape_results,
                    schema="public",
                )
                conn.commit()
            except Exception as e:
                try:
                    conn.rollback()
                except Exception:
                    pass
                raise HTTPException(status_code=500, detail=f"rescrape pipeline failed: {e}")
        finally:
            # Mark the run as finished regardless of success/failure so the row
            # doesn't sit orphaned (NULL finished_at → dashboard misclassifies
            # as "Failed"). log_run_finish runs even after a rollback because
            # it does its own UPDATE + commit on the scrape_runs row only.
            if link_run_id:
                try:
                    log_run_finish(conn, link_run_id)
                except Exception as e_fin:
                    print(f"warn: log_run_finish failed for link_run_id={link_run_id}: {e_fin}")

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
        "worksites_created":    0,
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
                    bool_or(jel.event_type = 'worksite_created')                       AS created_sf_worksite,
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
                    count(*) FILTER (WHERE jel.event_type = 'sf_field_quarantined')              AS fields_quarantined,
                    count(*) FILTER (WHERE jel.event_type = 'mapping_blocked_no_practice_value') AS blocked_no_practice,
                    count(*) FILTER (WHERE jel.event_type = 'scrape_silent_failure')             AS silent_failures,
                    count(*) FILTER (WHERE jel.event_type = 'sf_scrape_fields_recovered')        AS push_recovered,
                    count(*) FILTER (WHERE jel.event_type = 'sf_scrape_fields_error')            AS push_errors,
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
                    ) AS stuck,
                    -- Did the scrape's field sync reach a terminal resolution (patched,
                    -- recovered, or a deliberate skip)? A MAPPED row with NONE of these never
                    -- had its details written to Salesforce — the silent #20046 failure.
                    bool_or(jel.event_type IN (
                      'sf_scrape_fields_patched', 'sf_scrape_fields_recovered', 'sf_scrape_fields_skip'
                    )) AS field_sync_resolved
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
                  COALESCE(ev.created_sf_worksite, false) AS created_sf_worksite,
                  COALESCE(ev.ext_id_swap, false)  AS ext_id_swap,
                  COALESCE(ev.manual_rescraped, false) AS manual_rescraped,
                  COALESCE(ev.auto_retried, false) AS auto_retried,
                  COALESCE(ev.fields_quarantined, 0) AS fields_quarantined,
                  COALESCE(ev.blocked_no_practice, 0) AS blocked_no_practice,
                  COALESCE(ev.silent_failures, 0)  AS silent_failures,
                  COALESCE(ev.push_recovered, 0)   AS push_recovered,
                  COALESCE(ev.push_errors, 0)      AS push_errors,
                  COALESCE(ev.push_error_unresolved, false) AS push_error_unresolved,
                  COALESCE(ev.stuck, false)        AS stuck,
                  COALESCE(ev.field_sync_resolved, false) AS field_sync_resolved,
                  -- Did the job's fields sync PROMPTLY (within 10 min of being mapped)?
                  -- If it was mapped but only synced later (or not at all), a sync FAILURE
                  -- happened during this period — even if it was recovered afterward. The
                  -- report records that it went wrong, not just the current state.
                  EXISTS (
                    SELECT 1 FROM job_event_log m, job_event_log fp
                    WHERE m.job_id = we.job_post_id AND m.event_type = 'sf_ids_update'
                      AND COALESCE(m.payload->'next'->>'sf_job_id', '') <> ''
                      AND fp.job_id = we.job_post_id
                      AND fp.event_type IN ('sf_scrape_fields_patched', 'sf_scrape_fields_recovered', 'sf_scrape_fields_skip')
                      AND fp.created_at BETWEEN m.created_at - INTERVAL '5 minutes' AND m.created_at + INTERVAL '10 minutes'
                  ) AS field_sync_prompt
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

    # Issue / action / created-record counts are reported at a 1-PER-JOB level: a job
    # with several emails (or several blocked retries) must count ONCE, not once per
    # event — otherwise "9 blocked" reads as 9 failed jobs when it's really one job
    # (#20038) retried 9 times. Only the email funnel + field totals stay per-event.
    def _jobs(pred) -> int:
        return len({(r.get("job_post_id") or "") for r in rows
                    if (r.get("job_post_id") or "") and pred(r)})

    stats["emails_received"]     = len(rows)                                   # emails (volume)
    stats["scraped_ok"]          = sum(1 for r in rows if r["scrape_ok"])      # emails (funnel)
    stats["sf_mapped"]           = sum(1 for r in rows if r["sf_mapped"])      # emails (funnel)
    stats["field_patches_total"] = sum(int(r["fields_changed"] or 0) for r in rows)  # fields
    stats["fields_quarantined"]  = sum(int(r["fields_quarantined"] or 0) for r in rows)  # fields
    stats["push_errors_total"]   = sum(int(r["push_errors"] or 0) for r in rows)     # errors
    # ── 1-per-job ──
    stats["sf_jobs_created"]     = _jobs(lambda r: r["created_sf_job"])
    stats["worksites_created"]   = _jobs(lambda r: r["created_sf_worksite"])
    stats["ext_id_swaps"]        = _jobs(lambda r: r["ext_id_swap"])
    stats["manual_rescrapes"]    = _jobs(lambda r: r["manual_rescraped"])
    stats["auto_retries"]        = _jobs(lambda r: r["auto_retried"])
    stats["stuck_jobs"]          = _jobs(lambda r: r["stuck"])
    stats["scrape_failures"]     = _jobs(lambda r: not r["scrape_ok"] and not r["stuck"])
    stats["blocked_no_practice"] = _jobs(lambda r: int(r["blocked_no_practice"] or 0) > 0)
    stats["silent_failures"]     = _jobs(lambda r: int(r["silent_failures"] or 0) > 0)
    stats["pushes_recovered"]    = _jobs(lambda r: int(r["push_recovered"] or 0) > 0)
    stats["push_errors_unresolved"] = _jobs(lambda r: r["push_error_unresolved"])
    # Sync failures that occurred this period (the report records what went wrong, not
    # just the current state):
    #   • still broken  — mapped but never synced (the silent #20046 failure). Critical.
    #   • recovered     — mapped, didn't sync promptly, but was fixed afterward. Still a
    #                     failure that happened and must be shown, just not critical.
    stats["mapped_not_synced"]     = _jobs(lambda r: r["sf_mapped"] and not r.get("field_sync_resolved"))
    stats["sync_failed_recovered"] = _jobs(
        lambda r: r["sf_mapped"] and r.get("field_sync_resolved") and not r.get("field_sync_prompt")
    )

    return stats


# Operational buckets shared across pulse periods. `status: Active` is treated
# as an open (role went live / came back on the market); `updated` is a content
# edit; `status: Closed` is a close.
def _pulse_bucket(action: str) -> str:
    a = (action or "").strip().lower()
    if a in ("new", "status: active"):
        return "opened"
    if a == "status: closed":
        return "closed"
    return "updated"


def _pulse_et():
    from datetime import timezone, timedelta
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo("America/New_York")
    except Exception:
        return timezone(timedelta(hours=-5))


def _pulse_window_utc(d_start, d_end, ET):
    from datetime import datetime, time, timezone
    s = datetime.combine(d_start, time(0, 0, 0), tzinfo=ET).astimezone(timezone.utc)
    e = datetime.combine(d_end,   time(23, 59, 59), tzinfo=ET).astimezone(timezone.utc)
    return s, e


def _build_period_stats(get_conn, cur_start_utc, cur_end_utc,
                        prv_start_utc, prv_end_utc, period_label, ET) -> dict:
    """
    Shared pulse aggregation for an arbitrary reporting window. Used by both the
    weekly and monthly summaries; the caller supplies the window bounds + label
    and adds any period-specific trend/series afterward. Returns the core
    dataset: current/previous aggregates, hours saved, states, cadence
    histograms, cumulative-since-launch, and lifecycle.
    """
    import psycopg2.extras
    from datetime import datetime

    _bucket = _pulse_bucket

    def _aggregate(get_conn, start_utc, end_utc):
        agg = {
            "emails_received":      0,
            "scraped_ok":           0,
            "sf_mapped":            0,
            "opened":               0,
            "updated":              0,
            "closed":               0,
            "errors":               0,    # emails that did not parse to usable content
            "content_emails":       0,    # job-post emails that carry content (new/updated; not status pings)
            "content_ok":           0,    # of those, parsed to a usable title + description
            "field_patches_total":  0,
            "median_latency_min":      None,
            "mean_latency_min":        None,   # outlier-trimmed average (headline)
            "mean_latency_raw_min":    None,   # untrimmed, for reference
            "latency_outliers_removed": 0,
            "daily_counts":         {},   # date_iso → total emails
            "daily_split":          {},   # date_iso → {opened, updated, closed}
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
                      SELECT id, job_post_id, COALESCE("date", created_at) AS received_at, action_or_change
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
                      SELECT we.id AS email_scrape_id, we.job_post_id, we.received_at, we.action_or_change,
                        count(*) FILTER (WHERE jel.event_type = 'sf_scrape_fields_patched') AS patch_events,
                        COALESCE(SUM(jsonb_array_length(COALESCE(jel.payload->'fields_changed','[]'::jsonb)))
                                 FILTER (WHERE jel.event_type = 'sf_scrape_fields_patched'), 0) AS fields_changed,
                        bool_or(jel.event_type = 'job_created_in_salesforce') AS created_sf_job,
                        bool_or(jel.event_type = 'worksite_created')           AS created_sf_worksite,
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
                      GROUP BY we.id, we.job_post_id, we.received_at, we.action_or_change
                    )
                    SELECT
                      ev.email_scrape_id, ev.job_post_id, ev.received_at, ev.action_or_change,
                      COALESCE(jc.scrape_ok, false) AS scrape_ok,
                      COALESCE(jc.sf_mapped, false) AS sf_mapped,
                      jc.job_title, jc.posting_org, jc.practice_value, jc.sf_job_id,
                      COALESCE(ev.fields_changed, 0)    AS fields_changed,
                      COALESCE(ev.created_sf_job, false) AS created_sf_job,
                      COALESCE(ev.created_sf_worksite, false) AS created_sf_worksite,
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
        agg["field_patches_total"] = sum(int(r["fields_changed"] or 0) for r in rows)
        # Distinct jobs needing a human — not email rows (one stuck job often
        # spans several emails, which would otherwise inflate the count).
        stuck_jobs = sorted({r["job_post_id"] for r in rows if r["stuck"] and r.get("job_post_id")})
        agg["needs_attention"]      = len(stuck_jobs)
        agg["needs_attention_jobs"] = stuck_jobs
        agg["errors"]               = agg["emails_received"] - agg["scraped_ok"]

        # Operational buckets + per-day split (ET date).
        for r in rows:
            b = _bucket(r.get("action_or_change"))
            agg[b] += 1
            d = r["received_at"].astimezone(ET).date().isoformat()
            agg["daily_counts"][d] = agg["daily_counts"].get(d, 0) + 1
            slot = agg["daily_split"].setdefault(d, {"opened": 0, "updated": 0, "closed": 0})
            slot[b] += 1
            # Capture rate is measured only over content-bearing job-post emails
            # (new / updated). Status-change pings carry no content to capture.
            if not (r.get("action_or_change") or "").strip().lower().startswith("status"):
                agg["content_emails"] += 1
                if r["scrape_ok"]:
                    agg["content_ok"] += 1

        # Note: time-to-first-scrape is computed separately, per newly-opened
        # role, in ``_role_latency`` — not per email — so updates/closes don't
        # pollute the "speed on new roles" number.
        return agg

    cur_agg = _aggregate(get_conn, cur_start_utc, cur_end_utc)
    prv_agg = _aggregate(get_conn, prv_start_utc, prv_end_utc)

    # ── Time to first scrape, per newly-opened role ──────────────────────────
    # For each role opened in the window (its first 'new'/'status: Active'
    # email), minutes to Proxi's first Salesforce action. Trimmed mean drops
    # Tukey outliers so a single stuck role can't distort the headline.
    def _role_latency(get_conn, start_utc, end_utc):
        out = {"median": None, "mean": None, "raw_mean": None, "removed": 0}
        vals: list[float] = []
        with get_conn() as conn:
            if conn is None:
                return out
            with conn.cursor() as cur:
                cur.execute(
                    """
                    WITH opens AS (
                      SELECT job_post_id, MIN(COALESCE("date", created_at)) AS opened_at
                      FROM email_scrapes
                      WHERE COALESCE("date", created_at) >= %(s)s
                        AND COALESCE("date", created_at) <= %(e)s
                        AND action_or_change IN ('new', 'status: Active')
                        AND job_post_id IS NOT NULL AND job_post_id <> ''
                      GROUP BY job_post_id
                    ),
                    act AS (
                      SELECT job_id, MIN(created_at) AS first_act
                      FROM job_event_log
                      WHERE event_type IN ('sf_scrape_fields_patched', 'job_created_in_salesforce')
                      GROUP BY job_id
                    )
                    SELECT EXTRACT(EPOCH FROM (act.first_act - opens.opened_at)) / 60.0
                    FROM opens JOIN act ON act.job_id = opens.job_post_id
                    WHERE act.first_act >= opens.opened_at
                    """,
                    {"s": start_utc, "e": end_utc},
                )
                vals = sorted(float(r[0]) for r in cur.fetchall())
        if not vals:
            return out
        n = len(vals)
        mid = n // 2
        out["median"]   = round(vals[mid] if n % 2 else (vals[mid-1] + vals[mid]) / 2, 1)
        out["raw_mean"] = round(sum(vals) / n, 1)

        def _quantile(p):
            idx = p * (n - 1)
            lo = int(idx); hi = min(lo + 1, n - 1)
            return vals[lo] + (vals[hi] - vals[lo]) * (idx - lo)
        q1, q3 = _quantile(0.25), _quantile(0.75)
        iqr = q3 - q1
        inliers = [x for x in vals if q1 - 1.5 * iqr <= x <= q3 + 1.5 * iqr]
        if inliers:
            out["mean"]    = round(sum(inliers) / len(inliers), 1)
            out["removed"] = n - len(inliers)
        return out

    _lat = _role_latency(get_conn, cur_start_utc, cur_end_utc)
    cur_agg["median_latency_min"]       = _lat["median"]
    cur_agg["mean_latency_min"]         = _lat["mean"]
    cur_agg["mean_latency_raw_min"]     = _lat["raw_mean"]
    cur_agg["latency_outliers_removed"] = _lat["removed"]

    # ── Manual-time model ────────────────────────────────────────────────────
    # Hands-on minutes a person spends per action, PLUS a context-switch tax on
    # every inbound email — because doing this by hand means stopping work to
    # check Kimedics each time one lands. That interrupt cost is where most of
    # the reclaimed time comes from.
    MIN_PER_OPEN    = 8      # open / activate a role
    MIN_PER_OTHER   = 1.5    # an update or a close
    MIN_PER_SWITCH  = 2      # context-switch per inbound email she'd otherwise field

    def _hours_saved(agg):
        return round(
            (agg["opened"]                       * MIN_PER_OPEN
             + (agg["updated"] + agg["closed"])  * MIN_PER_OTHER
             + agg["emails_received"]            * MIN_PER_SWITCH) / 60.0,
            1,
        )

    hours_saved     = _hours_saved(cur_agg)
    hours_saved_prv = _hours_saved(prv_agg)

    # ── States touched — normalized to upper-case 2-letter codes so casing
    #    variants (e.g. "AL" vs "Al") merge into one state and map cleanly. ────
    top_states: list[tuple[str, int]] = []
    states_total = 0
    with get_conn() as conn:
        if conn is not None:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT upper(trim(jc.state)) AS st, count(*)::int AS n
                    FROM job_content jc
                    JOIN email_scrapes es ON es.id = jc.email_scrape_id
                    WHERE COALESCE(es."date", es.created_at) >= %s
                      AND COALESCE(es."date", es.created_at) <= %s
                      AND COALESCE(trim(jc.state), '') <> ''
                    GROUP BY upper(trim(jc.state))
                    ORDER BY n DESC
                    """,
                    (cur_start_utc, cur_end_utc),
                )
                for row in cur.fetchall():
                    top_states.append((row[0], int(row[1])))
            states_total = len(top_states)

    # ── Cadence distributions: full day-of-week + hour-of-day histograms ─────
    weekday_totals = {i: 0 for i in range(7)}
    for d_iso, n in cur_agg["daily_counts"].items():
        try:
            d = datetime.strptime(d_iso, "%Y-%m-%d").date()
            weekday_totals[d.weekday()] += n
        except Exception:
            pass
    weekday_labels = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    weekday_hist = [(weekday_labels[i], weekday_totals[i]) for i in range(7)]

    # Weekday split by opened/updated/closed (Mon–Fri only — Aspen HR is closed
    # on weekends, so Sat/Sun add nothing but empty bars).
    weekday_split_totals = {i: {"opened": 0, "updated": 0, "closed": 0} for i in range(7)}
    for d_iso, parts in cur_agg["daily_split"].items():
        try:
            wd = datetime.strptime(d_iso, "%Y-%m-%d").date().weekday()
        except Exception:
            continue
        for k in ("opened", "updated", "closed"):
            weekday_split_totals[wd][k] += int(parts.get(k, 0))
    weekday_split = [(weekday_labels[i], weekday_split_totals[i]) for i in range(5)]  # Mon–Fri

    # Hour-of-day histogram (all 24 ET hours), split by opened/updated/closed.
    hour_counts = {h: 0 for h in range(24)}
    hour_split_totals = {h: {"opened": 0, "updated": 0, "closed": 0} for h in range(24)}
    with get_conn() as conn:
        if conn is not None:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT EXTRACT(HOUR FROM (COALESCE("date", created_at) AT TIME ZONE 'America/New_York'))::int AS hr,
                           action_or_change, count(*)::int AS n
                    FROM email_scrapes
                    WHERE COALESCE("date", created_at) >= %s AND COALESCE("date", created_at) <= %s
                      AND job_post_id IS NOT NULL AND job_post_id <> ''
                    GROUP BY hr, action_or_change
                    """,
                    (cur_start_utc, cur_end_utc),
                )
                for hr, action, n in cur.fetchall():
                    hr = int(hr); n = int(n or 0)
                    hour_counts[hr] += n
                    hour_split_totals[hr][_bucket(action)] += n
    hour_hist = [(h, hour_counts[h]) for h in range(24)]
    hour_split = [(h, hour_split_totals[h]) for h in range(24)]


    # ── Cumulative since launch ─────────────────────────────────────────────
    cum = {
        "emails":       0,
        "patches":      0,
        "fields":       0,
        "new_jobs":     0,
        "opened":       0,
        "updated":      0,
        "closed":       0,
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
                # All-time action buckets — drive the manual-time model.
                cur.execute(
                    """
                    SELECT action_or_change, count(*)::int
                    FROM email_scrapes
                    WHERE job_post_id IS NOT NULL AND job_post_id <> ''
                      AND COALESCE("date", created_at) <= %s
                    GROUP BY action_or_change
                    """,
                    (cur_end_utc,),
                )
                for action, n in cur.fetchall():
                    cum[_bucket(action)] += int(n or 0)
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
        (cum["opened"]                      * MIN_PER_OPEN
         + (cum["updated"] + cum["closed"]) * MIN_PER_OTHER
         + cum["emails"]                    * MIN_PER_SWITCH) / 60.0,
        1,
    )

    # ── Lifecycle: open duration + the fast-close blind spot ─────────────────
    # One consistent definition across scopes: a role "opens" at its first
    # 'new'/'status: Active' email and "closes" at the first 'status: Closed'
    # AFTER that open (so reopened roles can't produce negative durations).
    # Duration = close − open. "first_act" is Proxi's first Salesforce touch; a
    # role whose close precedes first_act was never acted on in time. The weekly
    # figures are the subset of this same table whose open falls in the window —
    # so weekly can never exceed all-time.
    def _lifecycle(get_conn, win_start, win_end, end_utc):
        out = {
            "all":  {"closed_total": 0, "lt_1h": 0, "h1_24": 0, "d1_7": 0, "gt_7d": 0,
                     "median_hours": None, "fast_total": 0, "fast_grabbed": 0},
            "week": {"le_1h": 0, "le_24h": 0, "fast_grabbed": 0},
        }
        with get_conn() as conn:
            if conn is None:
                return out
            with conn.cursor() as cur:
                cur.execute(
                    """
                    WITH opens AS (
                      SELECT job_post_id, MIN(COALESCE("date", created_at)) AS opened_at
                      FROM email_scrapes
                      WHERE action_or_change IN ('new', 'status: Active')
                        AND job_post_id IS NOT NULL AND job_post_id <> ''
                        AND COALESCE("date", created_at) <= %(end)s
                      GROUP BY job_post_id
                    ),
                    closes AS (
                      SELECT es.job_post_id, MIN(COALESCE(es."date", es.created_at)) AS closed_at
                      FROM email_scrapes es
                      JOIN opens o ON o.job_post_id = es.job_post_id
                      WHERE es.action_or_change = 'status: Closed'
                        AND COALESCE(es."date", es.created_at) >= o.opened_at
                        AND COALESCE(es."date", es.created_at) <= %(end)s
                      GROUP BY es.job_post_id
                    ),
                    acts AS (
                      SELECT job_id, MIN(created_at) AS first_act
                      FROM job_event_log
                      WHERE event_type IN ('sf_scrape_fields_patched', 'job_created_in_salesforce')
                      GROUP BY job_id
                    ),
                    life AS (
                      SELECT o.opened_at, c.closed_at, a.first_act,
                             EXTRACT(EPOCH FROM (c.closed_at - o.opened_at)) / 3600.0 AS hrs
                      FROM opens o
                      JOIN closes c ON c.job_post_id = o.job_post_id
                      LEFT JOIN acts a ON a.job_id    = o.job_post_id
                    )
                    SELECT
                      count(*)::int,
                      count(*) FILTER (WHERE hrs <= 1)::int,
                      count(*) FILTER (WHERE hrs > 1 AND hrs <= 24)::int,
                      count(*) FILTER (WHERE hrs > 24 AND hrs <= 168)::int,
                      count(*) FILTER (WHERE hrs > 168)::int,
                      percentile_cont(0.5) WITHIN GROUP (ORDER BY hrs),
                      count(*) FILTER (WHERE hrs <= 1)::int,
                      count(*) FILTER (WHERE hrs <= 1 AND first_act IS NOT NULL AND first_act <= closed_at)::int,
                      (SELECT count(*) FROM opens WHERE opened_at BETWEEN %(ws)s AND %(we)s)::int,
                      count(*) FILTER (WHERE opened_at BETWEEN %(ws)s AND %(we)s AND hrs <= 1)::int,
                      count(*) FILTER (WHERE opened_at BETWEEN %(ws)s AND %(we)s AND hrs > 1 AND hrs <= 24)::int,
                      count(*) FILTER (WHERE opened_at BETWEEN %(ws)s AND %(we)s AND hrs > 24 AND hrs <= 168)::int,
                      count(*) FILTER (WHERE opened_at BETWEEN %(ws)s AND %(we)s AND hrs > 168)::int,
                      percentile_cont(0.5) WITHIN GROUP (ORDER BY hrs) FILTER (WHERE opened_at BETWEEN %(ws)s AND %(we)s),
                      count(*) FILTER (WHERE opened_at BETWEEN %(ws)s AND %(we)s AND hrs <= 1
                                        AND first_act IS NOT NULL AND first_act <= closed_at)::int
                    FROM life
                    """,
                    {"end": end_utc, "ws": win_start, "we": win_end},
                )
                row = cur.fetchone()
                if row:
                    out["all"] = {
                        "closed_total": row[0], "lt_1h": row[1], "h1_24": row[2],
                        "d1_7": row[3], "gt_7d": row[4],
                        "median_hours": round(float(row[5]), 1) if row[5] is not None else None,
                        "fast_total": row[6], "fast_grabbed": row[7],
                    }
                    wk_lt1h, wk_h1_24, wk_d1_7, wk_gt7d = row[9], row[10], row[11], row[12]
                    out["week"] = {
                        "opened": row[8],
                        "closed": wk_lt1h + wk_h1_24 + wk_d1_7 + wk_gt7d,
                        "lt_1h": wk_lt1h, "h1_24": wk_h1_24, "d1_7": wk_d1_7, "gt_7d": wk_gt7d,
                        "median_hours": round(float(row[13]), 1) if row[13] is not None else None,
                        "fast_grabbed": row[14],
                    }
        return out

    lifecycle = _lifecycle(get_conn, cur_start_utc, cur_end_utc, cur_end_utc)


    return {
        "period_label":        period_label,
        "window_start_utc":    cur_start_utc,
        "window_end_utc":      cur_end_utc,
        "current":             cur_agg,
        "previous":            prv_agg,
        "hours_saved_estimate":     hours_saved,
        "hours_saved_prev":         hours_saved_prv,
        "manual_time_model":   {
            "min_per_open":   MIN_PER_OPEN,
            "min_per_other":  MIN_PER_OTHER,
            "min_per_switch": MIN_PER_SWITCH,
        },
        "top_states":          top_states,   # all active states, for the tile map
        "states_total":        states_total,
        "weekday_hist":        weekday_hist,    # [(label, count) × 7]
        "weekday_split":       weekday_split,   # [(label, {opened,updated,closed}) × Mon–Fri]
        "hour_hist":           hour_hist,       # [(hour, count) × 24]
        "hour_split":          hour_split,      # [(hour, {opened,updated,closed}) × 24]
        "cumulative":          cum,
        "lifecycle":           lifecycle,
    }


def _build_weekly_stats(get_conn) -> dict:
    """
    Weekly pulse dataset for ``send_weekly_summary`` — previous calendar week
    (Mon 00:00 → Sun 23:59:59 ET), WoW deltas vs the week before.
    """
    from datetime import datetime, timedelta
    ET = _pulse_et()
    now_et = datetime.now(ET)
    last_sunday  = (now_et - timedelta(days=now_et.weekday() + 1)).date()
    last_monday  = last_sunday - timedelta(days=6)
    prior_sunday = last_monday - timedelta(days=1)
    prior_monday = prior_sunday - timedelta(days=6)
    cur_start_utc, cur_end_utc = _pulse_window_utc(last_monday,  last_sunday,  ET)
    prv_start_utc, prv_end_utc = _pulse_window_utc(prior_monday, prior_sunday, ET)
    period_label = f"{last_monday.strftime('%b %-d')} – {last_sunday.strftime('%b %-d, %Y')}"

    stats = _build_period_stats(get_conn, cur_start_utc, cur_end_utc,
                                prv_start_utc, prv_end_utc, period_label, ET)

    # 4-week throughput trend ending the reported week.
    stats["trend_weekly"] = _pulse_weekly_trend(get_conn, last_sunday, 4, ET)
    stats["hours_trend"]  = _pulse_weekly_hours_trend(get_conn, last_sunday, 8, ET)
    daily_counts = stats["current"]["daily_counts"]
    daily_split  = stats["current"]["daily_split"]
    stats["daily_series"] = [
        ((last_monday + timedelta(days=i)).isoformat(),
         daily_counts.get((last_monday + timedelta(days=i)).isoformat(), 0))
        for i in range(7)
    ]
    stats["daily_split_series"] = [
        ((last_monday + timedelta(days=i)).isoformat(),
         daily_split.get((last_monday + timedelta(days=i)).isoformat(),
                         {"opened": 0, "updated": 0, "closed": 0}))
        for i in range(7)
    ]
    return stats


def _pulse_weekly_trend(get_conn, end_sunday, weeks, ET):
    """Emails per week for the trailing ``weeks`` weeks ending ``end_sunday``."""
    from datetime import timedelta
    trend: list = []
    with get_conn() as conn:
        if conn is None:
            return trend
        with conn.cursor() as cur:
            for back in range(weeks - 1, -1, -1):
                wk_end = end_sunday - timedelta(days=back * 7)
                wk_start = wk_end - timedelta(days=6)
                ws, we = _pulse_window_utc(wk_start, wk_end, ET)
                cur.execute(
                    """
                    SELECT count(*)::int FROM email_scrapes
                    WHERE COALESCE("date", created_at) >= %s
                      AND COALESCE("date", created_at) <= %s
                      AND job_post_id IS NOT NULL AND job_post_id <> ''
                    """,
                    (ws, we),
                )
                trend.append((wk_end.strftime("%b %-d"), int(cur.fetchone()[0])))
    return trend


def _pulse_period_hours(cur, start_utc, end_utc):
    """Hours saved in a window (same model as _hours_saved: 8/open, 1.5/other, 2/email)."""
    cur.execute(
        """
        SELECT action_or_change, count(*)::int FROM email_scrapes
        WHERE COALESCE("date", created_at) >= %s AND COALESCE("date", created_at) <= %s
          AND job_post_id IS NOT NULL AND job_post_id <> ''
        GROUP BY action_or_change
        """,
        (start_utc, end_utc),
    )
    b = {"opened": 0, "updated": 0, "closed": 0}
    emails = 0
    for action, n in cur.fetchall():
        b[_pulse_bucket(action)] += int(n or 0)
        emails += int(n or 0)
    return round((b["opened"] * 8 + (b["updated"] + b["closed"]) * 1.5 + emails * 2) / 60.0, 1)


def _pulse_weekly_hours_trend(get_conn, end_sunday, weeks, ET):
    """Hours saved per week, trailing ``weeks`` weeks ending ``end_sunday``."""
    from datetime import timedelta
    out: list = []
    with get_conn() as conn:
        if conn is None:
            return out
        with conn.cursor() as cur:
            for back in range(weeks - 1, -1, -1):
                we = end_sunday - timedelta(days=back * 7)
                ws = we - timedelta(days=6)
                s, e = _pulse_window_utc(ws, we, ET)
                out.append((we.strftime("%b %-d"), _pulse_period_hours(cur, s, e)))
    return out


def _pulse_monthly_hours_trend(get_conn, report_month_start, months, ET):
    """Hours saved per calendar month, trailing ``months`` months (drops leading zeros)."""
    import datetime as dt
    from datetime import timedelta
    y, m = report_month_start.year, report_month_start.month
    seq = []
    for _ in range(months):
        seq.append((y, m)); m -= 1
        if m == 0:
            m = 12; y -= 1
    seq.reverse()
    out: list = []
    with get_conn() as conn:
        if conn is None:
            return out
        with conn.cursor() as cur:
            for yy, mm in seq:
                ms = dt.date(yy, mm, 1)
                ns = dt.date(yy + 1, 1, 1) if mm == 12 else dt.date(yy, mm + 1, 1)
                me = ns - timedelta(days=1)
                s, e = _pulse_window_utc(ms, me, ET)
                out.append((ms.strftime("%b"), _pulse_period_hours(cur, s, e)))
    while out and out[0][1] == 0:
        out.pop(0)
    return out


def _build_monthly_stats(get_conn) -> dict:
    """
    Monthly pulse dataset for ``send_monthly_summary`` — previous calendar month,
    MoM deltas vs the month before. Adds per-week trend + opened/updated/closed
    split by week of the month.
    """
    from datetime import datetime, timedelta
    ET = _pulse_et()
    now_et = datetime.now(ET)
    first_this_month = now_et.date().replace(day=1)
    last_month_end   = first_this_month - timedelta(days=1)       # last day prior month
    last_month_start = last_month_end.replace(day=1)              # first day prior month
    prev_month_end   = last_month_start - timedelta(days=1)
    prev_month_start = prev_month_end.replace(day=1)
    cur_start_utc, cur_end_utc = _pulse_window_utc(last_month_start, last_month_end, ET)
    prv_start_utc, prv_end_utc = _pulse_window_utc(prev_month_start, prev_month_end, ET)
    period_label = last_month_start.strftime("%B %Y")

    stats = _build_period_stats(get_conn, cur_start_utc, cur_end_utc,
                                prv_start_utc, prv_end_utc, period_label, ET)

    trend, weekly_split = _pulse_month_weeks(get_conn, last_month_start, last_month_end, ET)
    stats["trend_weekly"]        = trend          # emails per week-of-month
    stats["weekly_split_series"] = weekly_split   # [(label, {opened,updated,closed})]
    stats["monthly_trend"]       = _pulse_monthly_trend(get_conn, last_month_start, 6, ET)
    stats["hours_trend"]         = _pulse_monthly_hours_trend(get_conn, last_month_start, 6, ET)
    return stats


def _build_impact_stats(get_conn) -> dict:
    """
    All-time impact dataset for ``send_impact_report`` — covers launch → today.
    Same validated aggregates as the pulse, over the whole window: totals,
    capture, latency, hours saved, open-duration lifecycle, geography, plus a
    full month-by-month trend of activity and hours. No period deltas (it's the
    whole history).
    """
    from datetime import datetime, timedelta
    ET = _pulse_et()
    now_et = datetime.now(ET)

    launch = None
    with get_conn() as conn:
        if conn is not None:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT MIN(COALESCE("date", created_at)) FROM email_scrapes
                       WHERE job_post_id IS NOT NULL AND job_post_id <> ''"""
                )
                row = cur.fetchone()
                if row and row[0]:
                    launch = row[0].astimezone(ET).date()
    if launch is None:
        launch = now_et.date()

    cur_start_utc, cur_end_utc = _pulse_window_utc(launch, now_et.date(), ET)
    # Degenerate "previous" window before launch — yields zero, deltas unused.
    prv_start_utc, prv_end_utc = _pulse_window_utc(launch - timedelta(days=2), launch - timedelta(days=1), ET)
    period_label = f"{launch.strftime('%b %-d, %Y')} – {now_et.strftime('%b %-d, %Y')}"

    stats = _build_period_stats(get_conn, cur_start_utc, cur_end_utc,
                                prv_start_utc, prv_end_utc, period_label, ET)

    months = (now_et.year - launch.year) * 12 + (now_et.month - launch.month) + 1
    this_month_start = now_et.date().replace(day=1)
    stats["monthly_trend"] = _pulse_monthly_trend(get_conn, this_month_start, months, ET)
    stats["hours_trend"]   = _pulse_monthly_hours_trend(get_conn, this_month_start, months, ET)
    stats["launch_iso"]    = launch.isoformat()
    stats["today_iso"]     = now_et.date().isoformat()
    stats["days_live"]     = (now_et.date() - launch).days + 1
    return stats


def _pulse_monthly_trend(get_conn, report_month_start, months, ET):
    """Emails per calendar month for the trailing ``months`` months (inclusive)."""
    import datetime as dt
    from datetime import timedelta
    y, m = report_month_start.year, report_month_start.month
    seq = []
    for _ in range(months):
        seq.append((y, m))
        m -= 1
        if m == 0:
            m = 12; y -= 1
    seq.reverse()
    out: list = []
    with get_conn() as conn:
        if conn is None:
            return out
        with conn.cursor() as cur:
            for yy, mm in seq:
                mstart = dt.date(yy, mm, 1)
                nstart = dt.date(yy + 1, 1, 1) if mm == 12 else dt.date(yy, mm + 1, 1)
                mend = nstart - timedelta(days=1)
                s, e = _pulse_window_utc(mstart, mend, ET)
                cur.execute(
                    """
                    SELECT count(*)::int FROM email_scrapes
                    WHERE COALESCE("date", created_at) >= %s
                      AND COALESCE("date", created_at) <= %s
                      AND job_post_id IS NOT NULL AND job_post_id <> ''
                    """,
                    (s, e),
                )
                out.append((mstart.strftime("%b"), int(cur.fetchone()[0])))
    while out and out[0][1] == 0:   # drop pre-launch leading zero months
        out.pop(0)
    return out


def _pulse_month_weeks(get_conn, m_start, m_end, ET):
    """Split a month into 7-day chunks; return (trend, opened/updated/closed split)."""
    from datetime import timedelta
    chunks: list = []
    ws = m_start
    while ws <= m_end:
        we = min(ws + timedelta(days=6), m_end)
        chunks.append((ws, we))
        ws = we + timedelta(days=1)
    trend: list = []
    split: list = []
    with get_conn() as conn:
        if conn is None:
            return trend, split
        with conn.cursor() as cur:
            for ws, we in chunks:
                s, e = _pulse_window_utc(ws, we, ET)
                cur.execute(
                    """
                    SELECT action_or_change, count(*)::int FROM email_scrapes
                    WHERE COALESCE("date", created_at) >= %s
                      AND COALESCE("date", created_at) <= %s
                      AND job_post_id IS NOT NULL AND job_post_id <> ''
                    GROUP BY action_or_change
                    """,
                    (s, e),
                )
                buckets = {"opened": 0, "updated": 0, "closed": 0}
                total = 0
                for action, n in cur.fetchall():
                    buckets[_pulse_bucket(action)] += int(n or 0)
                    total += int(n or 0)
                label = ws.strftime("%b %-d")
                trend.append((label, total))
                split.append((label, buckets))
    return trend, split


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


@app.function(
    image=_light_image,
    secrets=[modal.Secret.from_name("salesforce-automation")],
    timeout=60,
)
def preview_insight_compress(value: str) -> dict:
    """Run sanitize_insight_for_salesforce on a value and report what happens."""
    sys.path.insert(0, "/root")
    from utils.insight_sanitize import sanitize_insight_for_salesforce
    out = sanitize_insight_for_salesforce(value)
    return {
        "input_len":  len(value),
        "output":     out,
        "output_len": len(out or ""),
    }


@app.local_entrypoint()
def run_preview_insight(value: str):
    """``modal run … :: run_preview_insight --value '*This assignment …'``"""
    info = preview_insight_compress.remote(value)
    print(f"INPUT  ({info['input_len']:3d} chars)")
    print(f"OUTPUT ({info['output_len']:3d} chars / 255):")
    print(info["output"])


@app.local_entrypoint()
def run_weekly_summary_once():
    """Run the C-level weekly pulse once (same as scheduled ``weekly_summary_job``)."""
    ok = weekly_summary_job.remote()
    print(f"Done: weekly pulse email sent={ok}")


@app.local_entrypoint()
def run_monthly_summary_once():
    """Run the monthly pulse once (same as scheduled ``monthly_summary_job``)."""
    ok = monthly_summary_job.remote()
    print(f"Done: monthly pulse email sent={ok}")


@app.function(
    image=_light_image,
    secrets=[modal.Secret.from_name("salesforce-automation")],
    timeout=180,
)
def impact_report_job(recipients_csv: str = "") -> bool:
    """Build + send the all-time impact report (launch → today). On-demand only — no
    schedule. ``recipients_csv`` overrides the default distribution (used to send a
    review-only copy); empty = the standard ALERT_RECIPIENTS list."""
    sys.path.insert(0, "/root")
    from utils.supabase_db import get_conn
    from utils.alert_email import send_impact_report

    recips = [r.strip() for r in recipients_csv.split(",") if r.strip()] or None
    print("impact_report_job: starting...")
    stats = _build_impact_stats(get_conn)
    ok = send_impact_report(stats, djc=None, recipients=recips)
    print(f"impact_report_job: email sent={ok}, emails={stats.get('current', {}).get('emails_received')}")
    return ok


@app.function(
    image=_endpoint_image,
    # from_dotenv first, named secret second so prod creds win for shared keys; the endpoint
    # token (IMPACT_ENDPOINT_TOKEN) lives only in .env and survives.
    secrets=[modal.Secret.from_dotenv(), modal.Secret.from_name("salesforce-automation")],
    timeout=300,
)
@modal.fastapi_endpoint(method="POST")
def impact_report_endpoint(payload: dict):
    """On-demand all-time Kimedics impact report, triggered from the automation hub.
    Body: {token, recipients: [email, ...], invoker?}. Sends to the given recipients."""
    sys.path.insert(0, "/root")
    from fastapi import HTTPException
    from utils.supabase_db import get_conn
    from utils.alert_email import send_impact_report

    expected = (os.environ.get("IMPACT_ENDPOINT_TOKEN") or "").strip()
    supplied = (payload.get("token") or "").strip() if isinstance(payload, dict) else ""
    if not expected or supplied != expected:
        raise HTTPException(status_code=401, detail="unauthorized")

    recipients = [str(e).strip() for e in (payload.get("recipients") or []) if str(e).strip()]
    if not recipients:
        raise HTTPException(status_code=400, detail="no recipients")

    stats = _build_impact_stats(get_conn)
    ok = send_impact_report(stats, djc=None, recipients=recipients)
    return {"ok": bool(ok), "recipients": recipients,
            "emails": int(stats.get("current", {}).get("emails_received", 0))}


@app.function(
    image=_endpoint_image,
    secrets=[modal.Secret.from_dotenv(), modal.Secret.from_name("salesforce-automation")],
    timeout=300,
)
@modal.fastapi_endpoint(method="POST")
def checkin_endpoint(payload: dict):
    """Manual Kimedics check-in, triggered from the hub (Admin → Check-In).
    Body: {token, invoker?}. Read-only: builds review packets for the 10 most
    concerning recently-touched jobs, emails Andy + Sean, returns the full report
    JSON for the hub to render. Reuses IMPACT_ENDPOINT_TOKEN so the Modal secret
    never needs editing. Spec: docs/superpowers/specs/2026-07-29-kimedics-checkin-design.md
    """
    sys.path.insert(0, "/root")
    from fastapi import HTTPException
    from utils.alert_email import send_checkin_report
    from utils.checkin import run_checkin
    from utils.salesforce import get_token_auto
    from utils.supabase_db import get_conn

    expected = (os.environ.get("IMPACT_ENDPOINT_TOKEN") or "").strip()
    supplied = (payload.get("token") or "").strip() if isinstance(payload, dict) else ""
    if not expected or supplied != expected:
        raise HTTPException(status_code=401, detail="unauthorized")

    try:
        token_data = get_token_auto(
            os.environ.get("SALESFORCE_CONSUMER_KEY", ""),
            os.environ.get("SALESFORCE_CONSUMER_SECRET", ""),
            token_url=os.environ.get("SALESFORCE_TOKEN_URL") or None,
        )
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Salesforce auth failed: {str(e)[:300]}")

    # get_conn yields None on connection failure (e.g. the Supabase session pooler's
    # 15-client cap when a scrape tick holds slots). Transient — retry briefly, and
    # surface a real message instead of Modal's opaque "Internal Server Error".
    report = None
    for attempt in range(3):
        with get_conn() as conn:
            if conn is None:
                time.sleep(3)
                continue
            try:
                report = run_checkin(conn, token_data["instance_url"], token_data["access_token"])
            except Exception as e:
                raise HTTPException(status_code=500, detail=f"check-in failed: {str(e)[:300]}")
        break
    if report is None:
        raise HTTPException(
            status_code=503,
            detail="database busy (connection pool full) — wait a minute and run again",
        )

    report["invoker"] = str(payload.get("invoker") or "")[:200]
    report["email_sent"] = bool(send_checkin_report(report))
    return report


@app.local_entrypoint()
def run_impact_report_once(to: str = "seanhyang1@gmail.com"):
    """Generate + send the all-time impact report. Defaults to a REVIEW-ONLY copy to
    one address; pass --to '' to send to the full distribution, or a comma list."""
    ok = impact_report_job.remote(to)
    print(f"Done: impact report sent={ok} to={to or 'default list'}")


@app.function(
    image=_light_image,
    secrets=[modal.Secret.from_name("salesforce-automation")],
    timeout=60,
)
def dates_alert_sample_job():
    """Send a [SAMPLE] low-confidence date-review alert (the email a real scrape
    fires when AI resolves a job's active dates with < 100% confidence)."""
    sys.path.insert(0, "/root")
    import utils.alert_email as ae

    _orig = ae._send
    ae._send = lambda subject, html, text="", recipients=None: _orig(
        f"[SAMPLE] {subject}", html, text, recipients=recipients
    )
    ok = ae.send_dates_review_alert(
        "19796",
        kimedics_url="https://portal.kimedics.com/app/workspace/job-posts/19796",
        sf_job_id="a015f00000KxSFwAAN",
        structured="June 1-2, 11-12, 15-19, 22",
        resolved="June 1-2, 15-19, 22",
        confidence=72,
        reason=("Cancellation wording \"cancelled the need for June 11/12\" is slightly "
                "ambiguous about whether both the 11th and 12th are removed."),
    )
    print(f"dates_alert_sample_job: sent={ok}")
    return ok


@app.local_entrypoint()
def run_dates_alert_sample():
    """Send the [SAMPLE] low-confidence date-review alert via Modal."""
    ok = dates_alert_sample_job.remote()
    print(f"Done: sample low-confidence dates alert sent={ok}")


@app.function(
    image=_light_image,
    secrets=[modal.Secret.from_name("salesforce-automation")],
    timeout=60,
)
def dates_missing_sample_job(to: str = "seanhyang1@gmail.com"):
    """Send a [SAMPLE] 'no coverage dates captured' alert (the email a real scrape
    fires when an open job yields no dates) to a single recipient."""
    sys.path.insert(0, "/root")
    import utils.alert_email as ae

    _orig = ae._send
    ae._send = lambda subject, html, text="", recipients=None: _orig(
        f"[SAMPLE] {subject}", html, text, recipients=[to]
    )
    ok = ae.send_dates_review_alert(
        "19586",
        kind="missing",
        kimedics_url="https://portal.kimedics.com/app/workspace/job-posts/19586",
        sf_job_id="a015f00000KxSFwAAN",
        structured="Other job post notes: April 7",
        resolved="",
        confidence=0,
        reason="No 'Dates' line or recognizable coverage dates were found in the post.",
    )
    print(f"dates_missing_sample_job: sent={ok} -> {to}")
    return ok


@app.local_entrypoint()
def run_dates_missing_sample(to: str = "seanhyang1@gmail.com"):
    """Send the [SAMPLE] 'no dates captured' alert via Modal to a single recipient."""
    ok = dates_missing_sample_job.remote(to)
    print(f"Done: sample missing-dates alert sent={ok} -> {to}")

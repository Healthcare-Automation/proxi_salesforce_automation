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

# TEMPORARY: 2 h Gmail window (was 1 h). Keep lookback ≥ email window for dedupe.
EMAIL_HOURS             = 2.0
SUPABASE_LOOKBACK_HOURS = 3.0

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

    return len(new_rows)


# ── Daily summary (9 AM ET = 13:00 UTC, covers EDT; adjust for EST in winter) ─

@app.function(
    image=_light_image,
    schedule=modal.Cron("0 13 * * *"),
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
    Query Supabase for the last 24 h and return a stats dict for send_daily_summary.
    Isolated into a helper so it can be tested locally without Modal.
    """
    import psycopg2.extras
    from datetime import datetime, timezone, timedelta

    now    = datetime.now(timezone.utc)
    since  = now - timedelta(hours=24)
    period = f"{since.strftime('%b %-d')}–{now.strftime('%-d, %Y')}"

    stats = {
        "period_label":       period,
        "emails_received":    0,
        "scrape_attempts":    0,
        "scrape_success":     0,
        "scrape_partial":     0,
        "scrape_failed":      0,
        "total_warnings":     0,
        "total_criticals":    0,
        "sf_mapped":          0,   # has sf_job_id
        "sf_unmapped":        0,   # scraped but no sf_job_id yet
        "sf_worksite_mapped": 0,   # has sf_worksite_account_id
        "sf_unmapped_jobs":   [],  # list of {job_post_id, job_title, status, view_job_link, scraped_at}
        "example_jobs":       [],
        "issue_log":          [],
        "runs":               [],
    }

    with get_conn() as conn:
        if conn is None:
            print("daily_summary_job: Supabase connection unavailable")
            return stats

        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:

            # ── Email scrapes (last 24h) ─────────────────────────────────────
            cur.execute(
                "SELECT COUNT(*) AS n FROM email_scrapes WHERE created_at > %s;",
                (since,),
            )
            row = cur.fetchone()
            stats["emails_received"] = int((row or {}).get("n", 0))

            # ── Pipeline runs (last 24h) ─────────────────────────────────────
            cur.execute(
                """SELECT id AS run_id, run_type, started_at, finished_at
                   FROM scrape_runs
                   WHERE created_at > %s
                   ORDER BY started_at ASC
                   LIMIT 100;""",
                (since,),
            )
            raw_runs = [dict(r) for r in cur.fetchall()]
            stats["runs"] = _pair_runs(raw_runs)

            # ── Job content rows (last 24h) ──────────────────────────────────
            cur.execute(
                """SELECT
                     job_post_id, job_id, title_line, location_line, practice_value,
                     city, state, job_title, posting_org, priority, status,
                     point_of_contact, provider_start_date, provider_end_date,
                     posted_date, description_full_text, sf_job_id, sf_worksite_account_id,
                     view_job_link, raw_columns_json, created_at
                   FROM job_content
                   WHERE created_at > %s
                   ORDER BY created_at DESC;""",
                (since,),
            )
            rows = cur.fetchall()

        # ── Classify each row ────────────────────────────────────────────────
        total = len(rows)
        stats["scrape_attempts"] = total
        example_jobs: list[dict] = []
        issue_log:    list[dict] = []

        for row in rows:
            d = dict(row)
            # Rebuild cleaned dict from DB columns for validation
            cleaned = {k: (d.get(k) or "") for k in [
                "title_line", "job_id", "job_title", "status", "priority",
                "provider_start_date", "provider_end_date", "posted_date",
                "location_line", "state", "city", "posting_org", "point_of_contact",
                "practice_value", "description_full_text",
            ]}
            # raw_columns_json may have extra fields (e.g. position_type)
            if d.get("raw_columns_json"):
                try:
                    import json
                    extra = d["raw_columns_json"]
                    if isinstance(extra, str):
                        extra = json.loads(extra)
                    for k in ("rates",):
                        if not cleaned.get(k) and extra.get(k):
                            cleaned[k] = extra[k]
                except Exception:
                    pass

            issues = validate_scraped_job(cleaned, job_post_id=str(d.get("job_post_id") or ""))
            summ   = issues_summary(issues)
            stats["total_criticals"] += summ["critical"]
            stats["total_warnings"]  += summ["warning"]

            # SF mapping status
            has_sf_job_id   = bool((d.get("sf_job_id") or "").strip())
            has_sf_worksite = bool((d.get("sf_worksite_account_id") or "").strip())
            if has_sf_job_id:
                stats["sf_mapped"] += 1
            if has_sf_worksite:
                stats["sf_worksite_mapped"] += 1

            title = (d.get("title_line") or "").strip()
            if not title:
                stats["scrape_failed"] += 1
                issue_log.append({
                    "job_post_id":   d.get("job_post_id", "?"),
                    "view_job_link": d.get("view_job_link", ""),
                    "issues_text":   issues_as_text(issues, str(d.get("job_post_id", ""))),
                })
            elif summ["critical"] > 0 or summ["warning"] >= 3:
                stats["scrape_partial"] += 1
                issue_log.append({
                    "job_post_id":   d.get("job_post_id", "?"),
                    "view_job_link": d.get("view_job_link", ""),
                    "issues_text":   issues_as_text(issues, str(d.get("job_post_id", ""))),
                })
            else:
                stats["scrape_success"] += 1
                # Track unmapped jobs (successfully scraped but no SF job ID yet)
                if not has_sf_job_id:
                    stats["sf_unmapped"] += 1
                    stats["sf_unmapped_jobs"].append({
                        "job_post_id":   d.get("job_post_id", "?"),
                        "job_title":     (d.get("job_title") or "").strip(),
                        "status":        (d.get("status") or "").strip(),
                        "posting_org":   (d.get("posting_org") or "").strip(),
                        "view_job_link": d.get("view_job_link", ""),
                        "scraped_at":    str(d.get("created_at", ""))[:16],
                    })
                example_jobs.append(cleaned | {
                    "job_post_id": d.get("job_post_id"),
                    "sf_job_id":   d.get("sf_job_id") or "",
                    "scraped_at":  str(d.get("created_at", ""))[:19],
                })

        stats["example_jobs"] = example_jobs
        stats["issue_log"]    = issue_log

    return stats


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

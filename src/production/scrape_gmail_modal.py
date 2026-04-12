"""
Modal jobs: scraping pipeline (every 10 min) + daily summary report.

Functions
---------
scrape_gmail_job   — Gmail → Playwright → Supabase → Salesforce (every 10 min)
daily_summary_job  — Query Supabase, validate quality, send digest email (daily 9 AM ET)

Secrets (salesforce-automation): GMAIL_APP_PASSWORD, GMAIL_EMAIL, DB_PASSWORD,
KIMEDICS_EMAIL, KIMEDICS_PASSWORD, OPENAI_API_KEY, plus Salesforce env vars.

Deploy (from project root):
  modal deploy src/production/scrape_gmail_modal.py

Run once:
  modal run src/production/scrape_gmail_modal.py::scrape_gmail_job
  modal run src/production/scrape_gmail_modal.py::daily_summary_job
"""

import os
import sys
from pathlib import Path

import modal

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

app = modal.App("salesforce-automation")

EMAIL_HOURS             = 1.0
SUPABASE_LOOKBACK_HOURS = 2.0

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

    ok = sum(1 for r in scrape_results if not r.get("error"))
    print(f"Link scrape done: {ok}/{len(scrape_results)} logged to job_content")
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
    """Run the incremental scrape once (same as scheduled job)."""
    n = scrape_gmail_job.remote()
    print(f"Done: {n} new email(s) logged")

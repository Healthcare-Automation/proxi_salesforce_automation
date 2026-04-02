"""
Modal job: full incremental pipeline every 30 minutes (end-to-end).
1. Gmail: fetch last 1 hour; check Supabase; log only NEW emails.
2. Link scrape: for each new email with view_job_link, Playwright → parse → log job_content + job_current.

Secrets (salesforce-automation): GMAIL_APP_PASSWORD, DB_PASSWORD, KIMEDICS_EMAIL, KIMEDICS_PASSWORD,
plus Salesforce env vars for post-scrape ``sf_job_id`` / worksite resolution (same as local ``pull_all_jobs``).

Deploy (from project root):
  modal deploy src/production/scrape_gmail_modal.py

Run once:
  modal run src/production/scrape_gmail_modal.py::scrape_gmail_job
"""

import os
import sys
from pathlib import Path

import modal

_modal_dir = Path(__file__).resolve().parent
_src_root = _modal_dir.parent
image = (
    modal.Image.debian_slim()
    .pip_install("psycopg2-binary", "playwright", "python-dotenv")
    .run_commands("playwright install chromium", "playwright install-deps chromium")
    .add_local_dir(_src_root / "utils", remote_path="/root/utils")
)

app = modal.App("salesforce-automation")

EMAIL_HOURS = 1.0
SUPABASE_LOOKBACK_HOURS = 2.0


@app.function(
    image=image,
    schedule=modal.Period(minutes=30),
    secrets=[modal.Secret.from_name("salesforce-automation")],
    timeout=600,  # 10 min for Gmail + Playwright scrape
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
        log_job_content,
        log_run_finish,
        log_run_start,
    )

    email_account = os.environ.get("GMAIL_EMAIL", "proxi@scrubnetwork.com")
    email_password = os.environ.get("GMAIL_APP_PASSWORD", "")
    kimedics_email = os.environ.get("KIMEDICS_EMAIL", "").strip()
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

    # Connect to Supabase (surface real error for debugging)
    conn = None
    try:
        import psycopg2
        conn_str = get_connection_string()
        has_pwd = bool(os.environ.get("DB_PASSWORD", "").strip())
        print(f"DB_PASSWORD set: {has_pwd}, connecting to Supabase...")
        conn = psycopg2.connect(conn_str)
    except Exception as e:
        err_msg = str(e)
        if "password" in err_msg.lower() or "auth" in err_msg.lower():
            print("Supabase auth failed: check DB_PASSWORD in Modal secret (no typos, no extra spaces)")
        else:
            print(f"Supabase connection failed: {err_msg}")
        return 0

    try:
        ensure_tables(conn)
        existing = get_existing_email_keys(conn, since_hours_ago=SUPABASE_LOOKBACK_HOURS)
        new_rows = filter_parsed_emails_not_logged(parsed, existing)

        if not new_rows:
            print("No new emails in the window (all already logged)")
            conn.close()
            return 0

        csv_fields = ["job_post_id", "location", "action_or_change", "view_job_link", "subject", "date", "from_"]
        run_id = log_run_start(conn, "gmail", csv_fields)
        if not run_id:
            conn.close()
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
            sf_cache: dict = {}
            touched_job_ids: set[str] = set()
            for r in scrape_results:
                try:
                    cl = r.get("cleaned") or {}
                    jid = str(cl.get("job_id") or r.get("job_post_id") or "").strip()
                    if jid:
                        touched_job_ids.add(jid)
                except Exception:
                    pass
                log_job_content(
                    conn,
                    link_run_id,
                    r["job_post_id"],
                    r["email_received_date"],
                    r.get("cleaned") or {},
                    email_scrape_id=r.get("email_scrape_id"),
                    sf_lookup_cache=sf_cache,
                    view_job_link=r.get("view_job_link"),
                )
            # After logging, resolve sf_job_id + worksite id; then patch empty External_* / test_* on Job__c when mapped.
            if touched_job_ids:
                try:
                    from utils.sf_job_supabase_resolve import resolve_sf_ids_for_job_ids

                    updated = resolve_sf_ids_for_job_ids(
                        conn,
                        sorted(touched_job_ids),
                        schema="public",
                        run_id=link_run_id,
                    )
                    if updated:
                        print(
                            f"Resolved sf ids for {updated}/{len(touched_job_ids)} touched job(s)."
                        )
                except Exception as e:
                    print(f"SF id resolution step skipped (error): {e}")
                try:
                    from utils.sf_scrape_sync import sync_missing_scrape_fields_for_job_ids

                    att, patched = sync_missing_scrape_fields_for_job_ids(
                        conn, sorted(touched_job_ids), schema="public", run_id=link_run_id
                    )
                    if patched:
                        print(
                            f"Patched Salesforce scrape fields (External ID/Link + test fields) for "
                            f"{patched}/{att} mapped job(s)."
                        )
                except Exception as e:
                    print(f"SF scrape-field sync skipped (error): {e}")
            if link_run_id:
                log_run_finish(conn, link_run_id)

    ok = sum(1 for r in scrape_results if not r.get("error"))
    print(f"Link scrape done: {ok}/{len(scrape_results)} logged to job_content")
    return len(new_rows)


@app.local_entrypoint()
def run_once():
    """Run the incremental scrape once (same as scheduled job)."""
    n = scrape_gmail_job.remote()
    print(f"Done: {n} new email(s) logged")

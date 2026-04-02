#!/usr/bin/env python3
"""
Batch scrape Kimedics job links using Playwright (browser login).

**One login, then all URLs in the same session:** Opens the first link, breaks the auth
barrier once (Sign In), then navigates to each job URL in the same window and saves
cleaned CSV per job (format: {job_id}_{email_received_date}.csv). Also logs to Supabase.

Usage:
  python tests/scrape_kimedics_batch_playwright.py           # all links from job_emails.csv
  python tests/scrape_kimedics_batch_playwright.py --max 10   # first 10
  python tests/scrape_kimedics_batch_playwright.py --pg-schema public   # skip prompt (from run_incremental)

If ``--pg-schema`` is omitted, you are prompted to type STAGING or PRODUCTION (all caps), same as other local tools.

Requires: pip install playwright && playwright install chromium
.env: KIMEDICS_EMAIL, KIMEDICS_PASSWORD, DB_PASSWORD (for Supabase logging)
"""

import csv
import os
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
JOB_EMAILS_CSV = DATA_DIR / "job_emails.csv"
JOB_CONTENT_DIR = DATA_DIR / "job_content"
RESULTS_CSV = DATA_DIR / "job_link_scrape_results_playwright.csv"

# Allow importing from src
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
if str(PROJECT_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT / "src"))
# Login once: wait for Angular login form on first URL
BETWEEN_URLS_S = 0.5


def load_job_emails(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def safe_job_id(job_id: str) -> str:
    s = "".join(c for c in (job_id or "unknown") if c.isalnum() or c in "-_")
    return s or "unknown"


def _email_scrape_id_from_row(row: dict) -> int | None:
    raw = row.get("email_scrape_id")
    if raw is None or str(raw).strip() == "":
        return None
    try:
        return int(str(raw).strip())
    except ValueError:
        return None


def email_date_to_filename_safe(date_iso: str | None) -> str:
    """Turn email date (ISO) into YYYY-MM-DD for filename."""
    if not date_iso or not isinstance(date_iso, str):
        return "unknown"
    s = date_iso.strip()[:10]
    return s if s and all(c.isdigit() or c == "-" for c in s) else "unknown"


def main():
    max_links = None
    csv_path = JOB_EMAILS_CSV
    pg_schema_arg: str | None = None
    args = sys.argv[1:]
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--max" and i + 1 < len(args):
            try:
                max_links = int(args[i + 1])
            except ValueError:
                pass
            i += 2
            continue
        if a == "--csv" and i + 1 < len(args):
            csv_path = Path(args[i + 1])
            i += 2
            continue
        if a == "--pg-schema" and i + 1 < len(args):
            pg_schema_arg = args[i + 1].strip()
            i += 2
            continue
        i += 1

    from dotenv import load_dotenv
    load_dotenv(PROJECT_ROOT / ".env")
    email = os.environ.get("KIMEDICS_EMAIL", "").strip()
    password = os.environ.get("KIMEDICS_PASSWORD", "").strip()
    if not email or not password:
        print("Set KIMEDICS_EMAIL and KIMEDICS_PASSWORD in .env")
        sys.exit(1)

    if pg_schema_arg:
        pg_schema = pg_schema_arg
        print(f"Supabase schema (from --pg-schema): {pg_schema!r}")
    else:
        from utils.run_target_prompt import prompt_pg_schema

        pg_schema = prompt_pg_schema()
        print(f"Supabase target schema: {pg_schema!r}")

    rows = load_job_emails(csv_path)
    with_links = [r for r in rows if (r.get("view_job_link") or "").strip()]
    if max_links is not None:
        with_links = with_links[:max_links]
    if not with_links:
        print(f"No view_job_link in {csv_path}. Run src/local/local_run_scrape_gmail.py or run_incremental first.")
        sys.exit(1)

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("Install Playwright: pip install playwright && playwright install chromium")
        sys.exit(1)

    from utils.playwright_job_scrape import (
        _navigate_and_login_if_needed,
        canonical_kimedics_url,
        visit_and_extract,
        NAV_TIMEOUT_MS as PW_NAV_TIMEOUT_MS,
    )
    from utils.supabase_db import (
        HAS_PSYCOPG2,
        ensure_schema_for_writes,
        get_conn,
        log_job_content,
        log_run_finish,
        log_run_start,
    )

    if not HAS_PSYCOPG2:
        print(
            "WARNING: psycopg2 not installed — job_content / job_current will not be saved. "
            "Install: pip install psycopg2-binary",
            file=sys.stderr,
        )

    JOB_CONTENT_DIR.mkdir(parents=True, exist_ok=True)
    results = []
    total = len(with_links)
    sf_cache: dict = {}

    # Do NOT hold one DB connection open during Playwright (can be 30+ min). Poolers drop
    # idle-in-transaction connections and @contextmanager may skip commit on errors.
    run_id = None
    try:
        with get_conn() as conn:
            if conn is not None:
                ensure_schema_for_writes(conn, pg_schema)
                run_id = log_run_start(
                    conn,
                    "link_batch",
                    ["job_post_id", "url", "content_length", "error"],
                    schema=pg_schema,
                )
                if run_id is None:
                    print(
                        "WARNING: log_run_start returned no id — job_content rows will not be written.",
                        file=sys.stderr,
                    )
                else:
                    print(f"Supabase link_batch run_id={run_id} (schema={pg_schema!r})")
            elif HAS_PSYCOPG2:
                print(
                    "WARNING: No database connection — job_content / job_current will not be saved. "
                    "See stderr above; check DB_PASSWORD / DIRECT_URL / DATABASE_URL in .env.",
                    file=sys.stderr,
                )
    except Exception as e:
        print(f"ERROR: Supabase log_run_start failed: {e}", file=sys.stderr)

    try:
        with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                page = browser.new_page()
                page.set_default_timeout(PW_NAV_TIMEOUT_MS)

                # --- Phase 1: Login via canonical URL (stable, no redirect chain) ---
                first_canon = canonical_kimedics_url(with_links[0].get("job_post_id", ""))
                login_url = first_canon or (with_links[0].get("view_job_link") or "").strip()
                print(f"Logging in ({login_url[:70]}...)...")
                _navigate_and_login_if_needed(page, login_url, email, password)
                print("Scraping all URLs in same session (re-login if needed)...")

                # --- Phase 2: Visit each URL ---
                for idx, row in enumerate(with_links, 1):
                    job_id = row.get("job_post_id", "")
                    url = (row.get("view_job_link") or "").strip()
                    email_date = row.get("date")
                    if email_date is not None and hasattr(email_date, "isoformat"):
                        email_date = email_date.isoformat()
                    elif email_date is not None:
                        email_date = str(email_date).strip() or None
                    job_id_safe = safe_job_id(job_id)
                    date_safe = email_date_to_filename_safe(email_date)
                    print(f"[{idx}/{total}] Job #{job_id} — {url[:50]}...")
                    try:
                        body_text, desc_value = visit_and_extract(
                            page, url, job_id, email, password,
                        )
                        if desc_value:
                            body_text = body_text + "\n\n--- Description (full text) ---\n" + desc_value
                        # Save raw scraped text for debugging
                        txt_path = JOB_CONTENT_DIR / f"{job_id_safe}_{date_safe}.txt"
                        txt_path.write_text(body_text, encoding="utf-8")
                        # Clean into structured row and save as CSV: {job_id}_{email_received_date}.csv
                        from utils.job_content_parser import parse_job_content_txt, JOB_CONTENT_COLUMNS, cleaned_row_to_flat_dict
                        cleaned = parse_job_content_txt(body_text)
                        flat = cleaned_row_to_flat_dict(cleaned)
                        csv_name = f"{job_id_safe}_{date_safe}.csv"
                        out_path = JOB_CONTENT_DIR / csv_name
                        with open(out_path, "w", newline="", encoding="utf-8") as f:
                            w = csv.DictWriter(f, fieldnames=JOB_CONTENT_COLUMNS, quoting=csv.QUOTE_NONNUMERIC)
                            w.writeheader()
                            w.writerow(flat)
                        results.append({
                            "job_post_id": job_id,
                            "url": url,
                            "content_length": len(body_text),
                            "error": "",
                            "email_received_date": email_date,
                            "cleaned": cleaned,
                        })
                        # Fresh DB connection per row (Playwright holds no DB txn open).
                        if run_id is not None:
                            try:
                                with get_conn() as conn:
                                    if conn is None:
                                        print(
                                            f"  ! Supabase: no connection for job {job_id!r} (skipped).",
                                            file=sys.stderr,
                                        )
                                    else:
                                        cid = log_job_content(
                                            conn,
                                            run_id,
                                            job_id,
                                            email_date,
                                            cleaned,
                                            email_scrape_id=_email_scrape_id_from_row(row),
                                            schema=pg_schema,
                                            sf_lookup_cache=sf_cache,
                                            view_job_link=url,
                                        )
                                        if cid is None:
                                            print(
                                                f"  ! Supabase: log_job_content None for job {job_id!r} "
                                                f"(no matching email_scrape row for this job_post_id + date?).",
                                                file=sys.stderr,
                                            )
                                        else:
                                            print(f"  -> saved job_content id={cid}", flush=True)
                            except Exception as ex:
                                print(
                                    f"  ! Supabase log_job_content error (job {job_id!r}): {ex}",
                                    file=sys.stderr,
                                )
                        print(f"  -> {len(body_text)} chars -> {csv_name}")
                    except Exception as e:
                        msg = str(e)[:200]
                        results.append({
                            "job_post_id": job_id,
                            "url": url,
                            "content_length": 0,
                            "error": msg,
                            "email_received_date": email_date,
                            "cleaned": {},
                        })
                        print(f"  -> Error: {msg}")
                    if idx < total:
                        time.sleep(BETWEEN_URLS_S)

                browser.close()

    except Exception as e:
        print(f"ERROR: Playwright batch aborted: {e}", file=sys.stderr)

    touched_job_ids: set[str] = set()
    for r in results:
        if (r.get("error") or "").strip():
            continue
        try:
            cl = r.get("cleaned") or {}
            jid = str(cl.get("job_id") or r.get("job_post_id") or "").strip()
            if jid:
                touched_job_ids.add(jid)
        except Exception:
            pass
    if run_id is not None and touched_job_ids:
        try:
            from utils.sf_job_supabase_resolve import resolve_sf_ids_for_job_ids
            from utils.sf_scrape_sync import sync_missing_scrape_fields_for_job_ids

            with get_conn() as conn:
                if conn is not None:
                    n = resolve_sf_ids_for_job_ids(
                        conn,
                        sorted(touched_job_ids),
                        schema=pg_schema,
                        run_id=run_id,
                    )
                    if n:
                        print(
                            f"Resolved sf_job_id / worksite for {n}/{len(touched_job_ids)} job(s).",
                            flush=True,
                        )
                    att, patched = sync_missing_scrape_fields_for_job_ids(
                        conn, sorted(touched_job_ids), schema=pg_schema, run_id=run_id
                    )
                    if patched:
                        print(
                            f"Patched Salesforce scrape fields for {patched}/{att} mapped job(s).",
                            flush=True,
                        )
        except Exception as e:
            print(f"WARNING: SF id resolution / scrape-field sync after batch failed: {e}", file=sys.stderr)

    if run_id is not None:
        try:
            with get_conn() as conn:
                if conn is not None:
                    log_run_finish(conn, run_id, schema=pg_schema)
        except Exception as e:
            print(f"WARNING: log_run_finish failed: {e}", file=sys.stderr)

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    fieldnames = ["job_post_id", "url", "content_length", "error"]
    with open(RESULTS_CSV, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames, quoting=csv.QUOTE_NONNUMERIC)
        w.writeheader()
        w.writerows([{k: r.get(k, "") for k in fieldnames} for r in results])
    print(f"\nWrote {len(results)} results to {RESULTS_CSV}")


if __name__ == "__main__":
    main()

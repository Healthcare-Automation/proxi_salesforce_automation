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

        if scrape_err:
            print(f"  [SCRAPE ERROR] Job #{job_post_id}: {scrape_err}")
            try:
                send_scrape_alert(
                    job_post_id=job_post_id,
                    issues=issues,
                    cleaned=cl,
                    view_job_link=view_link,
                )
            except Exception as mail_err:
                print(f"  [alert_email] Could not send error alert: {mail_err}")
        elif should_send_immediate_alert(issues):
            print(
                f"  [VALIDATION] Job #{job_post_id}: "
                f"{summary['critical']} critical, {summary['warning']} warnings — sending alert"
            )
            try:
                send_scrape_alert(
                    job_post_id=job_post_id,
                    issues=issues,
                    cleaned=cl,
                    view_job_link=view_link,
                )
            except Exception as mail_err:
                print(f"  [alert_email] Could not send validation alert: {mail_err}")
        elif issues:
            print(
                f"  [VALIDATION] Job #{job_post_id}: "
                f"{summary['info']} info — logged, no alert"
            )
        else:
            print(f"  [VALIDATION] Job #{job_post_id}: all checks passed ✓")

        if issues:
            print(issues_as_text(issues, job_post_id))

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

    return touched_job_ids

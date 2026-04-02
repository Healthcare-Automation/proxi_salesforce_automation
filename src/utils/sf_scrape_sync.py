"""
After a Kimedics scrape, push a small set of Job__c fields to Salesforce **only when** we have
``sf_job_id`` and the Salesforce field is currently blank (do not overwrite existing SF values).

Fields: External Job ID / Link (standard custom names) + org-specific test fields.
"""

from __future__ import annotations

import os
from datetime import datetime
from typing import Any

from utils.sf_job_payload import _truncate_external_job_id, external_job_link_from_job_row
from utils.sf_job_rest_minimal import DEFAULT_REST_VERSION, describe_sobject, rest_json, update_job_record
from utils.sf_partial_update import prepare_patch_payload
from utils.salesforce import get_token_auto

# Custom fields created for this integration (safe to update).
# NOTE: match your org's API names exactly (case-sensitive).
SF_FIELD_TEST_STATUS = "test_status__c"
SF_FIELD_TEST_POSTED_DATE = "test_posted_date__c"

SCRAPE_SYNC_FIELD_ORDER: tuple[str, ...] = (
    "External_Job_ID__c",
    "External_Job_Link__c",
    SF_FIELD_TEST_STATUS,
    SF_FIELD_TEST_POSTED_DATE,
)


def _sf_field_empty(val: Any) -> bool:
    if val is None:
        return True
    if isinstance(val, bool):
        return False
    return str(val).strip() == ""


def posted_date_to_salesforce_date(raw: str | None) -> str | None:
    """Normalize Kimedics posted_date text to YYYY-MM-DD for a Salesforce Date field."""
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None
    if len(s) >= 10 and s[4] == "-" and s[7] == "-":
        return s[:10]
    for fmt in ("%m/%d/%y", "%m/%d/%Y", "%Y-%m-%d", "%m-%d-%Y"):
        try:
            return datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def desired_scrape_sync_fields_from_job_row(row: dict | None) -> dict[str, Any]:
    """Values we want on Salesforce from the latest scraped job row (Supabase-shaped dict)."""
    r = dict(row or {})
    out: dict[str, Any] = {}
    ext_id = _truncate_external_job_id(r.get("job_id"))
    if ext_id:
        out["External_Job_ID__c"] = ext_id
    link = external_job_link_from_job_row(r)
    if link:
        out["External_Job_Link__c"] = link
    st = (r.get("status") or "").strip()
    if st:
        out[SF_FIELD_TEST_STATUS] = st
    pd = posted_date_to_salesforce_date(r.get("posted_date"))
    if pd:
        out[SF_FIELD_TEST_POSTED_DATE] = pd
    return out


def _rest_token_from_env() -> tuple[str, str] | None:
    ck = (os.environ.get("SALESFORCE_CONSUMER_KEY") or "").strip()
    cs = (os.environ.get("SALESFORCE_CONSUMER_SECRET") or "").strip()
    if not ck or not cs:
        return None
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


def _get_job_fields(
    instance_url: str,
    access_token: str,
    sf_job_id: str,
    fields: tuple[str, ...],
    *,
    api_version: str = DEFAULT_REST_VERSION,
) -> dict[str, Any]:
    rid = (sf_job_id or "").strip()
    if not rid:
        return {}
    # GET sobjects/Job__c/{id}?fields=...
    field_list = ",".join(fields)
    path = f"sobjects/Job__c/{rid}?fields={field_list}"
    out = rest_json(instance_url, access_token, "GET", path, api_version=api_version)
    return out if isinstance(out, dict) else {}


def sync_missing_scrape_fields_to_salesforce(
    job_row: dict,
    *,
    conn=None,
    job_id_for_log: str | None = None,
    schema: str = "public",
    run_id: int | None = None,
    dry_run: bool = False,
    job_object_name: str = "Job__c",
) -> dict[str, Any]:
    """
    PATCH Job__c for ``job_row['sf_job_id']`` with any of the scrape fields that are empty in SF
    but non-empty in ``desired_scrape_sync_fields_from_job_row``.

    If ``conn`` is set, appends ``job_event_log`` rows on success/skip/error (best-effort).
    """
    jid_log = (job_id_for_log or job_row.get("job_id") or "").strip()
    sf_job_id = (job_row.get("sf_job_id") or "").strip()
    out: dict[str, Any] = {"ok": False, "sf_job_id": sf_job_id or None, "patched": False, "fields": []}

    if not sf_job_id:
        out["reason"] = "no_sf_job_id"
        return out

    tok = _rest_token_from_env()
    if not tok:
        out["reason"] = "no_salesforce_credentials"
        _maybe_log(conn, jid_log, "sf_scrape_fields_skip", schema, {"reason": out["reason"]}, run_id=run_id)
        return out

    instance_url, access_token = tok
    desired = desired_scrape_sync_fields_from_job_row(job_row)
    if not desired:
        out["ok"] = True
        out["reason"] = "nothing_to_send"
        _maybe_log(conn, jid_log, "sf_scrape_fields_skip", schema, {"reason": out["reason"]}, run_id=run_id)
        return out

    describe = describe_sobject(instance_url, access_token, job_object_name)
    current = _get_job_fields(instance_url, access_token, sf_job_id, SCRAPE_SYNC_FIELD_ORDER)

    patch: dict[str, Any] = {}
    for fname in SCRAPE_SYNC_FIELD_ORDER:
        if fname not in desired:
            continue
        want = desired[fname]
        if want is None or (isinstance(want, str) and not want.strip()):
            continue
        if not _sf_field_empty(current.get(fname)):
            continue
        patch[fname] = want

    if not patch:
        out["ok"] = True
        out["reason"] = "all_fields_already_set_in_salesforce"
        _maybe_log(
            conn,
            jid_log,
            "sf_scrape_fields_skip",
            schema,
            {"reason": out["reason"], "checked": list(SCRAPE_SYNC_FIELD_ORDER)},
            run_id=run_id,
        )
        return out

    body = prepare_patch_payload(describe, patch, coerce_picklists=True)
    if not body:
        out["reason"] = "patch_empty_after_describe_filter"
        _maybe_log(conn, jid_log, "sf_scrape_fields_error", schema, {"reason": out["reason"], "attempt": patch}, run_id=run_id)
        return out

    if dry_run:
        out["ok"] = True
        out["patched"] = True
        out["fields"] = sorted(body.keys())
        out["dry_run_body"] = body
        return out

    try:
        update_job_record(instance_url, access_token, job_object_name, sf_job_id, body)
    except Exception as e:
        out["error"] = str(e)[:2000]
        _maybe_log(
            conn,
            jid_log,
            "sf_scrape_fields_error",
            schema,
            {"error": out["error"], "attempt": body},
            run_id=run_id,
        )
        return out

    out["ok"] = True
    out["patched"] = True
    out["fields"] = sorted(body.keys())
    _maybe_log(
        conn,
        jid_log,
        "sf_scrape_fields_patched",
        schema,
        {"fields": out["fields"], "values": {k: body[k] for k in out["fields"]}},
        run_id=run_id,
    )
    return out


def _maybe_log(conn, job_id: str, event_type: str, schema: str, payload: dict, *, run_id: int | None = None) -> None:
    if conn is None or not job_id:
        return
    try:
        from utils.supabase_db import log_job_event

        log_job_event(conn, job_id=job_id, event_type=event_type, payload=payload, schema=schema, run_id=run_id)
    except Exception:
        pass


def sync_missing_scrape_fields_for_job_ids(
    conn,
    job_ids: list[str],
    *,
    schema: str = "public",
    dry_run: bool = False,
    run_id: int | None = None,
) -> tuple[int, int]:
    """
    Load ``job_current`` rows and run :func:`sync_missing_scrape_fields_to_salesforce` for each.

    Returns (attempted_count, patched_count).
    """
    from utils.supabase_db import get_job_current

    ids = sorted({str(x or "").strip() for x in job_ids if str(x or "").strip()})
    if not ids or conn is None:
        return (0, 0)

    from utils.supabase_db import log_job_event

    rows = get_job_current(conn, job_ids=ids, limit=None, schema=schema)
    by_job = {str(r.get("job_id") or "").strip(): r for r in rows}
    attempted = 0
    patched = 0
    for jid in ids:
        row = by_job.get(jid) or {}
        if not (row.get("sf_job_id") or "").strip():
            log_job_event(
                conn,
                job_id=jid,
                event_type="sf_sync_skipped_no_mapping",
                run_id=run_id,
                schema=schema,
                payload={"reason": "no sf_job_id on job_current"},
            )
            continue
        attempted += 1
        r = sync_missing_scrape_fields_to_salesforce(
            dict(row), conn=conn, job_id_for_log=jid, schema=schema, dry_run=dry_run, run_id=run_id
        )
        if r.get("patched"):
            patched += 1
    return (attempted, patched)

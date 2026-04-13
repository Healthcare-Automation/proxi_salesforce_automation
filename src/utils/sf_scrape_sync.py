"""
After a Kimedics scrape, push Job__c fields to Salesforce whenever we have ``sf_job_id``.

By default we compute the desired field set with the same rules as ``prepare_payload_for_write``
(full mapped/updateable fields + canonical description).

Org-specific **test** fields (``test_status__c``, ``test_posted_date__c``) are included only when
``PROXI_SF_TEST_MODE=true``.
"""

from __future__ import annotations

import os
from datetime import datetime
from typing import Any, Optional, Union

from utils.sf_job_payload import (
    _canonical_description_use_html,
    _truncate_external_job_id,
    external_job_link_from_job_row,
    prepare_payload_for_write,
)
from utils.sf_job_rest_minimal import DEFAULT_REST_VERSION, describe_sobject, rest_json, update_job_record
from utils.sf_partial_update import prepare_patch_payload
from utils.salesforce import get_token_auto

# Custom fields created for this integration (safe to update).
# NOTE: match your org's API names exactly (case-sensitive).
SF_FIELD_TEST_STATUS = "test_status__c"
SF_FIELD_TEST_POSTED_DATE = "test_posted_date__c"

def _env_truthy(name: str) -> bool:
    return (os.environ.get(name) or "").strip().lower() in ("1", "true", "yes")


def _normalize_sf_compare_value(val: Any) -> str:
    """
    Normalize values for "should we PATCH?" comparisons.
    Salesforce REST GET may return None, strings, numbers, booleans; we compare trimmed strings.
    """
    if val is None:
        return ""
    if isinstance(val, bool):
        return "true" if val else "false"
    return str(val).strip()


def posted_date_to_salesforce_date(raw: Optional[str]) -> Optional[str]:
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


def _nonempty_desired_field_names(desired: dict[str, Any]) -> list[str]:
    """API names we intend to sync (non-null, non-blank string values)."""
    out: list[str] = []
    for fname, want in desired.items():
        if want is None:
            continue
        if isinstance(want, str) and not want.strip():
            continue
        out.append(fname)
    return sorted(out)


def desired_scrape_sync_fields_from_job_row(row: Optional[dict]) -> dict[str, Any]:
    """Values we want on Salesforce from the latest scraped job row (Supabase-shaped dict)."""
    r = dict(row or {})
    out: dict[str, Any] = {}
    ext_id = _truncate_external_job_id(r.get("job_id"))
    if ext_id:
        out["External_Job_ID__c"] = ext_id
    link = external_job_link_from_job_row(r)
    if link:
        out["External_Job_Link__c"] = link
    # SF org requires Job_Ranking__c on PATCH; default when blank matches full payload rules.
    out["Job_Ranking__c"] = str(r.get("job_ranking") or "B").strip() or "B"
    st = (r.get("status") or "").strip()
    if st:
        out[SF_FIELD_TEST_STATUS] = st
    pd = posted_date_to_salesforce_date(r.get("posted_date"))
    if pd:
        out[SF_FIELD_TEST_POSTED_DATE] = pd
    return out


def _rest_token_from_env() -> Optional[tuple[str, str]]:
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
    job_id_for_log: Optional[str] = None,
    schema: str = "public",
    run_id: Optional[int] = None,
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
        _maybe_log(
            conn,
            jid_log,
            "sf_sync_skipped_no_mapping",
            schema,
            {
                "reason": "no_sf_job_id",
                "job_id": jid_log or None,
                "detail": (
                    "sync_missing_scrape_fields_to_salesforce was invoked without sf_job_id on the job row; "
                    "no Salesforce write was attempted."
                ),
            },
            run_id=run_id,
        )
        return out

    tok = _rest_token_from_env()
    if not tok:
        out["reason"] = "no_salesforce_credentials"
        _maybe_log(
            conn,
            jid_log,
            "sf_scrape_fields_skip",
            schema,
            {
                "reason": out["reason"],
                "detail": "Salesforce API credentials are not configured in the runtime environment, so no field write was attempted.",
            },
            run_id=run_id,
        )
        return out

    instance_url, access_token = tok

    describe = describe_sobject(instance_url, access_token, job_object_name)
    desired = prepare_payload_for_write(
        job_row,
        describe,
        use_canonical_description=True,
        for_update=True,
        description_use_html=_canonical_description_use_html(),
    )
    if conn and jid_log and not (job_row.get("sf_worksite_account_id") or "").strip():
        _maybe_log(
            conn,
            jid_log,
            "sf_worksite_missing_on_job_row",
            schema,
            {
                "sf_job_id": sf_job_id or None,
                "message": "sf_worksite_account_id empty; Job_Worksite_Location_1__c omitted from payload (no default Id).",
            },
            run_id=run_id,
        )
    if _env_truthy("PROXI_SF_TEST_MODE"):
        desired.update(desired_scrape_sync_fields_from_job_row(job_row))

    if not desired:
        out["ok"] = True
        out["reason"] = "nothing_to_send"
        _maybe_log(
            conn,
            jid_log,
            "sf_scrape_fields_skip",
            schema,
            {
                "reason": out["reason"],
                "detail": "After mapping the scrape to Salesforce fields, there was nothing non-empty to compare or send for this job.",
            },
            run_id=run_id,
        )
        return out

    field_names = tuple(sorted(desired.keys()))
    current = _get_job_fields(instance_url, access_token, sf_job_id, field_names)

    patch: dict[str, Any] = {}
    for fname, want in desired.items():
        if want is None or (isinstance(want, str) and not want.strip()):
            continue
        have = current.get(fname)
        if _normalize_sf_compare_value(have) == _normalize_sf_compare_value(want):
            continue
        patch[fname] = want

    if not patch:
        out["ok"] = True
        out["reason"] = "already_matches_salesforce"
        compared = _nonempty_desired_field_names(desired)
        prev_full = {k: current.get(k) for k in compared}
        next_full = {k: desired.get(k) for k in compared}
        _maybe_log(
            conn,
            jid_log,
            "sf_scrape_fields_skip",
            schema,
            {
                "reason": out["reason"],
                "detail": "Compared desired values from the scrape to Salesforce; all fields already matched, so no write was sent.",
                "sf_job_id": sf_job_id or None,
                "checked": list(field_names),
                "fields_compared": compared,
                "fields_changed": [],
                "prev": prev_full,
                "next": next_full,
            },
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
        _maybe_log(
            conn,
            jid_log,
            "sf_scrape_fields_skip",
            schema,
            {
                "reason": "dry_run",
                "detail": "Dry-run mode: computed Salesforce update body but did not send the API request.",
                "sf_job_id": sf_job_id or None,
                "would_update": sorted(body.keys()),
            },
            run_id=run_id,
        )
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
    compared = _nonempty_desired_field_names(desired)
    prev_full = {k: current.get(k) for k in compared}
    next_full: dict[str, Any] = {}
    for k in compared:
        if k in body:
            next_full[k] = body[k]
        else:
            next_full[k] = current.get(k)
    _maybe_log(
        conn,
        jid_log,
        "sf_scrape_fields_patched",
        schema,
        {
            "sf_job_id": sf_job_id or None,
            "fields_changed": out["fields"],
            "fields_compared": compared,
            "prev": prev_full,
            "next": next_full,
        },
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
    run_id: Optional[int] = None,
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
                payload={
                    "reason": "no_sf_job_id",
                    "job_id": jid,
                    "detail": (
                        "The latest scraped row for this job has no Salesforce Job__c Id yet "
                        "(sf_job_id is empty). Field sync runs only after mapping resolves a Job record. "
                        "This is expected for brand-new jobs until Salesforce and mapping catch up."
                    ),
                },
            )
            continue
        attempted += 1
        r = sync_missing_scrape_fields_to_salesforce(
            dict(row), conn=conn, job_id_for_log=jid, schema=schema, dry_run=dry_run, run_id=run_id
        )
        if r.get("patched"):
            patched += 1
    return (attempted, patched)

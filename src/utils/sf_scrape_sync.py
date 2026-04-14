"""
After a Kimedics scrape, push Job__c fields to Salesforce whenever we have ``sf_job_id``.

By default we compute the desired field set with the same rules as ``prepare_payload_for_write``
(full mapped/updateable fields + canonical description).

Org-specific **test** fields (``test_status__c``, ``test_posted_date__c``) are included only when
``PROXI_SF_TEST_MODE=true``.

Set ``PROXI_SF_UPDATE_JOBS=false`` to skip all Salesforce **writes** from this module (no PATCH;
see also ``utils.sf_write_flags`` for create-job / worksite-create gates).
"""

from __future__ import annotations

import os
from datetime import datetime
from typing import Any, Optional, Union

from utils.sf_job_payload import (
    CANONICAL_JOB_C_PUSH_FIELD_NAMES,
    _canonical_description_use_html,
    _truncate_external_job_id,
    coerce_picklists_to_valid,
    external_job_link_from_job_row,
    merge_job_role_defaults_for_empty_sf_fields,
    prepare_payload_for_write,
)
from utils.sf_job_rest_minimal import (
    DEFAULT_REST_VERSION,
    describe_sobject,
    filter_field_names_to_describe,
    is_salesforce_deleted_entity_error,
    rest_json,
    update_account_record,
    update_job_record,
)
from utils.sf_partial_update import prepare_patch_payload
from utils.salesforce import get_token_auto
from utils.sf_write_flags import proxi_sf_writes_enabled

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


def _scrape_sync_audit_field_names(*, test_mode: bool) -> tuple[str, ...]:
    """
    Every Job__c API name automation may send (canonical push set).

    Used for the Salesforce GET field list and for ``fields_compared`` / ``prev`` / ``next`` in
    ``job_event_log`` so the hub shows a full audit — not only fields present in the PATCH body.
    Role/source picklists are often omitted from ``desired`` when Salesforce already has a value,
    but we still fetch and display them.
    """
    names = set(CANONICAL_JOB_C_PUSH_FIELD_NAMES)
    if test_mode:
        names.add(SF_FIELD_TEST_STATUS)
        names.add(SF_FIELD_TEST_POSTED_DATE)
    return tuple(sorted(names))


def _scrape_sync_prev_next_full(
    compared: tuple[str, ...],
    desired: dict[str, Any],
    current: dict[str, Any],
    body: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Build hub audit dicts: after PATCH prefer ``body``; else automation ``desired``; else SF ``current``."""
    prev_full = {k: current.get(k) for k in compared}
    next_full: dict[str, Any] = {}
    for k in compared:
        if k in body:
            next_full[k] = body[k]
        elif k in desired:
            next_full[k] = desired[k]
        else:
            next_full[k] = current.get(k)
    return prev_full, next_full


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


def _sync_worksite_account_shipping_for_job_formula(
    instance_url: str,
    access_token: str,
    *,
    job_row: dict,
    desired: dict[str, Any],
    current: dict[str, Any],
    dry_run: bool,
    api_version: str = DEFAULT_REST_VERSION,
) -> dict[str, Any]:
    """
    Job__c.Name is a formula using ``Job_Worksite_Location_1__r.ShippingCity/ShippingState``;
    keep those in sync with Job_City__c / Job_State__c when we have a worksite Account Id.
    """
    wid = (
        (job_row.get("sf_worksite_account_id") or "").strip()
        or str(current.get("Job_Worksite_Location_1__c") or "").strip()
        or str(desired.get("Job_Worksite_Location_1__c") or "").strip()
    )
    city = str(desired.get("Job_City__c") or current.get("Job_City__c") or "").strip()
    state = str(desired.get("Job_State__c") or current.get("Job_State__c") or "").strip()
    out: dict[str, Any] = {
        "ok": True,
        "worksite_account_id": wid or None,
        "account_patched": False,
        "account_fields": [],
        "skipped_reason": None,
        "error": None,
    }
    if not wid:
        out["skipped_reason"] = "no_worksite_account_id"
        return out
    if not city and not state:
        out["skipped_reason"] = "no_job_city_state"
        return out
    acc = rest_json(
        instance_url,
        access_token,
        "GET",
        f"sobjects/Account/{wid}?fields=ShippingCity,ShippingState",
        api_version=api_version,
    )
    if not isinstance(acc, dict):
        out["ok"] = False
        out["error"] = "account_get_not_dict"
        return out
    patch_acc: dict[str, str] = {}
    if city and _normalize_sf_compare_value(acc.get("ShippingCity")) != _normalize_sf_compare_value(city):
        patch_acc["ShippingCity"] = city
    if state and _normalize_sf_compare_value(acc.get("ShippingState")) != _normalize_sf_compare_value(state):
        patch_acc["ShippingState"] = state
    if not patch_acc:
        out["skipped_reason"] = "shipping_already_matches"
        return out
    out["account_fields"] = sorted(patch_acc.keys())
    if dry_run:
        out["account_patched"] = True
        out["skipped_reason"] = "dry_run"
        return out
    try:
        update_account_record(instance_url, access_token, wid, patch_acc, api_version=api_version)
        out["account_patched"] = True
    except Exception as e:
        out["ok"] = False
        out["error"] = str(e)[:2000]
    return out


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

    if not proxi_sf_writes_enabled():
        out["reason"] = "sf_writes_disabled"
        out["ok"] = True
        _maybe_log(
            conn,
            jid_log,
            "sf_scrape_fields_skip",
            schema,
            {
                "reason": out["reason"],
                "detail": (
                    "PROXI_SF_UPDATE_JOBS is false (or 0/no/off). No Salesforce API calls "
                    "(including reads for compare) are made from scrape field sync."
                ),
                "sf_job_id": sf_job_id or None,
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
    test_mode = _env_truthy("PROXI_SF_TEST_MODE")
    if test_mode:
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

    audit_field_names = _scrape_sync_audit_field_names(test_mode=test_mode)
    known_audit = filter_field_names_to_describe(describe, audit_field_names)
    field_names = filter_field_names_to_describe(
        describe,
        tuple(sorted(frozenset(desired.keys()) | frozenset(known_audit))),
    )
    current = _get_job_fields(instance_url, access_token, sf_job_id, field_names)
    merge_job_role_defaults_for_empty_sf_fields(desired, current)
    coerce_picklists_to_valid(describe, desired)
    # Job__c.Name is a formula — do not PATCH. Worksite Account Shipping* drives part of that formula.
    shipping_sync = _sync_worksite_account_shipping_for_job_formula(
        instance_url,
        access_token,
        job_row=job_row,
        desired=desired,
        current=current,
        dry_run=dry_run,
    )
    out["worksite_shipping_sync"] = shipping_sync

    patch: dict[str, Any] = {}
    for fname, want in desired.items():
        if want is None or (isinstance(want, str) and not want.strip()):
            continue
        have = current.get(fname)
        if _normalize_sf_compare_value(have) == _normalize_sf_compare_value(want):
            continue
        patch[fname] = want

    if not patch and not shipping_sync.get("account_patched"):
        if not shipping_sync.get("ok"):
            out["reason"] = "worksite_shipping_sync_error"
            _maybe_log(
                conn,
                jid_log,
                "sf_scrape_fields_error",
                schema,
                {
                    "reason": out["reason"],
                    "error": shipping_sync.get("error"),
                    "worksite_shipping_sync": shipping_sync,
                    "sf_job_id": sf_job_id or None,
                },
                run_id=run_id,
            )
            return out
        out["ok"] = True
        out["reason"] = "already_matches_salesforce"
        compared = known_audit
        prev_full, next_full = _scrape_sync_prev_next_full(compared, desired, current, {})
        _maybe_log(
            conn,
            jid_log,
            "sf_scrape_fields_skip",
            schema,
            {
                "reason": out["reason"],
                "detail": "Compared desired values from the scrape to Salesforce; all Job fields already matched and worksite shipping is current.",
                "sf_job_id": sf_job_id or None,
                "checked": list(field_names),
                "fields_compared": list(compared),
                "fields_changed": [],
                "prev": prev_full,
                "next": next_full,
                "worksite_shipping_sync": shipping_sync,
            },
            run_id=run_id,
        )
        return out

    if not patch:
        if dry_run:
            out["ok"] = True
            out["patched"] = True
            out["fields"] = []
            out["dry_run_worksite_shipping"] = shipping_sync
            dr_prev, dr_next = _scrape_sync_prev_next_full(known_audit, desired, current, {})
            _maybe_log(
                conn,
                jid_log,
                "sf_scrape_fields_skip",
                schema,
                {
                    "reason": "dry_run",
                    "detail": "Dry-run: would PATCH worksite Account ShippingCity/ShippingState only (Job fields already matched).",
                    "sf_job_id": sf_job_id or None,
                    "worksite_shipping_sync": shipping_sync,
                    "checked": list(field_names),
                    "fields_compared": list(known_audit),
                    "fields_changed": [],
                    "prev": dr_prev,
                    "next": dr_next,
                },
                run_id=run_id,
            )
            return out
        if not shipping_sync.get("ok"):
            out["reason"] = "worksite_shipping_sync_error"
            out["error"] = shipping_sync.get("error")
            _maybe_log(
                conn,
                jid_log,
                "sf_scrape_fields_error",
                schema,
                {"reason": out["reason"], "worksite_shipping_sync": shipping_sync},
                run_id=run_id,
            )
            return out
        out["ok"] = True
        out["patched"] = True
        out["fields"] = []
        sh_prev, sh_next = _scrape_sync_prev_next_full(known_audit, desired, current, {})
        _maybe_log(
            conn,
            jid_log,
            "sf_scrape_fields_patched",
            schema,
            {
                "sf_job_id": sf_job_id or None,
                "fields_changed": [],
                "worksite_account_shipping_updated": shipping_sync.get("account_fields"),
                "detail": "Job fields already matched; updated worksite Account ShippingCity/ShippingState for Job Name formula.",
                "worksite_shipping_sync": shipping_sync,
                "checked": list(field_names),
                "fields_compared": list(known_audit),
                "prev": sh_prev,
                "next": sh_next,
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
        dr2_prev, dr2_next = _scrape_sync_prev_next_full(known_audit, desired, current, body)
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
                "checked": list(field_names),
                "fields_compared": list(known_audit),
                "fields_changed": sorted(body.keys()),
                "prev": dr2_prev,
                "next": dr2_next,
            },
            run_id=run_id,
        )
        return out

    try:
        update_job_record(instance_url, access_token, job_object_name, sf_job_id, body)
    except Exception as e:
        body_retry = {
            k: v for k, v in body.items() if k != "Job_Worksite_Location_1__c"
        }
        if (
            is_salesforce_deleted_entity_error(e)
            and "Job_Worksite_Location_1__c" in body
            and body_retry
        ):
            try:
                update_job_record(
                    instance_url, access_token, job_object_name, sf_job_id, body_retry
                )
                body = body_retry
            except Exception as e2:
                out["error"] = str(e2)[:2000]
                _maybe_log(
                    conn,
                    jid_log,
                    "sf_scrape_fields_error",
                    schema,
                    {
                        "error": out["error"],
                        "attempt": body_retry,
                        "prior_error": str(e)[:1500],
                        "note": "Retry without Job_Worksite_Location_1__c also failed.",
                    },
                    run_id=run_id,
                )
                return out
            compared = known_audit
            prev_full, next_full = _scrape_sync_prev_next_full(compared, desired, current, body)
            _maybe_log(
                conn,
                jid_log,
                "sf_scrape_fields_patched",
                schema,
                {
                    "sf_job_id": sf_job_id or None,
                    "fields_changed": sorted(body.keys()),
                    "fields_compared": list(compared),
                    "prev": prev_full,
                    "next": next_full,
                    "retried_without_worksite_lookup": True,
                    "detail": (
                        "First PATCH failed (entity deleted on worksite Id); applied remaining "
                        "fields without Job_Worksite_Location_1__c. Clear stale sf_worksite_account_id "
                        "in Supabase / sf_worksite_location_map and fix the Account in Salesforce."
                    ),
                },
                run_id=run_id,
            )
            out["ok"] = True
            out["patched"] = True
            out["fields"] = sorted(body.keys())
            return out

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
    compared = known_audit
    prev_full, next_full = _scrape_sync_prev_next_full(compared, desired, current, body)
    _maybe_log(
        conn,
        jid_log,
        "sf_scrape_fields_patched",
        schema,
        {
            "sf_job_id": sf_job_id or None,
            "fields_changed": out["fields"],
            "fields_compared": list(compared),
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
        if not proxi_sf_writes_enabled():
            log_job_event(
                conn,
                job_id=jid,
                event_type="sf_scrape_fields_skip",
                run_id=run_id,
                schema=schema,
                payload={
                    "reason": "sf_writes_disabled",
                    "job_id": jid,
                    "sf_job_id": (row.get("sf_job_id") or "").strip() or None,
                    "detail": "PROXI_SF_UPDATE_JOBS is false; scrape sync does not call Salesforce.",
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

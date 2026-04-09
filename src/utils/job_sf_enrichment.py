"""
Fill Salesforce-oriented columns on a parsed job row using Supabase lookup tables.

Worksite Account Id:
- Prefer ``sf_worksite_location_map`` (city + state).
- Optionally create a new Salesforce Account + map row when ``PROXI_SF_CREATE_WORKSITES=true``.

``sf_job_id`` still comes from practice / external-id matching or the create-job path in
``sf_job_supabase_resolve``.
"""

from __future__ import annotations

from typing import Any, Optional

from utils.sf_push_defaults import (
    SF_REFERENCE_KEY_PRIMARY,
    format_worksite_display_label,
)


def enrich_cleaned_row_salesforce_fields(
    conn,
    cleaned: dict,
    *,
    schema: str = "public",
    cache: Optional[dict[str, Any]] = None,
) -> None:
    """
    Mutates ``cleaned`` in place: sets sf_primary_account_id, sf_worksite_account_id,
    sf_worksite_display_label when missing and lookups succeed.

    ``cache`` is optional; reused across rows in one batch (keys: primary_id, worksite_default).
    """
    if conn is None or not cleaned:
        return
    local: dict[str, Any] = {} if cache is None else cache

    try:
        from utils.supabase_db import fetch_sf_reference_account_id
    except Exception:
        return

    try:
        if "primary_id" not in local:
            local["primary_id"] = fetch_sf_reference_account_id(
                conn, SF_REFERENCE_KEY_PRIMARY, schema=schema
            )
    except Exception:
        return

    city = (cleaned.get("city") or "").strip()
    state = (cleaned.get("state") or "").strip()

    if not (cleaned.get("sf_worksite_display_label") or "").strip():
        label = format_worksite_display_label(city, state)
        if label:
            cleaned["sf_worksite_display_label"] = label

    if not (cleaned.get("sf_primary_account_id") or "").strip() and local.get("primary_id"):
        cleaned["sf_primary_account_id"] = local["primary_id"]

    # Worksite Account Id: location map + optional Salesforce Account create
    if not (cleaned.get("sf_worksite_account_id") or "").strip() and city and state:
        try:
            from utils.supabase_db import fetch_worksite_account_id_for_location
            from utils.sf_worksite_create import fetch_or_create_worksite_account_id
            from utils.sf_scrape_sync import _rest_token_from_env
        except Exception:
            return
        wid = fetch_worksite_account_id_for_location(conn, city, state, schema=schema)
        if not wid:
            tok = _rest_token_from_env()
            if tok:
                iu, at = tok
                jid_log = (cleaned.get("job_id") or "").strip() or None
                wid = fetch_or_create_worksite_account_id(
                    conn,
                    city,
                    state,
                    instance_url=iu,
                    access_token=at,
                    schema=schema,
                    run_id=None,
                    job_id_for_log=jid_log,
                    skip_location_lookup=True,
                )
        if wid:
            cleaned["sf_worksite_account_id"] = wid

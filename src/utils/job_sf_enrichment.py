"""
Fill Salesforce-oriented columns on a parsed job row using Supabase lookup tables.

Important:
- We DO NOT guess or default a worksite location id.
- Worksite + Salesforce Job id resolution happens via practice mapping logic elsewhere.
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

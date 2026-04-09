"""
Create Salesforce Account (worksite) records when no location mapping exists, and persist mapping in Supabase.

Gated by env ``PROXI_SF_CREATE_WORKSITES=true``.
"""

from __future__ import annotations

import json
import os
from typing import Any, Optional

from utils.sf_job_rest_minimal import create_account_record, filter_createable_fields, describe_sobject
from utils.sf_push_defaults import format_worksite_display_label


def _env_truthy(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes")


def _account_extra_fields() -> dict[str, Any]:
    raw = (os.environ.get("PROXI_SF_ACCOUNT_CREATE_EXTRA_JSON") or "").strip()
    if not raw:
        return {}
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return obj if isinstance(obj, dict) else {}


def fetch_or_create_worksite_account_id(
    conn,
    city: str,
    state: str,
    *,
    instance_url: str,
    access_token: str,
    schema: str = "public",
    run_id: Optional[int] = None,
    job_id_for_log: Optional[str] = None,
    skip_location_lookup: bool = False,
) -> Optional[str]:
    """
    Return Account Id for (city, state): from ``sf_worksite_location_map``, or create in Salesforce + upsert map.

    Requires non-empty city+state for a stable location_key. Returns None if creation disabled or fails.
    """
    from utils.supabase_db import (
        fetch_worksite_account_id_for_location,
        upsert_worksite_account_id_for_location,
        log_job_event,
    )

    c = (city or "").strip()
    st = (state or "").strip()
    if not c or not st:
        return None

    if not skip_location_lookup:
        existing = fetch_worksite_account_id_for_location(conn, c, st, schema=schema)
        if existing:
            return existing

    if not _env_truthy("PROXI_SF_CREATE_WORKSITES"):
        return None

    display = format_worksite_display_label(c, st)
    body: dict[str, Any] = {"Name": display}
    body.update(_account_extra_fields())

    try:
        describe = describe_sobject(instance_url, access_token, "Account")
        fields = filter_createable_fields(describe, body)
        if not fields.get("Name"):
            fields["Name"] = display
        resp = create_account_record(instance_url, access_token, fields)
        new_id = (resp.get("id") or "").strip()
        if not new_id:
            raise RuntimeError(f"Account create returned no id: {resp!r}")
        upsert_worksite_account_id_for_location(
            conn,
            c,
            st,
            salesforce_account_id=new_id,
            display_label=display,
            source="sf_account_create",
            schema=schema,
        )
        if job_id_for_log:
            log_job_event(
                conn,
                job_id=job_id_for_log,
                event_type="worksite_created",
                run_id=run_id,
                schema=schema,
                payload={
                    "city": c,
                    "state": st,
                    "salesforce_account_id": new_id,
                    "display_label": display,
                },
            )
        return new_id
    except Exception as e:
        if job_id_for_log:
            try:
                log_job_event(
                    conn,
                    job_id=job_id_for_log,
                    event_type="worksite_create_failed",
                    run_id=run_id,
                    schema=schema,
                    payload={"error": str(e)[:2000], "city": c, "state": st},
                )
            except Exception:
                pass
        return None

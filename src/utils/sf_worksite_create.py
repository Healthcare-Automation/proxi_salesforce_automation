"""
Create Salesforce Account (worksite) records when no location mapping exists, and persist mapping in Supabase.

New worksite **Account** POST includes:

- ``Name`` — ``Aspen Dental - {City}, {ST}`` (2-letter state), see ``format_worksite_account_name``
- ``ShippingStreet`` — Kimedics ``address_line`` (normalized); trailing ``City, ST`` matching
  structured city/state is removed so it does not duplicate ``ShippingCity``/``ShippingState``.
- ``ParentId`` — Aspen Dental Management (``SF_ACCOUNT_ASPEN_DENTAL_MANAGEMENT_ID``)
- ``RecordTypeId`` — env ``PROXI_SF_ACCOUNT_WORKSITE_RECORD_TYPE_ID``, else describe ``Worksite`` record type

Job rows receive the new Id on ``Job_Worksite_Location_1__c`` via resolver / enrichment; ``Job_Worksite_1_Address__c``
is set from the same ``address_line`` in ``sf_job_payload``.

Gated by env ``PROXI_SF_CREATE_WORKSITES=true``. Salesforce Account **POST** is also skipped when
``PROXI_SF_UPDATE_JOBS=false`` (see ``utils.sf_write_flags``).
"""

from __future__ import annotations

import json
import os
from typing import Any, Optional

from utils.address_display_format import (
    format_us_address_line_for_display,
    strip_redundant_city_state_from_shipping_street,
)
from utils.sf_job_rest_minimal import create_account_record, filter_createable_fields, describe_sobject
from utils.sf_push_defaults import (
    SF_ACCOUNT_ASPEN_DENTAL_MANAGEMENT_ID,
    format_worksite_account_name,
    worksite_account_record_type_id,
)
from utils.sf_write_flags import proxi_sf_writes_enabled


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
    address_line: Optional[str] = None,
    schema: str = "public",
    run_id: Optional[int] = None,
    job_id_for_log: Optional[str] = None,
    skip_location_lookup: bool = False,
) -> Optional[str]:
    """
    Return Account Id for (city, state): from ``sf_worksite_location_map``, or create in Salesforce + upsert map.

    Requires non-empty city+state for a stable location_key. Returns None if creation disabled or fails.

    **New Account body:** ``Name``, ``ShippingStreet`` (from ``address_line``), ``ParentId``, ``RecordTypeId``,
    plus optional ``PROXI_SF_ACCOUNT_CREATE_EXTRA_JSON``. Only **createable** fields are sent (describe filter).
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

    if not proxi_sf_writes_enabled():
        return None

    if not _env_truthy("PROXI_SF_CREATE_WORKSITES"):
        return None

    display = format_worksite_account_name(c, st)
    raw_ship = (address_line or "").strip()
    ship_street = format_us_address_line_for_display(raw_ship) if raw_ship else None
    if ship_street == "":
        ship_street = None
    if ship_street:
        dedup = strip_redundant_city_state_from_shipping_street(ship_street, city=c, state=st)
        if dedup:
            ship_street = dedup
    if ship_street == "":
        ship_street = None

    body: dict[str, Any] = {
        "Name": display,
        "ParentId": SF_ACCOUNT_ASPEN_DENTAL_MANAGEMENT_ID,
    }
    if ship_street:
        body["ShippingStreet"] = ship_street

    body.update(_account_extra_fields())

    try:
        describe = describe_sobject(instance_url, access_token, "Account")
        rt_id = worksite_account_record_type_id(describe)
        if rt_id:
            body["RecordTypeId"] = rt_id

        fields = filter_createable_fields(describe, body)
        if not fields.get("Name"):
            fields["Name"] = display
        # ParentId / RecordTypeId may be stripped if not createable for the integration user — still attempt create.
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
                    "shipping_street_sent": bool(ship_street),
                    "record_type_id_sent": bool(rt_id and fields.get("RecordTypeId")),
                    "parent_id_sent": bool(fields.get("ParentId")),
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

"""
Partial updates to any Salesforce sObject by Id (Lever 1 style).

Use for ad-hoc field patches: supply API field names and values; describe + updateable
filter + optional picklist coercion match ``update_salesforce_job.py`` behavior.
"""

from __future__ import annotations

import json
from typing import Any

from utils.sf_job_payload import coerce_picklists_to_valid
from utils.sf_job_rest_minimal import (
    describe_sobject,
    filter_updateable_fields,
    update_job_record,
)


def prepare_patch_payload(
    describe: dict,
    fields: dict[str, Any],
    *,
    coerce_picklists: bool = True,
) -> dict[str, Any]:
    """
    Copy ``fields``, optionally coerce picklists against ``describe``, then keep only
    updateable non-empty values.
    """
    body = dict(fields)
    if coerce_picklists:
        coerce_picklists_to_valid(describe, body)
    return filter_updateable_fields(describe, body)


def patch_sobject_fields(
    instance_url: str,
    access_token: str,
    sobject_name: str,
    record_id: str,
    fields: dict[str, Any],
    *,
    coerce_picklists: bool = True,
    dry_run: bool = False,
    api_version: str | None = None,
) -> dict[str, Any]:
    """
    PATCH ``record_id`` on ``sobject_name`` with ``fields``.

    Returns the payload actually sent (after describe filtering). If ``dry_run`` is True,
    does not call the API.
    """
    ver_kw: dict[str, str] = {}
    if api_version is not None:
        ver_kw["api_version"] = api_version
    describe = describe_sobject(instance_url, access_token, sobject_name, **ver_kw)
    body = prepare_patch_payload(describe, fields, coerce_picklists=coerce_picklists)
    if dry_run:
        return body
    update_job_record(
        instance_url,
        access_token,
        sobject_name,
        record_id,
        body,
        **ver_kw,
    )
    return body


def format_patch_preview(body: dict[str, Any]) -> str:
    return json.dumps(body, indent=2, default=str)

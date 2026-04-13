"""
Automation Salesforce write switch.

When ``PROXI_SF_UPDATE_JOBS`` is false, the scrape pipeline must not call Salesforce
POST/PATCH (field sync, Job__c create, Account create). Reads (e.g. job pull for mapping)
still run unless you disable credentials separately.
"""

from __future__ import annotations

import os


def proxi_sf_writes_enabled() -> bool:
    """
    Return False when ``PROXI_SF_UPDATE_JOBS`` is ``false`` / ``0`` / ``no`` / ``off`` (any casing).

    Default when unset: **True** (preserve existing behavior).
    """
    raw = (os.environ.get("PROXI_SF_UPDATE_JOBS") or "true").strip().lower()
    return raw not in ("0", "false", "no", "off")

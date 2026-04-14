#!/usr/bin/env python3
"""
Deprecated: Job__c ``Name`` is a **formula field** in this org (worksite shipping city/state,
specialty, account, status). It cannot be PATCHed; automation updates the underlying fields
instead (see ``utils.sf_scrape_sync._sync_worksite_account_shipping_for_job_formula``).

This script is kept as a no-op entry point so old runbooks fail gracefully.
"""

from __future__ import annotations

import sys


def main() -> None:
    print(
        "Job__c Name is a formula — not writable via API. "
        "Update Job_City__c/Job_State__c, worksite ShippingCity/ShippingState, "
        "Job_Specialty__c, Job_Account__c, Job_Status__c instead.",
        file=sys.stderr,
    )
    raise SystemExit(2)


if __name__ == "__main__":
    main()

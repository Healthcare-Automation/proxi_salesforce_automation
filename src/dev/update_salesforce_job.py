#!/usr/bin/env python3
"""
Lever 1 — Update an existing Job__c from Supabase ``job_current`` (or a JSON row).

Maps the same field rules as ``utils.sf_job_payload`` (push-time defaults, state names,
salary inference, optional canonical description). Uses PATCH against a known Salesforce Id.

Auth: same as ``pull_salesforce_jobs.py`` / ``create_test_job_salesforce.py``
(``utils.salesforce.get_token_auto``).

Examples (repo root)::

  # Preview payload for a Kimedics job_id in Supabase (no write)
  python src/dev/update_salesforce_job.py \\
    --sf-id a01UP00000cwiFhYAI \\
    --from-supabase-job-id 19440 \\
    --dry-run

  # Apply update
  python src/dev/update_salesforce_job.py \\
    --sf-id a01UP00000cwiFhYAI \\
    --from-supabase-job-id 19440 \\
    --yes

  # Raw row JSON (file is a single JSON object with job_current-shaped keys)
  python src/dev/update_salesforce_job.py --sf-id a01UP00000cwiFhYAI --from-json ./row.json --dry-run

Environment: ``SALESFORCE_*`` and ``DB_PASSWORD`` / connection same as other scripts.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parent.parent
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from dotenv import load_dotenv

_env_path = _SRC.parent / ".env"
load_dotenv(_env_path)

from utils.salesforce import SalesforceLoginError, get_token_auto
from utils.sf_job_payload import (
    SF_PUSH_JOB_ROLE_DEFAULTS,
    build_salesforce_job_name,
    coerce_picklists_to_valid,
    merge_job_role_defaults_for_empty_sf_fields,
    prepare_payload_for_write,
)
from utils.sf_job_rest_minimal import (
    DEFAULT_REST_VERSION,
    describe_sobject,
    rest_json,
    update_job_record,
)
from utils.supabase_db import load_job_current_row_for_salesforce


def _authenticate(*, debug: bool) -> tuple[str, str]:
    consumer_key = os.environ.get("SALESFORCE_CONSUMER_KEY", "")
    consumer_secret = os.environ.get("SALESFORCE_CONSUMER_SECRET", "")
    if not consumer_key or not consumer_secret:
        print("Missing SALESFORCE_CONSUMER_KEY or SALESFORCE_CONSUMER_SECRET", file=sys.stderr)
        sys.exit(1)
    use_client_credentials = os.environ.get("SALESFORCE_USE_USERNAME_PASSWORD", "").lower() not in (
        "1",
        "true",
        "yes",
    )
    token_url = os.environ.get("SALESFORCE_TOKEN_URL") or "https://proxi.my.salesforce.com"
    username = os.environ.get("SALESFORCE_USERNAME", "")
    password = os.environ.get("SALESFORCE_PASSWORD", "")
    security_token = os.environ.get("SALESFORCE_SECURITY_TOKEN") or None
    use_sandbox = os.environ.get("SALESFORCE_USE_SANDBOX", "").lower() in ("1", "true", "yes")
    try:
        token = get_token_auto(
            consumer_key,
            consumer_secret,
            username or None,
            password or None,
            use_client_credentials=use_client_credentials,
            security_token=security_token,
            use_sandbox=use_sandbox,
            token_url=token_url,
        )
    except SalesforceLoginError as e:
        print(str(e), file=sys.stderr)
        raise SystemExit(1) from e
    if debug:
        print("DEBUG: token ok", file=sys.stderr)
    return token["instance_url"], token["access_token"]


def _load_row_from_supabase(job_id: str, *, schema: str) -> dict | None:
    try:
        return load_job_current_row_for_salesforce(job_id.strip(), schema=schema)
    except RuntimeError as e:
        print(str(e), file=sys.stderr)
        return None
    except ValueError as e:
        print(str(e), file=sys.stderr)
        return None


def main() -> None:
    p = argparse.ArgumentParser(description="PATCH Job__c from job_current-shaped data.")
    p.add_argument("--sf-id", required=True, help="Salesforce Job__c Id (18 or 15 char).")
    p.add_argument("--from-supabase-job-id", help="Kimedics job_id key in job_current.")
    p.add_argument("--from-json", type=Path, help="Path to JSON object (job_current-shaped).")
    p.add_argument("--schema", default="public", help="Postgres schema for job_current.")
    p.add_argument(
        "--raw-description",
        action="store_true",
        help="Use description_full_text only for Job_Client_Job_Description__c (no Proxi template).",
    )
    p.add_argument("--dry-run", action="store_true", help="Print JSON payload and exit.")
    p.add_argument("--verbose", "-v", action="store_true")
    p.add_argument(
        "--yes",
        action="store_true",
        help="Required to perform PATCH (otherwise dry-run only).",
    )
    args = p.parse_args()

    if bool(args.from_supabase_job_id) == bool(args.from_json):
        p.error("Provide exactly one of --from-supabase-job-id or --from-json")

    row: dict | None = None
    if args.from_supabase_job_id:
        row = _load_row_from_supabase(args.from_supabase_job_id, schema=args.schema)
    else:
        raw = args.from_json.read_text(encoding="utf-8")
        row = json.loads(raw)
        if not isinstance(row, dict):
            p.error("--from-json must contain a JSON object")

    if not row:
        raise SystemExit(1)

    job_object = os.environ.get("SALESFORCE_JOB_OBJECT", "Job__c").strip()
    debug = args.verbose or os.environ.get("SALESFORCE_DEBUG", "").lower() in ("1", "true", "yes")
    instance_url, access_token = _authenticate(debug=debug)
    describe = describe_sobject(instance_url, access_token, job_object)

    body = prepare_payload_for_write(
        row,
        describe,
        use_canonical_description=not args.raw_description,
        for_update=True,
    )

    sf_id = args.sf_id.strip()
    role_field_list = ",".join(SF_PUSH_JOB_ROLE_DEFAULTS.keys())
    name_ctx_fields = "Job_City__c,Job_State__c"
    cur = rest_json(
        instance_url,
        access_token,
        "GET",
        f"sobjects/{job_object}/{sf_id}?fields={role_field_list},{name_ctx_fields}",
        api_version=DEFAULT_REST_VERSION,
    )
    if isinstance(cur, dict):
        merge_job_role_defaults_for_empty_sf_fields(body, cur)
        body["Name"] = build_salesforce_job_name(
            row,
            job_name_location_fallback={
                "Job_City__c": cur.get("Job_City__c"),
                "Job_State__c": cur.get("Job_State__c"),
            },
        )
        coerce_picklists_to_valid(describe, body)

    print(json.dumps(body, indent=2, default=str))

    if args.dry_run or not args.yes:
        if not args.yes and not args.dry_run:
            print("\nNo --yes: dry-run only. Add --yes to PATCH.", file=sys.stderr)
        return

    update_job_record(instance_url, access_token, job_object, sf_id, body)
    print(f"\nPATCH sent for {job_object} Id {sf_id}.")


if __name__ == "__main__":
    main()

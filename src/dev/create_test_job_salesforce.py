#!/usr/bin/env python3
"""
Lever 2 — Create new Job__c (testing / integration).

Creates exactly **one** obvious test row when passed ``--yes``. Use ``--auth-check`` to verify
OAuth only. For production creates later, reuse ``utils.sf_job_payload.prepare_payload_for_write``
(..., ``for_update=False``) + ``sf_job_rest_minimal.create_job_record`` with real ``job_current``
data (no test banner).

Auth uses ``utils.salesforce.get_token_auto`` with the **same env vars as**
``pull_salesforce_jobs.py`` (OAuth username-password **and** SOAP Partner fallback when
OAuth returns ``authentication failure`` — the minimal REST-only OAuth path cannot do that).

REST create/describe still use ``utils.sf_job_rest_minimal`` (Bearer token only).

Required env:
  SALESFORCE_CONSUMER_KEY, SALESFORCE_CONSUMER_SECRET

Optional (match pull_salesforce_jobs.py):
  SALESFORCE_USERNAME, SALESFORCE_PASSWORD, SALESFORCE_SECURITY_TOKEN
  SALESFORCE_USE_SANDBOX, SALESFORCE_TOKEN_URL
  SALESFORCE_USE_USERNAME_PASSWORD=true  (force username-password instead of client credentials)
  SALESFORCE_JOB_OBJECT=Job__c

If username/password are omitted, client-credentials flow is used (needs ECA "Run As" etc.).

Safety: you must pass --yes on the command line so this is never run by mistake.

Run (repo root):
  python src/dev/create_test_job_salesforce.py --yes
  python src/dev/create_test_job_salesforce.py --auth-check   # login only, no write
  python src/dev/create_test_job_salesforce.py --yes --verbose
  SALESFORCE_DEBUG=1 python src/dev/create_test_job_salesforce.py --yes
"""

from __future__ import annotations

import argparse
import os
import secrets
import sys
from datetime import datetime, timezone
from pathlib import Path

_SRC = Path(__file__).resolve().parent.parent
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from dotenv import load_dotenv

from utils.salesforce import SalesforceLoginError, get_token_auto
from utils.sf_job_payload import EXTERNAL_JOB_ID_MAX_LEN, prepare_payload_for_write
from utils.sf_job_rest_minimal import create_job_record, describe_sobject

_env_path = _SRC.parent / ".env"
load_dotenv(_env_path)

# Same default as pull_salesforce_jobs.py (used when SALESFORCE_TOKEN_URL is unset).
DEFAULT_TOKEN_URL = "https://proxi.my.salesforce.com"

TEST_BANNER = "[AUTOMATION TEST ROW — SAFE TO DELETE — NOT PRODUCTION DATA]"

def _test_external_job_id() -> str:
    """Unique value, len 20: TEST (4) + 16 hex (matches External_Job_ID__c max in org)."""
    return f"TEST{secrets.token_hex(8).upper()}"


def _debug_enabled(verbose_cli: bool) -> bool:
    return verbose_cli or os.environ.get("SALESFORCE_DEBUG", "").lower() in ("1", "true", "yes")


def _mask_username(username: str) -> str:
    u = (username or "").strip()
    if not u:
        return "(empty)"
    if "@" in u:
        local, _, domain = u.partition("@")
        if not local:
            return f"***@{domain}"
        return f"{local[0]}***@{domain}" if len(local) > 1 else f"***@{domain}"
    return f"{u[:2]}***" if len(u) > 2 else "***"


def _print_oauth_diagnostics(debug: bool) -> None:
    """Never prints passwords, secrets, or tokens — only shapes and env presence."""
    if not debug:
        return
    print("\n--- SALESFORCE_DEBUG: OAuth preflight (no secrets printed) ---", file=sys.stderr)
    user = (os.environ.get("SALESFORCE_USERNAME") or "").strip()
    pw_raw = os.environ.get("SALESFORCE_PASSWORD") or ""
    st_raw = os.environ.get("SALESFORCE_SECURITY_TOKEN") or ""
    print(f"  SALESFORCE_USERNAME (masked): {_mask_username(user)}", file=sys.stderr)
    print(f"  SALESFORCE_PASSWORD: length={len(pw_raw)} (raw .env, before strip)", file=sys.stderr)
    print(
        f"  SALESFORCE_SECURITY_TOKEN: {'set' if st_raw.strip() else 'NOT SET'} "
        f"(raw length={len(st_raw)})",
        file=sys.stderr,
    )
    if pw_raw != pw_raw.strip() or (st_raw and st_raw != st_raw.strip()):
        print(
            "  WARNING: leading/trailing whitespace on password/token in .env can break login "
            "(pull_salesforce_jobs passes values as-is).",
            file=sys.stderr,
        )
    ck = os.environ.get("SALESFORCE_CONSUMER_KEY") or ""
    cs = os.environ.get("SALESFORCE_CONSUMER_SECRET") or ""
    print(f"  SALESFORCE_CONSUMER_KEY length: {len(ck)}", file=sys.stderr)
    print(f"  SALESFORCE_CONSUMER_SECRET length: {len(cs)}", file=sys.stderr)
    use_cc = os.environ.get("SALESFORCE_USE_USERNAME_PASSWORD", "").lower() not in ("1", "true", "yes")
    print(f"  use_client_credentials (like pull): {use_cc}", file=sys.stderr)
    print(f"  SALESFORCE_USE_SANDBOX (raw): {repr(os.environ.get('SALESFORCE_USE_SANDBOX'))}", file=sys.stderr)
    print(
        f"  SALESFORCE_TOKEN_URL: {repr(os.environ.get('SALESFORCE_TOKEN_URL') or DEFAULT_TOKEN_URL)}",
        file=sys.stderr,
    )
    print("  Auth path: utils.salesforce.get_token_auto (OAuth + SOAP fallback)", file=sys.stderr)
    print("--- end preflight ---\n", file=sys.stderr)


def build_single_test_row(*, stamp: str) -> dict:
    """One dict only — unmistakably a script-generated test row."""
    view_link = "https://example.com/salesforce-test-job-placeholder"
    external_job_id = _test_external_job_id()
    desc = f"""{TEST_BANNER}

Created by: create_test_job_salesforce.py
Run stamp: {stamp}
External_Job_ID__c: {external_job_id}

Synthetic job body for API verification. Delete this record when done."""

    return {
        "job_id": external_job_id,
        "title_line": f"{TEST_BANNER} (#{external_job_id})",
        "location_line": "Testville, TS",
        "practice_value": "9999 - Test Practice (script)",
        "city": "Testville",
        "state": "Texas",
        "address_line": "1 Test Lane, Testville, Texas",
        "job_title": f"#{external_job_id}: Automation test job (stamp {stamp})",
        "posting_org": "TEST — DO NOT USE",
        "priority": "Normal",
        "job_ranking": "B",
        # May be replaced by picklist coercion using describe (restricted picklists).
        "status": "Active, not accepting new providers",
        "point_of_contact": "Test Contact (script)",
        "provider_start_date": "12/31/99",
        "provider_end_date": "12/31/99",
        "view_job_link": view_link,
        "insight": f"{TEST_BANNER}\nSynthetic insight line.",
        "dates_needed": "N/A (test)",
        "standard_schedule": "N/A",
        "types_of_cases": "N/A — test row",
        "support_staff": None,
        "sf_primary_account_id": "0015f00000HH63kAAD",
        "sf_worksite_account_id": "001UP00000HKOXRYA5",
        "sf_worksite_display_label": "TEST — Aspen Dental - Testville, TS",
        "description_full_text": desc,
        "last_job_content_id": -1,
        "updated_at": datetime.now(timezone.utc),
    }


def authenticate(*, debug: bool) -> tuple[str, str]:
    consumer_key = os.environ.get("SALESFORCE_CONSUMER_KEY", "")
    consumer_secret = os.environ.get("SALESFORCE_CONSUMER_SECRET", "")
    if not consumer_key or not consumer_secret:
        print("Missing SALESFORCE_CONSUMER_KEY or SALESFORCE_CONSUMER_SECRET", file=sys.stderr)
        sys.exit(1)

    _print_oauth_diagnostics(debug)

    use_client_credentials = os.environ.get("SALESFORCE_USE_USERNAME_PASSWORD", "").lower() not in (
        "1",
        "true",
        "yes",
    )
    token_url = os.environ.get("SALESFORCE_TOKEN_URL") or DEFAULT_TOKEN_URL
    username = os.environ.get("SALESFORCE_USERNAME", "")
    password = os.environ.get("SALESFORCE_PASSWORD", "")
    security_token = os.environ.get("SALESFORCE_SECURITY_TOKEN") or None
    use_sandbox = os.environ.get("SALESFORCE_USE_SANDBOX", "").lower() in ("1", "true", "yes")

    if debug:
        print(
            "DEBUG: calling get_token_auto (same as pull_salesforce_jobs.pull_all_jobs)",
            file=sys.stderr,
        )

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
        print(
            "\nTip: if `python src/local/pull_salesforce_jobs.py` works, copy its exact "
            "SALESFORCE_* values; this script now uses the same get_token_auto path.\n",
            file=sys.stderr,
        )
        raise SystemExit(1) from e

    if debug:
        iu = token.get("instance_url") or ""
        print(
            f"DEBUG: token ok — instance_url host: {iu.split('//', 1)[-1].split('/')[0] if iu else '(none)'}",
            file=sys.stderr,
        )
        print(f"DEBUG: access_token length: {len(token.get('access_token') or '')}", file=sys.stderr)

    return token["instance_url"], token["access_token"]


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Create exactly one obvious test Job__c in Salesforce.",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Required for write. Confirms you intend to create one test Job__c.",
    )
    parser.add_argument(
        "--auth-check",
        action="store_true",
        help="Only obtain an OAuth token and exit (no Job__c create).",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Print safe OAuth debug on stderr (or set SALESFORCE_DEBUG=1).",
    )
    args = parser.parse_args()
    if not args.yes and not args.auth_check:
        parser.error("use --yes to create a test job, or --auth-check to test login only")

    debug = _debug_enabled(args.verbose)
    job_object = os.environ.get("SALESFORCE_JOB_OBJECT", "Job__c")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%SZ")

    print("=" * 72)
    if args.auth_check:
        print(" SALESFORCE AUTH CHECK ONLY (no write)")
    else:
        print(" SALESFORCE TEST JOB CREATE — SINGLE ROW ONLY")
    print("=" * 72)
    print(f"  Object: {job_object}")
    if not args.auth_check:
        print(f"  Run stamp (description text only): {stamp}")
        print(f"  Banner on all human-readable fields: {TEST_BANNER[:50]}...")
        print(
            f"  External_Job_ID__c: will be {EXTERNAL_JOB_ID_MAX_LEN} chars "
            f"(e.g. TEST + 16 hex; field max length in org)"
        )
    print("  Auth: utils.salesforce.get_token_auto (same as pull_salesforce_jobs.py)")
    print(f"  Effective SALESFORCE_TOKEN_URL: {os.environ.get('SALESFORCE_TOKEN_URL') or DEFAULT_TOKEN_URL}")
    if debug:
        print("  Verbose: ON (--verbose or SALESFORCE_DEBUG=1) — details on stderr")
    print("=" * 72)

    instance_url, access_token = authenticate(debug=debug)

    if args.auth_check:
        print("\nAuth OK.")
        print("  instance_url:", instance_url)
        print("  access_token length:", len(access_token))
        return

    # Exactly one row dict, one POST.
    test_row = build_single_test_row(stamp=stamp)
    print(f"\n  Using External_Job_ID__c = {test_row['job_id']!r} (len {len(test_row['job_id'])})")

    describe = describe_sobject(instance_url, access_token, job_object)
    body = prepare_payload_for_write(
        test_row,
        describe,
        use_canonical_description=False,
        for_update=False,
    )

    print("\nFields sent (after describe filter):", list(body.keys()))
    result = create_job_record(instance_url, access_token, job_object, body)
    new_id = result.get("id")
    print("\nDone. Created Job__c Id:", new_id)
    if not new_id:
        print("Full response:", result)


if __name__ == "__main__":
    main()

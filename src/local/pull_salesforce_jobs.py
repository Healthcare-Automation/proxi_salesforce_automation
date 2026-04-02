#!/usr/bin/env python3
"""
Pull all jobs from Salesforce (READ-ONLY). Uses utils.salesforce for auth and query.
Saves to data/salesforce_jobs.csv.

Uses Client Credentials flow (External Client App / ECA) by default.

If you get "no valid scopes defined": In Salesforce go to Setup → External Client App Manager
→ Edit your app → OAuth section → add scopes e.g. "Manage user data via APIs (api)",
"Perform requests at any time (refresh_token, offline_access)" → Save.

Required env (in .env or environment):
  SALESFORCE_CONSUMER_KEY
  SALESFORCE_CONSUMER_SECRET

Optional:
  SALESFORCE_TOKEN_URL  (default: https://proxi.my.salesforce.com)
  SALESFORCE_USE_SANDBOX=true  (use test.salesforce.com)
  SALESFORCE_JOB_OBJECT=Job__c  (your job sobject name)

For username-password flow (classic Connected App) instead:
  SALESFORCE_USE_USERNAME_PASSWORD=true
  SALESFORCE_USERNAME=...
  SALESFORCE_PASSWORD=...
  SALESFORCE_SECURITY_TOKEN=... (if required)

Run (from repo root):
  python src/local/pull_salesforce_jobs.py
"""

import csv
import os
import sys
import time
from pathlib import Path

_SRC = Path(__file__).resolve().parent.parent
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from dotenv import load_dotenv

from utils.salesforce import pull_all_jobs

_env_path = _SRC.parent / ".env"
load_dotenv(_env_path)

# -----------------------------------------------------------------------------
# Config
# -----------------------------------------------------------------------------
DATA_DIR = _SRC.parent / "data"
CSV_PATH = DATA_DIR / "salesforce_jobs.csv"

# Proxi Salesforce custom domain (override with SALESFORCE_TOKEN_URL in .env if needed)
DEFAULT_TOKEN_URL = "https://proxi.my.salesforce.com"

CONSUMER_KEY = os.environ.get("SALESFORCE_CONSUMER_KEY", "")
CONSUMER_SECRET = os.environ.get("SALESFORCE_CONSUMER_SECRET", "")
USE_CLIENT_CREDENTIALS = os.environ.get("SALESFORCE_USE_USERNAME_PASSWORD", "").lower() not in ("1", "true", "yes")
TOKEN_URL = os.environ.get("SALESFORCE_TOKEN_URL") or DEFAULT_TOKEN_URL
USERNAME = os.environ.get("SALESFORCE_USERNAME", "")
PASSWORD = os.environ.get("SALESFORCE_PASSWORD", "")
SECURITY_TOKEN = os.environ.get("SALESFORCE_SECURITY_TOKEN") or None
USE_SANDBOX = os.environ.get("SALESFORCE_USE_SANDBOX", "").lower() in ("1", "true", "yes")
JOB_OBJECT = os.environ.get("SALESFORCE_JOB_OBJECT", "Job__c")


def _flatten_record(rec: dict) -> dict:
    """Flatten nested dicts/lists for CSV (e.g. nested 'Name' in refs)."""
    out = {}
    for k, v in rec.items():
        if isinstance(v, dict) and "Name" in v:
            out[k] = v.get("Name", "")
        elif isinstance(v, (list, dict)):
            out[k] = str(v)
        else:
            out[k] = v
    return out


def save_jobs_to_csv(records: list[dict], path: Path = CSV_PATH) -> Path:
    """Write job records to CSV. Creates data dir if needed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if not records:
        return path
    flat = [_flatten_record(r) for r in records]
    fieldnames = list(flat[0].keys())
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, quoting=csv.QUOTE_NONNUMERIC, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(flat)
    return path


def describe_job_fields() -> None:
    """Print Job__c field metadata: name, label, type, required, createable, updateable."""
    from utils.salesforce import describe_sobject, get_token_auto

    token_data = get_token_auto(
        CONSUMER_KEY,
        CONSUMER_SECRET,
        USERNAME or None,
        PASSWORD or None,
        use_client_credentials=USE_CLIENT_CREDENTIALS,
        security_token=SECURITY_TOKEN,
        use_sandbox=USE_SANDBOX,
        token_url=TOKEN_URL,
    )
    desc = describe_sobject(token_data["instance_url"], token_data["access_token"], JOB_OBJECT)
    fields = desc.get("fields", [])

    required, optional = [], []
    for f in fields:
        row = {
            "name": f["name"],
            "label": f.get("label", ""),
            "type": f.get("type", ""),
            "createable": f.get("createable", False),
            "updateable": f.get("updateable", False),
        }
        if not f.get("nillable") and not f.get("defaultedOnCreate") and f.get("createable"):
            required.append(row)
        else:
            optional.append(row)

    print(f"\n{'='*70}")
    print(f"  {JOB_OBJECT} — REQUIRED fields ({len(required)})")
    print(f"{'='*70}")
    for f in required:
        print(f"  {f['name']:<45} {f['type']:<15} {f['label']}")

    print(f"\n{'='*70}")
    print(f"  {JOB_OBJECT} — OPTIONAL fields ({len(optional)})")
    print(f"{'='*70}")
    for f in optional:
        flag = ""
        if not f["createable"] and not f["updateable"]:
            flag = " [read-only]"
        elif not f["createable"]:
            flag = " [update-only]"
        elif not f["updateable"]:
            flag = " [create-only]"
        print(f"  {f['name']:<45} {f['type']:<15} {f['label']}{flag}")

    print(f"\nTotal: {len(fields)} fields")


def main():
    import argparse
    p = argparse.ArgumentParser(description="Pull Salesforce jobs or describe Job__c fields.")
    p.add_argument("--describe", action="store_true", help="Print field metadata (required/optional) instead of pulling records.")
    args = p.parse_args()

    if not all([CONSUMER_KEY, CONSUMER_SECRET]):
        print("Missing required env: SALESFORCE_CONSUMER_KEY, SALESFORCE_CONSUMER_SECRET")
        sys.exit(1)
    if not USE_CLIENT_CREDENTIALS and not all([USERNAME, PASSWORD]):
        print(
            "For username-password flow set SALESFORCE_USE_USERNAME_PASSWORD=false and "
            "provide SALESFORCE_USERNAME, SALESFORCE_PASSWORD"
        )
        sys.exit(1)

    if args.describe:
        describe_job_fields()
        return

    start = time.perf_counter()
    jobs = pull_all_jobs(
        CONSUMER_KEY,
        CONSUMER_SECRET,
        username=USERNAME or None,
        password=PASSWORD or None,
        use_client_credentials=USE_CLIENT_CREDENTIALS,
        token_url=TOKEN_URL,
        security_token=SECURITY_TOKEN,
        use_sandbox=USE_SANDBOX,
        job_object_name=JOB_OBJECT,
    )
    elapsed = time.perf_counter() - start

    print(f"Pulled {len(jobs)} jobs from {JOB_OBJECT} (READ-ONLY)")
    if jobs:
        save_jobs_to_csv(jobs)
        print(f"Saved to {CSV_PATH}")
    print(f"Completed in {elapsed:.2f}s")
    return jobs


if __name__ == "__main__":
    main()

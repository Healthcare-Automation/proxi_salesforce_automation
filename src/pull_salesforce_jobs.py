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
"""

import csv
import os
import sys
import time
from pathlib import Path

_src_dir = Path(__file__).resolve().parent
if str(_src_dir) not in sys.path:
    sys.path.insert(0, str(_src_dir))

from dotenv import load_dotenv

from utils.salesforce import pull_all_jobs

_env_path = Path(__file__).resolve().parent.parent / ".env"
load_dotenv(_env_path)

# -----------------------------------------------------------------------------
# Config
# -----------------------------------------------------------------------------
DATA_DIR = Path(__file__).resolve().parent.parent / "data"
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


def main():
    if not all([CONSUMER_KEY, CONSUMER_SECRET]):
        print("Missing required env: SALESFORCE_CONSUMER_KEY, SALESFORCE_CONSUMER_SECRET")
        sys.exit(1)
    if not USE_CLIENT_CREDENTIALS and not all([USERNAME, PASSWORD]):
        print(
            "For username-password flow set SALESFORCE_USE_USERNAME_PASSWORD=false and "
            "provide SALESFORCE_USERNAME, SALESFORCE_PASSWORD"
        )
        sys.exit(1)

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

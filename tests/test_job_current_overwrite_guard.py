"""A failed/partial scrape must never erase good values in job_current.

Regression for job 20311 (2026-08-18): retries of a failing scrape kept upserting
empty practice/city/state into job_current, clobbering the values a successful
scrape had already captured and blocking Job__c creation for ~3 hours.

Integration test — runs against the staging schema; skipped when no DB is reachable.
"""
import uuid
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent

SCHEMA = "staging"


@pytest.fixture()
def db_conn():
    from dotenv import load_dotenv

    load_dotenv(PROJECT_ROOT / ".env")
    from utils.supabase_db import get_conn

    with get_conn() as conn:
        if conn is None:
            pytest.skip("database not reachable")
        yield conn


def test_blank_scrape_values_do_not_clobber_good_ones(db_conn):
    from utils.supabase_db import _upsert_job_current, get_job_current

    jid = f"test-guard-{uuid.uuid4().hex[:10]}"
    good = {
        "job_id": jid,
        "practice_value": "4412 - Humble, TX",
        "city": "Humble",
        "state": "TX",
        "address_line": "10007 Farm to Market Rd",
        "job_title": "title v1",
    }
    blank = {
        "job_id": jid,
        "practice_value": "",
        "city": "  ",
        "state": None,
        "address_line": "",
        "job_title": "title v2",
    }
    try:
        _upsert_job_current(db_conn, None, jid, good, schema=SCHEMA)
        _upsert_job_current(db_conn, None, jid, blank, schema=SCHEMA)
        row = dict(get_job_current(db_conn, job_ids=[jid], limit=1, schema=SCHEMA)[0])
        # Protected fields keep their good values.
        assert row["practice_value"] == "4412 - Humble, TX"
        assert row["city"] == "Humble"
        assert row["state"] == "TX"
        assert row["address_line"] == "10007 Farm to Market Rd"
        # Non-protected fields still take the newest value.
        assert row["job_title"] == "title v2"
        # A newer NON-empty value still wins on protected fields.
        _upsert_job_current(db_conn, None, jid, {**good, "practice_value": "4412 - Humble, TX (relabeled)"}, schema=SCHEMA)
        row = dict(get_job_current(db_conn, job_ids=[jid], limit=1, schema=SCHEMA)[0])
        assert row["practice_value"] == "4412 - Humble, TX (relabeled)"
    finally:
        with db_conn.cursor() as cur:
            cur.execute(f"DELETE FROM {SCHEMA}.job_current WHERE job_id = %s", (jid,))
        db_conn.commit()

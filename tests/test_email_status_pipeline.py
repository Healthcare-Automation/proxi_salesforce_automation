"""Integration: _apply_email_status_signal fills/overrides status and logs events.

Runs inside a rolled-back transaction on the staging schema; skipped without a DB.
"""
import uuid
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture()
def staging_conn():
    from dotenv import load_dotenv

    load_dotenv(PROJECT_ROOT / ".env")
    from utils.supabase_db import get_conn

    with get_conn() as conn:
        if conn is None:
            pytest.skip("database not reachable")
        with conn.cursor() as cur:
            cur.execute("SET search_path TO staging")
        yield conn
        conn.rollback()


def _insert_email(conn, job_post_id, action_or_change, minutes_ago):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO scrape_runs (run_type, started_at) VALUES ('test', NOW()) RETURNING id"
        )
        run_id = cur.fetchone()[0]
        cur.execute(
            """
            INSERT INTO email_scrapes (run_id, job_post_id, action_or_change, subject, date)
            VALUES (%s, %s, %s, 'test', NOW() - make_interval(mins => %s))
            RETURNING id
            """,
            (run_id, job_post_id, action_or_change, minutes_ago),
        )
        return cur.fetchone()[0]


def _events(conn, job_id):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT event_type, payload FROM job_event_log WHERE job_id = %s ORDER BY id",
            (job_id,),
        )
        return cur.fetchall()


def test_fill_from_new_email_and_event(staging_conn):
    from utils.pipeline_link_scrape import _apply_email_status_signal

    jid = f"tes-{uuid.uuid4().hex[:8]}"
    esid = _insert_email(staging_conn, jid, "new", minutes_ago=5)
    cl = {"status": ""}
    _apply_email_status_signal(
        staging_conn, {"email_scrape_id": esid}, cl, job_id=jid, run_id=None, schema="staging"
    )
    assert cl["status"] == "Active, accepting new providers"
    evs = _events(staging_conn, jid)
    assert [e[0] for e in evs] == ["status_filled_from_email"]


def test_fresh_status_email_overrides_and_alert_event(staging_conn):
    from utils.pipeline_link_scrape import _apply_email_status_signal

    jid = f"tes-{uuid.uuid4().hex[:8]}"
    esid = _insert_email(staging_conn, jid, "status: Closed", minutes_ago=3)
    cl = {"status": "Active, accepting new providers"}
    _apply_email_status_signal(
        staging_conn, {"email_scrape_id": esid}, cl, job_id=jid, run_id=None, schema="staging"
    )
    assert cl["status"] == "Closed"
    evs = _events(staging_conn, jid)
    assert [e[0] for e in evs] == ["status_email_page_mismatch"]
    assert evs[0][1]["email_won"] is True


def test_stale_new_email_does_not_roll_back_newer_status(staging_conn):
    from utils.pipeline_link_scrape import _apply_email_status_signal

    jid = f"tes-{uuid.uuid4().hex[:8]}"
    old_esid = _insert_email(staging_conn, jid, "new", minutes_ago=300)
    _insert_email(staging_conn, jid, "status: Closed", minutes_ago=10)
    cl = {"status": ""}
    _apply_email_status_signal(
        staging_conn, {"email_scrape_id": old_esid}, cl, job_id=jid, run_id=None, schema="staging"
    )
    assert cl["status"] == ""
    assert _events(staging_conn, jid) == []


def test_stale_status_email_logs_mismatch_but_page_wins(staging_conn):
    from utils.pipeline_link_scrape import _apply_email_status_signal

    jid = f"tes-{uuid.uuid4().hex[:8]}"
    esid = _insert_email(staging_conn, jid, "status: Active, accepting new providers", minutes_ago=240)
    cl = {"status": "Closed"}
    _apply_email_status_signal(
        staging_conn, {"email_scrape_id": esid}, cl, job_id=jid, run_id=None, schema="staging"
    )
    assert cl["status"] == "Closed"
    evs = _events(staging_conn, jid)
    assert [e[0] for e in evs] == ["status_email_page_mismatch"]
    assert evs[0][1]["email_won"] is False

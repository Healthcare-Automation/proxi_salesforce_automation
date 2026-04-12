"""
Contract tests: after Playwright, the pipeline resolves Salesforce ids and PATCHes
in the same run (no queue). Gmail → DB logging is asserted via source + import checks.
"""

from pathlib import Path

from unittest.mock import MagicMock, patch

from utils.pipeline_link_scrape import process_link_scrape_batch

_SRC_ROOT = Path(__file__).resolve().parent.parent / "src"
_AUTOMATION_ROOT = Path(__file__).resolve().parent.parent


def _one_scrape_row(*, job_post_id: str = "111", job_id: str = "99999") -> dict:
    return {
        "job_post_id": job_post_id,
        "email_received_date": "2026-04-01",
        "view_job_link": "https://portal.kimedics.com/app/workspace/job-posts/99999",
        "error": "",
        "email_scrape_id": 1,
        "cleaned": {
            "job_id": job_id,
            "title_line": "Test title line for validation",
            "job_title": "#99999: General Dentistry (DDS)",
            "status": "Active, accepting new providers",
            "provider_start_date": "04/01/26",
            "location_line": "HOBBS, NM",
            "state": "NM",
            "city": "Hobbs",
            "posting_org": "Aspen Dental",
            "practice_value": "4043 - Hobbs, NM",
            "point_of_contact": "Test Contact",
            "description_full_text": "x" * 400,
        },
    }


@patch("utils.alert_email.send_scrape_alert")
@patch("utils.scrape_validator.validate_scraped_job", return_value=[])
@patch("utils.supabase_db.log_job_content")
@patch("utils.sf_scrape_sync.sync_missing_scrape_fields_for_job_ids")
@patch("utils.sf_job_supabase_resolve.resolve_sf_ids_for_job_ids")
def test_resolve_runs_before_sync_same_run(
    mock_resolve,
    mock_sync,
    mock_log_job,
    mock_validate,
    mock_alert,
):
    """After each log_job_content, resolver runs, then SF sync — single invocation."""
    mock_resolve.return_value = 1
    mock_sync.return_value = (1, 1)
    conn = MagicMock()
    order: list[str] = []

    def _resolve(*a, **k):
        order.append("resolve")
        return 1

    def _sync(*a, **k):
        order.append("sync")
        return (1, 1)

    mock_resolve.side_effect = _resolve
    mock_sync.side_effect = _sync

    touched = process_link_scrape_batch(
        conn,
        link_run_id=42,
        scrape_results=[_one_scrape_row()],
        schema="public",
    )

    assert touched == {"99999"}
    assert order == ["resolve", "sync"]
    mock_resolve.assert_called_once()
    mock_sync.assert_called_once()
    r_args, r_kw = mock_resolve.call_args
    assert r_args[0] is conn
    assert r_args[1] == ["99999"]
    assert r_kw.get("run_id") == 42
    s_args, s_kw = mock_sync.call_args
    assert s_args[0] is conn
    assert s_args[1] == ["99999"]
    assert s_kw.get("run_id") == 42


@patch("utils.alert_email.send_scrape_alert")
@patch("utils.scrape_validator.validate_scraped_job", return_value=[])
@patch("utils.supabase_db.log_job_content")
@patch("utils.sf_scrape_sync.sync_missing_scrape_fields_for_job_ids")
@patch("utils.sf_job_supabase_resolve.resolve_sf_ids_for_job_ids")
def test_multiple_job_ids_passed_sorted_to_resolve_and_sync(
    mock_resolve,
    mock_sync,
    mock_log_job,
    mock_validate,
    mock_alert,
):
    mock_resolve.return_value = 2
    mock_sync.return_value = (2, 0)
    conn = MagicMock()
    rows = [
        _one_scrape_row(job_post_id="1", job_id="b"),
        _one_scrape_row(job_post_id="2", job_id="a"),
    ]
    process_link_scrape_batch(conn, link_run_id=7, scrape_results=rows, schema="public")
    assert mock_resolve.call_args[0][1] == ["a", "b"]
    assert mock_sync.call_args[0][1] == ["a", "b"]


@patch("utils.alert_email.send_scrape_alert")
@patch("utils.scrape_validator.validate_scraped_job", return_value=[])
@patch("utils.supabase_db.log_job_content")
@patch("utils.sf_scrape_sync.sync_missing_scrape_fields_for_job_ids")
@patch("utils.sf_job_supabase_resolve.resolve_sf_ids_for_job_ids")
def test_empty_scrape_results_no_resolve_or_sync(
    mock_resolve,
    mock_sync,
    mock_log_job,
    mock_validate,
    mock_alert,
):
    conn = MagicMock()
    touched = process_link_scrape_batch(conn, link_run_id=1, scrape_results=[], schema="public")
    assert touched == set()
    mock_resolve.assert_not_called()
    mock_sync.assert_not_called()


def test_scrape_gmail_modal_wires_single_post_scrape_pass():
    """Modal job must call ``process_link_scrape_batch`` once (persist + resolve + SF)."""
    modal_path = _AUTOMATION_ROOT / "src" / "production" / "scrape_gmail_modal.py"
    text = modal_path.read_text(encoding="utf-8")
    assert "process_link_scrape_batch(" in text
    assert "from utils.pipeline_link_scrape import process_link_scrape_batch" in text
    assert "enqueue_sf_patch_jobs" not in text
    assert "process_due_sf_patch_queue" not in text
    assert "sf_patch_queue" not in text
    assert "_drain_sf_patch_queue" not in text


def test_python_src_has_no_legacy_sf_patch_queue_api():
    """Guardrail: delayed-queue helpers stay removed from ``src``."""
    forbidden = (
        "enqueue_sf_patch_jobs",
        "process_due_sf_patch_queue",
        "_ensure_sf_patch_queue_table",
        "sf_patch_queue",
        "_drain_sf_patch_queue",
    )
    for path in sorted(_SRC_ROOT.rglob("*.py")):
        t = path.read_text(encoding="utf-8")
        for sym in forbidden:
            assert sym not in t, f"{sym!r} must not appear in {path.relative_to(_AUTOMATION_ROOT)}"

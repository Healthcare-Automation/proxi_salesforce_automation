"""Proxi job description template (HTML + plain)."""

import pytest

from utils.job_description_proxi_template import (
    build_proxi_job_posting_description,
    extract_kimedics_dates_update_preamble,
)


@pytest.fixture(autouse=True)
def _disable_ai_env_for_deterministic_template_tests(monkeypatch):
    monkeypatch.setenv("PROXI_JOB_DESCRIPTION_USE_AI", "false")


def _minimal_row():
    return {
        "city": "Palm Springs",
        "state": "CA",
        "dates_needed": "March 23–26",
        "standard_schedule": "7:30 AM – 4:30 PM",
        "description_full_text": "Pay Range: $125 – $145 per hour\n",
        "insight": "* First note from Kimedics\n* Second note from Kimedics",
    }


def test_build_html_contains_structure():
    out = build_proxi_job_posting_description(_minimal_row(), use_html=True)
    assert "<strong>" in out  # section labels (Patient mix, etc.)
    assert "<ul" in out and "<li" in out
    assert "Palm Springs" in out
    assert "California" in out
    assert "Dates:" in out and "Schedule:" in out
    assert "Highlights" not in out


def test_opening_paragraphs_not_over_bolded():
    out = build_proxi_job_posting_description(_minimal_row(), use_html=True)
    assert "<strong>Proxi" not in out
    assert "<strong>General Dentist</strong>" not in out


def test_build_html_escapes_angle_brackets():
    row = _minimal_row()
    row["city"] = "Test<script>"
    out = build_proxi_job_posting_description(row, use_html=True)
    assert "<script>" not in out
    assert "&lt;script&gt;" in out


def test_build_plain_has_section_breaks():
    out = build_proxi_job_posting_description(_minimal_row(), use_html=False)
    assert "\n\n" in out
    assert "<p>" not in out
    assert "HIGHLIGHTS" not in out


def test_extract_kimedics_dates_update_preamble():
    line = "4/8 update: May dates are closed for internal alignment."
    assert extract_kimedics_dates_update_preamble(f"{line}\n\nRequired procedures: …") == line
    assert extract_kimedics_dates_update_preamble("4/8/2026 update: Same.") == "4/8/2026 update: Same."
    assert extract_kimedics_dates_update_preamble("No header here") is None


def test_html_includes_kimedics_posting_update_in_dates_block():
    row = _minimal_row()
    note = "4/8 update: May dates are closed."
    row["description_full_text"] = f"{note}\n\nPay Range: $125 – $145 per hour\n"
    out = build_proxi_job_posting_description(row, use_html=True)
    assert "Kimedics posting update" in out
    assert "4/8 update" in out

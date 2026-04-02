"""Proxi job description template (HTML + plain)."""

from utils.job_description_proxi_template import build_proxi_job_posting_description


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
    assert "<ul>" in out and "<li>" in out
    assert "Palm Springs" in out
    assert "California" in out
    assert "Dates" in out and "Schedule" in out
    assert "source notes" in out.lower()
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
    assert "\n\n" in out or "\nPATIENT MIX\n" in out
    assert "<p>" not in out
    assert "source notes" in out.lower()
    assert "HIGHLIGHTS" not in out

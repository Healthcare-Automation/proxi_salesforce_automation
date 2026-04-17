"""Proxi job description template (HTML + plain)."""

import pytest

from utils.job_description_ai import merge_ai_intro_html_to_single_paragraph
from utils.job_description_proxi_template import (
    build_proxi_job_posting_description,
    extract_active_needs_dates,
    extract_kimedics_dates_update_preamble,
    strip_internal_presentation_phrases,
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
    # Pay line matches fixed default (not Kimedics Pay Range: $125 – $145 in description_full_text).
    assert "Pay Range:" in out
    assert "Starting at $125/hour" in out
    assert "$145" not in out


def test_opening_paragraphs_not_over_bolded():
    out = build_proxi_job_posting_description(_minimal_row(), use_html=True)
    assert "<strong>Proxi" not in out
    assert "<strong>General Dentist</strong>" not in out


def test_opening_intro_single_paragraph_including_ideal_role_sentence():
    row = _minimal_row()
    row["types_of_cases"] = "Surgical extractions for dentures\nrestorative"
    out = build_proxi_job_posting_description(row, use_html=True)
    idx_opening = out.find("We are seeking")
    idx_ideal = out.find("This role is ideal")
    assert idx_opening != -1 and idx_ideal != -1
    intro_close = out.find("</p>", idx_opening)
    assert intro_close != -1
    assert idx_ideal < intro_close


def test_merge_ai_intro_html_to_single_paragraph():
    merged = merge_ai_intro_html_to_single_paragraph(
        "<p>First sentence.</p>\n<p>Second sentence.</p>"
    )
    assert merged == "<p>First sentence. Second sentence.</p>"
    assert merge_ai_intro_html_to_single_paragraph("<p>Only one.</p>") == "<p>Only one.</p>"


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


def test_strip_internal_presentation_phrases_dash_before():
    raw = "extractions could include simple/ surgical/ full mouth- please notate any limitations in presentation"
    assert "notate" not in strip_internal_presentation_phrases(raw).lower()
    assert "presentation" not in strip_internal_presentation_phrases(raw).lower()
    assert "full mouth" in strip_internal_presentation_phrases(raw).lower()


def test_strip_note_any_limitations_after_comma_or_semicolon():
    for raw in (
        "Surgical extractions; note any limitations in presentation",
        "Surgical extractions, note any limitations in presentation",
    ):
        out = strip_internal_presentation_phrases(raw)
        assert "presentation" not in out.lower()
        assert "note any" not in out.lower()
        assert "surgical extractions" in out.lower()


def test_clinical_note_colon_line_not_stripped_by_note_any_clause():
    raw = "Clinical note: patient prefers morning visits."
    assert "Clinical note" in strip_internal_presentation_phrases(raw)


def test_clinical_scope_capitalizes_first_letter():
    row = _minimal_row()
    row["types_of_cases"] = "extractions could include simple and surgical\nrestorative procedures"
    out = build_proxi_job_posting_description(row, use_html=True)
    assert "Extractions could include" in out
    assert "Restorative procedures" in out


def test_strip_procedure_limitations_phrase_from_types_style_text():
    raw = (
        "Surgical extractions\n"
        "please notate any procedure limitations in presentation."
    )
    out = strip_internal_presentation_phrases(raw)
    assert "notate" not in out.lower()
    assert "presentation" not in out.lower()
    assert "surgical extractions" in out.lower()


def test_strip_presentation_note_variants():
    assert "notate" not in strip_internal_presentation_phrases(
        "Extractions — kindly note any limitations on presentation."
    ).lower()
    assert "notate" not in strip_internal_presentation_phrases(
        "Scope: pls notate limitations during presentation"
    ).lower()
    assert "presentation" not in strip_internal_presentation_phrases(
        "Scope: pls notate limitations during presentation"
    ).lower()


def test_clinical_note_word_not_stripped():
    """Bare word 'note' in clinical prose must not remove unrelated lines."""
    raw = "Clinical note: patient prefers morning visits."
    assert "Clinical note" in strip_internal_presentation_phrases(raw)


def test_strip_internal_phrases_in_html_clinical_scope():
    row = _minimal_row()
    row["types_of_cases"] = (
        "Surgical extractions\n"
        "restorative - please notate any limitations in presentation"
    )
    out = build_proxi_job_posting_description(row, use_html=True)
    assert "please notate" not in out.lower()
    assert "restorative" in out.lower()


def test_extract_active_needs_dates():
    line = (
        "4/8 update: May dates are closed for internal alignment. "
        "Partial confirmation for June/ July. "
        "Active needs are Fridays June 5, 12, 19, 26, July 10, 17, 24, 31"
    )
    want = "Fridays June 5, 12, 19, 26, July 10, 17, 24, 31"
    assert extract_active_needs_dates(line) == want
    assert extract_active_needs_dates(f"{line}\n\nMore body") == want
    assert extract_active_needs_dates("ACTIVE NEEDS ARE Mon–Wed") == "Mon–Wed"
    assert extract_active_needs_dates("No marker here") is None


def test_extract_active_need_singular_same_line_as_preamble():
    """Kimedics often puts 'Active need is <date>' after a status fragment on the first line."""
    line = "4/14 pending partial fill. Active need is May 20"
    assert extract_active_needs_dates(line) == "May 20"
    assert extract_active_needs_dates(f"{line}\n\nPay Range: $X\n") == "May 20"


def test_extract_active_need_colon_and_dash_variants():
    assert extract_active_needs_dates("Note. Active need: June 5 and 6") == "June 5 and 6"
    assert extract_active_needs_dates("Active need – May 20 (tentative)") == "May 20 (tentative)"


def test_active_needs_dates_in_template_overrides_dates_needed_column():
    row = _minimal_row()
    row["dates_needed"] = "Monday only"
    row["description_full_text"] = (
        "4/8 update: x. Active needs are Fridays June 5, 12, 19.\n\nPay Range: $125 – $145 per hour\n"
    )
    out = build_proxi_job_posting_description(row, use_html=True)
    assert "Fridays June 5, 12, 19" in out
    assert "Monday only" not in out


def test_active_need_is_overrides_dates_in_job_description():
    row = _minimal_row()
    row["dates_needed"] = "Monday only"
    row["description_full_text"] = (
        "4/14 pending partial fill. Active need is May 20\n\nPay Range: $125 – $145 per hour\n"
    )
    out = build_proxi_job_posting_description(row, use_html=True)
    assert "May 20" in out
    assert "Monday only" not in out


def test_extract_kimedics_dates_update_preamble():
    line = "4/8 update: May dates are closed for internal alignment."
    assert extract_kimedics_dates_update_preamble(f"{line}\n\nRequired procedures: …") == line
    assert extract_kimedics_dates_update_preamble("4/8/2026 update: Same.") == "4/8/2026 update: Same."
    assert extract_kimedics_dates_update_preamble("No header here") is None


def test_kimedics_admin_preamble_not_in_client_job_description():
    """Internal M/D update lines stay out of Job_Client_Job_Description__c (not client-facing)."""
    row = _minimal_row()
    note = "4/8 update: May dates are closed."
    row["description_full_text"] = f"{note}\n\nPay Range: $125 – $145 per hour\n"
    out_html = build_proxi_job_posting_description(row, use_html=True)
    out_plain = build_proxi_job_posting_description(row, use_html=False)
    assert "Kimedics posting update" not in out_html
    assert "Kimedics posting update" not in out_plain
    assert "4/8 update" not in out_html
    assert "4/8 update" not in out_plain
    assert extract_kimedics_dates_update_preamble(row["description_full_text"]) == note

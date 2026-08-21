"""Unit tests for job_content_parser description sections and city/state."""

from utils.job_content_ai import combined_types_of_cases, parse_with_pipeline
from utils.job_content_parser import (
    parse_job_content_txt,
    repair_flat_jobpost_text_missing_posted_date,
    repair_flat_jobpost_text_missing_practice,
    _backfill_practice_value,
)

SAMPLE_17967 = """Dentistry (Dentist (DMD/DDS)) (#17967)
Los Lunas, NM
Practice
4035 - Los Lunas, NM
Job Title
#17967: Dentistry (Dentist (DMD/DDS))
Posted Date
Posting Org
Aspen Dental
Priority
Normal
Status
Closed
Full Job Post
Description
Basics
Job title
Dentistry (Dentist (DMD/DDS))
Number of open positions
None
Priority
Normal
Status
Closed
Shift Credential Accepted
Dentist (DMD/DDS)
Position type
 None 
Time
Full Time, Part Time
Rates
Billable = $0-$0/hr
Only accept providers under the max rates
No
Point Of Contact
Christina Roth
Search Details
Why are you searching for providers?
Provider On Vacation
Provider start date
11/03/25
Provider end date
11/07/25
Which shifts are available for providers?
None
Estimated shifts per month
None
State License Required To Apply
Yes
Board specialty must match practice set up to apply
No
Privileges Available
None
Minimum Years of Experience
None
Geographic Restriction
None
Other notes
Nov 3-5
Sharing
Edit

--- Description (full text) ---
10/21 update: Assignment closed - filled

*CSR Required

Facility: 4035 - Los Lunas, NM
Address: 1700 Main Street SW
City: Los Lunas
State: NM

Dates: Nov 3-7
Hours: Mon-Fri 8a-5p
Max Bill Rate: 
 
Clinical Staff: 3 DA's, 1 LT, 1 HYG
Required procedures: Surgical extractions, Denture steps, Comprehensive treatment planning 
Avg patients per day: 
Additional requirements/ info: extractions could include simple/ surgical/ full mouth- please notate any limitations in presentation
"""


def test_parse_extracts_required_and_additional_from_description():
    body = (
        "Title #1\n"
        "Location: City, ST\n"
        "Practice\n"
        "Some practice\n"
        "...\n\n"
        "--- Description (full text) ---\n"
        "Required procedures\n"
        "Do fillings and crowns.\n\n"
        "Additional requirements\n"
        "Wear PPE.\n"
    )
    out = parse_job_content_txt(body)
    assert "fillings" in (out.get("required_procedures") or "").lower()
    assert "ppe" in (out.get("additional_requirements") or "").lower()


def test_avg_patients_per_day_from_labeled_line_in_description():
    body = (
        "Title #1\n"
        "Location: City, ST\n"
        "Practice\n"
        "Some practice\n"
        "...\n\n"
        "--- Description (full text) ---\n"
        "Clinical Staff: 2 DA\n"
        "Avg patients per day: 10-12\n"
        "Additional requirements: none\n"
    )
    out = parse_job_content_txt(body)
    assert (out.get("avg_patients_per_day") or "").strip() == "10-12"


def test_avg_patients_blank_does_not_absorb_additional_requirements_when_same_line():
    """
    Regression: when Kimedics concatenates multiple labels on one physical line, the first label
    must not absorb subsequent labels (e.g. Avg patients per day swallowing Additional requirements).
    """
    body = (
        "Title #1\n"
        "Location: City, ST\n"
        "Practice\n"
        "Some practice\n"
        "...\n\n"
        "--- Description (full text) ---\n"
        "Clinical Staff: 2 DA\n"
        "Avg patients per day: Additional requirements: PPE required\n"
    )
    out = parse_job_content_txt(body)
    assert (out.get("avg_patients_per_day") or "").strip() == ""
    assert "ppe" in (out.get("additional_requirements") or "").lower()


def test_multiple_labels_chained_on_one_line_are_split_and_extracted():
    """
    Regression: multiple label/value pairs may be chained on one physical line; each should be
    extracted into its own field.
    """
    body = (
        "Title #1\n"
        "Location: City, ST\n"
        "Practice\n"
        "Some practice\n"
        "...\n\n"
        "--- Description (full text) ---\n"
        "Required procedures: Fillings and crowns. Avg patients per day: 10-12. Additional requirements: Wear PPE.\n"
    )
    out = parse_job_content_txt(body)
    assert "fillings" in (out.get("required_procedures") or "").lower()
    assert (out.get("avg_patients_per_day") or "").strip() == "10-12."
    assert "ppe" in (out.get("additional_requirements") or "").lower()


def test_three_labels_in_one_line_extract_all_without_cross_contamination():
    body = (
        "Title #1\n"
        "Location: City, ST\n"
        "Practice\n"
        "Some practice\n"
        "...\n\n"
        "--- Description (full text) ---\n"
        "Clinical staff: 3 DA, 1 RDH Avg patients per day: 10-12 Additional requirements: none\n"
    )
    out = parse_job_content_txt(body)
    assert "3 da" in (out.get("support_staff") or "").lower()
    assert (out.get("avg_patients_per_day") or "").strip() == "10-12"
    assert "none" in (out.get("additional_requirements") or "").lower()


def test_roster_only_true_when_phrase_in_description():
    body = (
        "Title #1\n"
        "X, ST\n"
        "Practice\n"
        "Practice val\n"
        "...\n\n"
        "--- Description (full text) ---\n"
        "This is a Roster Only opportunity.\n"
    )
    out = parse_job_content_txt(body)
    assert (out.get("roster_only") or "").strip() == "true"


def test_roster_only_false_when_absent():
    body = (
        "Title #1\n"
        "X, ST\n"
        "Practice\n"
        "Practice val\n"
        "...\n\n"
        "--- Description (full text) ---\n"
        "Regular locums posting.\n"
    )
    out = parse_job_content_txt(body)
    assert (out.get("roster_only") or "").strip() == "false"


def test_roster_only_detects_roster_hyphen_only():
    out = parse_job_content_txt("Title #9\nLoc\nPractice\nP\n--- Description (full text) ---\nRoster-only shift.\n")
    assert (out.get("roster_only") or "").strip() == "true"


def test_roster_only_true_when_open_to_roster_in_description():
    body = (
        "Title #1\n"
        "X, ST\n"
        "Practice\n"
        "Practice val\n"
        "...\n\n"
        "--- Description (full text) ---\n"
        "We are open to roster for this site.\n"
    )
    out = parse_job_content_txt(body)
    assert (out.get("roster_only") or "").strip() == "true"


def test_avg_patients_per_day_from_section_heading():
    body = (
        "Title #99\n"
        "X, ST\n"
        "Practice\n"
        "999 - X, ST\n"
        "...\n\n"
        "--- Description (full text) ---\n"
        "Avg patients per day\n"
        "8 to 10 on busy days.\n"
    )
    out = parse_job_content_txt(body)
    assert "8 to 10" in (out.get("avg_patients_per_day") or "")


def test_avg_patients_blank_does_not_absorb_asterisk_bullet_on_next_line():
    """
    Regression (job 19664): 'Avg patients per day:' with empty value followed by a bullet line
    beginning with '*' (insight/footnote) must not be absorbed as the value. The bullet belongs
    to ``insight``, not to the labeled field. Applies to any number of leading ``*``.
    """
    body = (
        "Title #19664\n"
        "KATY, TX\n"
        "Practice\n"
        "4217 - Katy, TX\n"
        "...\n\n"
        "--- Description (full text) ---\n"
        "Address: 20230 KATY FWY, KATY TX\n"
        "\n"
        "*Must have active TX DEA with all schedules at time of submission to be considered\n"
        "**Previous Aspen experience required\n"
        "***Local provider (no flights/rental car)\n"
        "\n"
        "Dates: May 18\n"
        "Hours: 7:30a-5:30p\n"
        "\n"
        "Clinical Staff: 3 DA, 1 RDH\n"
        "Required procedures: Surgical extractions, Denture steps, Comprehensive treatment planning\n"
        "Avg patients per day:\n"
        "***Additional requirements/ info: extractions could include simple/ surgical/ full mouth- please notate any limitations in presentation\n"
    )
    out = parse_job_content_txt(body)
    assert (out.get("avg_patients_per_day") or "").strip() == ""
    # Insight still captures all ``*``-prefixed lines including the trailing one.
    insight = out.get("insight") or ""
    assert "Must have active TX DEA" in insight
    assert "Additional requirements/ info" in insight
    # Other labeled fields remain correct.
    assert "surgical extractions" in (out.get("required_procedures") or "").lower()
    assert "3 DA" in (out.get("support_staff") or "")
    assert (out.get("standard_schedule") or "").strip() == "7:30a-5:30p"


def _make_body(*description_lines: str) -> str:
    header = (
        "Title #1\n"
        "Loc, ST\n"
        "Practice\n"
        "x\n"
        "...\n\n"
        "--- Description (full text) ---\n"
    )
    return header + "".join(line if line.endswith("\n") else line + "\n" for line in description_lines)


def test_single_asterisk_bullet_after_empty_label_is_not_absorbed():
    out = parse_job_content_txt(_make_body(
        "Avg patients per day:",
        "*CSR required",
    ))
    assert (out.get("avg_patients_per_day") or "").strip() == ""
    assert "CSR required" in (out.get("insight") or "")


def test_double_asterisk_bullet_after_empty_label_is_not_absorbed():
    out = parse_job_content_txt(_make_body(
        "Avg patients per day:",
        "**Must have X license",
    ))
    assert (out.get("avg_patients_per_day") or "").strip() == ""
    assert "Must have X license" in (out.get("insight") or "")


def test_empty_required_procedures_not_absorbing_following_asterisk_bullet():
    """The fix must apply to every labeled description field, not just avg_patients_per_day."""
    out = parse_job_content_txt(_make_body(
        "Required procedures:",
        "*Must have active license",
    ))
    assert (out.get("required_procedures") or "").strip() == ""


def test_empty_clinical_staff_not_absorbing_following_asterisk_bullet():
    out = parse_job_content_txt(_make_body(
        "Clinical Staff:",
        "**Previous experience required",
    ))
    assert (out.get("support_staff") or "").strip() == ""


def test_empty_dates_not_absorbing_following_asterisk_bullet():
    out = parse_job_content_txt(_make_body(
        "Dates:",
        "***Local provider only",
    ))
    assert (out.get("dates_needed") or "").strip() == ""


def test_empty_hours_not_absorbing_following_asterisk_bullet():
    out = parse_job_content_txt(_make_body(
        "Hours:",
        "*All schedules required",
    ))
    assert (out.get("standard_schedule") or "").strip() == ""


def test_avg_patients_inline_value_retained_when_next_line_is_bullet():
    """Inline value on the label line must survive; bullet on next line stays in insight only."""
    out = parse_job_content_txt(_make_body(
        "Avg patients per day: 10-12",
        "*See footnote on billing",
    ))
    assert (out.get("avg_patients_per_day") or "").strip() == "10-12"
    assert "See footnote on billing" in (out.get("insight") or "")


def test_avg_patients_section_heading_form_does_not_absorb_asterisk_bullet():
    """Section-heading form ('Avg patients per day' with no colon) also must not absorb bullets."""
    out = parse_job_content_txt(_make_body(
        "Avg patients per day",
        "***Additional requirements/ info: extractions could include limitations",
    ))
    assert (out.get("avg_patients_per_day") or "").strip() == ""
    assert "Additional requirements/ info" in (out.get("insight") or "")


def test_asterisk_bullet_between_labels_does_not_leak_into_prior_value():
    """Bullet sandwiched between two labels must not extend the prior label's value."""
    out = parse_job_content_txt(_make_body(
        "Clinical Staff: 3 DA, 1 RDH",
        "*Must have active DEA",
        "Required procedures: Fillings",
    ))
    assert (out.get("support_staff") or "").strip() == "3 DA, 1 RDH"
    assert "fillings" in (out.get("required_procedures") or "").lower()
    assert "Must have active DEA" in (out.get("insight") or "")


def test_asterisk_in_middle_of_line_is_not_treated_as_bullet():
    """Only leading ``*`` marks an insight bullet; mid-line asterisks are regular content."""
    out = parse_job_content_txt(_make_body(
        "Avg patients per day:",
        "10-12 (projected 5*2 per provider)",
    ))
    assert (out.get("avg_patients_per_day") or "").strip() == "10-12 (projected 5*2 per provider)"


def test_blank_line_then_asterisk_bullet_does_not_cross_into_prior_empty_label():
    """Blank line breaks continuation first; bullet afterwards must not backfill the empty value."""
    out = parse_job_content_txt(_make_body(
        "Avg patients per day:",
        "",
        "*CSR required",
    ))
    assert (out.get("avg_patients_per_day") or "").strip() == ""
    assert "CSR required" in (out.get("insight") or "")


def test_multiple_empty_labels_followed_by_single_trailing_bullet_all_empty():
    """Stress: sequence of empty labels then one trailing bullet — every field should stay empty."""
    out = parse_job_content_txt(_make_body(
        "Required procedures:",
        "Clinical Staff:",
        "Avg patients per day:",
        "***Additional requirements/ info: notate any limitations",
    ))
    assert (out.get("required_procedures") or "").strip() == ""
    assert (out.get("support_staff") or "").strip() == ""
    assert (out.get("avg_patients_per_day") or "").strip() == ""
    assert "Additional requirements/ info" in (out.get("insight") or "")


def test_leading_whitespace_before_asterisk_still_treated_as_bullet():
    out = parse_job_content_txt(_make_body(
        "Avg patients per day:",
        "   *CSR required (indented)",
    ))
    assert (out.get("avg_patients_per_day") or "").strip() == ""
    # Insight captures it because ``_extract_insight_lines`` strips before checking ``*``.
    assert "CSR required" in (out.get("insight") or "")


def test_valid_multiline_continuation_still_works_when_no_bullet_follows():
    """Must not over-correct: continuation on subsequent non-bullet, non-label line still works."""
    out = parse_job_content_txt(_make_body(
        "Required procedures:",
        "Fillings, crowns, and",
        "surgical extractions",
        "Avg patients per day: 12",
    ))
    rp = (out.get("required_procedures") or "").lower()
    assert "fillings" in rp
    assert "surgical extractions" in rp
    assert (out.get("avg_patients_per_day") or "").strip() == "12"


def test_sf_job_volume_omitted_when_avg_patients_empty_for_19664():
    """
    End-to-end guard: for the 19664 scenario, the SF payload must not set Job_Volume__c (which
    would trip the 50-char max). This ties the parser fix to the downstream Salesforce result.
    """
    from utils.sf_job_payload import job_row_to_salesforce_fields

    body = (
        "Title #19664\n"
        "KATY, TX\n"
        "Practice\n"
        "4217 - Katy, TX\n"
        "...\n\n"
        "--- Description (full text) ---\n"
        "Address: 20230 KATY FWY, KATY TX\n"
        "Dates: May 18\n"
        "Hours: 7:30a-5:30p\n"
        "Clinical Staff: 3 DA, 1 RDH\n"
        "Required procedures: Surgical extractions\n"
        "Avg patients per day:\n"
        "***Additional requirements/ info: extractions could include simple/ surgical/ full mouth- please notate any limitations in presentation\n"
    )
    row = parse_job_content_txt(body)
    row["job_id"] = "19664"
    payload = job_row_to_salesforce_fields(row)
    assert "Job_Volume__c" not in payload or payload.get("Job_Volume__c") is None


def test_practice_value_backfilled_from_facility_when_header_has_job_title_on_line_3():
    body = (
        "Title #1\n"
        "Somewhere, ST\n"
        "Practice\n"
        "Job Title\n"
        "#1: Dentistry\n"
        "Status\n"
        "Open\n"
        "\n"
        "--- Description (full text) ---\n"
        "Facility: 4035 - Los Lunas, NM\n"
        "City: Los Lunas\n"
        "State: NM\n"
    )
    out = parse_job_content_txt(body)
    assert (out.get("practice_value") or "").strip() == "4035 - Los Lunas, NM"


def test_practice_value_backfilled_from_standalone_office_line_in_description():
    body = (
        "Title #99\n"
        "ROSENBERG, TX\n"
        "Practice\n"
        "Job Title\n"
        "#99: x\n"
        "Status\n"
        "Open\n"
        "\n"
        "--- Description (full text) ---\n"
        "3/23 note\n"
        "\n"
        "4361 - Rosenberg, TX\n"
        "Address: 6419 Reading Rd\n"
    )
    out = parse_job_content_txt(body)
    assert "4361" in (out.get("practice_value") or "")
    assert "Rosenberg" in (out.get("practice_value") or "")


def test_city_title_case_from_all_caps_label():
    body = (
        "Title #1\n"
        "Loc, ST\n"
        "Practice\n"
        "x\n"
        "...\n\n"
        "--- Description (full text) ---\n"
        "City: LOS ANGELES\n"
        "State: CA\n"
    )
    out = parse_job_content_txt(body)
    assert (out.get("city") or "").strip() == "Los Angeles"


def test_address_line_composes_from_labeled_address_city_state():
    body = (
        "Title #1\n"
        "Loc, ST\n"
        "Practice\n"
        "x\n"
        "...\n\n"
        "--- Description (full text) ---\n"
        "Address: 100 Main St\n"
        "City: Springfield\n"
        "State: IL\n"
    )
    out = parse_job_content_txt(body)
    assert (out.get("address_line") or "").strip() == "100 Main St, Springfield, IL"


def test_address_line_keeps_complete_line_without_duplicating_city_state():
    body = (
        "Title #1\n"
        "Loc, ST\n"
        "Practice\n"
        "x\n"
        "...\n\n"
        "--- Description (full text) ---\n"
        "Address: 1643 County Route 64, Horseheads NY\n"
        "City: Horseheads\n"
        "State: NY\n"
    )
    out = parse_job_content_txt(body)
    assert (out.get("address_line") or "").strip() == "1643 County Route 64, Horseheads NY"


def test_address_line_no_duplicate_when_street_has_city_abbrev_and_state_full_name():
    """Row State: full name while Address ends with ``City ST`` — do not append state again."""
    body = (
        "Title #1\n"
        "Loc, ST\n"
        "Practice\n"
        "x\n"
        "...\n\n"
        "--- Description (full text) ---\n"
        "Address: 3901 S Bolger Rd, Independence MO\n"
        "City: Independence\n"
        "State: Missouri\n"
    )
    out = parse_job_content_txt(body)
    assert (out.get("address_line") or "").strip() == "3901 S Bolger Rd, Independence MO"


def test_address_line_no_duplicate_when_street_has_full_state_and_row_has_abbrev():
    """Row State: 2-letter while Address already spells full state name."""
    body = (
        "Title #1\n"
        "Loc, ST\n"
        "Practice\n"
        "x\n"
        "...\n\n"
        "--- Description (full text) ---\n"
        "Address: 500 Oak Ln, Austin, Texas\n"
        "City: Austin\n"
        "State: TX\n"
    )
    out = parse_job_content_txt(body)
    assert (out.get("address_line") or "").strip() == "500 Oak Ln, Austin, Texas"


def test_address_line_city_state_only_when_no_address_label():
    body = (
        "Title #1\n"
        "Austin, TX\n"
        "Practice\n"
        "x\n"
        "...\n\n"
        "--- Description (full text) ---\n"
        "City: Austin\n"
        "State: TX\n"
    )
    out = parse_job_content_txt(body)
    assert (out.get("address_line") or "").strip() == "Austin, TX"


def test_practice_value_city_only_numeric_prefix_sets_city():
    """``CODE - City`` without comma (common on Client Job Id line) must populate city."""
    body = (
        "Title #19613\n"
        "Loc, ST\n"
        "Practice\n"
        "4190 - Gloucester\n"
        "...\n\n"
        "--- Description (full text) ---\n"
        "State: VA\n"
    )
    out = parse_job_content_txt(body)
    assert "gloucester" in (out.get("city") or "").lower()


def test_practice_value_period_before_state_parses_city_and_state():
    """``1029 - Keizer. OR`` (period where the comma should be) must still yield city+state."""
    body = (
        "Title #20306\n"
        "Loc, ST\n"
        "Practice\n"
        "1029 - Keizer. OR\n"
        "Job Title\n"
        "#20306: Dentistry (Dentist (DMD/DDS))\n"
    )
    out = parse_job_content_txt(body)
    assert (out.get("city") or "").strip() == "Keizer"
    assert (out.get("state") or "").strip().upper() == "OR"


def test_practice_value_space_before_state_parses_city_and_state():
    """``1029 - Keizer OR`` (no comma at all) must also yield city+state."""
    from utils.job_content_parser import _parse_city_state

    assert _parse_city_state("1029 - Keizer OR") == ("Keizer", "OR")
    assert _parse_city_state("1029 - Salt Lake City. UT") == ("Salt Lake City", "UT")
    # A trailing 2-letter token that is NOT a state code stays part of the city.
    assert _parse_city_state("4190 - Gloucester") == ("Gloucester", "")
    # No numeric prefix → unchanged behavior (practice names are not cities).
    assert _parse_city_state("Acme Dental QQ") == ("", "")


def test_practice_value_office_number_without_dash_parses_city_and_state():
    """``4412 Humble, TX`` (office number with NO dash) must not leak the number into the city."""
    from utils.job_content_parser import _parse_city_state

    assert _parse_city_state("4412 Humble, TX") == ("Humble", "TX")
    assert _parse_city_state("4412 Humble TX") == ("Humble", "TX")
    assert _parse_city_state("1029 Keizer. OR") == ("Keizer", "OR")
    # Bare number + word with no comma and no state token stays unparsed.
    assert _parse_city_state("4412 Humble") == ("", "")


def test_description_fallback_rescues_practice_without_dash():
    """When the Practice field is blank, the description line "4412 Humble, TX" must be used."""
    from utils.job_content_parser import _extract_practice_value_from_description

    assert _extract_practice_value_from_description(
        "8/18 update: pending confirmation\n\n**Updated hours\n4412 Humble, TX \nAddress: 10007 Farm Rd"
    ) == "4412 Humble, TX"
    assert _extract_practice_value_from_description("4361 - Rosenberg, TX\nmore") == "4361 - Rosenberg, TX"
    # A plain street address must NOT be mistaken for a practice id.
    assert _extract_practice_value_from_description("10007 Farm to Market Rd\nHumble") == ""


def test_practice_value_office_number_without_dash_end_to_end():
    body = (
        "Title #20311\n"
        "Loc, ST\n"
        "Practice\n"
        "4412 Humble, TX\n"
        "Job Title\n"
        "#20311: Dentistry (Dentist (DMD/DDS))\n"
    )
    out = parse_job_content_txt(body)
    assert (out.get("city") or "").strip() == "Humble"
    assert (out.get("state") or "").strip().upper() == "TX"


def test_parse_city_state_from_location_line():
    body = (
        "Title #1\n"
        "Location: Austin, TX\n"
        "Practice\n"
        "Acme\n"
    )
    out = parse_job_content_txt(body)
    assert (out.get("city") or "").strip().lower() == "austin"
    assert (out.get("state") or "").strip().upper() == "TX"


def test_state_strips_parenthetical_from_state_label():
    body = (
        "Title #1\n"
        "City, ST\n"
        "Practice\n"
        "1 - X, ST\n"
        "...\n\n"
        "--- Description (full text) ---\n"
        "State: IL (Rogers Park)\n"
    )
    out = parse_job_content_txt(body)
    assert (out.get("state") or "").strip().upper() == "IL"


def test_state_strips_parenthetical_from_location_line():
    # practice_value must not override city/state from location_line (use Job Title on line 3).
    body = (
        "Title #1\n"
        "Somewhere, TX (NW Crossing)\n"
        "Practice\n"
        "Job Title\n"
        "#1: x\n"
        "Status\n"
        "Open\n"
    )
    out = parse_job_content_txt(body)
    assert (out.get("state") or "").strip().upper() == "TX"


def test_support_staff_single_number_gets_team_members_suffix():
    body = (
        "Title #1\n"
        "Loc, ST\n"
        "Practice\n"
        "x\n"
        "...\n\n"
        "--- Description (full text) ---\n"
        "Clinical Staff: 7.\n"
    )
    out = parse_job_content_txt(body)
    assert (out.get("support_staff") or "").strip() == "7 team members"


def test_support_staff_single_number_without_period():
    body = (
        "Title #1\n"
        "Loc, ST\n"
        "Practice\n"
        "x\n"
        "...\n\n"
        "--- Description (full text) ---\n"
        "Clinical Staff: 12\n"
    )
    out = parse_job_content_txt(body)
    assert (out.get("support_staff") or "").strip() == "12 team members"


def test_repair_flat_text_inserts_posted_date_for_parser():
    """Simulate DOM-recovered date spliced between Kimedics label lines (see playwright_job_scrape)."""
    fixed = repair_flat_jobpost_text_missing_posted_date(SAMPLE_17967, "10/15/25")
    out = parse_job_content_txt(fixed)
    assert (out.get("posted_date") or "").strip() == "10/15/25"
    assert (out.get("posting_org") or "").strip() == "Aspen Dental"


def test_repair_practice_splices_dom_recovered_value():
    """Job #20038 regression: the sidebar 'Practice' value lives in an <input> (dropped by
    inner_text), and the description used 'Address:' not 'Facility:', so practice_value was
    empty. The DOM-recovery splice must let the sidebar parser read the value."""
    sidebar = "Practice\nJob Title\nPosted Date\nPosting Org\nPriority\nStatus"
    fixed = repair_flat_jobpost_text_missing_practice(sidebar, "4403 - Dublin, GA")
    assert "Practice\n4403 - Dublin, GA" in fixed

    # End-to-end from the real 20038 start: empty practice_value, no Facility: line.
    out = {"practice_value": "", "description_full_text": "Address: 2005 Veterans Blvd D4, Dublin GA"}
    _backfill_practice_value(out, fixed)
    assert out["practice_value"] == "4403 - Dublin, GA"


def test_repair_practice_guards():
    sidebar = "Practice\nJob Title\nPosted Date\nPosting Org"
    # No-op when the recovered value isn't an office-id line.
    assert repair_flat_jobpost_text_missing_practice(sidebar, "Dublin, GA") == sidebar
    assert repair_flat_jobpost_text_missing_practice(sidebar, "") == sidebar
    # Does not overwrite a value already present after the label.
    good = "Practice\n4361 - Rosenberg, TX\nJob Title"
    assert repair_flat_jobpost_text_missing_practice(good, "9999 - Wrong, ZZ") == good


def test_parse_17967_labeled_description_fields():
    out = parse_job_content_txt(SAMPLE_17967)
    assert (out.get("status") or "").strip() == "Closed"
    assert (out.get("city") or "").strip() == "Los Lunas"
    assert (out.get("state") or "").strip() == "NM"
    assert (out.get("address_line") or "").strip() == "1700 Main Street SW, Los Lunas, NM"
    assert "csr required" in (out.get("insight") or "").lower()
    assert "nov 3-7" in (out.get("dates_needed") or "").lower()
    assert "8a-5p" in (out.get("standard_schedule") or "").lower()
    assert "surgical extractions" in (out.get("required_procedures") or "").lower()
    assert "limitations" in (out.get("additional_requirements") or "").lower()
    assert "da" in (out.get("support_staff") or "").lower()


def test_combined_types_of_cases_joins_procedures_and_additional():
    row = parse_job_content_txt(SAMPLE_17967)
    bundle = combined_types_of_cases({k: str(v) for k, v in row.items()})
    assert "\n" in bundle
    assert "surgical extractions" in bundle.lower()
    assert "limitations" in bundle.lower()


def test_active_needs_are_overrides_dates_needed_including_labeled_dates_line():
    """Active needs clause wins over Dates: labeled line and prior header-derived values."""
    body = (
        "Title #1\n"
        "Loc, ST\n"
        "Practice\n"
        "x\n"
        "...\n\n"
        "--- Description (full text) ---\n"
        "4/8 update: May closed. Active needs are Fridays June 5, 12.\n"
        "Dates: Monday only\n"
    )
    out = parse_job_content_txt(body)
    assert (out.get("dates_needed") or "").strip() == "Fridays June 5, 12."


def test_active_need_is_overrides_dates_needed():
    """Singular 'Active need is …' on the first description line wins like 'Active needs are …'."""
    body = (
        "Title #1\n"
        "Loc, ST\n"
        "Practice\n"
        "x\n"
        "...\n\n"
        "--- Description (full text) ---\n"
        "4/14 pending partial fill. Active need is May 20\n"
        "Dates: Monday only\n"
    )
    out = parse_job_content_txt(body)
    assert (out.get("dates_needed") or "").strip() == "May 20"


def test_standard_schedule_cleared_when_not_hours_like():
    """Block-style 'Schedule' + person name must not populate standard_schedule."""
    body = (
        "Title #1\n"
        "Loc, ST\n"
        "Practice\n"
        "123 - Somewhere, ST\n"
        "...\n\n"
        "--- Description (full text) ---\n"
        "Schedule\n\n"
        "Megan Neubeck-Izaguirre\n"
    )
    out = parse_job_content_txt(body)
    assert (out.get("standard_schedule") or "").strip() == ""


def test_parse_with_pipeline_heuristic_only():
    r = parse_with_pipeline(SAMPLE_17967, run_validate=False, run_fix=False)
    assert r["stages"]["heuristic"] is True
    assert r["stages"]["ai_validate"] is False
    assert r["validation"] is None
    assert r["row"]["job_id"] == "17967"


def test_ui_elements_not_extracted_as_practice():
    """Test that UI elements like 'Sign Out' are not extracted as practice values."""
    # Test case 1: Sign Out on line 3 (the actual issue from job 19656)
    text1 = """New job post from Aspen Dental #19656
Location
Practice
Sign Out
Job Title
Dental Position
Posted Date
2024-04-20
--- Description (full text) ---
Actual job content here."""
    result1 = parse_job_content_txt(text1)
    assert result1["practice_value"] == ""  # Should be empty, not "Sign Out"
    assert result1["job_id"] == "19656"

    # Test case 2: Other UI elements that should be filtered
    ui_elements = ["Log Out", "Settings", "Profile", "Dashboard", "Home", "Menu", "sign out", "SIGN OUT"]
    for ui_element in ui_elements:
        text = f"""Job #12345
Location
Practice
{ui_element}
Job Title
Test Position"""
        result = parse_job_content_txt(text)
        assert result["practice_value"] == "", f"UI element '{ui_element}' should not be extracted as practice"

    # Test case 3: Valid practice should still work
    text3 = """Job #12345
Location
Practice
6313 - Cheektowaga, NY
Job Title
Test Position"""
    result3 = parse_job_content_txt(text3)
    assert result3["practice_value"] == "6313 - Cheektowaga, NY"  # Valid practice value should be extracted

    # Test case 4: Practice name with dental/clinic keywords should work
    text4 = """Job #12345
Location
Practice
Smile Dental Center
Job Title
Test Position"""
    result4 = parse_job_content_txt(text4)
    assert result4["practice_value"] == "Smile Dental Center"  # Valid practice name


# ─────────────────────────────────────────────────────────────────────────────
# Reconciliation against Salesforce practice map (job #19703-style scenarios).
# ─────────────────────────────────────────────────────────────────────────────


# Build a minimal map shaped like the one assembled in
# sf_job_supabase_resolve / playwright_job_scrape: { practice_key: {sf_job_id} }.
def _make_sf_map(*pairs):
    from utils.sf_practice_key import practice_key as _pk
    m: dict = {}
    for raw, sfid in pairs:
        m.setdefault(_pk(raw), set()).add(sfid)
    return m


# Simulates the Kimedics web body_text: sidebar shows the *correct* practice
# value, JD body shows the *typo* version. Without the SF map, today's heuristic
# may pick either one (depending on which line lands first); with the map, we
# pick the one that has a 1:1 SF hit.
JOB_19703_BODY = """Dentistry (Dentist (DMD/DDS)) (#19703)
GEORGETOWN, KY
Practice
2419 - Georgetown, KY
Job Title
#19703: Dentistry (Dentist (DMD/DDS))
Posted Date
04/28/26
Posting Org
Aspen Dental
Priority
Normal
Status
Active, accepting new providers
Full Job Post
Description
*Must have DEA with all schedules

419 - Georgetown, KY
Address: 450 CONNECTOR RD, GEORGETOWN KY

Dates: May 1-2
Hours: 8a-1p

--- Description (full text) ---
*Must have DEA with all schedules

419 - Georgetown, KY
Address: 450 CONNECTOR RD, GEORGETOWN KY
"""


def test_reconcile_19703_picks_sidebar_when_sf_has_2419():
    sf_map = _make_sf_map(("2419 - Georgetown, KY", "a01UP00000realJOB"))
    out = parse_job_content_txt(JOB_19703_BODY, sf_practice_map=sf_map)
    # Sidebar value (correct, matches SF) should win over JD body typo.
    assert out["practice_value"] == "2419 - Georgetown, KY"


def test_reconcile_falls_through_when_neither_candidate_matches_sf():
    # SF doesn't have any matching practice — leave whatever heuristic chose.
    sf_map = _make_sf_map(("9999 - Other Place, TX", "a01UP00000other"))
    out = parse_job_content_txt(JOB_19703_BODY, sf_practice_map=sf_map)
    # Unchanged from heuristic (header line picks 2419 since that's line 3).
    assert out["practice_value"] == "2419 - Georgetown, KY"


def test_reconcile_no_op_when_map_is_none():
    out = parse_job_content_txt(JOB_19703_BODY, sf_practice_map=None)
    # No reconciliation at all — same path as today's calls without the kwarg.
    assert out["practice_value"] == "2419 - Georgetown, KY"


def test_reconcile_picks_typo_candidate_when_sf_only_has_typo():
    # Pathological: SF actually has the 3-digit version (rare; client typo on
    # both sides). We pick the 1:1 SF match regardless of "trustworthiness".
    sf_map = _make_sf_map(("419 - Georgetown, KY", "a01UP00000typo"))
    # Use a body where the heuristic would pick "2419" (header line 3).
    out = parse_job_content_txt(JOB_19703_BODY, sf_practice_map=sf_map)
    assert out["practice_value"] == "419 - Georgetown, KY"


def test_reconcile_skips_ambiguous_sf_matches():
    # If the candidate hits >1 SF jobs, we don't pick it; fall through to next.
    sf_map = _make_sf_map(
        ("2419 - Georgetown, KY", "a01UP00000a"),
        ("2419 - Georgetown, KY", "a01UP00000b"),  # 2 hits → ambiguous
        ("419 - Georgetown, KY",  "a01UP00000c"),  # 1:1
    )
    out = parse_job_content_txt(JOB_19703_BODY, sf_practice_map=sf_map)
    assert out["practice_value"] == "419 - Georgetown, KY"


def test_reconcile_backward_compat_existing_call_signature():
    # Existing callers use parse_job_content_txt(text). New optional arg must
    # default to None and not change behavior.
    out = parse_job_content_txt(JOB_19703_BODY)
    assert "practice_value" in out  # still returns the dict, no crash


def test_extract_practice_value_from_sidebar_only_takes_well_formed_lines():
    from utils.job_content_parser import _extract_practice_value_from_sidebar

    # Standard case.
    body = "Practice\n2419 - Georgetown, KY\nJob Title\n#19703"
    assert _extract_practice_value_from_sidebar(body) == "2419 - Georgetown, KY"

    # No "Practice" label.
    assert _extract_practice_value_from_sidebar("foo\nbar") == ""

    # "Practice" followed by another label (no value present).
    body2 = "Practice\nJob Title\n#19703"
    assert _extract_practice_value_from_sidebar(body2) == ""

    # "Practice" followed by something that doesn't look like a clinic id.
    body3 = "Practice\nSign Out"
    assert _extract_practice_value_from_sidebar(body3) == ""

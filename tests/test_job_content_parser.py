"""Unit tests for job_content_parser description sections and city/state."""

from utils.job_content_ai import combined_types_of_cases, parse_with_pipeline
from utils.job_content_parser import parse_job_content_txt, repair_flat_jobpost_text_missing_posted_date

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

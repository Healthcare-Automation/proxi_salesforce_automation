"""Excluding a date that sits inside a range.

Dropping the pending 7/17 from "July 13-17" yields "July 13-16", which appears nowhere
in the post — the verbatim anti-hallucination guard rejected the one correct answer while
accepting the untouched list, so job 20084 shipped 7/17 as an active date.
"""

import json

import pytest

from utils.job_description_ai import _validate_ai_dates, expand_date_tokens

# Real post text for Kimedics job 20084.
POST_20084 = """7/17 pending confirmation

4096 - Statesville, NC
Address: 964 GLENWAY DR, STATESVILLE NC

*Aspen exp preferred
Dates: July 13-17, 20-22, 24, 27-29, 31.
Hours: Mon 8-6 Tue-Thurs 7-5 Fri 7-12
"""
STRUCTURED_20084 = "July 13-17, 20-22, 24, 27-29, 31."


def ai(dates, confidence=100, override=True):
    return json.dumps(
        {"override": override, "dates": dates, "confidence": confidence, "reason": ""}
    )


# ── expand_date_tokens ───────────────────────────────────────────────────────


def test_expand_ranges_and_standalone_days():
    assert expand_date_tokens("July 13-17, 20-22, 24") == {
        (7, 13), (7, 14), (7, 15), (7, 16), (7, 17),
        (7, 20), (7, 21), (7, 22), (7, 24),
    }


def test_expand_carries_month_across_commas():
    """"July 2, Aug 10-14, 17" — the trailing 17 belongs to August, not July."""
    assert expand_date_tokens("July 2, Aug 10-14, 17") == {
        (7, 2), (8, 10), (8, 11), (8, 12), (8, 13), (8, 14), (8, 17),
    }


def test_expand_handles_slash_dates_and_no_month():
    assert expand_date_tokens("7/17") == {(7, 17)}
    assert expand_date_tokens("13-17") == set()  # no month → nothing to trust
    assert expand_date_tokens("") == set()


# ── the guard ────────────────────────────────────────────────────────────────


def test_narrowed_range_is_accepted():
    """The correct 20084 answer. Regression: this used to raise."""
    res = _validate_ai_dates(
        ai("July 13-16, 20-22, 24, 27-29, 31"), POST_20084, STRUCTURED_20084
    )
    assert res.dates == "July 13-16, 20-22, 24, 27-29, 31"


def test_split_range_is_accepted():
    """Removing a day from the middle splits the range."""
    res = _validate_ai_dates(
        ai("July 13-14, 16-17, 20-22, 24, 27-29, 31"), POST_20084, STRUCTURED_20084
    )
    assert res.dates.startswith("July 13-14, 16-17")


def test_invented_month_still_rejected():
    with pytest.raises(ValueError):
        _validate_ai_dates(ai("August 5"), POST_20084, STRUCTURED_20084)


def test_invented_day_outside_structured_still_rejected():
    """July 18 is not in the structured list, so it cannot be a narrowing of it."""
    with pytest.raises(ValueError):
        _validate_ai_dates(
            ai("July 13-18, 20-22, 24, 27-29, 31"), POST_20084, STRUCTURED_20084
        )


def test_verbatim_dates_still_accepted_without_structured_list():
    """The fast path must keep working when there is no structured list to subset."""
    res = _validate_ai_dates(ai("July 13-17"), POST_20084, "")
    assert res.dates == "July 13-17"


def test_no_structured_list_cannot_smuggle_a_narrowed_range():
    with pytest.raises(ValueError):
        _validate_ai_dates(ai("July 13-16"), POST_20084, "")


def test_worded_date_stamp_outside_structured_is_rejected():
    """"6/29 date reopened" is when the note was written, not a needed date. The prompt
    is what keeps the model from returning it; the guard is the backstop for the worded
    form. Note it cannot catch the literal "6/29", which is verbatim in the post."""
    post = "6/29 date reopened\n\nFacility: 2481 - Pikeville, KY\nDates: July 20\n"
    with pytest.raises(ValueError):
        _validate_ai_dates(ai("June 29"), post, "July 20")


def test_override_false_returns_no_change():
    res = _validate_ai_dates(ai("", override=False), POST_20084, STRUCTURED_20084)
    assert res.dates is None


def test_confidence_is_preserved_on_narrowed_range():
    res = _validate_ai_dates(
        ai("July 13-16, 20-22, 24, 27-29, 31", confidence=90), POST_20084, STRUCTURED_20084
    )
    assert res.confidence == 90

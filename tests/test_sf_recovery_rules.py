"""Tests for utils.sf_recovery_rules — heuristics that flag parser-contaminated values."""

import sys
from pathlib import Path

_src = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(_src))

from utils.sf_recovery_rules import (  # noqa: E402
    adjacent_empty,
    evaluate,
    exceeds_length,
    starts_with_noise_marker,
    value_appears_in_sibling,
)


def test_starts_with_asterisks():
    assert starts_with_noise_marker("***Additional requirements/ info: ...") == "starts_with:'***'"
    assert starts_with_noise_marker("3-4 NP, 6-7 EP") is None
    assert starts_with_noise_marker("") is None
    assert starts_with_noise_marker(None) is None


def test_starts_with_additional_prefix():
    assert starts_with_noise_marker("Additional requirements/ info: extractions") is not None


def test_value_appears_in_sibling_fires():
    # Real 19664 case: Volume text bled in from Insight
    vol = "***Additional requirements/ info: extractions could include simple surgical"
    siblings = {
        "Insight__c": "*Must have active TX DEA. Additional requirements/ info: extractions could include simple surgical procedures"
    }
    assert value_appears_in_sibling(vol, "Job_Volume__c", siblings) == "appears_in:Insight__c"


def test_value_appears_in_sibling_negative():
    assert value_appears_in_sibling("3-4 NP", "Job_Volume__c", {"Insight__c": "must have DEA"}) is None


def test_exceeds_length():
    assert exceeds_length("abcdef", 5) == "exceeds_length:5"
    assert exceeds_length("abc", 5) is None
    assert exceeds_length("anything", None) is None


def test_adjacent_empty():
    assert adjacent_empty(
        "Job_Volume__c",
        {"Job_Support_Staff__c": "", "Job_Types_of_Cases__c": ""},
        ("Job_Support_Staff__c", "Job_Types_of_Cases__c"),
    ) is not None
    assert adjacent_empty(
        "Job_Volume__c",
        {"Job_Support_Staff__c": "3 DA", "Job_Types_of_Cases__c": ""},
        ("Job_Support_Staff__c", "Job_Types_of_Cases__c"),
    ) is None


def test_evaluate_19664_case():
    """The actual 19664 production case."""
    bad = "***Additional requirements/ info: extractions could include simple/ surgical"
    siblings = {
        "Insight__c": "*Must have TX DEA. Additional requirements/ info: extractions could include simple/ surgical procedures",
        "Job_Support_Staff__c": "3 DA, 1 RDH",
    }
    got = evaluate(field="Job_Volume__c", value=bad, siblings=siblings, max_length=50)
    # Any of the heuristics firing is acceptable — the first one short-circuits.
    # In this case ``***`` prefix will fire first.
    assert got is not None
    assert got.startswith("starts_with:")


def test_evaluate_clean_value_returns_none():
    """A normal valid value should not fire any heuristic."""
    got = evaluate(
        field="Job_Volume__c",
        value="3-4 NP, 6-7 EP",
        siblings={"Insight__c": "Has DEA"},
        max_length=50,
    )
    assert got is None

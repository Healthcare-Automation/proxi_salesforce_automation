"""Tests for utils.sf_error_classify — the SF error parser used by the recovery engine."""

import sys
from pathlib import Path

_src = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(_src))

from utils.sf_error_classify import (  # noqa: E402
    build_label_to_api_from_describe,
    classify_sf_error,
)


def test_volume_too_large_job_19664():
    """The real 19664 error from the production log."""
    err = (
        "Salesforce REST PATCH HTTP 400: Volume: data value too large: "
        "***Additional requirements/ info: extractions could include simple/ surgical/ "
        "full mouth- please notate any limitations in presentation (max length=50)"
    )
    labels = {"Volume": "Job_Volume__c"}
    c = classify_sf_error(err, label_to_api=labels)
    assert c.error_class == "too_large"
    assert c.offending_fields == ("Job_Volume__c",)
    assert c.max_length == 50


def test_required_missing_job_ranking():
    err = "Salesforce REST PATCH HTTP 400: Required fields are missing: [Job_Ranking__c]"
    c = classify_sf_error(err)
    assert c.error_class == "required_missing"
    assert c.offending_fields == ("Job_Ranking__c",)


def test_required_missing_multiple():
    err = "Required fields are missing: [Job_Ranking__c, Job_Status__c]"
    c = classify_sf_error(err)
    assert c.error_class == "required_missing"
    assert c.offending_fields == ("Job_Ranking__c", "Job_Status__c")


def test_bad_picklist():
    err = "INVALID_OR_NULL_FOR_RESTRICTED_PICKLIST:bad value for restricted picklist field: Job_Status__c"
    c = classify_sf_error(err)
    assert c.error_class == "bad_picklist"
    assert c.offending_fields == ("Job_Status__c",)


def test_worksite_deleted():
    err = "INVALID_CROSS_REFERENCE_KEY: entity is deleted: [Job_Worksite_Location_1__c]"
    c = classify_sf_error(err)
    assert c.error_class == "worksite_deleted"
    assert c.offending_fields == ("Job_Worksite_Location_1__c",)


def test_transient_dns():
    err = "<urlopen error [Errno -2] Name or service not known>"
    c = classify_sf_error(err)
    assert c.error_class == "transient"


def test_transient_http_5xx():
    for status in ("HTTP 500", "HTTP 502", "HTTP 503", "HTTP 504"):
        c = classify_sf_error(f"Upstream failed: {status} Server Error")
        assert c.error_class == "transient", status


def test_transient_session_expired():
    c = classify_sf_error("INVALID_SESSION_ID: Session expired")
    assert c.error_class == "transient"


def test_unknown():
    c = classify_sf_error("Some totally unexpected non-classified failure")
    assert c.error_class == "unknown"
    assert c.offending_fields == ()


def test_build_label_to_api_from_describe():
    desc = {
        "fields": [
            {"name": "Job_Volume__c", "label": "Volume"},
            {"name": "Job_Ranking__c", "label": "Ranking"},
        ]
    }
    m = build_label_to_api_from_describe(desc)
    assert m == {"Volume": "Job_Volume__c", "Ranking": "Job_Ranking__c"}


def test_too_large_with_nested_parens_in_bad_value():
    """
    Regression: the bad value itself may contain ``(...)`` before the final
    ``(max length=N)``. The classifier must still extract the offending field.
    Real case from production: Insight text included ``(no flights/rental car)``.
    """
    err = (
        "Salesforce REST PATCH HTTP 400: Insight: data value too large: "
        "*Must have active TX DEA<br>*Previous Aspen required<br>"
        "*Local provider (no flights/rental car)<br>*extractions could include "
        "simple/ surgical/ full mouth (max length=255)"
    )
    c = classify_sf_error(err, label_to_api={"Insight": "Insight__c"})
    assert c.error_class == "too_large"
    assert c.offending_fields == ("Insight__c",)
    assert c.max_length == 255


def test_label_heuristic_fallback_when_describe_missing():
    """When label_to_api lacks the SF label, the 'Job_' prefix / '__c' suffix heuristic should still resolve it."""
    err = "Salesforce REST PATCH HTTP 400: Volume: data value too large: bad (max length=50)"
    labels = {"Job_Volume__c": "Job_Volume__c"}  # keyed by API name, not by label
    c = classify_sf_error(err, label_to_api=labels)
    assert c.offending_fields == ("Job_Volume__c",)

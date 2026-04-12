"""Insight parsing and deduplication for Salesforce Insight__c."""

from utils.insight_sanitize import (
    dedupe_insight_items,
    parse_insight_bullet_items,
    sanitize_insight_for_salesforce,
)


def test_parse_inline_star_segments():
    raw = (
        "*Must have active AR DEA with all schedules **Prefer one provider for both dates "
        "*Must have active DEA with all schedules"
    )
    items = parse_insight_bullet_items(raw)
    assert len(items) == 3
    assert "Prefer one provider for both dates" in items[1]


def test_dedupe_exact_repeat():
    items = ["Must have active DEA with all schedules", "Must have active DEA with all schedules"]
    assert dedupe_insight_items(items) == ["Must have active DEA with all schedules"]


def test_sanitize_insight_multiline():
    raw = "*First note\n*Second note\n*First note"
    out = sanitize_insight_for_salesforce(raw)
    assert out is not None
    assert out.count("*") == 2
    assert "First note" in out
    assert "Second note" in out


def test_sanitize_empty():
    assert sanitize_insight_for_salesforce("") is None
    assert sanitize_insight_for_salesforce("   ") is None

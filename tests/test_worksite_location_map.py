"""Location key normalization for sf_worksite_location_map."""

from utils.supabase_db import _normalize_location_key


def test_normalize_location_key_case_and_space():
    assert _normalize_location_key(" Wichita ", " ks ") == "wichita|KS"
    assert _normalize_location_key("New York", "NY") == "new york|NY"


def test_normalize_location_key_requires_some_location():
    assert _normalize_location_key("", "") == ""

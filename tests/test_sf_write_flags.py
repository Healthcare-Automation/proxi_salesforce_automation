"""Tests for utils.sf_write_flags.proxi_sf_writes_enabled."""

import pytest

from utils.sf_write_flags import proxi_sf_writes_enabled


def test_unset_defaults_true(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("PROXI_SF_UPDATE_JOBS", raising=False)
    assert proxi_sf_writes_enabled() is True


def test_empty_string_defaults_true(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("PROXI_SF_UPDATE_JOBS", "")
    assert proxi_sf_writes_enabled() is True


@pytest.mark.parametrize(
    "value, expected",
    [
        ("true", True),
        ("TRUE", True),
        ("1", True),
        ("yes", True),
        ("false", False),
        ("FALSE", False),
        ("0", False),
        ("no", False),
        ("off", False),
    ],
)
def test_explicit_values(monkeypatch: pytest.MonkeyPatch, value: str, expected: bool):
    monkeypatch.setenv("PROXI_SF_UPDATE_JOBS", value)
    assert proxi_sf_writes_enabled() is expected

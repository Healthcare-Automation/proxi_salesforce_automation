"""
Active-dates override (AI + regex fallback) — covers the job #19920 regression
where SF ``Job_Dates_Needed__c`` must reflect the top-line "Dates added" need,
not the full structured ``Dates:`` list.
"""
import os

import pytest

import utils.job_description_ai as ai
import utils.job_description_proxi_template as tpl


# Real #19920 (6/12) post text — top line is the active "Dates added" need.
DESC_DATES_ADDED = (
    "6/12 Dates added: June 29-30, July 1-2, 6-10, 13-17, 20-21.\n\n"
    "*Must have Active MO DEA with all schedules and CSR at time of submission\n"
    "3185 - St. Joseph, MO\n"
    "Dates: June 8-9, 17-19, 26, 29-30, July 1-2, 6-10, 13-17, 20-21.\n"
    "Hours: Mon-Thurs 7:15a-5p, Fri 745a-1p"
)
ADDED_DATES = "June 29-30, July 1-2, 6-10, 13-17, 20-21"

# Real #19920 (6/5) post text — top line is an "Active need" clause.
DESC_ACTIVE_NEED = (
    "6/5 update. June 8-9 have been cancelled. Partial fill. Active need is June 26\n"
    "3185 - St. Joseph, MO\n"
    "Dates: June 8-9, 17-19, 26"
)

DESC_NO_OVERRIDE = (
    "3185 - St. Joseph, MO\n"
    "Address: 5101 N BELT HWY, SAINT JOSEPH MO\n"
    "Dates: June 8-9, 17-19, 26\n"
    "Hours: Mon-Thurs 7:15a-5p"
)


@pytest.fixture(autouse=True)
def _clear_cache():
    tpl._ACTIVE_DATES_OVERRIDE_CACHE.clear()
    yield
    tpl._ACTIVE_DATES_OVERRIDE_CACHE.clear()


# ── gate ─────────────────────────────────────────────────────────────────────
def test_gate_triggers_on_date_top_line():
    assert ai._has_top_line_date_signal(DESC_DATES_ADDED)
    assert ai._has_top_line_date_signal(DESC_ACTIVE_NEED)


def test_gate_skips_when_no_date_in_top_lines():
    assert not ai._has_top_line_date_signal(
        "*Must have Active MO DEA with all schedules\n**Aspen exp preferred\nClinical Staff: 3 DA, 1 RDH"
    )


# ── _validate_ai_dates (pure) ────────────────────────────────────────────────
def test_validate_returns_verbatim_override():
    raw = '{"override": true, "dates": "June 29-30, July 1-2, 6-10, 13-17, 20-21"}'
    assert ai._validate_ai_dates(raw, DESC_DATES_ADDED) == ADDED_DATES


def test_validate_strips_code_fence_and_trailing_period():
    raw = '```json\n{"override": true, "dates": "June 29-30, July 1-2, 6-10, 13-17, 20-21."}\n```'
    assert ai._validate_ai_dates(raw, DESC_DATES_ADDED) == ADDED_DATES


def test_validate_no_override_returns_none():
    assert ai._validate_ai_dates('{"override": false, "dates": ""}', DESC_DATES_ADDED) is None


def test_validate_rejects_hallucinated_dates():
    # Model invents dates not present in the post → must raise (→ regex fallback).
    with pytest.raises(ValueError):
        ai._validate_ai_dates('{"override": true, "dates": "December 25"}', DESC_DATES_ADDED)


def test_validate_rejects_malformed_json():
    with pytest.raises(ValueError):
        ai._validate_ai_dates("not json at all", DESC_DATES_ADDED)


# ── ai_active_dates_override end-to-end with a stubbed OpenAI client ──────────
def _stub_openai(monkeypatch, output_text):
    import openai

    class _Resp:
        pass

    class _Responses:
        def create(self, **_kw):
            r = _Resp()
            r.output_text = output_text
            return r

    class _Client:
        def __init__(self, **_kw):
            self.responses = _Responses()

    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setattr(openai, "OpenAI", _Client)


def test_ai_override_on_dates_added(monkeypatch):
    _stub_openai(monkeypatch, '{"override": true, "dates": "June 29-30, July 1-2, 6-10, 13-17, 20-21"}')
    assert ai.ai_active_dates_override(DESC_DATES_ADDED) == ADDED_DATES


def test_ai_returns_none_when_gate_fails(monkeypatch):
    # No date in the top lines → never calls the model, returns None.
    _stub_openai(monkeypatch, '{"override": true, "dates": "whatever"}')
    assert ai.ai_active_dates_override("Requirements only\nNo dates here") is None


# ── active_dates_override + effective_dates_needed wiring ─────────────────────
def test_override_passthrough_when_ai_succeeds(monkeypatch):
    monkeypatch.setattr(ai, "ai_active_dates_override", lambda t: ADDED_DATES)
    assert tpl.active_dates_override(DESC_DATES_ADDED) == ADDED_DATES


def test_falls_back_to_regex_when_ai_unavailable(monkeypatch):
    def _boom(_t):
        raise RuntimeError("no AI")

    monkeypatch.setattr(ai, "ai_active_dates_override", _boom)
    # Regex still catches the "Active need is" clause.
    assert tpl.active_dates_override(DESC_ACTIVE_NEED) == "June 26"


def test_effective_dates_needed_prefers_override(monkeypatch):
    monkeypatch.setattr(ai, "ai_active_dates_override", lambda t: ADDED_DATES)
    row = {"description_full_text": DESC_DATES_ADDED, "dates_needed": "June 8-9, 17-19, 26, 29-30, July 1-2"}
    assert tpl.effective_dates_needed(row) == ADDED_DATES


def test_effective_dates_needed_uses_structured_when_no_override(monkeypatch):
    monkeypatch.setattr(ai, "ai_active_dates_override", lambda t: None)
    row = {"description_full_text": DESC_NO_OVERRIDE, "dates_needed": "June 8-9, 17-19, 26"}
    assert tpl.effective_dates_needed(row) == "June 8-9, 17-19, 26"


def test_cancellation_is_not_an_override_when_ai_says_false(monkeypatch):
    # Pure-cancellation line ("cancelled the need for June 11/12") must NOT set those
    # dates as the need — the model returns override=false, so we fall through.
    monkeypatch.setattr(ai, "ai_active_dates_override", lambda t: None)
    txt = "6/10 the office has cancelled the need for June 11/12\nDates: June 1-2, 11-12, 15-19, 22."
    assert tpl.active_dates_override(txt) is None


# ── Live prompt checks (skipped in CI; run when OPENAI_API_KEY is present) ────
@pytest.mark.skipif(not os.environ.get("OPENAI_API_KEY"), reason="needs live OpenAI")
def test_live_dates_added_overrides():
    txt = ("6/12 Dates added: June 29-30, July 1-2, 6-10, 13-17, 20-21.\n"
           "Dates: June 8-9, 17-19, 26, 29-30, July 1-2, 6-10, 13-17, 20-21.")
    assert ai.ai_active_dates_override(txt) == ADDED_DATES


@pytest.mark.skipif(not os.environ.get("OPENAI_API_KEY"), reason="needs live OpenAI")
def test_live_pure_cancellation_does_not_override():
    txt = "6/10 the office has cancelled the need for June 11/12\nDates: June 1-2, 11-12, 15-19, 22."
    assert ai.ai_active_dates_override(txt) is None


@pytest.mark.skipif(not os.environ.get("OPENAI_API_KEY"), reason="needs live OpenAI")
def test_live_cancellation_with_restated_need_uses_need():
    txt = "6/5 update. June 8-9 have been cancelled. Partial fill. Active need is June 26\nDates: June 8-9, 17-19, 26"
    assert ai.ai_active_dates_override(txt) == "June 26"

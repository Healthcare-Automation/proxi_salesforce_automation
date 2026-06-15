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


@pytest.fixture
def live_ai(monkeypatch):
    """Opt back into the real OpenAI call for live-prompt checks: load the key from
    .env (the autouse conftest fixture removes it for determinism). Skips if absent."""
    from pathlib import Path

    env = Path(__file__).resolve().parent.parent / ".env"
    key = ""
    if env.exists():
        for line in env.read_text().splitlines():
            if line.strip().startswith("OPENAI_API_KEY="):
                key = line.split("=", 1)[1].strip().strip('"').strip("'")
                break
    if not key:
        pytest.skip("no OPENAI_API_KEY in .env")
    monkeypatch.setenv("OPENAI_API_KEY", key)


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
    monkeypatch.setattr(ai, "ai_active_dates_override", lambda t, s=None: ADDED_DATES)
    assert tpl.active_dates_override(DESC_DATES_ADDED) == ADDED_DATES


def test_falls_back_to_regex_when_ai_unavailable(monkeypatch):
    def _boom(_t, _s=None):
        raise RuntimeError("no AI")

    monkeypatch.setattr(ai, "ai_active_dates_override", _boom)
    # Regex still catches the "Active need is" clause.
    assert tpl.active_dates_override(DESC_ACTIVE_NEED) == "June 26"


def test_effective_dates_needed_prefers_override(monkeypatch):
    monkeypatch.setattr(ai, "ai_active_dates_override", lambda t, s=None: ADDED_DATES)
    row = {"description_full_text": DESC_DATES_ADDED, "dates_needed": "June 8-9, 17-19, 26, 29-30, July 1-2"}
    assert tpl.effective_dates_needed(row) == ADDED_DATES


def test_effective_dates_needed_uses_structured_when_no_override(monkeypatch):
    monkeypatch.setattr(ai, "ai_active_dates_override", lambda t, s=None: None)
    row = {"description_full_text": DESC_NO_OVERRIDE, "dates_needed": "June 8-9, 17-19, 26"}
    assert tpl.effective_dates_needed(row) == "June 8-9, 17-19, 26"


def test_validate_allows_cancellation_subtraction():
    # Subtraction result is not a single substring, but every piece is in the source.
    raw = '{"override": true, "dates": "June 1-2, 15-19, 22"}'
    structured = "June 1-2, 11-12, 15-19, 22."
    desc = "6/10 the office cancelled the need for June 11/12"
    assert ai._validate_ai_dates(raw, desc, structured) == "June 1-2, 15-19, 22"


def test_cancellation_passes_through_when_ai_subtracts(monkeypatch):
    # AI removes the cancelled dates → effective_dates_needed returns the trimmed list.
    monkeypatch.setattr(ai, "ai_active_dates_override", lambda t, s=None: "June 1-2, 15-19, 22")
    row = {"description_full_text": "6/10 office cancelled June 11/12",
           "dates_needed": "June 1-2, 11-12, 15-19, 22."}
    assert tpl.effective_dates_needed(row) == "June 1-2, 15-19, 22"


# ── Live prompt checks (skipped in CI; run when OPENAI_API_KEY is present) ────
def test_live_dates_added_overrides(live_ai):
    txt = ("6/12 Dates added: June 29-30, July 1-2, 6-10, 13-17, 20-21.\n"
           "Dates: June 8-9, 17-19, 26, 29-30, July 1-2, 6-10, 13-17, 20-21.")
    assert ai.ai_active_dates_override(txt, "June 8-9, 17-19, 26, 29-30, July 1-2, 6-10, 13-17, 20-21") == ADDED_DATES


def test_live_cancellation_subtracts_cancelled_dates(live_ai):
    txt = "6/10 the office has cancelled the need for June 11/12\nDates: June 1-2, 11-12, 15-19, 22."
    assert ai.ai_active_dates_override(txt, "June 1-2, 11-12, 15-19, 22") == "June 1-2, 15-19, 22"


def test_live_cancellation_with_restated_need_uses_need(live_ai):
    txt = "6/5 update. June 8-9 have been cancelled. Partial fill. Active need is June 26\nDates: June 8-9, 17-19, 26"
    assert ai.ai_active_dates_override(txt, "June 8-9, 17-19, 26") == "June 26"

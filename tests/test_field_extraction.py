"""
AI field-extraction safety-net — recovers description fields the deterministic
parser missed (label with no colon like "Dates July 20-22", misspelled label),
gated so well-formed posts make zero AI calls.
"""
import pytest

import utils.job_description_ai as ai
import utils.job_content_parser as jcp

DESC_NO_COLON = (
    "Facility: 6438 - Fairview Park, OH\n"
    "Dates July 20-22, 28-29\n"
    "Hours: 8a-5p 1 hr unpaid lunch\n"
    "Clinical Staff: 1 HYG, 3 DAs"
)


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


# ── ai_extract_description_fields ────────────────────────────────────────────
def test_extract_recovers_no_colon_dates(monkeypatch):
    _stub_openai(monkeypatch, '{"dates_needed": "July 20-22, 28-29", "dates_confidence": 100}')
    assert ai.ai_extract_description_fields(DESC_NO_COLON, ["dates_needed"]).fields == {"dates_needed": "July 20-22, 28-29"}


def test_extract_drops_hallucinated_value(monkeypatch):
    # "December 25" is not in the post → dropped (anti-hallucination).
    _stub_openai(monkeypatch, '{"dates_needed": "December 25"}')
    assert ai.ai_extract_description_fields(DESC_NO_COLON, ["dates_needed"]).fields == {}


def test_extract_ignores_unknown_fields(monkeypatch):
    _stub_openai(monkeypatch, '{"made_up": "x"}')
    assert ai.ai_extract_description_fields(DESC_NO_COLON, ["made_up_field"]).fields == {}


def test_extract_carries_low_confidence(monkeypatch):
    # Ambiguous date recovered → confidence < 100 flows through for review.
    _stub_openai(monkeypatch, '{"dates_needed": "July 20-22, 28-29", "dates_confidence": 55, "dates_reason": "in notes"}')
    res = ai.ai_extract_description_fields(DESC_NO_COLON, ["dates_needed"])
    assert res.fields.get("dates_needed") and res.dates_confidence == 55 and res.dates_reason == "in notes"


def test_extract_confidence_forced_100_when_no_dates(monkeypatch):
    # If dates weren't recovered, a stray low confidence is ignored (nothing to flag).
    _stub_openai(monkeypatch, '{"dates_needed": "December 25", "dates_confidence": 20}')
    res = ai.ai_extract_description_fields(DESC_NO_COLON, ["dates_needed"])  # Dec 25 dropped
    assert res.fields == {} and res.dates_confidence == 100


def test_extract_raises_without_key(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(RuntimeError):
        ai.ai_extract_description_fields(DESC_NO_COLON, ["dates_needed"])


# ── parser gate: 0 tokens for well-formed posts ──────────────────────────────
def test_parser_gate_skips_ai_when_structured_fields_present(monkeypatch):
    calls = {"n": 0}

    def fake(*a, **k):
        calls["n"] += 1
        return ai.FieldExtraction({}, 100, "")

    monkeypatch.setattr(ai, "ai_extract_description_fields", fake)
    out = {"description_full_text": "no date signal here",
           "dates_needed": "July 1-2", "standard_schedule": "8a-5p"}
    jcp._fill_from_description_blocks(out)
    assert calls["n"] == 0


def test_parser_gate_calls_ai_when_dates_missing(monkeypatch):
    calls = {"n": 0}

    def fake(desc, needed, **k):
        calls["n"] += 1
        return ai.FieldExtraction({"dates_needed": "July 20-22, 28-29"}, 100, "")

    monkeypatch.setattr(ai, "ai_extract_description_fields", fake)
    out = {"description_full_text": DESC_NO_COLON}  # dates_needed not pre-filled
    jcp._fill_from_description_blocks(out)
    assert calls["n"] == 1 and out.get("dates_needed") == "July 20-22, 28-29"


def test_parser_flags_low_confidence_dates(monkeypatch):
    monkeypatch.setattr(
        ai, "ai_extract_description_fields",
        lambda *a, **k: ai.FieldExtraction({"dates_needed": "April 7"}, 55, "date in notes"),
    )
    out = {"description_full_text": "Other job post notes: April 7\nHours: 7am-5pm"}
    jcp._fill_from_description_blocks(out)
    assert out.get("dates_needed") == "April 7"
    assert out.get("dates_confidence") == 55 and out.get("dates_reason") == "date in notes"


# ── Live (opt in via .env key) ───────────────────────────────────────────────
@pytest.fixture
def live_ai(monkeypatch):
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


def test_live_recovers_no_colon_dates(live_ai):
    res = ai.ai_extract_description_fields(DESC_NO_COLON, ["dates_needed", "support_staff"])
    assert res.fields.get("dates_needed") == "July 20-22, 28-29"
    assert res.fields.get("support_staff") == "1 HYG, 3 DAs"
    assert res.dates_confidence == 100  # clear Dates label → certain


def test_live_does_not_over_extract_dates_from_notes(live_ai):
    # "April 7" appears only in "Other job post notes" — NOT clearly coverage dates.
    desc = (
        "3333 - Dekalb, IL\nAddress: 2061 SYCAMORE RD\n"
        "Other job post notes: April 7\nHours: 7am-5pm\nClinical Staff: 3 DA, 1 RDH"
    )
    res = ai.ai_extract_description_fields(desc, ["dates_needed"])
    assert "dates_needed" not in res.fields  # must not guess a coverage date from a note

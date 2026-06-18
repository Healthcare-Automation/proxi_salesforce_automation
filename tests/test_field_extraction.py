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
    _stub_openai(monkeypatch, '{"dates_needed": "July 20-22, 28-29"}')
    assert ai.ai_extract_description_fields(DESC_NO_COLON, ["dates_needed"]) == {"dates_needed": "July 20-22, 28-29"}


def test_extract_drops_hallucinated_value(monkeypatch):
    # "December 25" is not in the post → dropped (anti-hallucination).
    _stub_openai(monkeypatch, '{"dates_needed": "December 25"}')
    assert ai.ai_extract_description_fields(DESC_NO_COLON, ["dates_needed"]) == {}


def test_extract_ignores_unknown_fields(monkeypatch):
    _stub_openai(monkeypatch, '{"made_up": "x"}')
    assert ai.ai_extract_description_fields(DESC_NO_COLON, ["made_up_field"]) == {}


def test_extract_raises_without_key(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(RuntimeError):
        ai.ai_extract_description_fields(DESC_NO_COLON, ["dates_needed"])


# ── parser gate: 0 tokens for well-formed posts ──────────────────────────────
def test_parser_gate_skips_ai_when_structured_fields_present(monkeypatch):
    calls = {"n": 0}
    monkeypatch.setattr(ai, "ai_extract_description_fields", lambda *a, **k: calls.__setitem__("n", calls["n"] + 1) or {})
    out = {"description_full_text": "no date signal here",
           "dates_needed": "July 1-2", "standard_schedule": "8a-5p"}
    jcp._fill_from_description_blocks(out)
    assert calls["n"] == 0


def test_parser_gate_calls_ai_when_dates_missing(monkeypatch):
    calls = {"n": 0}

    def fake(desc, needed, **k):
        calls["n"] += 1
        return {"dates_needed": "July 20-22, 28-29"}

    monkeypatch.setattr(ai, "ai_extract_description_fields", fake)
    out = {"description_full_text": DESC_NO_COLON}  # dates_needed not pre-filled
    jcp._fill_from_description_blocks(out)
    assert calls["n"] == 1 and out.get("dates_needed") == "July 20-22, 28-29"


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
    out = ai.ai_extract_description_fields(DESC_NO_COLON, ["dates_needed", "support_staff"])
    assert out.get("dates_needed") == "July 20-22, 28-29"
    assert out.get("support_staff") == "1 HYG, 3 DAs"

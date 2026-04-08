"""
AI-powered fuzzy matcher for Kimedics practice_value → Salesforce Job_Client_Job_Id__c.

Only invoked when deterministic practice_key matching produces zero hits.
Uses gpt-4o-mini via plain urllib — no openai package required.

Strategy (keeps cost near zero)
---------------------------------
1. Extract the numeric facility ID from the Kimedics practice value (e.g. "3185" from
   "3185 - St. Joseph, MO").  This is the strongest identifier and is present in both
   systems.
2. Pre-filter the full SF candidate list to only those whose Job_Client_Job_Id__c starts
   with the same facility number (typically 1-5 records).
3. If 0 pre-filtered candidates → return None immediately (AI can't help).
4. If 1+ candidates → ask gpt-4o-mini to pick the best match given the Kimedics string
   and the small candidate list.  The model only sees a handful of strings, so each call
   is ~150-250 input tokens (~$0.00003).

Failure modes handled
----------------------
- Missing OPENAI_API_KEY  → returns None silently (caller logs mapping_no_match).
- API timeout / HTTP error → returns None (never raises, never blocks pipeline).
- Model returns no confident match → returns None.
- Model returns a value not in the candidate list → returns None (safety check).
"""

from __future__ import annotations

import json
import os
import re
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from typing import NamedTuple, Optional

OPENAI_API_URL = "https://api.openai.com/v1/chat/completions"
MODEL          = "gpt-4o-mini"
TIMEOUT_SECS   = 15

# Accepted confidence levels that we'll act on.
_ACT_ON = {"high", "medium"}


class AIMatchResult(NamedTuple):
    matched_sf_value:  str   # the Job_Client_Job_Id__c string that won
    matched_sf_job_id: str   # the Salesforce Job__c.Id (a015f…)
    confidence:        str   # "high" | "medium"
    candidates_seen:   int   # how many SF candidates were sent to AI


# ── Facility number extraction ────────────────────────────────────────────────

_FACILITY_NUM_RE = re.compile(r"^\s*(\d{3,5})\s*[-\u2013\u2014]")


def _facility_number(val: str) -> Optional[str]:
    """Return the leading 3-5 digit facility ID, or None if absent."""
    m = _FACILITY_NUM_RE.match((val or "").strip())
    return m.group(1) if m else None


def _normalize_for_display(val: str) -> str:
    """Light normalisation for the prompt — fold dashes, collapse spaces."""
    s = unicodedata.normalize("NFKC", (val or "").strip())
    s = re.sub(r"[\u2010-\u2015\u2212\uFE58\uFE63\uFF0D]", "-", s)
    return re.sub(r"\s+", " ", s).strip()


# ── OpenAI call ───────────────────────────────────────────────────────────────

def _call_openai(prompt_messages: list[dict], api_key: str) -> Optional[dict]:
    """POST to OpenAI chat completions. Returns parsed JSON or None on any error."""
    payload = json.dumps({
        "model": MODEL,
        "messages": prompt_messages,
        "temperature": 0,
        "response_format": {"type": "json_object"},
        "max_tokens": 80,
    }).encode("utf-8")
    req = urllib.request.Request(OPENAI_API_URL, data=payload, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Authorization", f"Bearer {api_key}")
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_SECS) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        text = body["choices"][0]["message"]["content"]
        return json.loads(text)
    except Exception as exc:
        print(f"[sf_ai_matcher] OpenAI call failed: {exc}")
        return None


# ── Main public function ───────────────────────────────────────────────────────

def ai_match_practice(
    kimedics_practice: str,
    sf_candidates: list[dict],
) -> Optional[AIMatchResult]:
    """
    Try to fuzzy-match ``kimedics_practice`` against SF Job__c records.

    Parameters
    ----------
    kimedics_practice
        The raw Kimedics ``practice_value``, e.g. ``"3185 - St. Joseph, MO"``.
    sf_candidates
        Full list of SF Job__c records (dicts with at least ``Id`` and
        ``Job_Client_Job_Id__c``).  This is the same list pulled for the
        deterministic resolver — we pre-filter here before calling OpenAI.

    Returns
    -------
    AIMatchResult  if a confident match was found.
    None           if no match, API unavailable, or low confidence.
    """
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not api_key:
        print("[sf_ai_matcher] OPENAI_API_KEY not set — skipping AI match")
        return None

    # ── Step 1: pre-filter by facility number ─────────────────────────────────
    facility = _facility_number(kimedics_practice)
    if not facility:
        print(f"[sf_ai_matcher] No facility number found in {kimedics_practice!r} — skipping")
        return None

    # Build lookup: Job_Client_Job_Id__c → Id (keep only records with a value)
    filtered: list[tuple[str, str]] = []   # (Job_Client_Job_Id__c, Id)
    for rec in sf_candidates:
        sf_val = (rec.get("Job_Client_Job_Id__c") or "").strip()
        sf_id  = (rec.get("Id") or "").strip()
        if not sf_val or not sf_id:
            continue
        # Match if the sf value starts with the same facility number
        sf_fac = _facility_number(sf_val)
        if sf_fac == facility:
            filtered.append((sf_val, sf_id))

    if not filtered:
        print(f"[sf_ai_matcher] No SF candidates share facility #{facility} with {kimedics_practice!r}")
        return None

    # ── Step 2: if only 1 candidate and facility numbers match, ask AI to confirm ─
    kimedics_display  = _normalize_for_display(kimedics_practice)
    candidate_strings = [_normalize_for_display(v) for v, _ in filtered]

    system_msg = (
        "You match Kimedics job location names to Salesforce records. "
        "The same physical location can differ in apostrophes (St. Joseph vs St. Joseph's), "
        "extra suffixes (Suffolk, VA vs Suffolk, VA- Downtown), "
        "punctuation spacing (3185- vs 3185 -), or word order. "
        "Respond ONLY with a JSON object containing exactly two keys: "
        "\"match\" (the best-matching candidate string, or null if no confident match) "
        "and \"confidence\" (\"high\", \"medium\", or \"low\")."
    )

    candidates_formatted = "\n".join(f"  {i+1}. {s}" for i, s in enumerate(candidate_strings))
    user_msg = (
        f"Kimedics value: \"{kimedics_display}\"\n\n"
        f"Salesforce candidates:\n{candidates_formatted}\n\n"
        "Which candidate is the same location? Reply with the exact candidate string or null."
    )

    result = _call_openai(
        [{"role": "system", "content": system_msg},
         {"role": "user",   "content": user_msg}],
        api_key,
    )

    if not result:
        return None

    matched_str = (result.get("match") or "").strip()
    confidence  = (result.get("confidence") or "low").strip().lower()

    if confidence not in _ACT_ON or not matched_str:
        print(
            f"[sf_ai_matcher] Low/no confidence for {kimedics_practice!r}: "
            f"match={matched_str!r} confidence={confidence!r}"
        )
        return None

    # Safety: make sure the model returned something that's actually in our candidate list
    # (compare normalised to tolerate minor whitespace differences)
    matched_norm = re.sub(r"\s+", " ", matched_str.lower().strip())
    for sf_val, sf_id in filtered:
        if re.sub(r"\s+", " ", _normalize_for_display(sf_val).lower().strip()) == matched_norm:
            print(
                f"[sf_ai_matcher] Matched {kimedics_practice!r} → {sf_val!r} "
                f"(confidence={confidence}, candidates={len(filtered)})"
            )
            return AIMatchResult(
                matched_sf_value  = sf_val,
                matched_sf_job_id = sf_id,
                confidence        = confidence,
                candidates_seen   = len(filtered),
            )

    print(
        f"[sf_ai_matcher] Model returned {matched_str!r} which is not in the candidate list — ignoring"
    )
    return None

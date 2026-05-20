"""
Create Salesforce Account (worksite) records when no location mapping exists, and persist mapping in Supabase.

New worksite **Account** POST includes:

- ``Name`` — ``Aspen Dental - {City}, {ST}`` (2-letter state), see ``format_worksite_account_name``
- ``ShippingStreet`` — Kimedics ``address_line`` (normalized); trailing ``City, ST`` matching
  structured city/state is removed so it does not duplicate ``ShippingCity``/``ShippingState``.
- ``ParentId`` — Aspen Dental Management (``SF_ACCOUNT_ASPEN_DENTAL_MANAGEMENT_ID``)
- ``RecordTypeId`` — env ``PROXI_SF_ACCOUNT_WORKSITE_RECORD_TYPE_ID``, else describe ``Worksite`` record type

Job rows receive the new Id on ``Job_Worksite_Location_1__c`` via resolver / enrichment; ``Job_Worksite_1_Address__c``
is set from the same ``address_line`` in ``sf_job_payload``.

Gated by env ``PROXI_SF_CREATE_WORKSITES=true``. Salesforce Account **POST** is also skipped when
``PROXI_SF_UPDATE_JOBS=false`` (see ``utils.sf_write_flags``).
"""

from __future__ import annotations

import json
import os
import re
from typing import Any, Optional

from utils.address_display_format import (
    format_us_address_line_for_display,
    looks_like_real_street,
    strip_redundant_city_state_from_shipping_street,
)
from utils.sf_job_rest_minimal import create_account_record, filter_createable_fields, describe_sobject
from utils.sf_push_defaults import (
    SF_ACCOUNT_ASPEN_DENTAL_MANAGEMENT_ID,
    format_worksite_account_name,
    worksite_account_record_type_id,
)
from utils.sf_write_flags import proxi_sf_writes_enabled
from utils.us_state_expand import state_abbrev_for_job_title, state_name_for_salesforce


# ── Street-address normalization for worksite dedup ──────────────────────────
#
# Salesforce-side legacy data and Kimedics-scraped data use different
# abbreviations of the same address ("10955 Causeway Boulevard" vs
# "10955 Causeway Blvd"). Without folding these into one form, a SOQL exact
# match on ShippingStreet would miss the dup. Comparison rules:
#   - lowercase
#   - expand common street-type / direction abbreviations to a canonical form
#   - drop punctuation, suite/unit suffixes, multi-spaces
#
_STREET_ABBR = [
    (r"\bblvd\.?\b", "boulevard"),
    (r"\bave\.?\b", "avenue"),
    (r"\bst\.?\b", "street"),
    (r"\brd\.?\b", "road"),
    (r"\bpkwy\.?\b", "parkway"),
    (r"\bdr\.?\b", "drive"),
    (r"\bhwy\.?\b", "highway"),
    (r"\bln\.?\b", "lane"),
    (r"\bcir\.?\b", "circle"),
    (r"\bct\.?\b", "court"),
    (r"\bsq\.?\b", "square"),
    (r"\bpl\.?\b", "place"),
    (r"\btrl\.?\b", "trail"),
    (r"\brt\.?\b", "route"),
    (r"\bn\.?\b", "north"),
    (r"\bs\.?\b", "south"),
    (r"\be\.?\b", "east"),
    (r"\bw\.?\b", "west"),
    (r"\bne\.?\b", "northeast"),
    (r"\bnw\.?\b", "northwest"),
    (r"\bse\.?\b", "southeast"),
    (r"\bsw\.?\b", "southwest"),
]
# Suite / unit suffixes — strip when comparing (legacy data often has them, scrape often doesn't).
_SUITE_TAIL = re.compile(
    r"[,\s]+(ste|suite|unit|apt|apartment|#|fl|floor|bldg|building|frnt|front)\b.*$",
    flags=re.IGNORECASE,
)


def _normalize_street_for_match(addr: Optional[str]) -> str:
    s = (addr or "").lower().strip()
    if not s:
        return ""
    s = _SUITE_TAIL.sub("", s)
    for pat, rep in _STREET_ABBR:
        s = re.sub(pat, rep, s)
    # Drop everything that isn't alnum or space, then collapse spaces.
    s = re.sub(r"[^a-z0-9 ]", " ", s)
    s = " ".join(s.split())
    return s


def _street_matches(scrape_street: Optional[str], candidate_street: Optional[str]) -> bool:
    """True if the two addresses agree on the street number AND the first
    significant street-name token after normalization. Conservative — we want
    "10955 Causeway Blvd" ≈ "10955 Causeway Boulevard" (yes) but not
    "100 Main St" ≈ "100 Maple Ave" (no)."""
    a = _normalize_street_for_match(scrape_street)
    b = _normalize_street_for_match(candidate_street)
    if not a or not b:
        return False
    at = a.split()
    bt = b.split()
    # Need at least street_number + one name token to compare.
    if len(at) < 2 or len(bt) < 2:
        return a == b
    # First token must be the street number (or the first 4 chars match — handles "10955" vs "10955-A").
    if at[0] != bt[0] and at[0][:4] != bt[0][:4]:
        return False
    # Require at least one matching name token after the number.
    return any(t in bt[1:] for t in at[1:])


def _env_truthy(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes")


def _account_extra_fields() -> dict[str, Any]:
    raw = (os.environ.get("PROXI_SF_ACCOUNT_CREATE_EXTRA_JSON") or "").strip()
    if not raw:
        return {}
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return obj if isinstance(obj, dict) else {}


def _find_existing_worksite_in_sf(
    instance_url: str,
    access_token: str,
    city: str,
    state: str,
    address_line: Optional[str],
) -> tuple[Optional[dict], list[dict]]:
    """
    Ask Salesforce directly whether an Aspen-Dental worksite Account already
    exists at this (city, state). Returns (chosen, all_candidates).

    Match logic:
      - SOQL filter: same city, state in {abbreviation, full-name}, under the
        Aspen Dental Management parent, not deleted.
      - If 1 candidate → return it.
      - If >1 candidates and we have a scraped street → return the one whose
        normalized ShippingStreet matches (number + first name token).
      - Otherwise → (None, all_candidates) so the caller logs review_required
        and refuses to create a new dup.
    """
    from utils.salesforce import query_all

    c = (city or "").strip()
    st = (state or "").strip()
    if not c or not st:
        return None, []

    abbr = state_abbrev_for_job_title(st) or st.upper()
    full = state_name_for_salesforce(st) or st
    # SOQL-escape any single quotes in city / state values.
    def esc(v: str) -> str:
        return v.replace("\\", "\\\\").replace("'", "\\'")
    parent = SF_ACCOUNT_ASPEN_DENTAL_MANAGEMENT_ID
    soql = (
        "SELECT Id, Name, ShippingStreet, ShippingCity, ShippingState, ParentId "
        "FROM Account "
        f"WHERE ShippingCity = '{esc(c)}' "
        f"AND ShippingState IN ('{esc(abbr)}', '{esc(full)}') "
        f"AND ParentId = '{esc(parent)}' "
        "AND IsDeleted = false"
    )
    try:
        candidates = query_all(instance_url, access_token, soql)
    except Exception:
        return None, []

    if not candidates:
        return None, []
    if len(candidates) == 1:
        return candidates[0], candidates

    # Multiple — try to disambiguate by street.
    if address_line:
        matches = [r for r in candidates if _street_matches(address_line, r.get("ShippingStreet"))]
        if len(matches) == 1:
            return matches[0], candidates
    # Ambiguous: caller will log review_required and refuse to create.
    return None, candidates


def fetch_or_create_worksite_account_id(
    conn,
    city: str,
    state: str,
    *,
    instance_url: str,
    access_token: str,
    address_line: Optional[str] = None,
    schema: str = "public",
    run_id: Optional[int] = None,
    job_id_for_log: Optional[str] = None,
    skip_location_lookup: bool = False,
) -> Optional[str]:
    """
    Return Account Id for (city, state): from ``sf_worksite_location_map``, then
    a direct Salesforce SOQL probe, then create in Salesforce + upsert map.

    Requires non-empty city+state for a stable location_key. Returns None if
    creation disabled or fails.

    **Dedup order (only creates if all three miss):**
      1. ``sf_worksite_location_map`` (city,state) → Account Id. Fast / local.
      2. SOQL ``SELECT … FROM Account WHERE ShippingCity=… AND ShippingState IN (abbr, full) AND ParentId=Aspen``.
         Catches the case where a worksite already exists in Salesforce but our
         local map missed it (legacy data, manual creates, Supabase reset).
         A successful hit also **upserts** the Account into the local map so
         next time it short-circuits on step 1.
      3. POST a new Account. Only reached when SF has zero candidates for
         (city, state) — guarantees we will not create a duplicate worksite as
         long as the new one truly didn't exist anywhere in SF.

    **New Account body:** ``Name``, ``ShippingStreet`` (from ``address_line``), ``ParentId``, ``RecordTypeId``,
    plus optional ``PROXI_SF_ACCOUNT_CREATE_EXTRA_JSON``. Only **createable** fields are sent (describe filter).
    """
    from utils.supabase_db import (
        fetch_worksite_account_id_for_location,
        upsert_worksite_account_id_for_location,
        log_job_event,
    )

    c = (city or "").strip()
    st = (state or "").strip()
    if not c or not st:
        return None

    if not skip_location_lookup:
        existing = fetch_worksite_account_id_for_location(conn, c, st, schema=schema)
        if existing:
            return existing

    # ── Step 2: SOQL probe Salesforce directly. ──
    # This is the fix for the Brandon-FL class of bugs — without it we only
    # consult our local map, which is stale for any worksite the automation
    # didn't create. If we find a match, re-link instead of POSTing.
    sf_match, sf_candidates = _find_existing_worksite_in_sf(
        instance_url, access_token, c, st, address_line,
    )
    if sf_match:
        sf_id = (sf_match.get("Id") or "").strip()
        if sf_id:
            display = (sf_match.get("Name") or "").strip() or format_worksite_account_name(c, st)
            try:
                upsert_worksite_account_id_for_location(
                    conn, c, st,
                    salesforce_account_id=sf_id,
                    display_label=display,
                    source="sf_soql_relink",
                    schema=schema,
                )
            except Exception:
                pass
            if job_id_for_log:
                try:
                    log_job_event(
                        conn,
                        job_id=job_id_for_log,
                        event_type="worksite_relinked",
                        run_id=run_id,
                        schema=schema,
                        payload={
                            "city": c,
                            "state": st,
                            "salesforce_account_id": sf_id,
                            "display_label": display,
                            "source": "sf_soql_relink",
                            "candidate_count": len(sf_candidates),
                            "candidate_street": (sf_match.get("ShippingStreet") or "")[:200],
                            "scrape_street": (address_line or "")[:200],
                        },
                    )
                except Exception:
                    pass
            return sf_id

    # Ambiguous SOQL: multiple candidates and we couldn't pick one by street.
    # Refuse to create — surface for manual review.
    if sf_candidates and not sf_match:
        if job_id_for_log:
            try:
                log_job_event(
                    conn,
                    job_id=job_id_for_log,
                    event_type="worksite_review_required",
                    run_id=run_id,
                    schema=schema,
                    payload={
                        "reason": "ambiguous_existing_worksite",
                        "detail": (
                            f"Refused to create new worksite for {c}, {st}: Salesforce already "
                            f"has {len(sf_candidates)} Aspen Dental worksite Account(s) at this "
                            "city/state and our scraped street didn't uniquely match any of them. "
                            "Operator: pick the right one and add it to sf_worksite_location_map, "
                            "or merge the duplicates in SF first."
                        ),
                        "city": c,
                        "state": st,
                        "scrape_street": (address_line or "")[:200],
                        "candidates": [
                            {
                                "Id": r.get("Id"),
                                "Name": (r.get("Name") or "")[:120],
                                "ShippingStreet": (r.get("ShippingStreet") or "")[:200],
                                "ShippingState": r.get("ShippingState"),
                            }
                            for r in sf_candidates[:10]
                        ],
                    },
                )
            except Exception:
                pass
        return None

    if not proxi_sf_writes_enabled():
        return None

    if not _env_truthy("PROXI_SF_CREATE_WORKSITES"):
        return None

    display = format_worksite_account_name(c, st)
    raw_ship = (address_line or "").strip()
    ship_street = format_us_address_line_for_display(raw_ship) if raw_ship else None
    if ship_street == "":
        ship_street = None
    if ship_street:
        dedup = strip_redundant_city_state_from_shipping_street(ship_street, city=c, state=st)
        if dedup:
            ship_street = dedup
    if ship_street == "":
        ship_street = None
    # Final guard: when ``address_line`` only contained ``"City, ST"`` (no
    # actual street content), the value above is still ``"Freeport, IL"`` and
    # would land in ``ShippingStreet`` — leaving the new worksite Account
    # with City and State both duplicated into the Street field. Skip the
    # Street field entirely unless the value looks like a real street
    # address. ShippingCity / ShippingState carry the location either way.
    if ship_street and not looks_like_real_street(ship_street):
        ship_street = None

    # Default Account Owner for all new worksites
    # User: 0055f000007qcxEAAQ (as specified)
    DEFAULT_ACCOUNT_OWNER_ID = "0055f000007qcxEAAQ"

    body: dict[str, Any] = {
        "Name": display,
        "ParentId": SF_ACCOUNT_ASPEN_DENTAL_MANAGEMENT_ID,
        "OwnerId": DEFAULT_ACCOUNT_OWNER_ID,  # Set specific owner for all new accounts
    }
    if ship_street:
        body["ShippingStreet"] = ship_street
    # Ensure the address renders correctly on Job formula fields that reference the worksite Account.
    # We intentionally keep these as simple text fields (no geocoding assumptions).
    body["ShippingCity"] = c
    body["ShippingState"] = st

    body.update(_account_extra_fields())

    try:
        describe = describe_sobject(instance_url, access_token, "Account")
        rt_id = worksite_account_record_type_id(describe)
        if rt_id:
            body["RecordTypeId"] = rt_id

        fields = filter_createable_fields(describe, body)
        if not fields.get("Name"):
            fields["Name"] = display
        # ParentId / RecordTypeId may be stripped if not createable for the integration user — still attempt create.
        resp = create_account_record(instance_url, access_token, fields)
        new_id = (resp.get("id") or "").strip()
        if not new_id:
            raise RuntimeError(f"Account create returned no id: {resp!r}")
        upsert_worksite_account_id_for_location(
            conn,
            c,
            st,
            salesforce_account_id=new_id,
            display_label=display,
            source="sf_account_create",
            schema=schema,
        )
        if job_id_for_log:
            log_job_event(
                conn,
                job_id=job_id_for_log,
                event_type="worksite_created",
                run_id=run_id,
                schema=schema,
                payload={
                    "city": c,
                    "state": st,
                    "shipping_street_sent": bool(ship_street),
                    "record_type_id_sent": bool(rt_id and fields.get("RecordTypeId")),
                    "parent_id_sent": bool(fields.get("ParentId")),
                    "salesforce_account_id": new_id,
                    "display_label": display,
                },
            )
        return new_id
    except Exception as e:
        if job_id_for_log:
            try:
                log_job_event(
                    conn,
                    job_id=job_id_for_log,
                    event_type="worksite_create_failed",
                    run_id=run_id,
                    schema=schema,
                    payload={"error": str(e)[:2000], "city": c, "state": st},
                )
            except Exception:
                pass
        return None

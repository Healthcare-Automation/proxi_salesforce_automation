"""Canonical-record selection + consolidation when several Job__c share one practice key.

Salesforce marks ``Job_Client_Job_Id__c`` unique, so one practice is meant to have one
Job__c. Legacy pairs slipped past that on cosmetic drift ("4096- X" vs "4096 - X").
"""

from utils.sf_job_supabase_resolve import (
    _consolidate_practice_duplicates,
    _pick_canonical_practice_record,
    _recruiting_activity,
)


def job(sfid, *, created, placements=0, submittals=0, applications=0, ext=None):
    return {
        "Id": sfid,
        "CreatedDate": created,
        "Total_Placements__c": placements,
        "Total_Submittals__c": submittals,
        "Total_Applications__c": applications,
        "External_Job_ID__c": ext,
    }


class FakeConn:
    """Captures log_job_event writes without a database."""

    def __init__(self):
        self.events = []


def _patch_logger(monkeypatch, sink):
    import utils.supabase_db as db

    def fake_log(conn, *, job_id, event_type, payload=None, run_id=None, schema="public"):
        sink.append({"job_id": job_id, "event_type": event_type, "payload": payload or {}})
        return 1

    monkeypatch.setattr(db, "log_job_event", fake_log)


def test_recruiting_activity_sums_and_tolerates_nulls():
    assert _recruiting_activity(job("a", created="2022", placements=1, submittals=2)) == 3.0
    assert _recruiting_activity({"Total_Placements__c": None}) == 0.0
    assert _recruiting_activity({"Total_Submittals__c": "junk"}) == 0.0


def test_canonical_pick_prefers_recruiting_history_over_newest():
    """Freeport: the 2022 record has the real history and must survive."""
    old_busy = job("a015f_freeport", created="2022-02-23", placements=2, submittals=4, applications=4)
    new_quiet = job("a01UP_freeport", created="2026-05-14", submittals=1, applications=1)
    assert _pick_canonical_practice_record([new_quiet, old_busy])["Id"] == "a015f_freeport"


def test_canonical_pick_falls_back_to_newest_on_equal_activity():
    old = job("a015f_boise", created="2022-02-24")
    new = job("a01UP_boise", created="2025-09-18")
    assert _pick_canonical_practice_record([old, new])["Id"] == "a01UP_boise"


def test_canonical_pick_ignores_external_job_id():
    """The old rule preferred the record with no External_Job_ID__c — that is what
    spread jobs 20073/20084 across two records and kept both alive."""
    linked_busy = job("a01UP_statesville", created="2025-05-19", submittals=1, applications=2, ext="20073")
    unlinked_empty = job("a015f_statesville", created="2022-02-23")
    assert _pick_canonical_practice_record([unlinked_empty, linked_busy])["Id"] == "a01UP_statesville"


def test_canonical_pick_empty_returns_none():
    assert _pick_canonical_practice_record([]) is None


def test_consolidation_is_detect_only_without_the_env_flag(monkeypatch):
    monkeypatch.delenv("PROXI_SF_CONSOLIDATE_DUPLICATE_JOBS", raising=False)
    events = []
    _patch_logger(monkeypatch, events)

    def boom(*a, **k):  # any SF write here is a bug
        raise AssertionError("must not touch Salesforce when the flag is unset")

    monkeypatch.setattr("utils.sf_job_rest_minimal.update_job_record", boom)

    _consolidate_practice_duplicates(
        FakeConn(),
        job_id="20084",
        practice_key_value="4096 statesville nc",
        practice_raw="4096 - Statesville, NC",
        winner=job("a01UP", created="2025-05-19", submittals=3),
        losers=[job("a015f", created="2022-02-23")],
    )
    assert len(events) == 1
    payload = events[0]["payload"]
    assert events[0]["event_type"] == "practice_duplicate_consolidated"
    assert payload["consolidated"] is False
    assert payload["winner_sf_job_id"] == "a01UP"
    assert payload["duplicate_sf_job_ids"] == ["a015f"]
    assert "Detect-only" in payload["detail"]


def test_consolidation_releases_losers_before_claiming_winner(monkeypatch):
    """The unique field must be freed first or Salesforce rejects the winner's PATCH."""
    monkeypatch.setenv("PROXI_SF_CONSOLIDATE_DUPLICATE_JOBS", "true")
    monkeypatch.setenv("PROXI_SF_UPDATE_JOBS", "true")
    events, calls = [], []
    _patch_logger(monkeypatch, events)
    monkeypatch.setattr(
        "utils.sf_job_supabase_resolve._sf_rest_token", lambda: ("https://x.my.salesforce.com", "tok")
    )
    monkeypatch.setattr(
        "utils.sf_job_rest_minimal.update_job_record",
        lambda inst, tok, obj, rid, fields, **kw: calls.append((rid, fields)) or {},
    )

    _consolidate_practice_duplicates(
        FakeConn(),
        job_id="20084",
        practice_key_value="4096 statesville nc",
        practice_raw="4096 - Statesville, NC",
        winner=job("a01UP", created="2025-05-19", submittals=3),
        losers=[job("a015f", created="2022-02-23")],
    )

    assert [c[0] for c in calls] == ["a015f", "a01UP"]
    assert calls[0][1] == {"Job_Client_Job_Id__c": None, "Job_Status__c": "Closed"}
    assert calls[1][1] == {"Job_Client_Job_Id__c": "4096 - Statesville, NC"}
    assert events[0]["payload"]["consolidated"] is True
    assert events[0]["payload"]["released_sf_job_ids"] == ["a015f"]


def test_consolidation_skips_winner_write_when_loser_release_fails(monkeypatch):
    monkeypatch.setenv("PROXI_SF_CONSOLIDATE_DUPLICATE_JOBS", "true")
    monkeypatch.setenv("PROXI_SF_UPDATE_JOBS", "true")
    events, calls = [], []
    _patch_logger(monkeypatch, events)
    monkeypatch.setattr(
        "utils.sf_job_supabase_resolve._sf_rest_token", lambda: ("https://x.my.salesforce.com", "tok")
    )

    def failing(inst, tok, obj, rid, fields, **kw):
        calls.append(rid)
        raise RuntimeError("INSUFFICIENT_ACCESS")

    monkeypatch.setattr("utils.sf_job_rest_minimal.update_job_record", failing)

    _consolidate_practice_duplicates(
        FakeConn(),
        job_id="20084",
        practice_key_value="4096 statesville nc",
        practice_raw="4096 - Statesville, NC",
        winner=job("a01UP", created="2025-05-19"),
        losers=[job("a015f", created="2022-02-23")],
    )

    assert calls == ["a015f"]  # winner never attempted
    payload = events[0]["payload"]
    assert payload["consolidated"] is False
    assert payload["errors"][0]["sf_job_id"] == "a015f"


def test_consolidation_noop_without_losers(monkeypatch):
    events = []
    _patch_logger(monkeypatch, events)
    _consolidate_practice_duplicates(
        FakeConn(),
        job_id="20084",
        practice_key_value="k",
        practice_raw="p",
        winner=job("a01UP", created="2025-05-19"),
        losers=[],
    )
    assert events == []


def test_writes_gate_blocks_consolidation(monkeypatch):
    """PROXI_SF_UPDATE_JOBS=false must veto consolidation even with the flag on."""
    monkeypatch.setenv("PROXI_SF_CONSOLIDATE_DUPLICATE_JOBS", "true")
    monkeypatch.setenv("PROXI_SF_UPDATE_JOBS", "false")
    events = []
    _patch_logger(monkeypatch, events)
    monkeypatch.setattr(
        "utils.sf_job_rest_minimal.update_job_record",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("writes are disabled")),
    )
    _consolidate_practice_duplicates(
        FakeConn(),
        job_id="20084",
        practice_key_value="k",
        practice_raw="p",
        winner=job("a01UP", created="2025-05-19"),
        losers=[job("a015f", created="2022-02-23")],
    )
    assert events[0]["payload"]["consolidated"] is False

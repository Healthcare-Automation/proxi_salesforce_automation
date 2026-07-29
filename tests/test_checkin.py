"""Check-in review packets: per-job checks, soft flags, selection/assignment."""

import random

from utils.checkin import (
    concern_score,
    run_job_checks,
    select_and_assign,
    soft_flags,
)


def job_row(**over):
    base = {
        "job_id": "20084",
        "sf_job_id": "a01UP00000N8ixvYAB",
        "status": "Closed",
        "dates_needed": "July 13-17, 20-22, 24, 27-29, 31",
        "practice_value": "4096 - Statesville, NC",
        "sf_worksite_account_id": "0015f00000cQekaAAC",
    }
    base.update(over)
    return base


def sf_rec(**over):
    base = {
        "Id": "a01UP00000N8ixvYAB",
        "External_Job_ID__c": "20084",
        "External_Job_Link__c": "https://portal.kimedics.com/app/workspace/job-posts/20084",
        "Job_Status__c": "Closed",
        "Job_Open_Date__c": "2026-07-08",
        "Job_Dates_Needed__c": "July 13-17, 20-22, 24, 27-29, 31",
        "Job_Worksite_Location_1__c": "0015f00000cQekaAAC",
        "Job_Client_Job_Id__c": "4096 - Statesville, NC",
    }
    base.update(over)
    return base


def by_name(checks):
    return {c["name"]: c for c in checks}


def test_consistent_record_passes_everything():
    checks = run_job_checks(job_row(), sf_rec(), "2026-07-08")
    assert all(c["ok"] for c in checks), [c for c in checks if not c["ok"]]


def test_hybrid_record_fails_link_and_dates():
    """The real 2026-07-28 incident: winner claimed id 20084 while still showing
    20073's link and July 6-7 dates. The check-in must catch exactly this."""
    hybrid = sf_rec(
        External_Job_Link__c="https://portal.kimedics.com/app/workspace/job-posts/20073",
        Job_Dates_Needed__c="July 6-7",
        Standard_Schedule_Hours__c="Monday8-6 Tuesday 7-5",
    )
    checks = by_name(run_job_checks(job_row(), hybrid, "2026-07-08"))
    assert not checks["external_link"]["ok"]
    assert not checks["dates"]["ok"]  # July 6 is not in the structured list — not a narrowing
    assert checks["external_id"]["ok"]


def test_missing_sf_record_is_single_failure():
    checks = run_job_checks(job_row(), None, "2026-07-08")
    assert len(checks) == 1 and not checks[0]["ok"]


def test_narrowed_dates_pass_with_note_and_soft_flag():
    """7/17-excluded push: SF holds July 13-16 …, a subset of the structured list."""
    narrowed = sf_rec(Job_Dates_Needed__c="July 13-16, 20-22, 24, 27-29, 31")
    checks = run_job_checks(job_row(), narrowed, "2026-07-08")
    dates = by_name(checks)["dates"]
    assert dates["ok"] and dates.get("note")
    assert any("narrowed" in f for f in soft_flags(checks, email_count=2))


def test_extra_dates_fail():
    checks = by_name(run_job_checks(job_row(), sf_rec(Job_Dates_Needed__c="Aug 3-5"), "2026-07-08"))
    assert not checks["dates"]["ok"]


def test_open_date_mismatch_fails():
    checks = by_name(run_job_checks(job_row(), sf_rec(Job_Open_Date__c="2026-07-17"), "2026-07-08"))
    assert not checks["open_date"]["ok"]
    assert checks["open_date"]["expected"] == "2026-07-08"


def test_status_uses_push_rule():
    """'Active, not accepting' maps to Closed — SF showing Open must fail."""
    row = job_row(status="Active, not accepting new providers")
    checks = by_name(run_job_checks(row, sf_rec(Job_Status__c="Open"), "2026-07-08"))
    assert not checks["status"]["ok"]
    assert checks["status"]["expected"] == "Closed"


def test_practice_key_tolerates_spacing_drift():
    """"4096-" vs "4096 - " is the same practice — must NOT fail the check."""
    checks = by_name(run_job_checks(
        job_row(), sf_rec(Job_Client_Job_Id__c="4096- Statesville, NC"), "2026-07-08"))
    assert checks["practice_key"]["ok"]


def test_empty_practice_value_skips_with_note():
    checks = by_name(run_job_checks(job_row(practice_value=""), sf_rec(), "2026-07-08"))
    assert checks["practice_key"]["ok"] and "skipped" in checks["practice_key"]["note"]


def test_churny_email_count_is_a_soft_flag():
    checks = run_job_checks(job_row(), sf_rec(), "2026-07-08")
    assert soft_flags(checks, email_count=11) == ["churny posting — 11 emails"]
    assert soft_flags(checks, email_count=3) == []


def test_concern_score_weights_failures_over_flags():
    failing = run_job_checks(job_row(), None, None)  # 1 failure
    assert concern_score(failing, ["flag"]) == 3
    assert concern_score(run_job_checks(job_row(), sf_rec(), "2026-07-08"), []) == 0


# ── selection & assignment ───────────────────────────────────────────────────


def cand(jid, score):
    return {"job_id": jid, "score": score, "checks": [], "flags": []}


def test_selection_ranks_by_score_and_alternates_assignees():
    pool = [cand("a", 0), cand("b", 4), cand("c", 2), cand("d", 0)]
    got = select_and_assign(pool, n=4, rng=random.Random(1))
    assert [c["job_id"] for c in got[:2]] == ["b", "c"]  # most concerning first
    assert [c["assignee"] for c in got] == ["Andy", "Sean", "Andy", "Sean"]


def test_selection_caps_at_pool_size():
    got = select_and_assign([cand("a", 0), cand("b", 1)], n=10, rng=random.Random(1))
    assert len(got) == 2 and got[0]["job_id"] == "b"


def test_selection_tie_break_is_seeded_random():
    pool = [cand(str(i), 0) for i in range(20)]
    one = [c["job_id"] for c in select_and_assign(list(pool), n=5, rng=random.Random(7))]
    two = [c["job_id"] for c in select_and_assign(list(pool), n=5, rng=random.Random(7))]
    assert one == two  # deterministic under a seed → tie-break is the rng, not dict order


def test_superseded_job_is_single_info_pass():
    """A record legitimately re-pointed to a newer posting (one record per clinic)
    must not fire false alarms — the real 20084-vs-20200 shape."""
    newer = sf_rec(
        External_Job_ID__c="20200",
        External_Job_Link__c="https://portal.kimedics.com/app/workspace/job-posts/20200",
        Job_Dates_Needed__c="Aug 3-7",
        Job_Open_Date__c="2026-07-28",
    )
    checks = run_job_checks(job_row(), newer, "2026-07-08", superseded_by="20200")
    assert len(checks) == 1 and checks[0]["ok"] and "20200" in checks[0]["note"]


def test_unknown_ext_id_is_not_supersession():
    """superseded_by is only passed when the ext id maps to a pool job; without it,
    a wrong ext id must still fail loudly."""
    checks = by_name(run_job_checks(job_row(), sf_rec(External_Job_ID__c="99999"), "2026-07-08"))
    assert not checks["external_id"]["ok"]

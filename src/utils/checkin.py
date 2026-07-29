"""
Manual Kimedics check-in: build human review packets for recently-touched jobs.

Triggered from the automation hub (Admin → Check-In). Runs every consistency check
against the SAME production rules the pipeline pushes with (status mapping, open-date
rule, date-token subset, practice key) — never a reimplementation — then selects the
10 most concerning jobs, splits them between the two reviewers, and returns a report
the hub renders and ``alert_email.send_checkin_report`` mails.

Read-only by design: Supabase SELECTs and Salesforce SOQL only. No writes, no AI.

Spec: docs/superpowers/specs/2026-07-29-kimedics-checkin-design.md
"""

from __future__ import annotations

import random
from datetime import datetime, timezone
from typing import Any, Optional, Sequence

from utils.job_description_ai import expand_date_tokens
from utils.sf_job_payload import job_status_for_salesforce_push
from utils.sf_practice_key import practice_key

KIMEDICS_JOB_URL = "https://portal.kimedics.com/app/workspace/job-posts/{job_id}"
LIGHTNING_JOB_URL = "https://proxi.lightning.force.com/lightning/r/Job__c/{sf_id}/view"
REVIEWERS = ("Andy", "Sean")

# Soft flag threshold: a posting with this many emails has churned enough that a
# human should sanity-check the final state even when every automated check passes.
CHURNY_EMAIL_COUNT = 8


def _norm(v: Any) -> str:
    return " ".join(str(v or "").split()).strip().rstrip(".").lower()


def _check(name: str, ok: bool, expected: Any = None, actual: Any = None, note: str = "") -> dict:
    out: dict[str, Any] = {"name": name, "ok": bool(ok)}
    if not ok:
        out["expected"] = None if expected is None else str(expected)
        out["actual"] = None if actual is None else str(actual)
    if note:
        out["note"] = note
    return out


def run_job_checks(
    job_row: dict,
    sf_rec: Optional[dict],
    expected_open_date: Optional[str],
    *,
    superseded_by: Optional[str] = None,
) -> list[dict]:
    """All per-job consistency checks. Pure — no DB/SF access.

    ``superseded_by``: the practice's Job__c now carries a NEWER Kimedics posting
    (one record per clinic, latest wins). Comparing this older posting against the
    record would fail every identity/content check by design — report the
    supersession as a single informational pass instead of false alarms.
    """
    jid = str(job_row.get("job_id") or "").strip()
    if not sf_rec:
        return [_check("sf_record", False, expected=f"Job__c for #{jid}",
                       actual="not found in Salesforce")]

    if superseded_by:
        return [_check(
            "superseded", True,
            note=f"record now carries #{superseded_by}, the latest posting at this practice — "
                 "content checks apply to that job",
        )]

    checks = []

    ext = str(sf_rec.get("External_Job_ID__c") or "").strip()
    checks.append(_check("external_id", ext == jid, expected=jid, actual=ext or "(empty)"))

    link = str(sf_rec.get("External_Job_Link__c") or "").strip().rstrip("/")
    checks.append(_check(
        "external_link", link.endswith(f"/{jid}"),
        expected=KIMEDICS_JOB_URL.format(job_id=jid), actual=link or "(empty)",
    ))

    want_status = job_status_for_salesforce_push(job_row.get("status"))
    have_status = str(sf_rec.get("Job_Status__c") or "").strip() or None
    checks.append(_check("status", want_status == have_status,
                         expected=want_status, actual=have_status))

    have_open = str(sf_rec.get("Job_Open_Date__c") or "").strip() or None
    checks.append(_check("open_date", expected_open_date == have_open,
                         expected=expected_open_date, actual=have_open))

    structured = str(job_row.get("dates_needed") or "").strip()
    sf_dates = str(sf_rec.get("Job_Dates_Needed__c") or "").strip()
    if _norm(sf_dates) == _norm(structured):
        checks.append(_check("dates", True))
    else:
        # The pipeline may legitimately push a narrowing of the structured list (a
        # top-of-post override). A narrowing is a token-level subset; anything else
        # — extra days, different month, empty — is a real mismatch.
        allowed = expand_date_tokens(structured)
        got = expand_date_tokens(sf_dates)
        subset = bool(allowed and got and got <= allowed)
        checks.append(_check("dates", subset, expected=structured, actual=sf_dates or "(empty)",
                             note="narrowed from structured list" if subset else ""))

    want_ws = str(job_row.get("sf_worksite_account_id") or "").strip()
    have_ws = str(sf_rec.get("Job_Worksite_Location_1__c") or "").strip()
    checks.append(_check("worksite", want_ws == have_ws, expected=want_ws or "(empty)",
                         actual=have_ws or "(empty)"))

    ours = practice_key(job_row.get("practice_value"))
    theirs = practice_key(sf_rec.get("Job_Client_Job_Id__c"))
    if not ours:
        checks.append(_check("practice_key", True, note="no practice value in Supabase — skipped"))
    else:
        checks.append(_check("practice_key", ours == theirs,
                             expected=job_row.get("practice_value"),
                             actual=sf_rec.get("Job_Client_Job_Id__c") or "(empty)"))
    return checks


def soft_flags(checks: Sequence[dict], email_count: int) -> list[str]:
    """Pass-but-look flags: things a human should eyeball even when checks pass."""
    flags = []
    for c in checks:
        if c.get("name") == "dates" and c.get("ok") and c.get("note"):
            flags.append("dates were narrowed from the structured list")
    if email_count >= CHURNY_EMAIL_COUNT:
        flags.append(f"churny posting — {email_count} emails")
    return flags


def concern_score(checks: Sequence[dict], flags: Sequence[str]) -> int:
    return 2 * sum(1 for c in checks if not c.get("ok")) + len(flags)


def select_and_assign(candidates: list[dict], n: int = 10, rng: Optional[random.Random] = None) -> list[dict]:
    """Rank by concern score desc (random tie-break), take top ``n``, assign
    reviewers alternating down the ranking so each gets a mix of concerning and
    clean jobs. Mutates and returns the selected entries (adds ``assignee``)."""
    r = rng or random.Random()
    shuffled = list(candidates)
    r.shuffle(shuffled)  # pre-shuffle = random tie-break under the stable sort
    ranked = sorted(shuffled, key=lambda c: c.get("score", 0), reverse=True)
    selected = ranked[: max(0, n)]
    for i, entry in enumerate(selected):
        entry["assignee"] = REVIEWERS[i % len(REVIEWERS)]
    return selected


# ── Orchestration (DB + SF) ──────────────────────────────────────────────────


def _pool(conn, days: int = 7) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT job_id, sf_job_id, status, dates_needed, posted_date,
                   practice_value, sf_worksite_account_id, updated_at
            FROM job_current
            WHERE sf_job_id IS NOT NULL AND sf_job_id <> ''
              AND updated_at >= now() - (%s || ' days')::interval
            ORDER BY updated_at DESC
            """,
            (days,),
        )
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]


def _sf_records(instance_url: str, access_token: str, sf_ids: Sequence[str]) -> dict[str, dict]:
    from utils.salesforce import query_all

    out: dict[str, dict] = {}
    ids = sorted({s for s in sf_ids if s})
    for i in range(0, len(ids), 100):
        chunk = "','".join(ids[i : i + 100])
        soql = (
            "SELECT Id, External_Job_ID__c, External_Job_Link__c, Job_Status__c, "
            "Job_Open_Date__c, Job_Dates_Needed__c, Job_Worksite_Location_1__c, "
            f"Job_Client_Job_Id__c FROM Job__c WHERE Id IN ('{chunk}')"
        )
        for rec in query_all(instance_url, access_token, soql):
            out[rec["Id"]] = rec
    return out


def _email_counts(conn, job_ids: Sequence[str]) -> dict[str, int]:
    if not job_ids:
        return {}
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT jc.job_id, count(DISTINCT es.id)
            FROM job_content jc JOIN email_scrapes es ON es.id = jc.email_scrape_id
            WHERE jc.job_id = ANY(%s) GROUP BY jc.job_id
            """,
            (list(job_ids),),
        )
        return {str(j): int(n) for j, n in cur.fetchall()}


def _email_timelines(conn, job_ids: Sequence[str]) -> dict[str, list[dict]]:
    if not job_ids:
        return {}
    out: dict[str, list[dict]] = {j: [] for j in job_ids}
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT DISTINCT jc.job_id, es.date, es.subject, es.action_or_change
            FROM job_content jc JOIN email_scrapes es ON es.id = jc.email_scrape_id
            WHERE jc.job_id = ANY(%s)
            ORDER BY jc.job_id, es.date
            """,
            (list(job_ids),),
        )
        for jid, date, subject, action in cur.fetchall():
            out[str(jid)].append({
                "date": date.isoformat() if hasattr(date, "isoformat") else str(date),
                "subject": subject or "",
                "action": action or "",
            })
    return out


def _pulse(conn, sf_auth_ok: bool) -> list[dict]:
    checks = []
    with conn.cursor() as cur:
        cur.execute("SELECT max(started_at) FROM scrape_runs")
        last = cur.fetchone()[0]
        if last is None:
            checks.append(_check("scrape_cadence", False, expected="a scrape run", actual="none found"))
        else:
            age_min = (datetime.now(timezone.utc) - last).total_seconds() / 60.0
            checks.append(_check(
                "scrape_cadence", age_min < 30,
                expected="last run < 30 min ago", actual=f"{age_min:.0f} min ago",
                note=f"last run {age_min:.0f} min ago",
            ))

        cur.execute("SELECT count(*) FROM email_scrapes WHERE date >= now() - interval '24 hours'")
        n_emails = int(cur.fetchone()[0])
        checks.append(_check("email_ingestion", n_emails > 0,
                             expected="> 0 emails in 24h", actual=str(n_emails),
                             note=f"{n_emails} emails in 24h"))

        cur.execute(
            """
            SELECT count(*) FROM job_event_log
            WHERE created_at >= now() - interval '24 hours'
              AND (event_type ILIKE '%%error%%' OR event_type ILIKE '%%stuck%%'
                   OR event_type ILIKE '%%quarantin%%'
                   OR event_type = 'sf_field_dropped_unique_collision')
            """
        )
        n_err = int(cur.fetchone()[0])
        checks.append(_check("error_events", n_err == 0,
                             expected="0 error events in 24h", actual=str(n_err),
                             note=f"{n_err} error events in 24h"))

    checks.append(_check("sf_auth", sf_auth_ok, expected="token fetch succeeds",
                         actual="ok" if sf_auth_ok else "failed"))
    return checks


def run_checkin(
    conn,
    instance_url: str,
    access_token: str,
    *,
    sample_size: int = 10,
    pool_days: int = 7,
    rng: Optional[random.Random] = None,
) -> dict:
    """Full check-in: pulse + checks over every recently-touched job + review packets."""
    from utils.sf_job_payload import get_most_recent_open_date

    pool = _pool(conn, days=pool_days)
    sf_by_id = _sf_records(instance_url, access_token, [r["sf_job_id"] for r in pool])
    counts = _email_counts(conn, [str(r["job_id"]) for r in pool])

    # Jobs whose canonical record has been re-pointed to a newer posting at the same
    # practice: the SF ext id names another pool job on the same record → superseded.
    pool_ids = {str(r["job_id"]) for r in pool}

    candidates = []
    for row in pool:
        jid = str(row["job_id"])
        sf_rec = sf_by_id.get(str(row.get("sf_job_id") or ""))
        sf_ext = str((sf_rec or {}).get("External_Job_ID__c") or "").strip()
        superseded_by = sf_ext if (sf_ext and sf_ext != jid and sf_ext in pool_ids) else None
        expected_open = get_most_recent_open_date(conn, jid)
        checks = run_job_checks(row, sf_rec, expected_open, superseded_by=superseded_by)
        flags = soft_flags(checks, counts.get(jid, 0))
        candidates.append({
            "job_id": jid,
            "sf_job_id": str(row.get("sf_job_id") or ""),
            "checks": checks,
            "flags": flags,
            "score": concern_score(checks, flags),
            "state": {
                "status": row.get("status"),
                "dates_needed": row.get("dates_needed"),
                "open_date": expected_open,
                "practice_value": row.get("practice_value"),
                "worksite": row.get("sf_worksite_account_id"),
                "sf_status": (sf_rec or {}).get("Job_Status__c"),
                "sf_dates_needed": (sf_rec or {}).get("Job_Dates_Needed__c"),
                "sf_open_date": (sf_rec or {}).get("Job_Open_Date__c"),
            },
            "links": {
                "kimedics": KIMEDICS_JOB_URL.format(job_id=jid),
                "salesforce": LIGHTNING_JOB_URL.format(sf_id=row.get("sf_job_id")),
            },
        })

    selected = select_and_assign(candidates, n=sample_size, rng=rng)
    timelines = _email_timelines(conn, [c["job_id"] for c in selected])
    for entry in selected:
        entry["emails"] = timelines.get(entry["job_id"], [])

    failed_jobs = sum(1 for c in candidates if any(not k["ok"] for k in c["checks"]))
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "pulse": _pulse(conn, sf_auth_ok=bool(access_token)),
        "pool_size": len(pool),
        "checked_jobs": len(candidates),
        "failed_jobs": failed_jobs,
        "jobs": selected,
    }

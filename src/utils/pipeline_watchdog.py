"""
Independent pipeline watchdog: detects trouble the pipeline itself can't report.

The July 2 incident proved that in-pipeline alerting has a blind spot — when the
scrape task is hard-killed (Modal 600s timeout), every alert downstream of the
kill dies with it, the cron run looks clean, and nobody hears anything. This
module runs from its OWN cron (see ``pipeline_watchdog_job`` in
scrape_gmail_modal.py), reads only the database, and emails the team when:

  A. orphaned email_scrapes — job updates logged >45 min ago that still have no
     scraped content (auto-retry should clear a healthy orphan in ~15-30 min);
  B. mapped-but-unsynced jobs — linked to Salesforce >30 min ago with no
     field-sync resolution of any kind (the #20046/#20076 silent-failure shape);
  C. dead cron — the gmail scrape hasn't heart-beaten in >35 min (schedule is
     every 10, so 3 missed beats = the cron itself is down or hard-killed);
  D. inbox lockout — the cron is alive but hasn't completed a Gmail fetch in
     >90 min. The shared inbox has a hard 15-simultaneous-connection cap and an
     external client floods it daily for ~20-40 min (~08:46 UTC); the cron now
     skips those ticks quietly instead of failing loudly, so this signal is what
     pages a human if the lockout ever persists past the known daily window.

Alerts are deduped: at most one email every 2 h unless the situation escalates
(dead cron newly detected, or a backlog count grew by 3+).
"""

from __future__ import annotations

from typing import Optional

HEARTBEAT_GMAIL_CRON = "kimedics_gmail_cron"
HEARTBEAT_GMAIL_FETCH = "kimedics_gmail_fetch_ok"

ORPHAN_STALE_MINUTES = 45
UNSYNCED_STALE_MINUTES = 30
HEARTBEAT_STALE_MINUTES = 35
# Must comfortably exceed the daily flood window (~20-40 min) so the known
# blip stays silent, while a genuinely stuck inbox still pages within the hour+.
FETCH_STALE_MINUTES = 90
ALERT_COOLDOWN_HOURS = 2
ESCALATION_GROWTH = 3

_WATCHDOG_EVENT_JOB_ID = "_watchdog"  # sentinel job_id for job_event_log audit rows


def ensure_heartbeat_table(conn) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS automation_heartbeats (
              name    text PRIMARY KEY,
              beat_at timestamptz NOT NULL DEFAULT NOW()
            )
            """
        )


def record_heartbeat(conn, name: str) -> None:
    """Upsert a liveness beat. Callers commit (the gmail cron beats every run)."""
    ensure_heartbeat_table(conn)
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO automation_heartbeats (name, beat_at) VALUES (%s, NOW())
            ON CONFLICT (name) DO UPDATE SET beat_at = NOW()
            """,
            (name,),
        )


def _fetch_stale_orphans(conn) -> list[dict]:
    """Signal A: email updates logged but still unscraped well past auto-retry's window."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT es.job_post_id,
                   ROUND(EXTRACT(EPOCH FROM (NOW() - es.created_at)) / 60)::int AS age_min,
                   (SELECT count(*) FROM job_event_log r
                      WHERE r.event_type = 'auto_retry_completed'
                        AND (r.payload->>'email_scrape_id') = es.id::text) AS attempts
            FROM email_scrapes es
            WHERE es.created_at BETWEEN NOW() - INTERVAL '48 hours'
                                    AND NOW() - (%s::text || ' minutes')::interval
              AND COALESCE(es.view_job_link, '') <> ''
              AND COALESCE(es.job_post_id, '') <> ''
              AND NOT EXISTS (
                SELECT 1 FROM job_content jc
                WHERE jc.email_scrape_id = es.id
                  AND (COALESCE(NULLIF(jc.title_line, ''), '') <> ''
                       OR COALESCE(NULLIF(jc.description_full_text, ''), '') <> ''
                       OR COALESCE(NULLIF(jc.job_title, ''), '') <> '')
              )
            ORDER BY es.created_at
            """,
            (ORPHAN_STALE_MINUTES,),
        )
        return [
            {"job_id": str(r[0]), "age_min": int(r[1] or 0), "attempts": int(r[2] or 0)}
            for r in cur.fetchall()
        ]


def _fetch_stale_unsynced(conn) -> list[dict]:
    """Signal B: mapped to Salesforce but no field-sync resolution — same shape the
    resync sweep drains, but older than its expected clearing time (sweep runs every
    10 min cron; >30 min stale means the sweep isn't running or isn't succeeding)."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT jc.job_id,
                   ROUND(EXTRACT(EPOCH FROM (NOW() - jc.created_at)) / 60)::int AS age_min
            FROM job_content jc
            WHERE jc.id IN (SELECT MAX(id) FROM job_content GROUP BY job_id)
              AND COALESCE(NULLIF(jc.sf_job_id, ''), '') <> ''
              AND jc.created_at < NOW() - (%s::text || ' minutes')::interval
              AND jc.created_at > NOW() - INTERVAL '21 days'
              AND NOT EXISTS (
                SELECT 1 FROM job_event_log e
                WHERE e.job_id = jc.job_id
                  AND e.event_type IN (
                        'sf_scrape_fields_patched', 'sf_scrape_fields_recovered',
                        'sf_scrape_fields_skip', 'sf_scrape_fields_error',
                        'sf_sync_skipped_no_mapping')
                  AND e.created_at >= jc.created_at - INTERVAL '5 minutes'
              )
            ORDER BY jc.created_at
            """,
            (UNSYNCED_STALE_MINUTES,),
        )
        return [{"job_id": str(r[0]), "age_min": int(r[1] or 0)} for r in cur.fetchall()]


def _heartbeat_age_minutes(conn, name: str) -> Optional[int]:
    """Minutes since the last beat; None if no beat has ever been recorded."""
    ensure_heartbeat_table(conn)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT ROUND(EXTRACT(EPOCH FROM (NOW() - beat_at)) / 60)::int "
            "FROM automation_heartbeats WHERE name = %s",
            (name,),
        )
        row = cur.fetchone()
    return int(row[0]) if row else None


def _last_watchdog_alert(conn) -> Optional[dict]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT payload FROM job_event_log
            WHERE job_id = %s AND event_type = 'watchdog_alert_sent'
              AND created_at >= NOW() - (%s::text || ' hours')::interval
            ORDER BY created_at DESC LIMIT 1
            """,
            (_WATCHDOG_EVENT_JOB_ID, ALERT_COOLDOWN_HOURS),
        )
        row = cur.fetchone()
    return row[0] if row and isinstance(row[0], dict) else None


def _escalated(
    prior: dict, *, orphans: int, unsynced: int, cron_dead: bool, inbox_locked: bool = False
) -> bool:
    if cron_dead and not prior.get("cron_dead"):
        return True
    if inbox_locked and not prior.get("inbox_locked"):
        return True
    if orphans >= int(prior.get("orphan_count", 0)) + ESCALATION_GROWTH:
        return True
    if unsynced >= int(prior.get("unsynced_count", 0)) + ESCALATION_GROWTH:
        return True
    return False


def run_watchdog(conn) -> bool:
    """Check all signals; email + audit-log if anything is wrong. Returns True if alerted."""
    from utils.supabase_db import log_job_event

    orphans = _fetch_stale_orphans(conn)
    unsynced = _fetch_stale_unsynced(conn)
    beat_age = _heartbeat_age_minutes(conn, HEARTBEAT_GMAIL_CRON)
    cron_dead = beat_age is None or beat_age > HEARTBEAT_STALE_MINUTES
    # None = the fetch beat has never been written (pre-deploy DB) — not a lockout.
    fetch_age = _heartbeat_age_minutes(conn, HEARTBEAT_GMAIL_FETCH)
    inbox_locked = (
        not cron_dead and fetch_age is not None and fetch_age > FETCH_STALE_MINUTES
    )

    if not orphans and not unsynced and not cron_dead and not inbox_locked:
        print(
            f"watchdog: healthy — 0 stale orphans, 0 stale unsynced, "
            f"gmail cron beat {beat_age} min ago, last fetch {fetch_age} min ago"
        )
        return False

    print(
        f"watchdog: TROUBLE — orphans={len(orphans)} unsynced={len(unsynced)} "
        f"cron_dead={cron_dead} inbox_locked={inbox_locked} "
        f"(beat_age={beat_age}, fetch_age={fetch_age})"
    )

    prior = _last_watchdog_alert(conn)
    if prior is not None and not _escalated(
        prior, orphans=len(orphans), unsynced=len(unsynced), cron_dead=cron_dead,
        inbox_locked=inbox_locked,
    ):
        print("watchdog: alert suppressed (already alerted within cooldown, no escalation)")
        return False

    from utils.alert_email import send_pipeline_watchdog_alert

    sent = send_pipeline_watchdog_alert(
        orphans=orphans,
        unsynced=unsynced,
        cron_dead=cron_dead,
        beat_age_minutes=beat_age,
        inbox_locked=inbox_locked,
        fetch_age_minutes=fetch_age,
    )
    log_job_event(
        conn,
        job_id=_WATCHDOG_EVENT_JOB_ID,
        event_type="watchdog_alert_sent",
        payload={
            "email_sent": bool(sent),
            "orphan_count": len(orphans),
            "unsynced_count": len(unsynced),
            "cron_dead": cron_dead,
            "inbox_locked": inbox_locked,
            "beat_age_minutes": beat_age,
            "fetch_age_minutes": fetch_age,
            "orphan_jobs": [o["job_id"] for o in orphans][:25],
            "unsynced_jobs": [u["job_id"] for u in unsynced][:25],
        },
    )
    conn.commit()
    print(f"watchdog: alert email sent={sent}")
    return True

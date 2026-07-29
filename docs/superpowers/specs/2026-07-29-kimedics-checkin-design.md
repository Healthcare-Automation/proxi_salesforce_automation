# Kimedics Check-In — manual health review packets

**Date:** 2026-07-29 · **Status:** approved by Andy · **Repos:** `proxi_salesforce_automation` (endpoint + checks + email), `automation-hub` (Admin trigger + report UI)

## Goal

A manually triggered check-in that verifies the Kimedics → Salesforce automation is
running correctly and produces a **human review packet**: 10 recently-touched jobs
(5 assigned to Andy, 5 to Sean) with every automated consistency check pre-computed and
all context attached, so a person can eyeball each job quickly. No schedule — button only.

## Trigger flow

```
Admin UI (automation-hub /admin/checkin)
  → POST /api/admin/checkin/run          (signed admin cookie; proxy, clone of recovery run route)
    → Modal checkin_endpoint             (POST; token = IMPACT_ENDPOINT_TOKEN, already in Modal secret + hub env)
      → run checks → build packets → send email → return full report JSON
  ← report JSON rendered in the page
```

Reusing `IMPACT_ENDPOINT_TOKEN` avoids editing the Modal `salesforce-automation` secret
(`modal secret create --force` replaces every key — do not touch it for this feature).

## Phase 1 — pipeline pulse

| Check | Pass condition |
|---|---|
| Scrape cadence | most recent `scrape_runs` row < 30 min old |
| Email ingestion | count of `email_scrapes` in last 24 h (informational; warn at 0) |
| Error events | 0 events in 24 h matching `%error%`, `%stuck%`, `%quarantin%`, `sf_field_dropped_unique_collision` |
| SF auth | client-credentials token fetch succeeds |

## Phase 2 — per-job checks (all jobs touched in last 7 days)

Candidate pool: `job_current` rows with `sf_job_id` and `updated_at >= now() - 7 days`.
One batched SOQL fetches all their SF records. Per job:

| Check | Rule source (production code — never reimplemented) |
|---|---|
| External ID ↔ link | `External_Job_Link__c` ends with `/{job_id}` and `External_Job_ID__c == job_id` |
| Status mapping | `Job_Status__c == job_status_for_salesforce_push(job_current.status)` |
| Open date | `Job_Open_Date__c == get_most_recent_open_date(conn, job_id)` |
| Dates needed | SF value equals `job_current.dates_needed` **or** is a token-level subset of it (`expand_date_tokens`); **no AI call** — a legit AI narrowing passes the subset test, so no false positives and zero LLM cost |
| Worksite | `Job_Worksite_Location_1__c == job_current.sf_worksite_account_id` |
| Practice key | `practice_key(Job_Client_Job_Id__c) == practice_key(job_current.practice_value)` |

A check that cannot run (e.g. SF record deleted) reports ✗ with the reason; it never
crashes the run.

## Selection & assignment (10 jobs)

1. Score each candidate: 2 points per failed check, 1 per soft flag. Soft flags (pass,
   but worth human eyes): dates are a narrowing rather than exactly equal; job has >8
   emails in its timeline (churny posting).
2. Rank by score desc, tie-break random (seeded per run).
3. Take the top 10 (fewer if the pool is smaller); random fill guarantees clean jobs
   appear when there are few failures.
4. Assign alternating down the ranking: rank 1 → Andy, 2 → Sean, 3 → Andy, … so both
   reviewers get a mix of concerning and clean jobs.

## Review packet (per job)

- Links: Kimedics `https://portal.kimedics.com/app/workspace/job-posts/{job_id}` and
  SF `https://proxi.lightning.force.com/lightning/r/Job__c/{sf_job_id}/view`
- Email timeline: every `email_scrapes` row for the job (via `job_content`), each with
  timestamp, subject, and `action_or_change`
- Current state: status, dates needed, open date, practice value, worksite
- Check results: ✓/✗ per check, with expected vs actual on any ✗

## Delivery

- **Email — always sent, pass or fail** — to `seanhyang1@gmail.com` and
  `anddy0622@gmail.com`. One email to both, two sections: "Andy's 5" / "Sean's 5".
  Built in `alert_email` following the existing style: light `.card`s only, no dark
  hero (Gmail mobile strips `<style>` and auto-inverts — see the dark-mode rule).
  The email is the permanent record of a run.
- **UI** — the endpoint returns the full report JSON; `/admin/checkin` renders pulse
  rows, then the 10 packets grouped by assignee. No run history in the UI (the email
  is the history). No DB writes at all — the check-in is strictly read-only.

## New code

| Where | What |
|---|---|
| `src/utils/checkin.py` (new) | pure check functions over row dicts + selection/assignment; unit-testable without DB/SF |
| `src/utils/alert_email.py` | `send_checkin_report(report) -> bool` |
| `src/production/scrape_gmail_modal.py` | `checkin_endpoint` (clone shape of `impact_report_endpoint`) |
| hub `app/api/admin/checkin/run/route.ts` | cookie-auth proxy (clone of recovery run route; env `MODAL_CHECKIN_ENDPOINT_URL`) |
| hub `app/admin/checkin/page.tsx` | button + report renderer; link added on recovery page header |

## Cost & gating

Per click: 1 SF auth + ~3–5 SOQL + Supabase reads + 1 email. Zero LLM, zero scraping,
zero Kimedics logins, zero writes. Inside the automation run gate's read-only carve-out;
no per-run pre-flight needed. Not scheduled.

## Testing

- Unit tests for `checkin.py`: each check against pass/fail fixtures (including the real
  20084 hybrid-record shape as a failure fixture), selection scoring, alternating
  assignment, small-pool behavior.
- Hub: `npm run build` (no test script exists in the hub).
- One live click against prod as acceptance, verifying email lands and UI renders.

## Out of scope

Scheduling/cron, auto-remediation, DJC coverage, run history storage, per-reviewer
separate emails.

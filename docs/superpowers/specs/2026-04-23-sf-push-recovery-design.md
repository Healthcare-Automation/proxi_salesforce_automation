# Salesforce Push Recovery — Design

**Date:** 2026-04-23
**Status:** Design approved, ready for implementation plan

## Problem

When the Modal job pushes scraped Kimedics data to Salesforce, a single bad field value
fails the entire PATCH and none of the row's data lands. Today these failures are logged
as `sf_scrape_fields_error` in `job_event_log` and silently dropped — there is no retry,
no salvage, and no visibility in automation-hub that the issue was later addressed.

Two representative recent cases:

- **Job 19664** (2026-04-21 → 2026-04-22): `Job_Volume__c` populated with
  `"***Additional requirements/ info: extractions could include simple/ surgical/ full
  mouth- please notate any limitations in presentation"`. Salesforce rejected with
  `max length=50`. Root cause was a line-shift in the Kimedics parser — wrong text
  ended up in the Volume slot. The correct value was `"3-4 NP, 6-7 EP"`. Five
  consecutive runs failed; all five emitted `sf_scrape_fields_error`. None of the
  other valid fields in the PATCH body landed in Salesforce.
- **Job 19666** (2026-04-21): `job_create_failed` with `no_worksite_account_id`.

Only the first class is in scope for this design. Mapping-gap failures (`mapping_no_match`,
`no_worksite_account_id`) require human / AI matching and are explicitly out of scope.

## Goals

1. When a Salesforce PATCH fails on a subset of fields, recover the rest of the payload
   automatically so data lands instead of being lost.
2. Feed each recovery back into `job_event_log` using conventions the existing
   automation-hub dashboards already understand (`sf_scrape_fields_patched`), so the
   run page visibly shows the failure as "covered."
3. Make the recovery path smart about root cause: prefer re-parsing the stored Kimedics
   text (and, if needed, re-scraping the link) over blindly dropping fields. Field drop
   is a last resort.
4. Expose the same engine through two entrypoints: a scheduled auto-recovery inside
   `scrape_gmail_job`, and an on-demand CLI for manual backfill.

## Non-goals

- Resolving mapping gaps (practice / worksite matching).
- Changing the upstream Kimedics scraper or the `job_content_parser.py` logic itself.
  Recovery uses whatever parser is currently checked in; when the parser is fixed,
  previously quarantined jobs retry automatically.
- Changes to automation-hub. The design is additive — hub continues to work unchanged,
  and can later opt in to surfacing new event types.

## Architecture

One shared engine, two entrypoints, three new event types on the wire, coordinated
hub changes in the adjacent `automation-hub` repo.

```
                 ┌──────────────────────────────────┐
 (A) CLI ───────▶│                                  │
                 │   utils/sf_push_recovery.py      │───▶ SF PATCH (retry)
 (B) Modal ─────▶│        (engine)                  │
                 └─────────────┬────────────────────┘
                               │
                               ▼
                     ┌───────────────────────────────┐
                     │ job_event_log                 │
                     │  + sf_scrape_fields_recovered │  ← new
                     │  + sf_field_quarantined       │  ← new
                     │  + sf_push_unhandled_error    │  ← new
                     └─────────────┬─────────────────┘
                                   │
                                   ▼
                     ┌───────────────────────────────┐
                     │ automation-hub (sibling repo) │
                     │  - counts "unresolved" errors │
                     │  - timeline shows "recovered" │
                     │  - run card shows quarantine  │
                     └───────────────────────────────┘
```

Both entrypoints call `recover_recent_failures(conn, access_token, instance_url, since)`,
which enumerates candidate `job_event_log` rows, runs the recovery pipeline per job,
and writes new events. See `run_id strategy` below for how events are attributed.

### run_id strategy

All recovery-emitted events (`sf_scrape_fields_recovered`, `sf_field_quarantined`,
`sf_push_unhandled_error`) are written with **`run_id = original_error.run_id`** — i.e.
the run that originally failed — **not** the recovery run's id.

Why: the hub's validation page is indexed by run id. When a user opens the run where
the failure happened, they see the failure *and* its recovery in one place. Otherwise
a recovered error would only be discoverable by cross-referencing event types across
runs.

The recovery run's id is still captured in `payload.recovery_run_id` for audit.

### Entrypoints

- **(B) Auto-recovery — inside Modal job.** Added at the tail of
  `scrape_gmail_job` in `src/production/scrape_gmail_modal.py`, after the main sync
  loop. Window: **last 3 hours** of unresolved error events. Bounded so we do not
  hammer Salesforce replaying old failures.
- **(A) Manual CLI — `src/local/repair_sf_push_errors.py`.** Default window:
  **last 2 days**. Flags:
  - `--since <ISO8601>` — override window.
  - `--job-id <id>` — target a specific job.
  - `--dry-run` — print planned actions, no writes.
  - `--limit <n>` — cap rows processed in one invocation.

### Candidate selection

An error event is a candidate if all are true:

1. `event_type` in (`sf_scrape_fields_error`, `sf_mapping_pull_failed`).
2. `created_at` falls within the window.
3. No success event (`sf_scrape_fields_patched` or `sf_scrape_fields_recovered`) exists
   for the same `job_id` with `created_at >= error.created_at`.
4. Not already quarantined at the current `parser_version` (see Quarantine below).

Only the **most recent** qualifying error per `job_id` is processed per invocation, to
avoid duplicate work when the same job failed several times in a row.

## Error classification and salvage rules

The engine parses `payload.error` on the candidate event into
`(error_class, offending_fields[])`, then selects a strategy.

| SF error pattern                                           | error_class            | offending_fields                                        | Strategy                                  |
| ---------------------------------------------------------- | ---------------------- | ------------------------------------------------------- | ----------------------------------------- |
| `{Label}: data value too large: … (max length=N)`          | `too_large`            | resolve `Label` → API name via cached `describe`        | Root-cause flow (see below)               |
| `Required fields are missing: [X__c]`                      | `required_missing`     | `X__c`                                                  | Root-cause flow                           |
| `INVALID_OR_NULL_FOR_RESTRICTED_PICKLIST` on field X       | `bad_picklist`         | X                                                       | Root-cause flow                           |
| `entity is deleted` on `Job_Worksite_Location_1__c`        | `worksite_deleted`     | `Job_Worksite_Location_1__c`                            | Existing handling (keep as-is)            |
| HTTP 5xx / DNS failure / token 401                         | `transient`            | —                                                       | Retry once with backoff (≤ 2 attempts)    |
| `<urlopen error ...>` on describe (`sf_mapping_pull_failed`) | `transient`          | —                                                       | Retry once                                |
| Anything else                                              | `unknown`              | —                                                       | Log `sf_push_unhandled_error`, skip       |

When a new `unknown` pattern appears, it is logged but not retried. Adding a new row
to the rule table later is the only change needed to start handling it.

### Root-cause flow (for `too_large`, `required_missing`, `bad_picklist`)

```
error detected
  │
  ▼
(1) re-run current parser on stored Kimedics raw text
    (data/job_content/<id>.txt in dev; job_content.raw_text column in prod)
  │
  ├─ new parser output DIFFERS from the PATCH `attempt` on offending_fields?
  │    yes → rebuild full payload from fresh parse → PATCH → done
  │    no  → continue
  ▼
(2) run context heuristics on the bad value:
    - value appears verbatim in another field (line-shift) ?
    - value begins with parser-noise marker (`***`, `Additional requirements/`, …) ?
    - adjacent expected field is empty ?
    if none fire → skip step 3
  ▼
(3) re-scrape Kimedics link via existing link_scraper path
    (requires KIMEDICS_EMAIL/PASSWORD if login-gated; skip with reason
    "login_required" if credentials absent)
    → re-parse fresh text
    → if parser output now DIFFERS on offending_fields → PATCH → done
  ▼
(4) last resort: drop offending_fields from the PATCH body, push remainder,
    emit sf_field_quarantined per dropped field
```

Each step only runs if the prior step did not resolve. Re-scraping is gated behind the
heuristics so we do not hit Kimedics for every failure.

### Rationale: why re-parse before re-scrape before drop

- **Re-parse first** is free and handles the most common case: the parser was buggy at
  scrape time but has since been fixed in a deploy. No I/O, no external calls.
- **Re-scrape only when heuristics suspect bad source data**. Cheap heuristics ("does
  the bad value look like it leaked from another field?") predict whether a re-scrape
  would help. Protects the Kimedics endpoint from being retried for failures caused
  by SF-side issues (picklist changes, etc.).
- **Field drop last**. The rest of the row is still valuable even if one field is
  broken; dropping lets that value land while quarantine flags the broken field for
  human follow-up.

## Event logging

All recovery-emitted events use `run_id = original_error.run_id` (see run_id strategy
above). The payload captures everything the hub needs without additional joins.

### Success: single `sf_scrape_fields_recovered`

One authoritative event per recovered job. The shape of `prev` / `next` / `fields_pushed`
deliberately mirrors `sf_scrape_fields_patched` so the hub's existing field-diff
renderer (`components/SfFieldCompared.tsx`) is reused.

```json
{
  "recovered_from_event_id": 697,
  "original_error": "Salesforce REST PATCH HTTP 400: Volume: data value too large …",
  "original_run_id": 474,
  "recovery_run_id": 478,
  "invocation": "modal_auto" | "manual_cli",
  "invoker": "modal:scrape_gmail_job" | "cli:andylee@hostname",
  "action": "re_parsed" | "re_scraped" | "field_dropped" | "transient_retried",
  "offending_fields": ["Job_Volume__c"],
  "fields_pushed": ["Job_City__c", "Job_State__c", ...],
  "fields_quarantined": [],
  "prev": { "Job_City__c": "Billings", "...": "..." },
  "next": { "Job_City__c": "Billings", "...": "..." },
  "sf_job_id": "a015f00000JPG4zAAH",
  "parser_version": "6f2a625"
}
```

This replaces the earlier "dual-write `sf_scrape_fields_patched` + `sf_scrape_fields_recovered`"
sketch. Since the hub can be updated, a single event type is cleaner:
- `sfPatches` stays a pure "primary push succeeded" counter.
- A new `sfRecovered` counter tracks recovery wins separately.
- Errors that get recovered stop counting toward `sfErrors` via a NOT EXISTS
  subquery (see Hub integration below), so run cards turn green after recovery.

### Failure / partial: `sf_field_quarantined`

One row per dropped field. `sibling_values` is the subset of other fields from the
original error event's `attempt` payload — used by reviewers to see which nearby
fields were present when the bad value appeared, helping diagnose line-shift bugs.
```json
{
  "field": "Job_Volume__c",
  "bad_value": "***Additional requirements/ info: …",
  "sibling_values": {"Insight__c": "*Must have active TX DEA …"},
  "scrape_url": "https://portal.kimedics.com/app/workspace/job-posts/19664",
  "parser_version": "6f2a625",
  "heuristic_fired": "starts_with_asterisks",
  "recovered_from_event_id": 697,
  "sf_error": "Salesforce REST PATCH HTTP 400: Volume: data value too large … (max length=50)"
}
```

### Unhandled: `sf_push_unhandled_error`
Emitted when `error_class = unknown`. Carries the original error verbatim so we can
spot new failure modes and add a rule.

### parser_version

Short git SHA of `src/utils/job_content_parser.py` at recovery time. Stored on every
quarantine event. Next recovery pass, if SHA differs from the SHA on a job's last
quarantine, that job is re-tried first. This is why "once you fix the parser, the
next run just works" — the quarantine is self-expiring when parser changes.

Implementation: `git log -n 1 --pretty=%h -- src/utils/job_content_parser.py`,
cached in-process per invocation.

## Hub integration (automation-hub sibling repo)

The companion spec lives at `proxi/automation-hub/docs/superpowers/specs/2026-04-23-sf-push-recovery-hub.md`.
Both specs ship as a coordinated pair. This section lists the contract — what the hub
must change so the new event types render correctly.

### Locations to touch

| File | Change |
|---|---|
| `lib/mergeSalesforceFieldEvents.ts` | Add the three new types to `SF_FIELD_SYNC_EVENT_TYPES`. |
| `lib/queries.ts` (`getDailyStatus`, `getRecentRuns`, `getWeeklySummary`) | Change `sf_errors` count to only count *unresolved* errors via `NOT EXISTS` subquery. Add `sf_recovered` and `sf_quarantined` counts. |
| `lib/queries.ts` (`getValidationData`) | Add new event types to the history LATERAL `IN (...)` list so they surface in `ValidationPopup`. |
| `lib/types.ts` | Add `sfRecovered`, `sfQuarantined` to `DayStatus`; `sfRecoveredCount`, `sfQuarantinedCount`, `sfQuarantinedFields: string[]` to `RunDetail`. |
| `lib/mappingLabels.ts` | Add titles for the three new event types. |
| `lib/utils.ts` (`getDayStatusKind`) | When `sfErrors === 0` and `sfRecovered > 0`, stay `operational`. |
| `components/ValidationPopup.tsx` | Timeline handlers for the three new types — icons, subtitles, and visual "resolved" marker on the paired `sf_scrape_fields_error`. |
| `components/LayerBreakdown.tsx` | Subtle "↻ N recovered" badge next to the error badge; yellow "N quarantined" chip listing fields. |
| `components/StatusBarChart.tsx` | Tooltip shows recovered count alongside errors. |

### Key SQL change (the one that makes errors "go away" after recovery)

Replaces the existing `sf_errors` aggregate everywhere it appears (`getDailyStatus`,
`getRecentRuns`, `getWeeklySummary`'s completed filter):

```sql
count(DISTINCT jel.id) FILTER (
  WHERE jel.event_type IN ('sf_scrape_fields_error','sf_mapping_pull_failed')
    AND NOT EXISTS (
      SELECT 1 FROM job_event_log ok
      WHERE ok.job_id = jel.job_id
        AND ok.event_type IN ('sf_scrape_fields_patched','sf_scrape_fields_recovered')
        AND ok.created_at >= jel.created_at
    )
) AS sf_errors
```

And a new aggregate:

```sql
count(DISTINCT jel.id) FILTER (WHERE jel.event_type = 'sf_scrape_fields_recovered') AS sf_recovered,
count(DISTINCT jel.id) FILTER (WHERE jel.event_type = 'sf_field_quarantined')       AS sf_quarantined
```

### Rollout order

1. Hub ships first (additive: new types recognized but produce zero rows until the
   automation emits them). Hub stays fully backwards compatible.
2. Automation ships recovery engine. Rows start appearing; hub dashboards update.

Shipping the automation first would still work — the hub would just ignore the
new event types (the existing `SF_FIELD_SYNC_EVENT_TYPES` filter drops them) and
errors would keep showing in `sfErrors`. No breakage, just the visual "covered"
state would lag until the hub update.

## Interfaces

```python
# utils/sf_push_recovery.py

@dataclass(frozen=True)
class RecoveryResult:
    job_id: int
    action: Literal["re_parsed", "re_scraped", "field_dropped", "retried_transient",
                    "quarantined", "unhandled", "skipped"]
    fields_pushed: list[str]
    fields_quarantined: list[str]
    error: str | None

def recover_recent_failures(
    conn, access_token: str, instance_url: str,
    since: datetime, *, run_id: int, schema: str = "public",
    dry_run: bool = False, limit: int | None = None,
    job_ids: list[int] | None = None,
) -> list[RecoveryResult]:
    ...

def recover_job_push(
    conn, access_token: str, instance_url: str,
    error_event: dict, *, run_id: int, schema: str = "public",
    dry_run: bool = False,
) -> RecoveryResult:
    ...
```

Supporting pieces split into small modules so each stays testable in isolation:

- `utils/sf_error_classify.py` — parses SF error text → `(error_class, fields)`.
  Pure function, table-driven, easily unit-tested.
- `utils/sf_recovery_rules.py` — the rule table + heuristic predicates (`starts_with_asterisks`,
  `value_appears_in_sibling`, `adjacent_empty`). Pure functions.
- `utils/sf_push_recovery.py` — the orchestrator that wires classify → rules →
  re-parse / re-scrape / push → logging. Thin.

## Modal integration

At the end of `scrape_gmail_job` in `src/production/scrape_gmail_modal.py`, after the
existing loop and before the function returns:

```python
from utils.sf_push_recovery import recover_recent_failures
from datetime import datetime, timedelta, timezone

results = recover_recent_failures(
    conn, access_token, instance_url,
    since=datetime.now(timezone.utc) - timedelta(hours=3),
    run_id=run_id,
)
# one-line summary printed; daily_summary_job picks it up via the new event types
```

Daily summary (`daily_summary_job`) gets one new line in the digest: counts of
`sf_scrape_fields_recovered` and `sf_field_quarantined` in the last 24 h, plus
the top few quarantined fields.

## Manual CLI

`src/local/repair_sf_push_errors.py`:

```
python src/local/repair_sf_push_errors.py                  # last 2 days, prod
python src/local/repair_sf_push_errors.py --since 2026-04-21T00:00:00Z
python src/local/repair_sf_push_errors.py --job-id 19664 --dry-run
python src/local/repair_sf_push_errors.py --limit 10
```

Creates its own `run_id` in `scrape_runs` with `source="manual_repair"` so the hub can
distinguish manual repair runs from scheduled ones.

Follows the existing repo convention: the CLI prompts for `STAGING` or `PRODUCTION`
(all caps) before any DB write, matching `local_run_scrape_gmail.py` and
`run_incremental.py`. Automated Modal runs bypass the prompt (no TTY).

## Testing

- `tests/test_sf_error_classify.py` — table of (error_text, expected_class, expected_fields),
  including the 19664 "Volume: data value too large" text, the old `Job_Ranking__c`
  "Required fields are missing" text, picklist errors, HTTP 5xx, DNS, unknown.
- `tests/test_sf_recovery_heuristics.py` — `starts_with_asterisks`,
  `value_appears_in_sibling`, `adjacent_empty` positive and negative cases.
- `tests/test_sf_push_recovery.py` — integration with mocked `conn`, mocked SF PATCH,
  mocked parser. Drives each branch: re-parse succeeds, re-parse fails → re-scrape
  succeeds, re-scrape fails → drop → quarantine, unhandled error path, transient retry.
- `tests/test_repair_sf_push_errors_cli.py` — invokes CLI with `--dry-run` against a
  temp Supabase schema preloaded with known error rows.

## Risks / open items

- **Re-scrape cost and rate limits.** Gated behind heuristics and the 3-hour window
  caps worst-case volume. Real safeguard: if heuristics fire on > 10 jobs in a single
  auto-recovery pass, abort and log `sf_recovery_circuit_open` — probably a systemic
  parser regression better handled by rolling back, not by hammering Kimedics.
- **Double-write on concurrent runs.** Auto-recovery overlapping with a freshly
  failing run could try to fix the same job twice. Mitigated by candidate filter
  (must have no newer success event) and Salesforce's idempotent PATCH semantics.
- **parser_version SHA** requires `git` available in the Modal runtime. Fallback:
  hash of the parser file's contents (`sha256sum` of `job_content_parser.py`) if
  git is not reachable.
- **Event volume.** Two success events + per-field quarantine events could noticeably
  increase `job_event_log` size. The extra volume only kicks in when there are
  failures — during normal operation the table sees no new rows from this system.

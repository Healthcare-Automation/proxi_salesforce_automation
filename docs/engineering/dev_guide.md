# Developer Guide

Everything you need to run, test, and maintain the Kimedics → Salesforce automation pipeline.

---

## Prerequisites

- Python 3.11+
- A `.venv` set up at the repo root (`python3.11 -m venv .venv && source .venv/bin/activate`)
- `pip install -r requirements.txt`
- A `.env` file at the repo root (see below)
- [Modal CLI](https://modal.com/docs/guide) installed and authenticated (`modal token new`)
- Playwright browsers: `playwright install chromium`

---

## Environment variables (`.env`)

```
# Gmail IMAP
GMAIL_APP_PASSWORD=...         # Google App Password for proxi@scrubnetwork.com

# Salesforce (Connected App / External Client App)
SALESFORCE_CONSUMER_KEY=...
SALESFORCE_CONSUMER_SECRET=...
SALESFORCE_USERNAME=...        # sean+proxi@practices.fyi
SALESFORCE_PASSWORD=...
SALESFORCE_TOKEN_URL=https://login.salesforce.com
SALESFORCE_SECURITY_TOKEN=...
SALESFORCE_USE_SANDBOX=false

# Kimedics portal (for Playwright login)
KIMEDICS_EMAIL=...
KIMEDICS_PASSWORD=...

# Supabase Postgres
DB_PASSWORD=...
```

All of these must also be added to the **Modal secret** named `salesforce-automation` (Modal dashboard → Secrets). Missing SF keys are the most common reason the mapping step silently skips.

---

## Modal — production pipeline

The single production job is `src/production/scrape_gmail_modal.py`. It runs every 30 minutes via a Modal scheduled function.

### Deploy
```bash
modal deploy src/production/scrape_gmail_modal.py
```

### Run once (without waiting for schedule)
```bash
modal run src/production/scrape_gmail_modal.py
```

### Check logs
Modal dashboard → Apps → `salesforce-automation` → Logs.

### What the scheduled job does
1. Fetch Gmail emails from the last 1 hour (`donotreply@kimedics.com`)
2. Dedup against Supabase `email_scrapes` (skip already-logged by `job_post_id` + date)
3. Log new emails → `scrape_runs` + `email_scrapes`
4. For each new email with a job link: Playwright → scrape the Kimedics job page → parse → write `job_content` + upsert `job_current`
5. Resolve Salesforce `sf_job_id` + `sf_worksite_account_id` for touched jobs (practice match → AI fallback → unmapped)
6. Patch blank Salesforce fields on already-mapped jobs (External ID, External Link, status, posted date)

---

## Local testing

### Test the full pipeline locally (last N hours)

Edit `EMAIL_HOURS` and `SUPABASE_LOOKBACK_HOURS` in `src/local/run_incremental.py`, then:

```bash
# Staging (safe — separate schema, no production impact)
python src/local/run_incremental.py --pg-schema staging --scrape

# Production (requires flag)
python src/local/run_incremental.py --pg-schema public --production-ok --scrape
```

`--scrape` chains in the Playwright batch after the email step. Without it, only Gmail → Supabase runs.

### Run the Gmail scrape step only (last 30 days)
```bash
python src/local/local_run_scrape_gmail.py --pg-schema staging --append
```

### Run the Playwright batch from a CSV
```bash
python tests/scrape_kimedics_batch_playwright.py --csv data/job_emails.csv --pg-schema staging
```

### Pull Salesforce jobs (read-only export)
```bash
python src/local/pull_salesforce_jobs.py
```

### Inspect Salesforce field metadata
```bash
python src/local/pull_salesforce_jobs.py --describe
```
Prints all `Job__c` fields split into required vs optional, with API names and types.

---

## Staging vs production

Local scripts prompt you to type `STAGING` or `PRODUCTION` before writing to Supabase (unless you pass `--pg-schema` explicitly). `PRODUCTION` → schema `public`. `STAGING` → schema `staging`.

Modal never uses this prompt; it always writes to `public`.

The staging schema is a full mirror of `public` (same tables, same columns). Use it for any destructive or experimental runs.

---

## Manual triggers (one-off Salesforce updates)

These are for when you need to push data to an existing Salesforce record manually, outside the automated pipeline.

### Update an existing `Job__c` record (Lever 1)
```bash
# Dry run — shows what would be sent
python src/dev/update_salesforce_job.py \
  --sf-id a015f00000bHXfQAAW \
  --from-supabase-job-id 19571 \
  --dry-run

# Live update
python src/dev/update_salesforce_job.py \
  --sf-id a015f00000bHXfQAAW \
  --from-supabase-job-id 19571 \
  --yes
```

Or use the notebook: `manual/triggers/update_existing_job.ipynb`

### Create a test `Job__c` record (Lever 2)
```bash
python src/dev/create_test_job_salesforce.py --auth-check  # auth only, no insert
python src/dev/create_test_job_salesforce.py --yes          # insert one test row
```

---

## Key file map

```
src/
  production/
    scrape_gmail_modal.py       ← THE production job (Modal)
  local/
    run_incremental.py          ← local equivalent of Modal job (last N hours)
    local_run_scrape_gmail.py   ← full Gmail scrape → CSV + Supabase
    pull_salesforce_jobs.py     ← read-only SF export + --describe
    staging_full_email_rescrape.py  ← rebuild staging email tables from Gmail
    staging_run_link_batch.py   ← run Playwright batch against staging email_scrapes
  dev/
    update_salesforce_job.py    ← manual PATCH to existing Job__c
    create_test_job_salesforce.py ← manual POST of test Job__c
  utils/
    supabase_db.py              ← all Supabase read/write helpers + schema setup
    salesforce.py               ← Salesforce OAuth + SOQL query client
    gmail.py                    ← Gmail IMAP scrape + email parser
    playwright_job_scrape.py    ← Playwright orchestration for job pages
    job_content_parser.py       ← parse scraped HTML/text into structured fields
    sf_job_supabase_resolve.py  ← Kimedics ↔ SF Job__c resolver (cache → practice match → AI fallback)
    sf_ai_matcher.py            ← OpenAI gpt-4o-mini fuzzy fallback for unmatched practice values
    sf_scrape_sync.py           ← patch blank SF fields after resolve
    sf_job_payload.py           ← build the SF REST payload for create/update
    sf_practice_key.py          ← normalize practice_value for matching
    sf_push_defaults.py         ← static defaults applied at push time
    job_sf_enrichment.py        ← enrich job row with SF account IDs
    sf_job_rest_minimal.py      ← low-level SF REST helpers
    sf_partial_update.py        ← filter payload to updateable fields only
    sf_pay_range.py             ← extract pay range from raw description
    job_description_proxi_template.py ← canonical HTML job description builder
    us_state_expand.py          ← TX → Texas etc.
    link_scraper.py             ← HTTP link follower (non-Playwright)
    run_target_prompt.py        ← staging/production prompt helper

tests/
  scrape_kimedics_batch_playwright.py  ← local batch runner (used by run_incremental --scrape)
  test_*.py                            ← pytest unit tests
  conftest.py                          ← pytest fixtures

manual/
  sf_kimedics_mapping/         ← one-off notebooks for bulk SF mapping fixes
  triggers/                    ← notebooks for manual job create/update

docs/
  engineering/
    dev_guide.md                 ← this file
    salesforce_job_push_rules.md ← field mapping reference
    scrape_validation.md         ← scrape validation & alerting logic
  overview/
    client_overview.md           ← stakeholder-facing overview
```

---

## Salesforce ID resolution (`sf_job_id` + `sf_worksite_account_id`)

Every scraped Kimedics job must be linked to a Salesforce `Job__c` record (`sf_job_id`) and its associated worksite account (`sf_worksite_account_id`). The resolver runs automatically after each scrape and tries four strategies in order:

### Resolution order

| Step | Source | Condition | Notes |
|---|---|---|---|
| 1 | `job_current` cache | Both IDs already set | Fast path — no SF call needed |
| 2 | `job_content` history | Any previous scrape for this `job_id` has an ID | Carry-forward from older scrapes |
| 3 | Deterministic practice match | `practice_key(practice_value)` == `practice_key(Job_Client_Job_Id__c)` and exactly 1 hit | Normalizes Unicode dashes, case, punctuation |
| 4 | **AI fuzzy match** | Step 3 found 0 hits | `gpt-4o-mini` via `sf_ai_matcher.py` (see below) |
| — | `mapping_no_match` | All above failed | Job flagged as unmapped; won't be pushed to SF |

### AI fuzzy matching (`sf_ai_matcher.py`)

The deterministic `practice_key` normalizer handles most differences (dashes, case, punctuation) but fails on two recurring patterns:

| Pattern | Kimedics example | Salesforce example |
|---|---|---|
| Apostrophe in city name | `3185 - St. Joseph, MO` | `3185- St. Joseph's, MO` |
| Extra location suffix | `4140 - Suffolk, VA` | `4140 - Suffolk, VA- Downtown` |

The AI fallback resolves these without any manual intervention:

1. **Extract facility number** — pulls the leading 3–5 digit ID from the Kimedics `practice_value` (e.g. `3185` from `"3185 - St. Joseph, MO"`). This number is the strongest cross-system identifier.
2. **Pre-filter SF candidates** — scans all ~4400 SF `Job__c` records and keeps only those whose `Job_Client_Job_Id__c` starts with the same facility number. Typically 1–5 records. If 0, OpenAI is never called.
3. **Ask `gpt-4o-mini`** — sends the Kimedics value and the small candidate list. Model answers with `{"match": "...", "confidence": "high"|"medium"|"low"}`.
4. **Safety check** — the returned value must be present in the pre-filtered candidate list (prevents hallucinated answers). Only `high` or `medium` confidence is acted on.

**Cost**: ~$0.00003 per call (150–250 input tokens on `gpt-4o-mini`). Only fires on misses, so real-world cost is near zero.

**Requires** `OPENAI_API_KEY` in the Modal secret `salesforce-automation` (already set).

### Querying resolution outcomes

```sql
-- See every AI match that fired today
SELECT job_id, payload->>'kimedics_practice' AS kimedics, payload->>'sf_matched_value' AS sf_match,
       payload->>'confidence' AS confidence, created_at
FROM job_event_log
WHERE event_type = 'mapping_ai_match'
ORDER BY created_at DESC;

-- Jobs that are still unmatched (neither deterministic nor AI worked)
SELECT job_id, payload->>'practice_raw' AS practice, payload->>'ai_attempted' AS ai_attempted
FROM job_event_log
WHERE event_type = 'mapping_no_match'
ORDER BY created_at DESC;
```

### Manual notebook

`manual/sf_kimedics_mapping/sf_job_worksite_resolve.ipynb` — run the full resolver (including AI fallback) against all `job_current` rows, inspect results, and apply to Supabase in one notebook.

---

## Debugging via `job_event_log`

Every meaningful pipeline decision is written to `job_event_log` in Supabase. Query it to see what happened to any job:

```sql
-- Full event history for a job, with job context
SELECT
    e.created_at,
    e.event_type,
    e.run_id,
    e.payload,
    jc.practice_value,
    jc.sf_job_id,
    jc.sf_worksite_account_id
FROM job_event_log e
LEFT JOIN job_current jc USING (job_id)
WHERE e.job_id = '19571'
ORDER BY e.created_at DESC;

-- All SF mapping outcomes across a run
SELECT job_id, event_type, payload
FROM job_event_log
WHERE run_id = 119
ORDER BY created_at;
```

### Event types

| `event_type` | Meaning |
|---|---|
| `sf_mapping_skipped` | SF credentials missing from env/Modal secret |
| `sf_mapping_pull_failed` | SOQL query to Salesforce failed (payload has `error`) |
| `mapping_cache_hit` | Both SF ids already on `job_current`; no lookup needed |
| `mapping_ambiguous` | Multiple `Job__c` records matched the same practice key |
| `mapping_ai_match` | Deterministic match failed; AI fuzzy-matched the practice value (payload has `kimedics_practice`, `sf_matched_value`, `confidence`) |
| `mapping_no_match` | No Salesforce match found — neither deterministic nor AI (payload has `ai_attempted` flag) |
| `sf_ids_update` | SF ids written to Supabase (payload has `prev`/`next` diff) |
| `sf_sync_skipped_no_mapping` | Scrape sync skipped — no `sf_job_id` on this job yet (resolver didn't find a match) |
| `sf_scrape_fields_patched` | Fields patched on Salesforce successfully |
| `sf_scrape_fields_error` | Salesforce PATCH rejected (payload has `error` + attempted fields) |

---

## Common issues

**`sf_mapping_skipped` on every job**
→ SF credentials are missing from the Modal secret. Add all `SALESFORCE_*` vars to `salesforce-automation` in Modal dashboard, then redeploy.

**`sf_mapping_pull_failed` with SOQL error**
→ Check that `Job_Client_Job_Id__c` and `Job_Worksite_Location_1__c` are accessible to the integration user (Field-Level Security in Salesforce Setup).

**`sf_scrape_fields_error` — Required fields are missing: [Job_Ranking__c]**
→ Salesforce requires `Job_Ranking__c` on PATCH even when we're only updating other fields. Add it to `SCRAPE_SYNC_FIELD_ORDER` in `sf_scrape_sync.py` or make it optional in the SF org.

**`mapping_no_match` when you know the job exists in SF**
→ First check if the AI fallback was attempted (`ai_attempted: true` in the payload). If so, the facility number (e.g. `3185`) doesn't exist in Salesforce at all — the job may not be created there yet.

If `ai_attempted: false`, the `practice_value` was blank — check the scrape for that job.

For manual diagnosis: run `pull_salesforce_jobs.py` to see live `Job_Client_Job_Id__c` values, then compare with `practice_value` in `job_current`. Use `sf_practice_key.practice_key()` to normalize both and see if they should collide.

**AI is returning `mapping_no_match` for a job you expect to match**
→ The pre-filter in `sf_ai_matcher.py` requires the Kimedics and SF values to share the same leading facility number (e.g. both start with `3185`). If the numbers differ between systems, AI won't be triggered. Check `Job_Client_Job_Id__c` in Salesforce vs `practice_value` in `job_current` to confirm.

**Playwright login failing**
→ Check `KIMEDICS_EMAIL` / `KIMEDICS_PASSWORD` in `.env`. Try `tests/scrape_kimedics_batch_playwright.py` locally with `--pg-schema staging` to see the raw browser output.

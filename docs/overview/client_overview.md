# Automation Overview — Kimedics → Salesforce

This document explains what the automation does, how it works, what it keeps track of, and where its limits are. No technical background required.

---

## What problem does this solve?

Kimedics sends email notifications every time a dental job post changes — a new posting, an update, a closure. Without automation, someone would need to read each email, look up the job in Kimedics, and manually update the corresponding record in Salesforce. That's slow, error-prone, and doesn't scale.

This automation watches that email inbox, reads the job details automatically, and keeps Salesforce up to date — without anyone having to do it by hand.

---

## The big picture

```
  Every 30 minutes
        │
        ▼
┌───────────────────┐
│   Gmail Inbox     │  ← Kimedics sends job notification emails here
│ (proxi@scrub...)  │
└────────┬──────────┘
         │  New emails only (already-seen ones are skipped)
         ▼
┌───────────────────┐
│   Parse Email     │  ← Extract: job #, location, action (new/updated/closed), link
└────────┬──────────┘
         │
         ▼
┌───────────────────┐
│  Visit Job Page   │  ← Open the "View job post" link in a headless browser
│  (Kimedics site)  │  ← Scrape: title, city/state, dates, schedule, cases, pay, etc.
└────────┬──────────┘
         │
         ▼
┌───────────────────┐
│    Supabase DB    │  ← Store everything: every version of every job, permanently
│  (our database)   │
└────────┬──────────┘
         │  Match to existing Salesforce records
         ▼
┌───────────────────┐
│   Salesforce      │  ← Update the matched Job__c record with latest data
│   (CRM)           │
└───────────────────┘
```

---

## Step by step

### Step 1 — Watch the inbox

Every 30 minutes, the automation checks the Gmail inbox for emails from Kimedics. It only processes emails it hasn't seen before, so nothing gets double-processed.

Each email tells us:
- Which job it's about (a job number like `19571`)
- What changed (new posting, update, closure)
- A link to view the full job details on Kimedics

### Step 2 — Scrape the job page

The automation opens the job link in a browser (invisibly, in the cloud) and reads the full job posting. It extracts:

| What it reads | Example |
|---|---|
| Practice / facility name | `2387 - Benton Harbor, MI` |
| City and state | Benton Harbor, MI |
| Job title | Associate Dentist |
| Status | Active, accepting new providers |
| Dates needed | April 14–18, 2026 |
| Standard schedule | Mon–Fri, 8am–5pm |
| Types of cases | General dentistry, hygiene checks |
| Support staff | 2 dental assistants, front desk |
| Pay range | Starting at $125/hour |
| Posted date | March 25, 2026 |
| Point of contact | Jane Smith |

### Step 3 — Save to our database

Everything scraped is saved to Supabase, our cloud database. This happens even if Salesforce is temporarily unavailable — the data is never lost. We keep a full history:

- `email_scrapes` — one row per email received
- `job_content` — one row per scrape of each job (full history of every version)
- `job_current` — one row per job showing only the **latest** version

This means you can always look back and see what a job looked like on any given date.

### Step 4 — Match to Salesforce

Before updating Salesforce, the automation needs to figure out which Salesforce record corresponds to which Kimedics job. It does this in order:

```
Does Supabase already have the Salesforce ID cached?
  ├─ YES → Use it directly (fast path)
  └─ NO  → Check job history for a cached ID
             ├─ FOUND → Use it
             └─ NOT FOUND → Query Salesforce
                             │
                             Match by practice name
                             (Kimedics "practice_value" vs Salesforce "Job_Client_Job_Id__c")
                             │
                             ├─ Exactly 1 match → Resolved ✓
                             ├─ Multiple matches → Ambiguous, skip (logged)
                             └─ No match → No match (logged)
```

Once matched, the Salesforce ID is cached in our database so future runs use the fast path.

### Step 5 — Update Salesforce

Once we have a match, the automation patches the Salesforce `Job__c` record with:
- External Job ID (the Kimedics job number)
- External Job Link (direct URL to the Kimedics posting)
- Job Status
- Posted date

Larger field updates (full description, city, state, dates, etc.) are done via manual triggers — see the section on manual updates below.

---

## What gets logged (and why it matters)

Every decision the automation makes is written to an event log (`job_event_log`). You can query it to understand exactly what happened to any job at any point. Examples:

| What you'd see | What it means |
|---|---|
| `mapping_cache_hit` | The automation already knew this job's Salesforce ID — fast |
| `sf_ids_update` | Successfully matched and saved the Salesforce ID for this job |
| `mapping_no_match` | Couldn't find a matching Salesforce record — needs attention |
| `mapping_ambiguous` | Found more than one possible match — human review needed |
| `sf_scrape_fields_patched` | Salesforce was updated successfully |
| `sf_scrape_fields_error` | Salesforce rejected the update — error details are saved |

Nothing is silently dropped. If something goes wrong, there's always a record of what was tried and what the error was.

---

## Timing and cadence

```
Timeline (example: emails arrive at 2:05 PM)

  2:00 PM ─── Automation runs (checks last 1 hour)
               No new emails → done in seconds

  2:05 PM ─── 3 job emails arrive in inbox

  2:30 PM ─── Automation runs again
               Sees 3 new emails → scrapes job pages → updates Supabase + Salesforce
               Done in ~2–3 minutes

  3:00 PM ─── Automation runs again
               Those 3 emails already logged → skipped
               Done in seconds
```

**Maximum delay from email to Salesforce:** ~30 minutes (one scheduling interval).

**In practice:** Most emails that arrive just after a run will be picked up on the next cycle. The 1-hour lookback window means emails are never missed even if a run fails and retries.

---

## What the automation does NOT do

These are important caveats:

**❌ Does not create new Salesforce records**
If a Kimedics job has no matching `Job__c` in Salesforce yet, the automation saves the job data to our database but does not create the Salesforce record. Someone needs to create it first. Once it exists (and the practice name matches), future runs will pick it up automatically.

**❌ Does not overwrite existing Salesforce data**
The scrape-sync step only fills in fields that are currently *blank* in Salesforce. It will not overwrite something a human has already entered.

**❌ Does not handle ambiguous matches**
If two Salesforce `Job__c` records have the same practice name, the automation flags it as ambiguous and skips both. This needs a human to resolve.

**❌ Only tracks jobs that send email notifications**
The automation is triggered by Kimedics emails. If a job changes in Kimedics without triggering an email, the automation won't know about it.

**❌ Does not push the full job description automatically**
Full description pushes (with all structured fields populated in the SF record) are done via manual triggers. The automated pipeline patches a smaller set of fields.

---

## Manual updates (when automation isn't enough)

For jobs that need a full update pushed to Salesforce — with city, state, dates, schedule, description, etc. — there are manual tools:

**Update an existing Salesforce record** from our database:
Handled via `src/dev/update_salesforce_job.py` or the notebook `manual/triggers/update_existing_job.ipynb`. You provide the Salesforce record ID and Kimedics job ID, and it builds and pushes the full payload.

**Create a new Salesforce record:**
`manual/triggers/create_new_job.ipynb` — for when a job exists in Kimedics but not yet in Salesforce.

---

## Where data lives

```
┌─────────────────────────────────────────────────────┐
│                    Supabase (our DB)                 │
│                                                      │
│  email_scrapes ──── one row per email received       │
│  job_content   ──── full history of every scrape     │
│  job_current   ──── latest state per job             │
│  job_event_log ──── audit trail of every decision    │
│  scrape_runs   ──── one row per pipeline execution   │
│  sf_account_reference ── Salesforce account IDs      │
└─────────────────────────────────────────────────────┘
```

Data in Supabase is permanent. Nothing is deleted when Salesforce is updated. If you ever need to know what a job looked like on a specific date, the history is there.

---

## What "practice match" means

Kimedics identifies a job with a "practice value" like `2387 - Benton Harbor, MI`. Salesforce uses a field called `Job_Client_Job_Id__c` that should contain the same value.

The automation normalizes both (removes punctuation differences, lowercase, strips things like "(closed)") and checks for an exact 1-to-1 match. If they normalize to the same key, it's a match.

**This is the most important thing to keep clean:** the `Job_Client_Job_Id__c` field in Salesforce needs to match what Kimedics sends. If they diverge, the automation can't link the two systems.

---

## Monitoring

The automation runs in Modal (a cloud platform). Logs are available in the Modal dashboard under the `salesforce-automation` app.

In Supabase, you can query `job_event_log` to see exactly what happened to any job at any time. A query like this shows recent activity:

```sql
SELECT created_at, job_id, event_type, payload
FROM job_event_log
ORDER BY created_at DESC
LIMIT 50;
```

Any `sf_scrape_fields_error` rows need attention — they mean Salesforce rejected an update, and the `payload` column explains why.

---

## Glossary

| Term | Meaning |
|---|---|
| **Kimedics** | The job management platform that sends email notifications |
| **Supabase** | Our database — stores all emails, job data, and event history |
| **Salesforce** | The CRM where job records live (`Job__c` records) |
| **Modal** | The cloud platform that runs the automation every 30 minutes |
| **practice_value** | The facility identifier Kimedics uses (e.g. `2387 - Benton Harbor, MI`) |
| **Job_Client_Job_Id__c** | The matching field in Salesforce — must match practice_value |
| **sf_job_id** | The Salesforce record ID (18-char, starts with `a01...`) |
| **scrape** | Visiting a web page programmatically to read its content |
| **staging** | A safe copy of our database for testing — changes don't affect production |
| **job_event_log** | Audit log — every mapping decision, SF update, and error |

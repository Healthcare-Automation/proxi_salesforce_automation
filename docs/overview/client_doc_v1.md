# Kimedics to Salesforce Automation

*A full walkthrough of how the pipeline works, what it tracks, and what has already been verified.*

---

## Architecture at a Glance

```
  ┌─────────────────────────────────────────────────────────────────────┐
  │                     MODAL  (Cloud Scheduler)                        │
  │              Runs automatically every 30 minutes, 24/7              │
  └────────────────────────────┬────────────────────────────────────────┘
                               │
            ┌──────────────────▼──────────────────┐
            │             Gmail Inbox              │
            │   Reads new Kimedics emails only     │
            │   New  /  Updated  /  Closed jobs    │
            └──────────────────┬──────────────────┘
                               │
            ┌──────────────────▼──────────────────┐
            │           Kimedics Website           │
            │   Logs in with stored credentials    │
            │   Reads every field on the job page  │
            └──────────────────┬──────────────────┘
                               │
            ┌──────────────────▼──────────────────┐
            │         Supabase  (Database)         │
            │   Every email, every scrape,         │
            │   every version, every event         │
            │   Permanent historical record        │
            └──────────────────┬──────────────────┘
                               │
            ┌──────────────────▼──────────────────┐
            │             Salesforce               │
            │   Matched records updated safely     │
            │   Only blank fields filled (phase 1) │
            └─────────────────────────────────────┘
```

---

## The Five Systems Working Together


| #   | Platform       | Role                                                          |
| --- | -------------- | ------------------------------------------------------------- |
| 1   | **Gmail**      | Receives Kimedics job notification emails                     |
| 2   | **Kimedics**   | The automation logs in and reads each full job posting        |
| 3   | **Supabase**   | Private cloud database -- permanent record of everything      |
| 4   | **Salesforce** | CRM where job records are updated automatically               |
| 5   | **Modal**      | Cloud scheduler that runs the pipeline every 30 minutes, 24/7 |


---

## How It Works, Step by Step

### Step 1 -- Inbox Monitoring

Every 30 minutes, the automation checks the Gmail inbox for new emails from Kimedics. It only processes emails it has not seen before, so nothing gets handled twice.

Kimedics sends three types of notifications, and the automation captures all of them:


| Email Type      | What It Means                          |
| --------------- | -------------------------------------- |
| **New posting** | A brand-new job has been listed        |
| **Update**      | An existing job's details have changed |
| **Closure**     | A job has been closed or filled        |


Each email is parsed and the following fields are extracted and stored immediately:


| Field Captured              | Example                            |
| --------------------------- | ---------------------------------- |
| Job post ID                 | `19571`                            |
| Location / practice label   | `2387 - Benton Harbor, MI`         |
| Action / change type        | `new`, `updated`, `status: Closed` |
| View job link               | Direct URL to the Kimedics posting |
| Email subject, date, sender | Metadata stored alongside the job  |


**Source (GitHub)**

- `src/production/scrape_gmail_modal.py`  
[https://github.com/AndyLeeProjects/proxi_salesforce_automation/blob/main/src/production/scrape_gmail_modal.py](https://github.com/AndyLeeProjects/proxi_salesforce_automation/blob/main/src/production/scrape_gmail_modal.py)
- `src/utils/gmail.py`  
[https://github.com/AndyLeeProjects/proxi_salesforce_automation/blob/main/src/utils/gmail.py](https://github.com/AndyLeeProjects/proxi_salesforce_automation/blob/main/src/utils/gmail.py)

---

### Step 2 -- Reading the Full Job Page

The automation logs into Kimedics using stored credentials and opens each job link in a headless browser (invisible, running in the cloud). It reads and stores every structured field from the posting:


| Field Captured           | Example                                 |
| ------------------------ | --------------------------------------- |
| Practice / facility name | `2387 - Benton Harbor, MI`              |
| City and state           | Benton Harbor, MI                       |
| Job title                | `#19571: Dentistry (Dentist (DMD/DDS))` |
| Status                   | Active, accepting new providers         |
| Dates needed             | April 14-18, 2026                       |
| Standard schedule        | Mon-Fri, 8am-5pm                        |
| Types of cases           | General dentistry, hygiene checks       |
| Support staff            | 2 dental assistants, front desk         |
| Pay range                | Starting at $125/hour                   |
| Posted date              | March 25, 2026                          |
| Point of contact         | Jane Smith                              |
| Full job description     | Complete text, 300-1,000+ characters    |


> **Note:** This step requires valid Kimedics credentials stored securely in the environment. If credentials expire or change, this step fails and an alert fires immediately.

**Source (GitHub)**

- `src/utils/playwright_job_scrape.py`  
[https://github.com/AndyLeeProjects/proxi_salesforce_automation/blob/main/src/utils/playwright_job_scrape.py](https://github.com/AndyLeeProjects/proxi_salesforce_automation/blob/main/src/utils/playwright_job_scrape.py)
- `src/utils/job_content_parser.py`  
[https://github.com/AndyLeeProjects/proxi_salesforce_automation/blob/main/src/utils/job_content_parser.py](https://github.com/AndyLeeProjects/proxi_salesforce_automation/blob/main/src/utils/job_content_parser.py)

---

### Step 3 -- Saving to the Database (Supabase)

Before anything touches Salesforce, every piece of data is saved to Supabase. This is one of the most important parts of the system.

**What is stored and why it matters:**

- **Nothing is ever lost.** Even if Salesforce is temporarily unavailable, the data is already safe.
- **Every version of every job is kept.** The full history is preserved -- it is always possible to see what any job looked like on any given date.
- **Every event is recorded.** Not just the data, but every match attempt, every Salesforce update, every failure, every skip. A complete audit trail across every run.

**What is being tracked in detail:**

```
scrape_runs
  run_id, run_type, started_at, finished_at, duration, emails_seen, jobs_processed

email_scrapes
  job_post_id, location, action_or_change, view_job_link, subject, date, from_address

job_content  [FULL HISTORY -- every version of every job]
  job_id, job_title, status, city, state, posting_org, practice_value,
  point_of_contact, provider_start_date, provider_end_date, posted_date,
  rates, description_full_text, scraped_at, scrape_run_id

job_current  [latest snapshot per job]
  same fields as job_content -- updated on each successful scrape

job_event_log  [audit trail]
  created_at, job_id, event_type, payload
  -- every mapping decision, SF update, error, and resolution recorded here
```

**Source (GitHub)**

- `src/utils/supabase_db.py`  
[https://github.com/AndyLeeProjects/proxi_salesforce_automation/blob/main/src/utils/supabase_db.py](https://github.com/AndyLeeProjects/proxi_salesforce_automation/blob/main/src/utils/supabase_db.py)

> Data in Supabase is permanent and never deleted. Historical records are the safety net for audits, debugging, and verification.

---

### Step 4 -- Matching to the Right Salesforce Record

This is where significant engineering complexity lives. Before updating Salesforce, the automation must determine which Salesforce record corresponds to the Kimedics job it just scraped.

It does this through a multi-stage resolution process -- fastest path first, escalating until a match is found or the job is flagged:

```
  Is the Salesforce ID already cached in the database?
  |
  |-- YES  --> Use it directly (instant, zero API calls needed)
  |
  |-- NO   --> Check job history for a previously resolved ID
                |
                |-- FOUND  --> Use it
                |
                |-- NOT FOUND  --> Query Salesforce directly
                                    |
                                    Normalize both sides and match by practice name
                                    Kimedics "practice_value" vs SF "Job_Client_Job_Id__c"
                                    |
                                    |-- Exactly 1 match  --> Resolved
                                    |
                                    |-- No match         --> No SF record yet (logged)
                                    |
                                    |-- Multiple matches --> Ambiguous
                                                            |
                                                            OpenAI API steps in
                                                            Analyzes context to pick
                                                            the most likely match
                                                            |
                                                            |-- High confidence  --> Resolved
                                                            |-- Uncertain        --> Flagged for review
```

Once a match is confirmed, the Salesforce ID is cached so all future runs for that job skip straight to the instant path.

**Source (GitHub)**

- `src/utils/sf_job_supabase_resolve.py`  
[https://github.com/AndyLeeProjects/proxi_salesforce_automation/blob/main/src/utils/sf_job_supabase_resolve.py](https://github.com/AndyLeeProjects/proxi_salesforce_automation/blob/main/src/utils/sf_job_supabase_resolve.py)
- `src/utils/sf_ai_matcher.py`  
[https://github.com/AndyLeeProjects/proxi_salesforce_automation/blob/main/src/utils/sf_ai_matcher.py](https://github.com/AndyLeeProjects/proxi_salesforce_automation/blob/main/src/utils/sf_ai_matcher.py)

> **The most important data hygiene rule:** The `Job_Client_Job_Id__c` field in Salesforce must match how Kimedics labels each practice (e.g. `2387 - Benton Harbor, MI`). The automation normalizes both sides but the core identifier must align. If they drift apart, the automation cannot link the two systems.

---

### Step 5 -- Updating Salesforce

Once there is a confirmed match, the automation patches the Salesforce `Job__c` record.

**Current push scope (Phase 1 -- Testing):**

The automation is currently writing 4 fields to Salesforce:


| Field             | What It Contains                    |
| ----------------- | ----------------------------------- |
| External Job ID   | The Kimedics job number             |
| External Job Link | Direct URL to the Kimedics posting  |
| Job Status        | Active, closed, or updated          |
| Posted Date       | When the job was listed on Kimedics |


This is intentional. The pipeline was built and validated against these 4 fields first to confirm the end-to-end push works before expanding scope. It does work. Every update is logged, and the system has been verified against Salesforce's change logs.

**What comes next (Phase 2 -- Production):**

The system is fully architected to push a much broader set of fields: city, state, dates, schedule, full description, pay range, point of contact, and more. It can also create new Salesforce records directly from scraped Kimedics data, and replace existing field values rather than just filling blanks. Expanding scope is a configuration change, not a rebuild. The groundwork is already in place and waiting for the green light.

**Source (GitHub)**

- `src/utils/sf_scrape_sync.py`  
[https://github.com/AndyLeeProjects/proxi_salesforce_automation/blob/main/src/utils/sf_scrape_sync.py](https://github.com/AndyLeeProjects/proxi_salesforce_automation/blob/main/src/utils/sf_scrape_sync.py)
- `src/utils/sf_job_payload.py`  
[https://github.com/AndyLeeProjects/proxi_salesforce_automation/blob/main/src/utils/sf_job_payload.py](https://github.com/AndyLeeProjects/proxi_salesforce_automation/blob/main/src/utils/sf_job_payload.py)
- `docs/engineering/salesforce_job_push_rules.md`  
[https://github.com/AndyLeeProjects/proxi_salesforce_automation/blob/main/docs/engineering/salesforce_job_push_rules.md](https://github.com/AndyLeeProjects/proxi_salesforce_automation/blob/main/docs/engineering/salesforce_job_push_rules.md)

---

## Live System Dashboard

**Automation Hub (live):** [https://automation-hub-rosy.vercel.app](https://automation-hub-rosy.vercel.app)

The Automation Hub is a live dashboard built specifically for this pipeline. It shows the real-time health of the automation at a glance -- no technical knowledge needed to read it.

What is visible at any time:


| Metric                    | What It Shows                                                          |
| ------------------------- | ---------------------------------------------------------------------- |
| System Status             | Green (all operational) or flagged (needs attention)                   |
| Total emails processed    | Running count since launch                                             |
| Total jobs scraped        | How many Kimedics jobs have been read and stored                       |
| Salesforce records synced | How many SF records have been updated                                  |
| Run success rate          | Percentage of successful runs over the last 90 days                    |
| Per-run history           | Every single run: timestamp, duration, emails / jobs / SF push per run |
| Pipeline phase            | Current phase (Testing, Production, etc.) with start date              |


This dashboard is always live. There is no need to ask if the automation is running -- the status page shows every run in real time.

As the automation expands to additional workflows, each new pipeline will appear on this same status page. It becomes a single place to monitor everything.

---

## Every Decision Is Logged

Every single action the automation takes is written to the event log in Supabase. At any time, it is possible to look up exactly what happened to any job and why.


| Event                      | What It Means                                              |
| -------------------------- | ---------------------------------------------------------- |
| `mapping_cache_hit`        | Already had this job's Salesforce ID -- ran instantly      |
| `sf_ids_update`            | Successfully matched a Kimedics job to a Salesforce record |
| `mapping_no_match`         | No matching SF record found -- logged and pending          |
| `mapping_ambiguous`        | Multiple possible matches -- OpenAI attempted resolution   |
| `sf_scrape_fields_patched` | Salesforce updated successfully                            |
| `sf_scrape_fields_error`   | Salesforce rejected an update -- error details saved       |


Nothing is ever silently dropped. If something goes wrong, there is always a record of what was attempted and what the error was.

**Source (GitHub)**

- `src/utils/sf_job_supabase_resolve.py`  
[https://github.com/AndyLeeProjects/proxi_salesforce_automation/blob/main/src/utils/sf_job_supabase_resolve.py](https://github.com/AndyLeeProjects/proxi_salesforce_automation/blob/main/src/utils/sf_job_supabase_resolve.py)

---

## Timing

```
2:00 PM -- Automation runs. No new emails. Done in seconds.

2:05 PM -- 3 Kimedics emails arrive in the inbox.

2:30 PM -- Automation runs again.
            Sees 3 new emails -- scrapes job pages -- updates database + Salesforce.
            Done in approximately 2-3 minutes.

3:00 PM -- Automation runs again.
            Those 3 emails already logged -- skipped. Done in seconds.
```

Maximum delay from email arrival to Salesforce update: **30 minutes.**

---

## Validation and Alerting

One of the most important things built into this system is proactive validation. The automation does not push data blindly. It checks the quality of every scrape before anything touches Salesforce.

### Five Validation Layers (Every Job, Every Run)


| Layer                 | What It Checks                                                                   |
| --------------------- | -------------------------------------------------------------------------------- |
| **Structural**        | Did the Kimedics page load at all? Did the page layout change unexpectedly?      |
| **Critical fields**   | Are job title, status, and start date all present?                               |
| **Important fields**  | Are location, city, state, practice name, and point of contact present?          |
| **Format**            | Are dates formatted correctly? Is the status one of the 3 known Kimedics values? |
| **Cross-field logic** | Do fields agree with each other? Are there contradictions or impossible values?  |


The validator was calibrated against 554 real job records to establish exactly what a correctly scraped job looks like. Any deviation from that baseline triggers an alert.

**Source (GitHub)**

- `src/utils/scrape_validator.py`  
[https://github.com/AndyLeeProjects/proxi_salesforce_automation/blob/main/src/utils/scrape_validator.py](https://github.com/AndyLeeProjects/proxi_salesforce_automation/blob/main/src/utils/scrape_validator.py)

### Two Types of Alerts

**Immediate alert** -- fires the moment a problem is detected:

Triggered when a scrape has any critical issue, or 3+ warnings on a single job. The alert includes the specific issues found, a full field-by-field snapshot of what was and was not captured, and a direct link to the job on Kimedics.

**Daily digest email** -- sent every morning:

A summary of the previous 24 hours:

- How many emails were received
- How many jobs were scraped successfully
- Partial scrapes and failures
- Salesforce mapping status (matched vs still pending)
- List of any jobs that need attention

> *[Screenshot of validation alert email to be inserted here]*

### Note on Email Recipients

Both alert types are currently sent to the internal Proxi team only. This is intentional -- the team is actively monitoring and maintaining the automation during this phase. The alert system can be extended to include client recipients at any time if preferred.

**Source (GitHub)**

- `src/utils/alert_email.py`  
[https://github.com/AndyLeeProjects/proxi_salesforce_automation/blob/main/src/utils/alert_email.py](https://github.com/AndyLeeProjects/proxi_salesforce_automation/blob/main/src/utils/alert_email.py)

---

## Current Phase and Limits

### Phase 1 (Active Now -- Testing)

**Field push is scoped to 4 fields**
The pipeline pushes External Job ID, External Job Link, Job Status, and Posted Date. These were chosen to validate the full pipeline end-to-end. They work. Expanding to additional fields is ready and waiting for the green light.

**Only fills blank Salesforce fields**
In Phase 1, the sync step only writes to fields that are currently empty. This ensures nothing is overwritten during the testing period. In Phase 2, the automation will update fields regardless of whether a value already exists -- replacing stale data with the latest from Kimedics.

**New Salesforce records**
In Phase 1, the automation does not create new `Job__c` records. If a job exists in Kimedics but not yet in Salesforce, the data is captured and held in the database. Creating new records from scraped Kimedics data is fully supported in the system and will be activated once given the go-ahead.

**Ambiguous practice name matches**
In rare cases, two Salesforce records share the same practice name. When this happens, OpenAI attempts to resolve the ambiguity. If it cannot do so with confidence, the job is flagged rather than guessed. The Proxi team receives an immediate alert and reviews manually. Based on current data, this situation is extremely uncommon.

**Source (GitHub)**

- `src/utils/sf_scrape_sync.py`  
[https://github.com/AndyLeeProjects/proxi_salesforce_automation/blob/main/src/utils/sf_scrape_sync.py](https://github.com/AndyLeeProjects/proxi_salesforce_automation/blob/main/src/utils/sf_scrape_sync.py)
- `src/utils/sf_ai_matcher.py`  
[https://github.com/AndyLeeProjects/proxi_salesforce_automation/blob/main/src/utils/sf_ai_matcher.py](https://github.com/AndyLeeProjects/proxi_salesforce_automation/blob/main/src/utils/sf_ai_matcher.py)

---

## Glossary


| Term                     | Plain English                                                                  |
| ------------------------ | ------------------------------------------------------------------------------ |
| **Kimedics**             | The platform that manages dental job postings and sends email notifications    |
| **Supabase**             | The private cloud database that stores all job data and history permanently    |
| **Salesforce**           | The CRM where job records live                                                 |
| **Modal**                | The cloud service that runs the automation on a schedule                       |
| **Scrape**               | A program reading a web page automatically, the same way a person would        |
| **Practice value**       | How Kimedics identifies a facility, e.g. `2387 - Benton Harbor, MI`            |
| **Job_Client_Job_Id__c** | The Salesforce field that must match the Kimedics practice value               |
| **Event log**            | The complete audit trail -- every decision, match, and error recorded          |
| **Headless browser**     | A browser that runs invisibly in the cloud with no visible window              |
| **OpenAI API**           | The AI service used to resolve ambiguous Salesforce matches                    |
| **Staging**              | A test copy of the database used during development -- never affects real data |


---

## Go Deeper

Plain URLs in the second column link automatically in Notion when pasted.


| Resource               | GitHub URL                                                                                                                                                                                                                                       |
| ---------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| Main pipeline          | [https://github.com/AndyLeeProjects/proxi_salesforce_automation/blob/main/src/production/scrape_gmail_modal.py](https://github.com/AndyLeeProjects/proxi_salesforce_automation/blob/main/src/production/scrape_gmail_modal.py)                   |
| Email parsing          | [https://github.com/AndyLeeProjects/proxi_salesforce_automation/blob/main/src/utils/gmail.py](https://github.com/AndyLeeProjects/proxi_salesforce_automation/blob/main/src/utils/gmail.py)                                                       |
| Kimedics scraper       | [https://github.com/AndyLeeProjects/proxi_salesforce_automation/blob/main/src/utils/playwright_job_scrape.py](https://github.com/AndyLeeProjects/proxi_salesforce_automation/blob/main/src/utils/playwright_job_scrape.py)                       |
| Database layer         | [https://github.com/AndyLeeProjects/proxi_salesforce_automation/blob/main/src/utils/supabase_db.py](https://github.com/AndyLeeProjects/proxi_salesforce_automation/blob/main/src/utils/supabase_db.py)                                           |
| SF matching logic      | [https://github.com/AndyLeeProjects/proxi_salesforce_automation/blob/main/src/utils/sf_job_supabase_resolve.py](https://github.com/AndyLeeProjects/proxi_salesforce_automation/blob/main/src/utils/sf_job_supabase_resolve.py)                   |
| AI matching            | [https://github.com/AndyLeeProjects/proxi_salesforce_automation/blob/main/src/utils/sf_ai_matcher.py](https://github.com/AndyLeeProjects/proxi_salesforce_automation/blob/main/src/utils/sf_ai_matcher.py)                                       |
| Validation logic       | [https://github.com/AndyLeeProjects/proxi_salesforce_automation/blob/main/src/utils/scrape_validator.py](https://github.com/AndyLeeProjects/proxi_salesforce_automation/blob/main/src/utils/scrape_validator.py)                                 |
| Alert emails           | [https://github.com/AndyLeeProjects/proxi_salesforce_automation/blob/main/src/utils/alert_email.py](https://github.com/AndyLeeProjects/proxi_salesforce_automation/blob/main/src/utils/alert_email.py)                                           |
| SF field mapping rules | [https://github.com/AndyLeeProjects/proxi_salesforce_automation/blob/main/docs/engineering/salesforce_job_push_rules.md](https://github.com/AndyLeeProjects/proxi_salesforce_automation/blob/main/docs/engineering/salesforce_job_push_rules.md) |
| Engineering guide      | [https://github.com/AndyLeeProjects/proxi_salesforce_automation/blob/main/docs/engineering/dev_guide.md](https://github.com/AndyLeeProjects/proxi_salesforce_automation/blob/main/docs/engineering/dev_guide.md)                                 |
| Scrape validation spec | [https://github.com/AndyLeeProjects/proxi_salesforce_automation/blob/main/docs/engineering/scrape_validation.md](https://github.com/AndyLeeProjects/proxi_salesforce_automation/blob/main/docs/engineering/scrape_validation.md)                 |



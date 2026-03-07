# Salesforce automation

Automations for **Kimedics job emails** (Gmail) and **Salesforce jobs** (REST API). All scripts are read-only where possible; Salesforce uses a read-only pull.

---

## Project structure

```
proxi_salesforce_automation/
├── .env                    # Secrets (see Environment variables)
├── data/
│   ├── job_emails.csv      # Parsed Kimedics emails (from scrape_gmail.py)
│   └── salesforce_jobs.csv # Salesforce job records (from pull_salesforce_jobs.py)
├── src/
│   ├── scrape_gmail.py         # Local Gmail scraper → CSV
│   ├── scrape_gmail_modal.py   # Modal job (every 30 min) → Modal Dict
│   ├── pull_salesforce_jobs.py  # Pull all jobs from Salesforce → CSV
│   └── utils/
│       ├── gmail.py        # IMAP scrape + Kimedics email parsing
│       └── salesforce.py   # OAuth (Client Credentials) + SOQL query
└── README_EMAIL_SCRAPER.md # This file
```

---

## 1. Gmail: Kimedics job emails

Reads your inbox (e.g. **anddy0622@gmail.com**) for emails from **donotreply@kimedics.com**, parses them into structured rows, and optionally saves to CSV or a Modal Dict.

### Parsed fields

| Field              | Example / notes |
|--------------------|-----------------|
| `job_post_id`      | Number after `#` (e.g. `19440` from "job post: #19440") |
| `location`         | e.g. `4143 - Greenville, NC` (parenthetical like `(GREENVILLE, NC)` removed) |
| `action_or_change` | `updated`, `new`, `status: Closed`, `accept_to_submit` |
| `view_job_link`    | URL from "View job post" or "Accept to submit providers" link in HTML |
| `subject`, `date`, `from_` | Pass-through |

### Local script (IMAP + CSV)

- **Script:** `src/scrape_gmail.py`
- **Requires:** Gmail App Password in `.env` (see [Environment variables](#environment-variables)).
- **Output:** `data/job_emails.csv` and a short summary in the terminal.

**Run (from project root):**

```bash
python src/scrape_gmail.py
```

### Modal job (scheduled + Dict)

- **Script:** `src/scrape_gmail_modal.py`
- **Schedule:** Every 30 minutes.
- **Secrets:** Modal secret **salesforce-automation** with `GMAIL_APP_PASSWORD`.
- **Storage:** Parsed rows are stored in a Modal Dict named **gmail-scraped-emails**.

**Deploy:**

```bash
modal deploy src/scrape_gmail_modal.py
```

**Run scraper once (test):**

```bash
modal run src/scrape_gmail_modal.py::scrape_gmail_job
```

**View stored data (sample):**

```bash
modal run src/scrape_gmail_modal.py::inspect_emails
modal run src/scrape_gmail_modal.py::inspect_emails --sample-size 10
```

### Gmail App Password

Create an App Password (2-Step Verification must be on):

**[https://myaccount.google.com/apppasswords](https://myaccount.google.com/apppasswords)**

Use the 16-character password in `.env` as `GMAIL_APP_PASSWORD` (no spaces).

---

## 2. Salesforce: Pull all jobs

Pulls all records from your Salesforce **job** sobject (e.g. `Job__c`) in **read-only** mode using the REST API.

- **Script:** `src/pull_salesforce_jobs.py`
- **Auth:** OAuth 2.0 **Client Credentials** flow (External Client App / ECA).
- **Domain:** Default token URL is **https://proxi.my.salesforce.com** (override with `SALESFORCE_TOKEN_URL` in `.env` if needed).
- **Output:** `data/salesforce_jobs.csv` (all queryable fields per record).

**Run (from project root):**

```bash
python src/pull_salesforce_jobs.py
```

### Required env (Client Credentials)

- `SALESFORCE_CONSUMER_KEY`
- `SALESFORCE_CONSUMER_SECRET`

### Optional env

- `SALESFORCE_TOKEN_URL` — default is `https://proxi.my.salesforce.com`
- `SALESFORCE_USE_SANDBOX=true` — use `test.salesforce.com`
- `SALESFORCE_JOB_OBJECT=Job__c` — your job sobject API name

### If you see "no valid scopes defined"

In Salesforce: **Setup → External Client App Manager** → open your app → **Edit** → **OAuth** section → add scopes (e.g. **Manage user data via APIs (api)**, **Perform requests at any time (refresh_token, offline_access)**) → **Save**.

### Username–password flow (classic Connected App)

To use username/password instead of Client Credentials, set in `.env`:

- `SALESFORCE_USE_USERNAME_PASSWORD=true`
- `SALESFORCE_USERNAME=...`
- `SALESFORCE_PASSWORD=...`
- `SALESFORCE_SECURITY_TOKEN=...` (if your org requires it)

---

## Environment variables

Use a `.env` file in the **project root** (or export in the shell). `.env` is not committed.

**Gmail**

- `GMAIL_APP_PASSWORD` — App Password from [myaccount.google.com/apppasswords](https://myaccount.google.com/apppasswords)

**Salesforce (Client Credentials by default)**

- `SALESFORCE_CONSUMER_KEY` — from your External Client App
- `SALESFORCE_CONSUMER_SECRET`
- `SALESFORCE_TOKEN_URL` — optional; default `https://proxi.my.salesforce.com`
- `SALESFORCE_JOB_OBJECT` — optional; default `Job__c`

**Salesforce (optional, for username-password flow)**

- `SALESFORCE_USE_USERNAME_PASSWORD=true`
- `SALESFORCE_USERNAME`
- `SALESFORCE_PASSWORD`
- `SALESFORCE_SECURITY_TOKEN`

---

## Utils

- **`utils/gmail.py`** — `scrape_emails_from_sender()`, `parse_kimedics_job_email()`, body/HTML extraction, link parsing for "View job post" and "Accept to submit providers".
- **`utils/salesforce.py`** — `get_token_client_credentials()`, `get_token()` (username/password), `describe_sobject()`, `query_all()`, `pull_all_jobs()`. All Salesforce usage is read-only (no create/update/delete).

---

## Quick reference

| Task                    | Command / note |
|-------------------------|----------------|
| Scrape Kimedics emails → CSV | `python src/scrape_gmail.py` |
| Deploy Modal Gmail job | `modal deploy src/scrape_gmail_modal.py` |
| Inspect Modal Dict data | `modal run src/scrape_gmail_modal.py::inspect_emails` |
| Pull Salesforce jobs → CSV | `python src/pull_salesforce_jobs.py` |
| Gmail App Password      | [myaccount.google.com/apppasswords](https://myaccount.google.com/apppasswords) |

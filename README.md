# Salesforce automation

Automations for **Kimedics job emails** (Gmail) and **Salesforce jobs** (REST API). All scripts are read-only where possible; Salesforce uses a read-only pull.

**Where things live:** [docs/project_layout.md](docs/project_layout.md) (production vs local vs tests). **Salesforce → Supabase field rules & levers:** [docs/salesforce_job_push_rules.md](docs/salesforce_job_push_rules.md).

---

## Project structure (short)

```
proxi_salesforce_automation/
├── .env
├── docs/                   # Layout guide, Modal deploy, Salesforce field rules
├── manual/                 # One-off notebooks (Supabase ↔ Salesforce, etc.)
├── tests/                  # Pytest + dev scrapers + explorer notebooks
├── data/                   # gitignored outputs (CSV, scraped .txt)
├── prox_streamlit_app/     # Optional monitoring UI
└── src/
    ├── production/         # Modal scheduled job (only deploy entrypoints)
    ├── local/              # CLI: Gmail, incremental, staging repair, HTTP link scrape, SF CSV pull
    ├── dev/                # Integration-test / scratch scripts (e.g. create test SF rows)
    └── utils/              # Shared library (gmail, supabase_db, salesforce, …)
```

---

## 1. Gmail: Kimedics job emails

Reads your inbox (e.g. **proxi@scrubnetwork.com**) for emails from **donotreply@kimedics.com**, parses them into structured rows, and optionally saves to CSV or a Modal Dict.

### Parsed fields

| Field              | Example / notes |
|--------------------|-----------------|
| `job_post_id`      | Number after `#` (e.g. `19440` from "job post: #19440") |
| `location`         | e.g. `4143 - Greenville, NC` (parenthetical like `(GREENVILLE, NC)` removed) |
| `action_or_change` | `updated`, `new`, `status: Closed`, `accept_to_submit` |
| `view_job_link`    | URL from "View job post" or "Accept to submit providers" link in HTML |
| `subject`, `date`, `from_` | Pass-through |

### Local script (IMAP + CSV)

- **Script:** `src/local/local_run_scrape_gmail.py`
- **Requires:** Gmail App Password in `.env` (see [Environment variables](#environment-variables)).
- **Output:** `data/job_emails.csv` and a short summary in the terminal.
- **Supabase:** You are prompted to type **`STAGING`** or **`PRODUCTION`** (all caps) before any DB write (`PRODUCTION` → `public`, `STAGING` → staging schema). Modal jobs do not use this prompt.

**Run (from project root):**

```bash
python src/local/local_run_scrape_gmail.py
```

### Modal job (scheduled, Supabase + optional Playwright)

- **Script:** `src/production/scrape_gmail_modal.py`
- **Schedule:** Every 30 minutes.
- **Secrets:** Modal secret **salesforce-automation** (e.g. `GMAIL_APP_PASSWORD`, `DB_PASSWORD`, Kimedics creds for link scrape).

**Deploy / run once:** see [docs/modal_deploy.md](docs/modal_deploy.md).

```bash
modal deploy src/production/scrape_gmail_modal.py
modal run src/production/scrape_gmail_modal.py::scrape_gmail_job
```

### Gmail App Password

Create an App Password (2-Step Verification must be on):

**[https://myaccount.google.com/apppasswords](https://myaccount.google.com/apppasswords)**

Use the 16-character password in `.env` as `GMAIL_APP_PASSWORD` (no spaces).

### 1.1 Scrape link content (View job post)

Each parsed email has a **view_job_link** (the "View job post" URL). This step **fetches that URL** and **extracts the page’s main text** so you have the full job description, not just the email snippet.

**Logic (see `utils/link_scraper.py` for full docstrings):**

1. **Fetch** — HTTP GET with a browser-like User-Agent; follow redirects (up to a limit).
2. **Detect login** — If the final URL contains `/login`, `/signin`, etc., or the page text contains "Sign in", "Log in", we set `login_required=True`. We do **not** implement login (no credentials or browser automation in this project).
3. **Extract text** — Strip scripts/styles, take text from `<main>`/`<article>` if present, else the whole body; normalize whitespace.

**When login is required:** The scraper records `login_required` in the results and does not save content. To get content behind login you’d need to add cookie-based auth or a headless browser (e.g. Playwright) with stored credentials; that is not implemented here.

- **Script:** `src/local/scrape_link_content.py`
- **Input:** `data/job_emails.csv` (run `src/local/local_run_scrape_gmail.py` first).
- **Output:**
  - `data/job_link_scrape_results.csv` — one row per link: `job_post_id`, `url`, `status_code`, `login_required`, `content_length`, `error`.
  - `data/job_content/<job_id>.txt` — full extracted text (only when the page was not a login gate).

**Run (from project root):**

```bash
# Scrape all links in job_emails.csv (with 1s delay between requests)
python src/local/scrape_link_content.py

# Scrape only the first 5 links
python src/local/scrape_link_content.py --max 5

# Test a single URL
python src/local/scrape_link_content.py "https://example.com/job/123"
```

---

## 2. Salesforce: Pull all jobs

Pulls all records from your Salesforce **job** sobject (e.g. `Job__c`) in **read-only** mode using the REST API.

- **Script:** `src/local/pull_salesforce_jobs.py`
- **Auth:** OAuth 2.0 **Client Credentials** flow (External Client App / ECA).
- **Domain:** Default token URL is **https://proxi.my.salesforce.com** (override with `SALESFORCE_TOKEN_URL` in `.env` if needed).
- **Output:** `data/salesforce_jobs.csv` (all queryable fields per record).

**Run (from project root):**

```bash
python src/local/pull_salesforce_jobs.py
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

**Kimedics (for link scraping when login is required)**

- `KIMEDICS_EMAIL` — used by `tests/scrape_kimedics_with_login.py` to log in when a job link requires auth
- `KIMEDICS_PASSWORD`
- Optional: `KIMEDICS_LOGIN_URL`, `KIMEDICS_BASE_URL`, `KIMEDICS_EMAIL_FIELD`, `KIMEDICS_PASSWORD_FIELD`

---

## Utils

- **`utils/gmail.py`** — `scrape_emails_from_sender()`, `parse_kimedics_job_email()`, body/HTML extraction, link parsing for "View job post" and "Accept to submit providers".
- **`utils/link_scraper.py`** — `fetch_url()`, `detect_login_required()`, `extract_main_text()`, `scrape_link()`. Fetches a URL, detects login gates, extracts main text; no login implementation.
- **`utils/salesforce.py`** — `get_token_client_credentials()`, `get_token()` (username/password), `describe_sobject()`, `query_all()`, `pull_all_jobs()`. All Salesforce usage is read-only (no create/update/delete).

---

## Quick reference

| Task                    | Command / note |
|-------------------------|----------------|
| Scrape Kimedics emails → CSV | `python src/local/local_run_scrape_gmail.py` |
| Incremental Gmail → Supabase | `python src/local/run_incremental.py` |
| Scrape view_job_link content | `python src/local/scrape_link_content.py` (needs job_emails.csv) |
| Deploy Modal Gmail job | `modal deploy src/production/scrape_gmail_modal.py` (see `docs/modal_deploy.md`) |
| Pull Salesforce jobs → CSV | `python src/local/pull_salesforce_jobs.py` |
| Create one test Job__c (writes SF) | `python src/dev/create_test_job_salesforce.py --yes` |
| Run scraping + Gmail tests | `pytest tests/ -v` |
| Scrape job links with Kimedics login | `python tests/scrape_kimedics_with_login.py` (see `tests/README.md`) |
| Gmail App Password      | [myaccount.google.com/apppasswords](https://myaccount.google.com/apppasswords) |

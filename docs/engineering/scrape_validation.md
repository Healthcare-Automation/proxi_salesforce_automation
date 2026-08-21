# Scrape Validation & Alerting

Automatically detects when Kimedics changes its page layout or when scraped data looks wrong.  
Alerts go to **anddy0622@gmail.com** and **seanhyang1@gmail.com**.

---

## How It Works

Every time a Kimedics job page is scraped, the parsed result passes through a **5-layer validator** before being written to Supabase.  
If problems are found, an email is sent immediately.  
A separate **daily digest** email arrives each morning summarising everything that happened in the last 24 hours.

```
Gmail email → Playwright scrape → parse_job_content_txt()
                                         ↓
                               validate_scraped_job()   ←── scrape_validator.py
                                         ↓
                            should_send_immediate_alert?
                                   ↓ yes         ↓ no
                          send_scrape_alert()   log only
                                   ↓
                          log_job_content() → Supabase
```

---

## Severity Levels

| Level | Meaning | Alert behaviour |
|-------|---------|----------------|
| **CRITICAL** | Page structure likely changed (selector broke, login wall) | Immediate email every time |
| **WARNING** | A specific field is missing or formatted wrong | Immediate email when **3+ warnings** on a single job, or when combined with any CRITICAL |
| **INFO** | Soft quality note (e.g. unknown status value) | Daily summary only, no immediate alert |

---

## Validation Layers

### Layer 1 — Structural (Layout Change Detection)

These checks fire when the Kimedics page itself didn't render correctly.
They are the earliest warning that something fundamental has changed.

| Check | Trigger | Severity |
|-------|---------|---------|
| `title_line` is empty | `.sections__container` never rendered — login redirect or CSS class renamed | CRITICAL |
| `title_line` has no `#XXXXX` job ID | Job header restructured; job ID no longer in the first line | CRITICAL |
| `title_line` is > 300 chars | Wrong container element was captured | CRITICAL |
| `description_full_text` is empty | The `section-builder.full-jobpost textarea` selector broke | CRITICAL |
| `description_full_text` < 40 chars | Textarea loaded but content was truncated | WARNING |
| 4+ critical fields empty simultaneously | Complete scrape failure (timeout, hard login wall, etc.) | CRITICAL |

**Why `description_full_text` matters:**  
The full job description lives in a separate Playwright-hydrated `<textarea>`.  
If this is empty it almost always means the CSS selector changed, not just missing data.

---

### Layer 2 — Critical Field Presence

These fields are required for Salesforce submissions and provider matching.
Missing = WARNING (not CRITICAL by itself, but combined with others will trigger an alert).

| Field | Why it matters |
|-------|---------------|
| `title_line` | Covered in Layer 1 |
| `job_title` | Mapped to `SF Job__c.Title__c` |
| `status` | Active/Closed determines whether to submit providers |
| `provider_start_date` | When coverage starts — critical for scheduling |

---

### Layer 3 — Important Field Presence

**Missing = WARNING** — 100% fill rate in 554 real rows; absence means something broke:  
`location_line`, `state`, `city`, `posting_org`, `practice_value`, `point_of_contact`

**Missing = INFO** — sometimes legitimately absent; won't trigger immediate alert:  
`rates` (almost never present), `posted_date`

> **Note:** `position_type` and `number_of_open_positions` are never populated by Kimedics and are intentionally excluded from all checks.

---

### Layer 4 — Format Checks

Checks that present values are in the expected shape, calibrated against real data.

| Field | Check | Severity |
|-------|-------|---------|
| `status` | Must be one of exactly 3 known values (case-sensitive) | WARNING |
| `priority` | Must be one of exactly 3 known values | INFO |
| `state` | Must be a valid 2-letter US state/territory abbreviation | WARNING |
| `provider_start_date` | Must match `MM/DD/YY`, `MM/DD/YYYY`, `YYYY-MM-DD`, or `"ASAP"` | WARNING |
| `provider_end_date` | Same as above | WARNING |
| `posted_date` | Same as above | WARNING |
| `job_title` | Must start with `#XXXXX:` (e.g. `#19406: Dentistry (Dentist (DMD/DDS))`) | WARNING |
| `job_id` | Must be purely numeric digits | WARNING |
| `rates` | If present: must contain a `$` or number pattern (absence is normal) | INFO |

**Exact known status values** (from 554 real rows — these are the only 3 that exist):
- `Closed`
- `Active, accepting new providers`
- `Active, not accepting new providers`

**Exact known priority values:**
- `Normal`, `High`, `Critical`

---

### Layer 5 — Anomaly Checks

Cross-field logic that catches subtler problems.

| Check | Trigger | Severity |
|-------|---------|---------|
| `practice_value` format | Doesn't match `XXXX - City, ST` or `XXXX – City, ST` (em-dash variant) | INFO |
| `point_of_contact` starts with digit | Likely captured wrong field | WARNING |
| `city` is purely numeric | Parse error — a number was put in the city field | WARNING |

**Practice value format note:** 546/554 real rows match `XXXX - City, ST`. The remaining 8 use an em-dash (`–`) instead of a hyphen — both are accepted as valid.

---

## Alert Emails

### Immediate Alert (`send_scrape_alert`)

**Sent when:** any CRITICAL issue is found, OR 3+ WARNING issues on a single job.  
**Subject:** `🚨 CRITICAL Scrape Alert — Job #XXXXX — N critical issue(s)`  
**Contains:**
- Summary badge (CRITICAL / WARNING / INFO counts)
- Direct link to the Kimedics job page
- Validation issues table with severity colour-coding
- Full data snapshot: every key field with missing ones highlighted in red

### Daily Digest (`send_daily_summary`)

**Sent:** every day at **9 AM ET** (13:00 UTC).  
**Subject:** `✅ Proxi Daily Report — Apr 1–2, 2025 — 12/14 scrapes OK`  
**Contains:**
- Stat boxes: emails received, scrape attempts, fully successful, partial (warnings), failed, SF mapped, SF patched
- Pipeline runs table (all `scrape_runs` in last 24h with start/finish times)
- **Example data table** — up to 5 most recent successful scrapes so you can visually confirm data is flowing correctly
- Flagged jobs — any job with CRITICAL or 3+ WARNINGs, with issue details
- Overall health badge: HEALTHY / WARNINGS / NEEDS ATTENTION

---

## Files

| File | Purpose |
|------|---------|
| `src/utils/scrape_validator.py` | All validation logic; `validate_scraped_job()`, `should_send_immediate_alert()` |
| `src/utils/alert_email.py` | Email sending via Gmail SMTP; `send_scrape_alert()`, `send_daily_summary()` |
| `src/production/scrape_gmail_modal.py` | Modal jobs; validation wired into scrape loop; `daily_summary_job` scheduled function |

---

## Adding a New Validation Check

1. Open `src/utils/scrape_validator.py`
2. Add your check to the appropriate layer function (`_check_structural`, `_check_critical_fields`, `_check_important_fields`, `_check_formats`, or `_check_anomalies`)
3. Append a `ValidationIssue(Severity.X, "field_name", "message")` to the `issues` list
4. No other changes needed — it will automatically appear in alert emails and the daily summary

Example:
```python
def _check_formats(c: dict, issues: list) -> None:
    ...
    # New: number_of_open_positions should be a digit string
    n = c.get("number_of_open_positions", "")
    if n and not re.match(r"^\d+$", n.strip()):
        issues.append(ValidationIssue(
            Severity.WARNING, "number_of_open_positions",
            f"Expected a digit but got: '{n[:40]}'",
            value=n,
        ))
```

---

## Deploying Changes

```bash
# From project root
modal deploy src/production/scrape_gmail_modal.py   # deploys scrape + daily_summary together

# Test daily summary right now (same as scheduled job)
modal run src/production/scrape_gmail_modal.py::run_daily_summary_once

# Test scrape pipeline right now
modal run src/production/scrape_gmail_modal.py::run_once
```

---

## Email Credentials

Emails are sent from `andy@uzu.studio` using the `GMAIL_APP_PASSWORD` Modal secret (same credential used for IMAP reading).  
No extra packages are required — uses Python stdlib `smtplib`.

To change recipients, edit `ALERT_RECIPIENTS` in `src/utils/alert_email.py`:
```python
ALERT_RECIPIENTS = ["anddy0622@gmail.com", "seanhyang1@gmail.com"]
```

# Modal commands

Run everything from the **project root**. Ensure `modal` is installed and you’re logged in (`modal token new` if needed).

---

## Gmail scraper app (`src/scrape_gmail_modal.py`)

**App name:** `salesforce-automation`  
**Secret:** `salesforce-automation` (set `GMAIL_APP_PASSWORD` in Modal dashboard)

### Deploy (upload / update code)

```bash
modal deploy src/scrape_gmail_modal.py
```

Use after editing the script or `utils/gmail.py`. Redeploys the app and keeps the 30‑minute schedule.

---

### Run the scraper once

```bash
modal run src/scrape_gmail_modal.py::scrape_gmail_job
```

Runs the job immediately without waiting for the schedule. Good for testing.

---

### Get data (inspect stored emails)

```bash
modal run src/scrape_gmail_modal.py::inspect_emails
```

Prints a sample (default 5 rows) from the Dict **gmail-scraped-emails**: job #, location, action, link, subject, date.

**Larger sample:**

```bash
modal run src/scrape_gmail_modal.py::inspect_emails --sample-size 10
```

---

## Summary

| Action        | Command |
|---------------|--------|
| Deploy/upload | `modal deploy src/scrape_gmail_modal.py` |
| Run job once  | `modal run src/scrape_gmail_modal.py::scrape_gmail_job` |
| Get data      | `modal run src/scrape_gmail_modal.py::inspect_emails` |

**Storage:** Parsed Kimedics emails live in the Modal Dict **gmail-scraped-emails**. View or manage it in the [Modal dashboard](https://modal.com/apps) under Storage → Dicts.

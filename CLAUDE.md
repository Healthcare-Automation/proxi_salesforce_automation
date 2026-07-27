# Kimedics Automation (Proxi — client project)

## Deployment account
- Vercel: **proxi@scrubnetwork.com** (client account). NEVER deploy with the personal account (anddy0622@gmail.com).
- Run `vercel whoami` before any deploy and confirm the account matches.

## What this is
Pipeline: Kimedics job emails (Gmail) → Playwright scrape → Supabase → Salesforce Job__c/Worksite create-or-update.
Runs on Modal every 10 min, fully autonomous — no human-in-the-loop step.

## Commands
- Test: `pytest tests/ -v` (conftest puts `src/` on path; run from repo root)
- Deploy: `modal deploy src/production/scrape_gmail_modal.py` — the ONLY deploy entrypoint; registers all scheduled fns on app `salesforce-automation`
- Manual run: `modal run src/production/scrape_gmail_modal.py::run_once` (scrape) or `::run_daily_summary_once`
- After every deploy: follow the `proxi-automation-changelog` skill (Updates tab) before ending the turn

## Architecture
- `src/production/scrape_gmail_modal.py` — Modal app: 10-min scrape, daily 13:00 UTC summary, 30-min watchdog, FastAPI endpoints
- `src/utils/sf_job_supabase_resolve.py` — resolver: link Kimedics job to existing Job__c or create Job__c (+Worksite)
- `src/utils/` — shared lib (gmail, playwright_job_scrape, salesforce, supabase_db, scrape_validator)
- `src/local/` — manual CLI scripts (incremental runs, repairs, SF pulls); `src/dev/` — scratch
- `docs/RESOLVER_LOGIC.md` — resolver deep-dive

## Gotchas
- Resolver order: External_Job_ID__c match → practice-key match → AI fallback (acts on `high` confidence only, `medium` dropped) → `mapping_no_match` then auto-create. N>1 matches ID-swap into one deterministic candidate, never error.
- N>1 on a *practice key* means duplicate Job__c for one practice (SF marks `Job_Client_Job_Id__c` unique; legacy pairs slipped through on `"4096-"` vs `"4096 - "` drift). Canonical winner = most recruiting activity (placements+submittals+applications), tie-break newest `CreatedDate` — NOT "prefer no External_Job_ID__c", which spread postings across duplicates and kept them all alive. Consolidation writes are gated by `PROXI_SF_CONSOLIDATE_DUPLICATE_JOBS=true`; unset = detect-only, still logs `practice_duplicate_consolidated`.
- `Job_Open_Date__c` = first Open after the latest close (start of the newest contiguous Open run), falling back to Kimedics `posted_date`. Open/Closed MUST come from `job_status_for_salesforce_push` — "Active, not accepting new providers" is Closed, and it contains the substring "accepting new provider", which is what made the old open-date check restamp closed jobs.
- `mapping_review_required` / `mapping_ambiguous` events in logs are HISTORICAL — the current resolver never emits them; `mapping_no_match` now leads to create, not review.
- Each Kimedics job_id gets its own Job__c; closed prior postings at the same worksite are never reused.
- `PROXI_SF_UPDATE_JOBS=false` kills all SF writes (POST/PATCH); unset defaults to TRUE — writes are live.
- New Job__c/Account owner is set (Cara Griffin) on CREATE only — never re-assert OwnerId on update, it would yank recruiter reassignments every 10 min.
- Worksite auto-create is gated by `PROXI_SF_CREATE_WORKSITES=true`.
- Secrets live in Modal (`salesforce-automation`, `gmail-oauth`), not .env; deleting the three gmail-oauth keys reverts Gmail fetch to IMAP with no redeploy.
- Tests monkeypatch OPENAI_API_KEY away (autouse) — AI paths use regex fallbacks unless a test opts in.

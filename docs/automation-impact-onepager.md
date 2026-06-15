# Proxi Automations — Impact & Status
*As of June 15, 2026*

## 1. Kimedics → Salesforce  ·  **LIVE** (since Feb 26, 2026)

**What it does**
- Reads every Kimedics job email, opens the job page, and syncs it into Salesforce automatically — no human data entry.
- Decides per job: **update** an existing `Job__c` (matched by Kimedics ID → practice value → AI fuzzy match), or **create** a new Job, attaching to an existing Worksite or creating one.
- Runs every **10 minutes** on Modal, 24/7.

**Impact since launch (110 days, all measured from sync logs)**
- **188.6 hours** of manual data-entry time recouped *(estimate — see basis below)*.
- **1,910 job emails** processed end to end.
- **712 jobs opened**, **837 jobs updated**, **361 jobs closed** in Salesforce.
- **8,988 Salesforce field updates** written automatically.
- **35 states** covered — top: NY, IL, NJ, VA, TX.

**Quality & reliability**
- **99.5% capture rate** — 1,901 of 1,910 emails fully synced (9 genuine misses).
- **8.5 min** average email → Salesforce sync time (outliers excluded).
- **Hiring velocity:** 2.9-day median time a job stays open, across 322 completed open→close lifecycles.
- Growth trajectory: 42 emails (Feb, partial) → ~500/mo (Mar–Apr) → 591 (May) → 297 (June, month-to-date).

**Hours-saved basis (the one estimate):** 8 min manual entry per job opened, 1.5 min per update/close, +2 min context-switching per email, applied to the 1,910 emails actually processed. Every other figure is counted directly from logs.

## 2. Dentist Job Cafe (DJC) → Salesforce  ·  **IN BUILD** (started June 2026)

**What it will do**
- Sources **dental candidates** from Dentist Job Cafe and creates Salesforce **Contacts** (with résumé attached) — the candidate-sourcing counterpart to the Kimedics job pipeline, on the same Salesforce org.
- Logs into DJC, iterates each role/specialty under fixed filters (last 7 days, exclude visa), scrapes candidates, recovers contact info, skips anyone uncontactable or already in Salesforce, creates the Contact, and validates job matches by zipcode.
- **V1 is create-only** (no record updates). V2 adds record updates + LinkedIn-based phone recovery (pending legal/ToS review).

**Architecture / stack**
- **Playwright** for scraping + a persisted phone-OTP login session (re-auth alert on expiry).
- **Claude Haiku 4.5** parses résumés **in memory** to recover missing phone/email — nothing stored; CV bytes discarded after attaching to Salesforce.
- **Supabase** for run state, dedup keys, and audit log; **Salesforce REST API** for create + CV upload.
- Runs **daily ~12:03 AM ET** on Modal, deliberately off-tick from the 10-minute Kimedics job to avoid API collisions.

**Key design choices**
- **Dedup:** a candidate "already exists" if phone OR email OR (name + DJC profile link) matches — on match, V1 skips.
- **Data integrity:** after each create, reads back key fields (zipcode, license, preferred state) and queries job matches by zipcode; a zero match is valid, a failed write is flagged.
- **Coexistence:** isolated Connected App credentials + exponential backoff (Salesforce API limits are org-wide, not per-app).

**Status:** ~4–5 week build (incl. a stabilization period); **no production data yet** — no candidates have been synced, so there are no impact metrics to report for DJC at this time.

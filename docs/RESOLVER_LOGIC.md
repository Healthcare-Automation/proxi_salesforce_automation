---
name: proxi-kimedics-sf-pipeline
description: How the Proxi automation decides whether to update an existing Salesforce
  Job__c, create a new one, attach to an existing Worksite, or create a new Worksite.
  Use this skill any time the user asks about job/worksite resolver logic, duplicate
  worksites, "why did it create a new job", "why did it ID-swap", mapping_no_match,
  mapping_review_required, or the relationship between Kimedics emails and Salesforce
  records.
origin: ECC
---

# Proxi · Kimedics → Salesforce Resolver Logic

> **Audience:** the operator (technical) and the team (non-technical).
> Co-workers without an engineering background should be able to read this end-to-end and understand exactly how the system decides what to do when a new job posting arrives.

This skill is the single source of truth for how the automation handles the decision tree below. Reference it any time the topic comes up.

---

## 1. What the system does, in one sentence

When a new job email arrives from Kimedics, the automation reads the email, opens the job page, and tries to put that job into Salesforce — either by **updating an existing record** or by **creating a fresh one** — without ever asking a human for help.

---

## 2. The two things the automation creates in Salesforce

There are exactly two record types it touches:

| Record | What it represents |
|---|---|
| **Worksite** (`Account` under "Aspen Dental Management") | The physical clinic location — e.g. *"Aspen Dental – Muskogee, OK"*. One per real-world location. |
| **Job** (`Job__c`) | A specific staffing posting at a worksite — e.g. *"#19836 Dentistry at Muskogee, OK"*. One per Kimedics job_id. |

Every **Job** points to one **Worksite**. A Worksite can have many Jobs over time.

---

## 3. The big picture flow

```
Kimedics email arrives
      │
      ▼
┌──────────────────────────────────────┐
│  Step 1.  Does this job already      │
│            exist in Salesforce?      │
│                                      │
│   ✔ Yes  →  UPDATE the existing      │
│              record. Done.           │
│                                      │
│   ✘ No   →  go to Step 2             │
└──────────────────────────────────────┘
                │
                ▼
┌──────────────────────────────────────┐
│  Step 2.  Find the worksite.         │
│                                      │
│   ✔ Worksite already in SF →         │
│       CREATE a new Job there.        │
│                                      │
│   ✘ Worksite doesn't exist →         │
│       CREATE Worksite + Job.         │
└──────────────────────────────────────┘
```

That's the entire decision. The rest of this skill explains how each step decides.

---

## 4. Step 1 in detail — "Does the job already exist?"

The automation looks for an existing **Job__c** in Salesforce in this order. As soon as one matches, it stops looking and updates that record.

### 4a. Match by Kimedics ID

Every Job__c in Salesforce has a field called `External_Job_ID__c`. The automation puts the Kimedics `job_id` into this field whenever it creates or updates a Job__c. So when an email for `job_id 19836` comes in:

> *"Is there any Job__c in Salesforce with `External_Job_ID__c = 19836`?"*

| Result | Action |
|---|---|
| Exactly 1 match | **Update that one.** It's the same posting we've seen before. |
| More than 1 match | This shouldn't happen (the field is supposed to be unique). Pick the most recently modified one, update it, and log a warning so the duplicates can be cleaned up. |
| Zero matches | Move on to **4b**. |

### 4b. Match by practice value

Every Job__c also has a field called `Job_Client_Job_Id__c`. This stores the Kimedics practice value — the location identifier like *"4247 – Houston, TX (NW Crossing)"*.

> *"Is there any Job__c in Salesforce whose `Job_Client_Job_Id__c` matches our Kimedics practice value?"*

| Result | Action |
|---|---|
| Exactly 1 match | **Update that one.** Same physical posting, just no Kimedics-id link yet. We fill in the External_Job_ID__c at the same time. |
| More than 1 match | Pick deterministically (prefer the one without an existing External_Job_ID__c so we don't break another Kimedics link, otherwise most recently modified). Update it. |
| Zero matches | Move on to **4c**. |

### 4c. AI fuzzy match (last resort before creating)

If the practice value didn't match exactly, the automation asks GPT-4o-mini whether any of the Salesforce candidates "look like the same place" — handles cases like *"St. Joseph"* vs *"St. Joseph's"* or *"3185-"* vs *"3185 -"*.

| Result | Action |
|---|---|
| **High confidence** match | Update that record. |
| Anything less | Treat as no match. Move on to Step 2. |

`Medium`-confidence guesses are intentionally ignored — too risky for unattended automation.

### 4d. No match found → emit `mapping_no_match`

This is a transient event. It does NOT mean we stop — it just means none of the matchers above produced a hit. The automation immediately falls through to **Step 2**.

---

## 5. Step 2 in detail — "Find or create the worksite, then create the new Job"

When Step 1 found no existing Job, the automation needs to figure out where to put the new one.

### 5a. Find the worksite

In order:

1. **Local cache check.** We keep a small table (`sf_worksite_location_map`) that remembers `(city, state) → Salesforce Account Id`. If `(Muskogee, OK)` is in the cache, use that Account.

2. **Ask Salesforce directly** (added 2026-05-22). Even if our cache doesn't know about a worksite, Salesforce might already have one. The automation runs two probes under the Aspen Dental Management parent Account:
   - **(a) Exact Name match** — looks for an Account whose Name equals what we'd create *(e.g., "Aspen Dental - Muskogee, OK")*. Catches the common case where the SF record exists but our cache never saw it.
   - **(b) City + State match** — looks for an Account whose `ShippingCity` exactly equals ours and whose `ShippingState` normalizes to the same 2-letter abbreviation (so `"OK"` and `"Oklahoma"` match).

   If either probe finds a hit → use it, **backfill the cache**, and log a `worksite_relinked` event. We do NOT create a duplicate.

3. **If both probes find nothing** → create a brand new Worksite Account.

### 5b. Create the new Job at that worksite

A fresh `Job__c` record is created in Salesforce with:
- `External_Job_ID__c` = the Kimedics job_id
- `Job_Client_Job_Id__c` = the practice value
- `Job_Worksite_Location_1__c` = the worksite Account Id from 5a
- All scraped fields (title, dates, rate, schedule, requirements, …)

Historical Job__c records at the same worksite are left alone. They keep their Closed status. The new one is its own row.

---

## 6. What the events in the dashboard mean

When you open a job's Validation Details popup, you'll see a Timeline. Here's what each event type means in plain English:

| Event | Plain English |
|---|---|
| **email** | A Kimedics email arrived. Always the first event for a posting. |
| **supabase latest job snapshot** | The scrape succeeded and our local DB has fresh data. |
| **mapping cache hit** | We found this practice in our local cache. Skipped the slow lookup. |
| **mapping no match** | Couldn't match this practice to any existing SF Job (yet). Triggers the create path. |
| **mapping ambiguous** *(legacy)* | The resolver used to stop here. **Doesn't fire anymore** — current code picks deterministically. |
| **mapping review required** *(legacy)* | The resolver used to stop here too. **Doesn't fire anymore.** |
| **mapping AI match** | The AI fallback found a high-confidence match. |
| **sf ids update** | We linked the Kimedics job to a Salesforce Job. |
| **worksite created** | We created a new Worksite Account in Salesforce. |
| **worksite relinked** | We found an existing SF Worksite via direct lookup and reused it. |
| **job created in salesforce** | A new Job__c was created. |
| **sf scrape fields patched** | Job__c fields were updated successfully. |
| **sf scrape fields error** | A field update failed (push error). |
| **sf scrape fields recovered** | An earlier push error got auto-fixed on the next run. |
| **sf field quarantined** | One field was dropped (Salesforce rejected the value) so the rest of the record could land. |
| **manual rescrape completed** | An admin clicked "Rescrape" in `/admin/recovery`. |
| **auto retry completed** | The cron retried a previously-stuck job. |
| **mapping external id duplicate resolved** | Two SF Jobs had the same Kimedics ID. The automation picked one and flagged the duplicates. |

---

## 7. Common questions

### "Why did it create a brand new Job when there's already one at the same worksite?"

Because that existing Job's **practice value didn't match ours**. Per the rule above, only practice-value matches (or Kimedics-id matches) trigger an update. Worksite-only matches do not. That's deliberate — two different postings (e.g. one closed, one new) at the same physical clinic deserve their own records.

### "Why did it ID-swap instead of creating a new one?"

ID-swap happens *only* when a matching record already exists in Salesforce (Steps 4a / 4b). The automation links the Kimedics job to that existing record. It's an UPDATE in spirit — no new Job is created.

### "Why did it create a duplicate Worksite?"

Before 2026-05-22, the worksite lookup only checked our local cache. If the cache was empty for that location (e.g. the SF Worksite was created manually outside the automation), we'd create a duplicate.

**Fixed.** The automation now asks Salesforce directly before creating any Worksite. See **5a step 2**.

### "What's an 'amended later' badge in the dashboard?"

A run that hit a SF-side failure (e.g. expired password) but whose jobs were later patched by a subsequent rescrape will show `↻ N amended later` in the SF Push column. The run itself didn't push anything; a later run did.

---

## 8. Known limitations

These are the cases the automation **does not** handle today. Worth knowing so you can spot when manual cleanup is needed.

1. **Misspelled city/state on the existing SF record.** If someone manually created a Worksite as "Muscogee" instead of "Muskogee", our exact-city probe won't match and we'll create a duplicate.
2. **Worksite under a different parent Account.** The probe filters by Aspen Dental Management. A worksite Account placed under a different parent won't be found.
3. **Practice value drift the AI doesn't recognize as high-confidence.** Falls through to create-new. Rare, but possible.
4. **Closed Job__c records.** Currently they're still searched; but a Closed-and-archived record might behave differently. If you see this kind of edge case, flag it.

---

## 9. Daily summary email — what each stat box means

The daily email (and the dashboard's run pills) surface these metrics:

| Box / Pill | What it counts |
|---|---|
| **Emails Received** | How many Kimedics emails landed in our inbox in the window. |
| **Scraped OK** | How many produced real job content (title + description). |
| **SF Job__c Mapped** | How many have a Salesforce Job__c linked. |
| **New SF Job Records** | New Job__c records created during the window. |
| **New SF Worksites** | New Worksite Account records created during the window. |
| **SF Fields Patched** | Total field updates that landed in Salesforce. |
| **ID Swaps** | Existing SF Jobs whose External_Job_ID__c was repointed to a new Kimedics job_id. |
| **Push Recovered** | SF push errors that auto-recovered. |
| **Fields Dropped** | Fields Salesforce rejected; the rest of the record still landed. |
| **Blocked: No Practice** | Scrape returned no practice value (often a Kimedics login wall). |
| **Silent Scrape Fails** | Scrape ran but produced nothing. |
| **Push Errors** | SF push errors still unresolved at the end of the window. |
| **Auto Retries** | Times the cron re-ran a previously-stuck job. |
| **Manual Rescrapes** | Operator-triggered rescrapes via `/admin/recovery`. |
| **Stuck (needs fix)** | Jobs received an email but never landed in Salesforce; needs human attention. |
| **Scrape Failures** | Scrape didn't produce real content and isn't already counted as stuck. |

---

## 10. Quick reference: cheat sheet

| Situation | What the automation does |
|---|---|
| New Kimedics job with known `External_Job_ID__c` | Update existing Job__c. |
| New Kimedics job with matching practice value in SF | Update existing Job__c (link by practice). |
| New Kimedics job with no match anywhere, worksite exists in SF | Create new Job__c at the existing Worksite. |
| New Kimedics job with no match, worksite doesn't exist in SF | Create new Worksite + new Job__c. |
| Two SF Jobs share a Kimedics ID | Pick most-recent, update it, log duplicates for cleanup. |
| Two SF Jobs share a practice value | Pick deterministically, update one. |
| Practice has obvious typo / format drift | AI tries to match on `high` confidence only. If yes → update. If no → create new. |
| Worksite "Muskogee, OK" exists in SF but not in our cache | Direct SF lookup finds it, reuses it, populates cache. |
| SF auth fails | Whole pipeline fails for that tick. Jobs go to "stuck" state. Admin can re-run after fixing creds. |

---

## 11. For the non-technical reader — the 30-second version

1. Kimedics sends us emails about job postings.
2. We open the email, read the page, and put the job into Salesforce.
3. If the same job already exists in Salesforce, we update it.
4. If it doesn't, we create it — at an existing worksite if there is one, or we make a new worksite.
5. We never create duplicates of the same job. We try hard not to create duplicate worksites either (and we cleaned up the historical ones we did create).
6. The dashboard shows you every step. The daily email summarizes them. The `#automation-alert` Slack channel pings you only when something genuinely fails.

That's it. No human is in the loop. Everything else in this skill is just the detail of *how* steps 3–5 actually work.

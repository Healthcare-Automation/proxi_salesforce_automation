# Salesforce field logic — manual parity reference (Kimedics → Job__c)

Audience: anyone comparing **manual** Kimedics-to-Salesforce work with **automation** so day-to-day decisions stay aligned.

Companion: high-level pipeline and env rules live in `[salesforce_job_push_rules.md](./salesforce_job_push_rules.md)`. This document is **field-centric**: what goes to each API field, from what source, and what transforms/guards apply.

---

## How automation builds a Job__c payload (mental model)

- **Source row:** Supabase `job_current` / `job_content` (same column names as `utils/job_content_parser.py` output).
- **Core mapper:** `utils/sf_job_payload.job_row_to_salesforce_fields` → dict of API names → values.
- **Static defaults:** merged from `SF_PUSH_STATIC_DEFAULTS` (Locums, General Dentistry, Dentist, job source, legacy DJC mirrors, worksite parent, patient ages, volume default).
- **Before API write:** `prepare_payload_for_write`:
  - Runs `**coerce_picklists_to_valid`** (describe: match API value, else label, else **first active** value — **verify org picklists** if a value looks wrong).
  - Drops keys the org does not allow (**createable** vs **updateable** from describe). If a field never appears in Salesforce, check FLS/metadata — not “automation skipped it” silently if describe filters it out.
- **PATCH (scrape sync / lever scripts):**
  - Payload is built **for update**; fields in `SF_PUSH_JOB_ROLE_DEFAULTS` (**role picklists + `Job_Job_Source__c`**) are **removed** from the row-based dict, then **re-filled only where Salesforce GET shows blank** (`merge_job_role_defaults_for_empty_sf_fields`). That avoids overwriting recruiter-edited SF values.
  - Only fields whose **normalized** desired value differs from **GET** are sent (`utils/sf_scrape_sync`, string compare on trimmed values; booleans → `true`/`false` strings).

---

## Environment gates (when writes happen)

- `**PROXI_SF_UPDATE_JOBS`:** when false/`0`/`no`/`off`, automation skips Salesforce **POST/PATCH** from the scrape pipeline (including field-sync reads used for compare, in that path). Default when unset: **on**.
- `**PROXI_SF_CREATE_JOBS`:** allows **auto-create Job__c** when resolver finds **no** safe 1:1 match (see resolver doc). Still respects write kill switch above where coded.
- `**PROXI_SF_CREATE_WORKSITES`:** allows **POST worksite Account** + map upsert when policy says to create a new location Account.
- `**PROXI_SF_TEST_MODE`:** adds `**test_status__c`** (raw Kimedics `status` string) and `**test_posted_date__c**` (normalized date) to scrape-sync **desired** payload only — org must have those fields.

---

## Cross-cutting rules (all fields)

- **Picklists:** Automation never sends arbitrary strings into restricted picklists without passing `**coerce_picklists_to_valid`**. Wrong-looking values usually mean **no exact API value / label match** — check Setup picklist entries.
- **Empty vs omit:** Many fields become `**null`/omitted** when source is blank after trim; PATCH only includes **non-empty** desired values that **differ** from SF.
- **Truncation:** `External_Job_ID__c` truncated to **20** chars; `External_Job_Link__c` truncated to **255** when using fallback `view_job_link`.
- **Primary Account:** `Job_Account__c` defaults to `**0015f00000HH63kAAD`** (Aspen Dental Management Inc.) unless row has `sf_primary_account_id` / override.
- **Worksite lookup:** `Job_Worksite_Location_1__c` is set **only** when `sf_worksite_account_id` (or caller override) is known — **no placeholder Id**.

---

## Job__c fields — logic per API name

Below, **“Supabase column”** is the human/process input from Kimedics scrape/parser unless noted.

### `Name`

- **Intent:** Record title / header line in Salesforce (manual parity with recruiter naming).
- **Pattern:** `{StateAbbr} ({City}) {Specialty} - {Brand} - {Open|Closed}`  
Examples: `OH (Shelby) General Dentistry - Midwest Dental - Closed`, `SC (Summerville) General Dentistry - Heartland Dental - Open`.
- **Brand (`posting_org`):** `job_name_brand_display_for_row` — substring match on trimmed lowercased **posting_org**: **Heartland** → `Heartland Dental`; **Midwest** → `Midwest Dental`; **Aspen** → `Aspen Dental Management Inc.`; other non-empty values pass through as-is; empty → **Aspen Dental Management Inc.**
- **City:** Primary `city` column; if blank, city from `**practice_value`** via `_parse_city_state` (e.g. `1234 - Summerville, SC`); if still blank, first segment of `**location_line**` before the last comma (parenthetical suffix stripped). **If no city is found, the `(City)` parentheses are omitted** so you never get `() General Dentistry - …`.
- **Specialty:** static default **General Dentistry** (same as `Job_Specialty__c` / `Specialty_DJC__c` defaults).
- **Status:** `**job_status_for_salesforce_push`** → **Open** or **Closed** only.
- **Length:** Hard cap **80** characters; tail replaced with `…` if longer.

### `External_Job_ID__c`

- **Source:** Kimedics `job_id` (string).
- **Logic:** Strip; truncate to `**EXTERNAL_JOB_ID_MAX_LEN` (20)**.
- **Use:** Matching / dedupe key against Kimedics in resolver logic (truncated key comparison).

### `External_Job_Link__c`

- **Primary:** If `job_id` is **all digits**, canonical URL  
`https://portal.kimedics.com/app/workspace/job-posts/{job_id}` (avoids long email tracker links).
- **Fallback:** `view_job_link` when `job_id` missing or non-numeric (e.g. tests); **truncate 255**.

### `Job_Client_Job_Id__c`

- **Source:** `practice_value` (trimmed Kimedics practice line, e.g. `3185 - St. Joseph, MO`).
- **Empty:** omitted/null.

### `Job_Facility_Display__c`

- **Source:** Same as `practice_value` (trimmed).
- **Note:** May be duplicate of client job id line; both are populated from practice label for orgs that use one field for display and one for matching.

### `Job_Account__c`

- **Value:** Primary Account Id — default `**0015f00000HH63kAAD`**, or row `sf_primary_account_id` / function arg override.

### `Job_Worksite_Location_1__c`

- **Value:** Salesforce **Account Id** for the **worksite** location (not the management account).
- **Source:** `sf_worksite_account_id` on row or explicit `worksite_account_id` override.
- **If missing:** field **omitted** from payload (automation does not invent an Id).
- **PATCH failure:** If Salesforce returns **deleted entity** for this lookup, scrape sync may **retry PATCH without** this field; stale map rows should be cleared (see main push rules doc).

### `Job_Worksite_1_Address__c`

- **Source:** Supabase `address_line` (parser composes from `Address:` + `City:` + `State:` lines when needed).
- **Push-time:** `format_us_address_line_for_display`: whitespace collapse; **Unicode comma → ASCII**; drop **empty comma segments**; strip trailing comma junk; optional **ALL CAPS → title-style** (street types, directionals, `PO Box`, 2-letter states).
- **Same string** feeds new worksite Account `**ShippingStreet`** when creating Account (`sf_worksite_create`).
- **Org caveat:** If field is **not updateable** in Salesforce, PATCH may never fix bad legacy values — FLS/layout must allow automation to write.

### `Job_Status__c`

- **Source:** `status` (Kimedics).
- **Mapping (`job_status_for_salesforce_push`):** Salesforce only gets **Open** or **Closed** (never raw Inactive, etc.).
  - **Closed** if phrase contains **“not accepting”** (checked before “accepting”).
  - **Open** if contains **“accepting new provider”** or raw equals **open** (case-insensitive normalized spacing).
  - **Any other** non-empty status → **Closed**.
- **Empty source:** `null` omitted.

### `Job_State__c`

- **Source:** `state` (often 2-letter from parser).
- **Push:** **Full state name** via `state_name_for_salesforce` (e.g. TX → Texas).

### `Job_City__c`

- **Source:** `city` (trimmed; parser title-cases from Kimedics).

### `Job_Point_of_Contact__c`

- **Source:** `point_of_contact` (trimmed).

### `Job_Dates_Needed__c`

- **Primary:** `effective_dates_needed(row)`:
  - If `**description_full_text`** contains `**Active needs are**`  (case-insensitive) on **any line**, use text **after that phrase on the same line** (first match). Overrides structured `dates_needed`.
  - Else use `dates_needed`.
- **Parser:** `_fill_from_description_blocks` also writes `dates_needed` from that clause when present (so DB stays consistent on scrape).
- **Canonical job posting** uses the same effective string for the **Dates:** line in the template.

### `Job_Standard_Schedule__c` / `Standard_Schedule_Hours__c`

- **Source:** Same Supabase column `**standard_schedule`** (trimmed) copied to **both** API fields (org uses one for “schedule” and one for “hours”-style field).

### `Job_Provider_Start_Date__c` / `Job_Provider_End_Date__c`

- **Source:** `provider_start_date` / `provider_end_date` as **MM/DD/YY** (or similar string the parser stores).
- **Push:** Parsed to **ISO date** (`YYYY-MM-DD`) when `strptime` succeeds; else omitted/null.

### `Insight__c`

- **Source:** `insight` (Kimedics `*` / `**` bullets).
- **Logic:** `sanitize_insight_for_salesforce` — parse bullet bodies, **dedupe** by normalized key (case/whitespace/punctuation-insensitive), emit `*line` per bullet.
- **Template:** Insight is **not** repeated as “source notes” in `Job_Client_Job_Description__c` (candidate-facing copy stays clean).

### `Job_Types_of_Cases__c`

- **Source:** `types_of_cases` (parser often joins required procedures + additional requirements; AI pipeline may refine in Supabase).
- **Push:** `strip_internal_presentation_phrases` removes internal *“please notate any limitations in presentation”* (and leading punctuation variants).
- **Template clinical bullets:** first letter of each bullet line capitalized for display.

### `Job_Support_Staff__c`

- **Source:** `support_staff`; omitted if blank after trim.

### `Salary_Pay_Range__c`

- **Source:** First pay-range match from `**description_full_text`** via `extract_pay_range_from_description` (dollar patterns / “starting at” patterns).
- **Default:** `**Starting at $125/hour`** if no match (`DEFAULT_SALARY_PAY_RANGE`).
- **Note:** Dashes normalized to en-dash in extracted fragment.

### `Job_Client_Job_Description__c`

- **Default (canonical):** `build_proxi_job_posting_description(row)` when `use_canonical_description=True`.
  - **HTML vs plain:** `PROXI_JOB_DESCRIPTION_HTML` default true → HTML for Rich Text fields; false → plain sections.
  - **AI intro:** `PROXI_JOB_DESCRIPTION_USE_AI` default true → optional OpenAI intro; on failure falls back to static paragraphs (`OPENAI_API_KEY` required for AI).
  - **Aligned with structured fields:** city/state (full state in prose), **dates** = `effective_dates_needed`, **schedule** = `standard_schedule`, **pay** = same extraction as `Salary_Pay_Range__c`, **clinical scope** / **support** / **requirements** blocks from structured columns.
  - **Not included:** Kimedics top-line `M/D update: …` admin preamble is **not** pasted into candidate body (tooling can still parse it via `extract_kimedics_dates_update_preamble`).
  - **Stripping:** Whole assembled HTML/plain output passed through `strip_internal_presentation_phrases` where applicable in template path for cases text; description source for raw mode uses strip on `description_full_text`.
- **Raw mode:** `use_canonical_description=False` → **only** stripped `description_full_text` (no Proxi template) — used for test harness / special levers.

### `Job_Ranking__c`

- **Source:** `job_ranking`; default `**B`** if missing/blank.

### `Job_Volume__c`

- **Default (static):** **Not Provided** from `SF_PUSH_STATIC_DEFAULTS`.
- **Override:** If `avg_patients_per_day` non-empty after trim, `**Job_Volume__c`** set to that value (Kimedics “avg patients per day”).

### `roster_only__c`

- **Source:** Supabase `roster_only` string `**"true"`** / `**"false"**` (parser sets from full post text “roster only” phrase).
- **Push:** JSON boolean `**true`/`false`** (`roster_only_string_from_row` returns strings `"true"`/`"false"` for checkbox-style fields — confirm org field type matches).

### `Job_Position_Type__c` / `Job_Specialty__c` / `Occupation_DJC__c`

- **Create / full payload:** Defaults **Locums**, **General Dentistry**, **Dentist** (see static defaults).
- **PATCH:** Removed from initial update payload then **filled only if Salesforce field is blank** (non-null/non-empty string considered “has value”).
- **Picklist:** Coerced via describe.

### `Job_Job_Source__c`

- **Default:** **Shiftwise - Aspen Dental - AMN** (in `SF_PUSH_JOB_ROLE_DEFAULTS` with role fields).
- **PATCH:** Same **null-fill** behavior as role fields (only send when SF empty).
- **Picklist:** Coerced to org API value.

### `Position_Type_DJC__c` / `Specialty_DJC__c`

- **Fixed at push:** **Locums** / **General Dentistry** (legacy mirror fields in `SF_PUSH_STATIC_DEFAULTS`).
- **Not** in the PATCH null-fill dict — they always ride the main row payload if present after `job_row_to_salesforce_fields` (still subject to describe updateable filter).

### `Worksite_Parent__c`

- **Fixed text:** **Aspen Dental Management Inc.**

### `Job_Patient_Ages__c`

- **Fixed:** **Mostly Adults**

---

## Optional test fields (scrape sync only, `PROXI_SF_TEST_MODE=true`)

### `test_status__c`

- **Value:** Raw trimmed Kimedics `**status`** string (not mapped to Open/Closed) — for debugging / QA fields only.

### `test_posted_date__c`

- **Source:** `posted_date` from row, normalized to **YYYY-MM-DD** when parse succeeds (`posted_date_to_salesforce_date`).

---

## Worksite Account (separate object — not Job__c)

When automation **creates** a worksite **Account** (`PROXI_SF_CREATE_WORKSITES=true`, write gate on):


| API field        | Logic                                                                                       |
| ---------------- | ------------------------------------------------------------------------------------------- |
| `Name`           | `Aspen Dental - {City}, {ST}` (2-letter state)                                              |
| `ShippingStreet` | Same formatting as `Job_Worksite_1_Address__c` from `address_line`                          |
| `ParentId`       | Aspen Dental Management Inc. Id                                                             |
| `RecordTypeId`   | `PROXI_SF_ACCOUNT_WORKSITE_RECORD_TYPE_ID` **or** Account describe **Worksite** record type |


Optional merge: `**PROXI_SF_ACCOUNT_CREATE_EXTRA_JSON`**.

---

## Supabase / parser — where structured columns come from (for manual parity)

- **Labeled lines** in `description_full_text` (`Address:`, `City:`, `State:`, `Dates:`, `Hours:`, etc.) populate columns; **sections** fill gaps when labels missing.
- `**address_line`:** Composed from street + city + state when partial; then `**format_us_address_line_for_display`** on parse exit.
- `**dates_needed`:** Overridden in `_fill_from_description_blocks` when `**Active needs are`**  clause exists (wins over `Dates:` line).
- `**types_of_cases`:** Often combined from required procedures + additional requirements (`job_content_ai.combined_types_of_cases` may run in pipeline).
- **AI validate/fix:** Optional stages in `job_content_ai` may adjust fields — automation still applies **push-time** rules above on whatever lands in `job_current`.

---

## Fields automation does **not** map (today)

- `**priority`** → stored in Supabase; `**Job_Recruitment_Level__c**` is **not** in `CANONICAL_JOB_C_PUSH_FIELD_NAMES` (not sent).
- Any Job__c API name **not** in the canonical set will never be emitted by `job_row_to_salesforce_fields` until code + doc are updated together.

---

## Manual checklist: “does automation match what I do?”

- **Status:** Do you always map Kimedics to **Open** vs **Closed** the same way (not accepting → Closed; accepting new providers → Open)?
- **State:** Do you enter **full state name** on Job__c while Kimedics shows **TX**?
- **Dates:** When Kimedics adds **“Active needs are …”** on a line, is that the **real** need date line you would type into Salesforce?
- **Address:** Do you strip trailing **empty commas** and fix ALL CAPS the same way?
- **Picklists:** Do your manual values match **API values** or **labels**? Automation coerces — mismatched org values surface as “first active” fallback (watch stderr/logs).
- **Worksite:** Do you only set **Worksite Location 1** when you have a real **Account Id**?
- **Job Name brand:** Does Kimedics **Posting org** (`posting_org`) match how you choose **Heartland Dental** vs **Midwest Dental** vs **Aspen** in the middle segment of the Job **Name**?
- **Role/source backfill:** On **updates**, do you avoid overwriting recruiter-filled **Position / Specialty / Occupation / Job Source** when SF already has a value? Automation skips those unless blank.

---

## Implementation index


| Concern                                                                | Module                                                               |
| ---------------------------------------------------------------------- | -------------------------------------------------------------------- |
| Field dict, Name, status, links, defaults, `prepare_payload_for_write` | `src/utils/sf_job_payload.py`                                        |
| Canonical description + dates override + presentation strip            | `src/utils/job_description_proxi_template.py`                        |
| Address normalization                                                  | `src/utils/address_display_format.py`                                |
| Pay extraction                                                         | `src/utils/sf_pay_range.py`                                          |
| State expand                                                           | `src/utils/us_state_expand.py`                                       |
| Insight dedupe                                                         | `src/utils/insight_sanitize.py`                                      |
| Scrape PATCH, GET-merge for roles, test fields                         | `src/utils/sf_scrape_sync.py`                                        |
| Describe filter / PATCH body                                           | `src/utils/sf_partial_update.py`, `src/utils/sf_job_rest_minimal.py` |
| Parser / `address_line` / active needs in DB                           | `src/utils/job_content_parser.py`                                    |
| Worksite Account POST                                                  | `src/utils/sf_worksite_create.py`                                    |
| Resolver / auto-create                                                 | `src/utils/sf_job_supabase_resolve.py`                               |



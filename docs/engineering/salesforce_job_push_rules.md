# Salesforce Job__c push rules

This document is the source of truth for how **Supabase `job_current`** (and the same keys from the Kimedics parser) map to **Salesforce `Job__c`** on **create** and **update**.

**Field-by-field logic** (for manual Kimedics ↔ Salesforce parity review): [`salesforce_field_logic_manual_parity.md`](./salesforce_field_logic_manual_parity.md).

Implementation lives in:

- `src/utils/sf_job_payload.py` — field dict, picklist coercion, push-time defaults
- `src/utils/job_description_proxi_template.py` — canonical long-form job description
- `src/utils/us_state_expand.py` — state abbreviations → full names (e.g. TX → Texas)
- `src/utils/sf_pay_range.py` — pay range extracted from raw description text
- `src/dev/update_salesforce_job.py` — **Lever 1**: PATCH existing record by Salesforce Id
- `src/dev/create_test_job_salesforce.py` — **Lever 2**: create one obvious test row (`--yes` / `--auth-check`)
- `src/utils/sf_worksite_create.py` — create worksite **Account** + `sf_worksite_location_map` upsert
- `src/utils/sf_job_supabase_resolve.py` — practice / external-id matching + optional **create** `Job__c`
- `src/utils/sf_scrape_sync.py` — PATCH Job__c after scrape (same pipeline run) using full payload + optional test fields
- `src/utils/pipeline_link_scrape.py` — link batch: persist rows, resolve ids, then call `sf_scrape_sync`
- `src/utils/sf_write_flags.py` — `proxi_sf_writes_enabled()` reads `**PROXI_SF_UPDATE_JOBS`**

---

## Salesforce write kill switch (`PROXI_SF_UPDATE_JOBS`)

When `**PROXI_SF_UPDATE_JOBS**` is `**false**`, `**0**`, `**no**`, or `**off**` (any casing), automation **does not** POST or PATCH Salesforce: scrape field sync (`sf_scrape_sync`), auto-create `Job__c` (`PROXI_SF_CREATE_JOBS`), or POST new worksite **Accounts** (`PROXI_SF_CREATE_WORKSITES`). Supabase ingestion and mapping **reads** (e.g. job list for practice matching) still run if credentials are present.

When **unset**, behavior matches historical defaults: writes are **on** (`true`).

Manual scripts under `src/dev/` (e.g. `update_salesforce_job.py`) are **not** gated by this flag unless you add checks there.

---

## Observability: auto-created `Job__c`

When automation POSTs a new `Job__c` after no 1:1 match, it writes `**job_event_log.event_type` = `job_created_in_salesforce`** (payload includes `sf_job_id`, `sf_worksite_account_id`, `automation_kind` = `salesforce_job_auto_created`, and a short `summary`). Failures use `job_create_failed`; skipped creates (e.g. `PROXI_SF_UPDATE_JOBS=false`) use `job_create_skipped`. The **automation-hub** app surfaces this in run history (violet “+N new Job__c”) and in the per-job validation UI (banner + violet timeline row).

---

## Hyperlink / Account lookups (REST = Id only)

In the Salesforce UI these behave as hyperlinks; the REST API accepts the **Account Id** only.


| Concept                                        | Default / rule                                                                                                                                                                                                      | Target API field             |
| ---------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------- |
| **Account** — Aspen Dental Management Inc.     | `0015f00000HH63kAAD` (unless row override)                                                                                                                                                                          | `Job_Account__c`             |
| **Worksite Location 1** — worksite **Account** | **No automatic default.** Set only when `sf_worksite_account_id` is known on the row (or a caller passes `worksite_account_id`). Otherwise `**Job_Worksite_Location_1__c` is omitted** from create/update payloads. | `Job_Worksite_Location_1__c` |


**Worksite Account `Name` / map label:** `Aspen Dental - {City}, {ST}` with a **2-letter state** (e.g. Dunkirk + NY → `Aspen Dental - Dunkirk, NY`) — `utils.sf_push_defaults.format_worksite_account_name` / `format_worksite_display_label`, used by `job_sf_enrichment` and `sf_worksite_create`.

**New worksite Account (create path):** When `PROXI_SF_CREATE_WORKSITES=true`, the REST **POST** includes:


| Field            | Source                                                                                                                         |
| ---------------- | ------------------------------------------------------------------------------------------------------------------------------ |
| `Name`           | `format_worksite_account_name(city, state)`                                                                                    |
| `ShippingStreet` | Kimedics `address_line` when non-empty (full single line, e.g. `101 Iowa Avenue West, Marshalltown, IA 50158`)                 |
| `ParentId`       | `SF_ACCOUNT_ASPEN_DENTAL_MANAGEMENT_ID` (`0015f00000HH63kAAD`)                                                                 |
| `RecordTypeId`   | Env `PROXI_SF_ACCOUNT_WORKSITE_RECORD_TYPE_ID`, or Account describe `recordTypeInfos` entry named / developer-named `Worksite` |


Non-createable keys are dropped by describe (same pattern as Job__c). Optional JSON merge: `PROXI_SF_ACCOUNT_CREATE_EXTRA_JSON`.

The returned Account Id is stored in `sf_worksite_location_map` and on the job row as `sf_worksite_account_id`, and pushed to `**Job_Worksite_Location_1__c`**. The same Kimedics `**address_line**` is also sent on the Job as `**Job_Worksite_1_Address__c**` (`sf_job_payload`).

**Deleted worksite Account Ids:** If Salesforce returns **entity is deleted** on Job__c POST (stale Id from `sf_worksite_location_map` / `sf_worksite_account_id` after the Account was removed in SF), the resolver clears the map row(s), logs `worksite_stale_map_cleared`, and **POSTs a new worksite Account** (when `PROXI_SF_CREATE_WORKSITES=true`) with `skip_location_lookup`, then retries Job create once. Scrape PATCH retries once **without** `Job_Worksite_Location_1__c` if the first PATCH fails with the same error.

**Canonical Job__c push fields:** Only API names in `utils.sf_job_payload.CANONICAL_JOB_C_PUSH_FIELD_NAMES` may be produced by `job_row_to_salesforce_fields` (plus describe/picklist coercion). Change the frozenset, mapper, and this doc together.

**Worksite Account Id** resolution order:

1. `job_current` / `job_content` cached `sf_worksite_account_id`
2. 1:1 Salesforce match (practice or External Job ID) via `utils.sf_job_supabase_resolve` → copies `Job_Worksite_Location_1__c` from the matched `Job__c`
3. Table `**sf_worksite_location_map`** keyed by normalized `(city, state)` → `salesforce_account_id` (`utils.supabase_db`)
4. If `**PROXI_SF_CREATE_WORKSITES=true`** and `**PROXI_SF_UPDATE_JOBS`** is not false: POST a new worksite **Account** (`Name`, `ShippingStreet`, `ParentId`, `RecordTypeId` as above), upsert the map row, set `sf_worksite_account_id` on the scraped row (`utils.sf_worksite_create`, called from `job_sf_enrichment` and from the create-job path in the resolver)

Optional Account fields at create time: JSON object in env `**PROXI_SF_ACCOUNT_CREATE_EXTRA_JSON`** (merged into the REST body; non-createable keys are dropped by describe filter).

**New `Job__c` when unmapped:** If practice + **External_Job_ID__c** + AI all yield **no** 1:1 match, the resolver logs `mapping_no_match`. When `**PROXI_SF_CREATE_JOBS=true`** and `**PROXI_SF_UPDATE_JOBS`** is not false, it POSTs a new `Job__c` from `prepare_payload_for_write(..., for_update=False)` and stores the new Id on `job_current` / `job_content` (`event_type`: `job_created_in_salesforce`). If create is skipped because writes are off, `job_event_log` may record `job_create_skipped`. Ambiguous (**N>1**) matches **never** auto-create.

**PATCH after scrape** (`utils.sf_scrape_sync`, invoked from `utils.pipeline_link_scrape` in the same Modal run as Playwright): when `**PROXI_SF_UPDATE_JOBS`** is not false, builds desired fields with `**prepare_payload_for_write(..., for_update=True)`** (same rules as manual lever 1) and PATCHes diffs; otherwise the sync is skipped (event `sf_scrape_fields_skip`). Org-specific **test** custom fields are included only when `**PROXI_SF_TEST_MODE=true`**. When `**sf_worksite_account_id`** is still empty on `job_current`, the sync writes `**job_event_log.event_type` = `sf_worksite_missing_on_job_row**` (payload includes `sf_job_id` and a short message) so the gap is auditable; the worksite field is not back-filled with a placeholder Id.

---

## Fixed at push time (not stored in Supabase)

These values are **always applied when building the Salesforce payload**, even if Supabase has empty or stale data. They should **not** be maintained as columns in Supabase.


| Salesforce field       | Default value                                                                                                             |
| ---------------------- | ------------------------------------------------------------------------------------------------------------------------- |
| `Job_Position_Type__c` | Locums                                                                                                                    |
| `Job_Specialty__c`     | General Dentistry                                                                                                         |
| `Occupation_DJC__c`    | Dentist                                                                                                                   |
| `Position_Type_DJC__c` | Locums (legacy; same as position type when present in org)                                                                |
| `Specialty_DJC__c`     | General Dentistry (legacy)                                                                                                |
| `Worksite_Parent__c`   | Aspen Dental Management Inc.                                                                                              |
| `Job_Patient_Ages__c`  | Mostly Adults                                                                                                             |
| `Job_Volume__c`        | Not Provided — **overridden** when Supabase has non-empty `avg_patients_per_day` (Kimedics **Avg patients per day** line) |
| `Job_Job_Source__c`    | Shiftwise - Aspen Dental - AMN (restricted picklist; API value aligned at push via describe)                              |


**Role and job source on update:** `Job_Position_Type__c`, `Job_Specialty__c`, `Occupation_DJC__c`, and `Job_Job_Source__c` are included on **create** with the defaults above. On **PATCH** (scrape sync and `update_salesforce_job.py`), they are sent **only when the existing Salesforce value is blank**, so non-null org data is not overwritten (`merge_job_role_defaults_for_empty_sf_fields`). Picklist labels are matched to valid API values before the write.

**Candidate-facing description:** Kimedics internal wording *“please notate any limitations in presentation”* (and the leading comma / dash) is stripped from `Job_Client_Job_Description__c`, raw-description pushes, and `Job_Types_of_Cases__c` via `strip_internal_presentation_phrases` in `utils.job_description_proxi_template`.

### Salary / pay range


| Salesforce field      | Rule                                                                                                                                                                        |
| --------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `Salary_Pay_Range__c` | If the **raw** `description_full_text` contains a plausible dollar range (e.g. `$125 – $145 per hour`), use the **first match**. Otherwise use `**Starting at $125/hour`**. |


Logic: `utils/sf_pay_range.extract_pay_range_from_description`.

---

## From job post / Supabase (structured columns)

Parsed from Kimedics pages into `job_content` / `job_current` (see `utils/job_content_parser.py`).


| Source column                               | Salesforce field                                          | Notes                                                                                                                                                                                                                                                                                                                                               |
| ------------------------------------------- | --------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `status`                                    | `Job_Status__c`                                           | Push-only: only **Open** or **Closed**. **Active, accepting new providers** (any casing) → **Open**; all other non-empty Kimedics statuses (e.g. not accepting, Inactive) → **Closed**.                                                                                                                                                             |
| `city`                                      | `Job_City__c`                                             | Title-cased in parser                                                                                                                                                                                                                                                                                                                               |
| `state`                                     | `Job_State__c`                                            | **Full state name** at push (TX → Texas): `us_state_expand.state_name_for_salesforce`                                                                                                                                                                                                                                                               |
| `insight`                                   | `Insight__c`                                              | Parser: `*` / `**` segments; **deduped** at push (case/whitespace-insensitive) so repeated bullets are not sent twice.                                                                                                                                                                                                                              |
| `dates_needed`                              | `Job_Dates_Needed__c`                                     | Heuristic + optional AI (`utils/job_content_ai.py`). **Override:** when `description_full_text` contains `Active needs are ` on a line, the text **after that phrase on the same line** wins for this field and for canonical description dates (`effective_dates_needed` in `utils.job_description_proxi_template`; parser applies it in `_fill_from_description_blocks`). |
| `standard_schedule`                         | `Job_Standard_Schedule__c`                                | Heuristic + optional AI                                                                                                                                                                                                                                                                                                                             |
| `standard_schedule`                         | `Standard_Schedule_Hours__c`                              | Same source as schedule line; hours-style field on Job__c                                                                                                                                                                                                                                                                                           |
| `types_of_cases`                            | `Job_Types_of_Cases__c`                                   | Join of “Required procedures” + “Additional requirements” in parser; AI can refine                                                                                                                                                                                                                                                                  |
| `support_staff`                             | `Job_Support_Staff__c`                                    | e.g. “Clinical Staff” line; AI can refine                                                                                                                                                                                                                                                                                                           |
| `avg_patients_per_day`                      | `Job_Volume__c`                                           | Parsed from description (`Avg patients per day:` / section); omitted in SF when empty (static default **Not Provided** then applies)                                                                                                                                                                                                                |
| `roster_only` (`"true"` / `"false"`)        | `roster_only__c`                                          | **Checkbox** (recommended) at push: JSON `true`/`false` when full Kimedics post matches `roster only` / `roster-only` (case-insensitive). If your org uses a **Text** field instead, change the payload in `sf_job_payload.py` to send `"true"`/`"false"` strings.                                                                                  |
| `priority`                                  | —                                                         | Parsed into Supabase only; **not** sent to Salesforce (`Job_Recruitment_Level__c` excluded from push).                                                                                                                                                                                                                                              |
| `job_ranking`                               | `Job_Ranking__c`                                          | Default `B` if missing                                                                                                                                                                                                                                                                                                                              |
| `practice_value`                            | `Job_Facility_Display__c`                                 |                                                                                                                                                                                                                                                                                                                                                     |
| `practice_value`                            | `Job_Client_Job_Id__c`                                    | Practice identifier used for SF matching; should mirror Kimedics practice label                                                                                                                                                                                                                                                                     |
| `address_line`                              | `Job_Worksite_1_Address__c`                               | Single-line worksite address on the Job; same value is used for new worksite Account `ShippingStreet` on create. **Display:** Empty comma slots are removed (no trailing `, ,`); ALL CAPS Kimedics lines become title-style words (`Blvd`, directionals `SW`, `PO Box`, states) via `utils.address_display_format` at parse and at Salesforce push. |
| (derived)                                   | `Name`                                                    | Header title: `{StateAbbr} ({City}) {Specialty_DJC default} - Aspen Dental Management Inc. - {Open|Closed}` (80 char cap).                                                                                                                                                                                                                          |
| `point_of_contact`                          | `Job_Point_of_Contact__c`                                 |                                                                                                                                                                                                                                                                                                                                                     |
| `provider_start_date` / `provider_end_date` | `Job_Provider_Start_Date__c` / `Job_Provider_End_Date__c` | Converted from `MM/DD/YY` → ISO date when possible                                                                                                                                                                                                                                                                                                  |
| `job_id` (numeric)                          | `External_Job_Link__c`                                    | Canonical Kimedics URL: `https://portal.kimedics.com/app/workspace/job-posts/{job_id}` (short; no truncation).                                                                                                                                                                                                                                      |
| `view_job_link`                             | `External_Job_Link__c`                                    | **Fallback** when `job_id` is missing or not all digits (e.g. test rows): value is truncated to **255** characters if longer.                                                                                                                                                                                                                       |
| `job_id`                                    | `External_Job_ID__c`                                      | **Truncated to org max length (20)** if longer (adjust `EXTERNAL_JOB_ID_MAX_LEN` in code if your field allows more).                                                                                                                                                                                                                                |
| `description_full_text`                     | See below                                                 |                                                                                                                                                                                                                                                                                                                                                     |


---

## Canonical job description (synchronous with structured fields)

**Rule:** `Job_Client_Job_Description__c` should reflect the **same** city, state, dates, schedule, cases, and support staff we send in the other fields.

When `use_canonical_description=True` (default on `update_salesforce_job.py`), the body is built with `build_proxi_job_posting_description()`:

- **Default output is HTML** (`<p>`, `<strong>`, `<ul>`/`<li>`, `<br/>`, `<hr/>`) so **Rich Text Area** fields show bold section titles and lists. Set env `**PROXI_JOB_DESCRIPTION_HTML=false`** for plain text (use when `Job_Client_Job_Description__c` is a **Long Text Area** — otherwise HTML tags would appear literally). The automated scrape → Salesforce sync (`sf_scrape_sync`) uses the same default as `sf_job_payload` (HTML when the variable is unset).
- **Opening:** by default, `**PROXI_JOB_DESCRIPTION_USE_AI`** is treated as on (unset → try OpenAI intro via `job_description_ai.generate_ai_intro_html`; requires `OPENAI_API_KEY`). Set to `false` to use the fixed template paragraphs only. On API/key failures the template falls back without failing the push.
- **Insight__c** is deduped from Kimedics `*` / `**` bullets (`utils.insight_sanitize`); the long-form **Job_Client_Job_Description__c** template does not repeat insight as “source notes” (keeps client-facing copy clean).
- **Dates** use `effective_dates_needed(row)` (same as `Job_Dates_Needed__c`): `dates_needed` unless `description_full_text` has an **Active needs are …** clause on a line, in which case only the substring after those words is used. **Schedule** still comes from `standard_schedule`. Kimedics admin lines at the top matching `M/D update:` / `M/D/YY(YY) update:` are **not** copied into the client-facing description; `extract_kimedics_dates_update_preamble()` remains available for other uses (e.g. tooling).
- **Pay Range** line aligned with `Salary_Pay_Range__c` (extract from raw text or default)
- **Patient Mix**, **Clinical Scope** (from `types_of_cases` or sensible defaults)
- **Support Staff** from `support_staff`
- **Requirements** tail (state license, DEA, etc.)
- Optional **Kimedics excerpt** appended under a `Source job post` separator for traceability

Use `--raw-description` on `update_salesforce_job.py` to push **only** `description_full_text` into `Job_Client_Job_Description__c` (no template).

**Create-test lever** (`create_test_job_salesforce.py`) uses `use_canonical_description=False` so the synthetic test banner in `description_full_text` stays the main body unless you change that flag in code.

---

## Other rules already in code

- `**External_Job_ID__c` length:** truncated in `sf_job_payload._truncate_external_job_id`.
- **Picklists:** values are matched to the org describe; invalid labels fall back to the first active value (`coerce_picklists_to_valid`).
- **Staging vs production DB:** local scripts may prompt for schema; Modal continues to use `public` defaults.

---

## Lever 1 — Update existing `Job__c`

Script: `src/dev/update_salesforce_job.py` · Notebook: `manual/lever1_patch_job_fields.ipynb`

1. Load a row from `**job_current`** (`--from-supabase-job-id`) or from `**--from-json`**.
2. Enrichment uses `**utils.supabase_db.load_job_current_row_for_salesforce`** (or the same logic inside the script): Account / worksite Ids from Supabase lookup tables.
3. Build payload with `**prepare_payload_for_write**` (updateable fields + picklists) + PATCH to `--sf-id` / notebook `RECORD_ID`.

```bash
python src/dev/update_salesforce_job.py \
  --sf-id a01UP00000cwiFhYAI \
  --from-supabase-job-id YOUR_KIMEDICS_JOB_ID \
  --dry-run

python src/dev/update_salesforce_job.py \
  --sf-id a01UP00000cwiFhYAI \
  --from-supabase-job-id YOUR_KIMEDICS_JOB_ID \
  --yes
```

---

## Lever 2 — Create new `Job__c` (testing vs production)

Script: `src/dev/create_test_job_salesforce.py`


| Mode                    | Flag           | Behavior                                                                                                                                |
| ----------------------- | -------------- | --------------------------------------------------------------------------------------------------------------------------------------- |
| **Auth smoke test**     | `--auth-check` | OAuth only; **no** insert                                                                                                               |
| **Create one test row** | `--yes`        | Inserts a **single** row with an obvious `[AUTOMATION TEST ROW…]` banner and synthetic `External_Job_ID__c` (`TEST` + hex, length ≤ 20) |


**Production “create job”** later should reuse `prepare_payload_for_write(..., for_update=False)` + `create_job_record` with real `job_current` data and your change-management flags — not the test banner row.

**Safety:** create script **requires** `--yes` so it is never run accidentally.

---

## Field API names and org drift

If a field API name in your org differs (e.g. no `_DJC__c` suffix), Salesforce **describe** will drop it from the payload (`Skipped (not updateable on object)` / `not createable`). Adjust `SF_PUSH_STATIC_DEFAULTS` and the mapper in `sf_job_payload.py` to match your org’s metadata.
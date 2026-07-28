# Data Model

There is no SQL database in v1. State lives in two places:

- **Redis** — authoritative for live reads. All API reads hit Redis.
- **Google Sheets** — durable append-only job log + client-editable prompt matrix. Written twice per job.

> ✅ **Reconciled in Phase 0a Step 6** (2026-07-26) against the client's real spreadsheet. The `JewelryType` and `ServiceType` enums below, and the matrix structure in §2, reflect the actual sheet — not the original n8n-prototype assumption. See `phases/phase-roadmap.md` → Scoping Changes for what differed and why.

---

## 1. Enums

### JobStatus

| Value | Terminal | Meaning |
|-------|----------|---------|
| `queued` | no | Accepted, waiting for a worker |
| `classifying` | no | Gemini vision call in flight |
| `resolving` | no | Looking up prompt + reference in the matrix |
| `submitting` | no | **Danger zone.** Submit to provider in flight or unconfirmed |
| `generating` | no | Provider accepted; polling for completion |
| `storing` | no | Downloading result and uploading to storage |
| `succeeded` | **yes** | Assets available |
| `failed` | **yes** | Terminal error; see `error_code` |
| `needs_input` | **yes** | Classification ambiguous; client must supply `jewelry_type` |
| `needs_review` | **yes** | Possible orphaned paid submit; human must check the provider |

Legal transitions:

```
queued      → classifying | resolving | failed
classifying → resolving | needs_input | failed
resolving   → submitting | failed
submitting  → generating | needs_review | failed
generating  → storing | failed
storing     → succeeded | failed
needs_input → resolving        (via POST /jobs/{id}/resolve)
```

Any other transition is a bug. `app/core/state.py` must reject it and raise.

### ErrorCode

| Code | Stage | Retryable | Meaning |
|------|-------|-----------|---------|
| `INVALID_IMAGE` | ingest | no | File is not decodable as an image |
| `IMAGE_TOO_LARGE` | ingest | no | Exceeds `MAX_IMAGE_BYTES` |
| `UNSUPPORTED_FORMAT` | ingest | no | Not JPEG/PNG/WebP |
| `NOT_JEWELRY` | classify | no | Gemini reports the image contains no jewellery |
| `LOW_CONFIDENCE` | classify | no | Below threshold → drives `needs_input`, not `failed` |
| `CLASSIFIER_ERROR` | classify | yes | Gemini call failed or returned malformed output |
| `MATRIX_MISS` | resolve | no | No row for this Type × Service combination |
| `MATRIX_UNAVAILABLE` | resolve | yes | Sheets unreachable and cache is cold |
| `PROVIDER_SUBMIT_FAILED` | submit | no | Provider rejected the request outright (confirmed no charge) |
| `ORPHANED_SUBMIT` | submit | **never** | Submit outcome unknown → `needs_review` |
| `PROVIDER_TIMEOUT` | generate | no | Exceeded `JOB_DEADLINE_SECONDS` |
| `PROVIDER_ERROR` | generate | yes | Provider reported generation failure |
| `STORAGE_ERROR` | store | yes | Drive upload failed |
| `SHEETS_ERROR` | any | yes | Sheets write failed (job still valid in Redis) |
| `BUDGET_EXCEEDED` | ingest | no | Daily spend ceiling hit |
| `RATE_LIMITED` | ingest | no | Per-key rate limit hit |
| `INTERNAL_ERROR` | any | yes | Unhandled |

### JewelryType

`RING`, `NECKLACE`, `EARRING`, `BRACELET`, `BANGLE`, `ANKLET`, `HIPBELT`

Stored uppercase. The classifier is constrained to exactly this set. This is the client's actual product range (7 types) — the originally-assumed `PENDANT`, `CHAIN`, `NOSE_PIN`, `MANGALSUTRA`, `BROOCH`, `TOE_RING` do not appear in the matrix and were removed; `HIPBELT` was added.

### ServiceType

The client's matrix crosses 4 model/context categories with 2 styles, not the originally-assumed shot types.

| Value | In v1 | Description |
|-------|-------|-------------|
| `FEMALE_MODEL_TRADITIONAL` | yes | Female model, traditional styling |
| `FEMALE_MODEL_MODERN` | yes | Female model, modern styling |
| `MALE_MODEL_TRADITIONAL` | yes | Male model, traditional styling |
| `MALE_MODEL_MODERN` | yes | Male model, modern styling |
| `MANNEQUIN_TRADITIONAL` | yes | Mannequin display, traditional styling — in practice only ever populated for `NECKLACE` |
| `MANNEQUIN_MODERN` | yes | Mannequin display, modern styling — in practice only ever populated for `NECKLACE` |
| `PRODUCT_STYLING_TRADITIONAL` | yes | Styled product shot (no model), traditional |
| `PRODUCT_STYLING_MODERN` | yes | Styled product shot (no model), modern |
| `REMOVE_BG` | **v2** | Reject with 422 in v1. Not present in the client's sheet |
| `CHANGE_BG` | **v2** | Reject with 422 in v1. Not present in the client's sheet |
| `MIX_PIECES` | **v2** | Reject with 422 in v1. Not present in the client's sheet |

### TypeSource

`PROVIDED` (client sent it) · `CLASSIFIED` (Gemini inferred it) · `RESOLVED` (client supplied it after `needs_input`)

---

## 2. Google Sheets

One spreadsheet, ID in `GOOGLE_SHEET_ID`. Service account needs Editor. As of Phase 0b the spreadsheet still contains only `Sheet1` (the prompt matrix) — **the `JobLog` tab (§ below) does not exist yet.** `app/store/sheets_store.py` is built and tested against a `FakeSheetsClient`; nothing in Phase 0b writes to the real spreadsheet. Confirmed live: the worker's `rehydrate()` startup call against the real `GOOGLE_SHEET_ID` fails cleanly (`worker.rehydration.failed`, caught, does not crash boot) because `JobLog!A:T` doesn't exist. **The `JobLog` tab must be created (headers A–T per the table below) before Phase 1 exercises real Sheets writes** — this is a precondition for Phase 1, not something Phase 0b could create unilaterally on the client's spreadsheet.

### Tab: `Sheet1` — the prompt matrix, client-editable, read-only to us

**Not a flat table.** The client authored this as a pivot grid by hand:

- **Row 1** is the header: column A is blank, columns B onward are `JewelryType` labels (`Anklets`, `Necklace`, `Earrings`, `Bangles`, `Bracelets`, `Hipbelt`, `Ring`) — mapped case-insensitively to `JewelryType` via `HEADER_TO_JEWELRY_TYPE` in `scripts/validate_matrix.py`.
- **Column A** carries block labels, not data:
  - A **category** label on its own row: `Female Model`, `Male Model`, `Mannequin - only in necklace`, `Product styling`.
  - Immediately followed (same row or a later row) by a **style** label: `Traditional` or `Modern`.
  - `service = f"{CATEGORY}_{STYLE}"`, e.g. `Female Model` + `Traditional` → `FEMALE_MODEL_TRADITIONAL`.
- **Data cells**: the style-label row and every following row with a blank column A belong to that (category, style) block, until the next label row. Each non-blank cell under a jewelry-type column is one **prompt variant** for that `(jewelry_type, service)` pair — the sheet has anywhere from 0 to 6 variants stacked per combination, entered by hand with no fixed count.
- **Reference URL is embedded, not a separate column**: each prompt cell ends with a Google Drive link (`https://drive.google.com/...`), split off by `REFERENCE_URL_RE` in `scripts/validate_matrix.py`. The remaining text (trimmed) is `prompt_snapshot`.
- **No `active`, `negative_prompt`, `provider_params`, or `updated_at` columns exist.** All active rows are implicitly active (non-blank = active); the other three fields are always `null` until the client's sheet grows them.
- **Multiple variants per `(jewelry_type, service)` are expected, not a duplicate-key error.** `app/services/matrix.py` (Phase 3) picks one variant at random per resolve — this is a deliberate client decision, not last-write-wins.
- `Mannequin - only in necklace` is descriptive, not enforced structurally — in practice the client has only ever filled in `NECKLACE` cells under the two `MANNEQUIN_*` services; other combinations are legitimate `MATRIX_MISS`es, not bugs.

`scripts/validate_matrix.py` is the only code that should parse this layout; nothing else may assume a flat row-per-combination structure.

### Tab: `JobLog` — written by us, read by the client

| Col | Header | Written on |
|-----|--------|------------|
| A | `job_id` | create |
| B | `created_at` | create |
| C | `api_key_name` | create |
| D | `service` | create |
| E | `jewelry_type` | create (may be `AUTO`) |
| F | `mock` | create |
| G | `content_hash` | create |
| H | `status` | terminal |
| I | `completed_at` | terminal |
| J | `duration_seconds` | terminal |
| K | `jewelry_type_final` | terminal |
| L | `type_source` | terminal |
| M | `confidence` | terminal |
| N | `provider` | terminal |
| O | `provider_job_id` | terminal |
| P | `asset_count` | terminal |
| Q | `storage_refs` | terminal (comma-separated) |
| R | `error_code` | terminal |
| S | `error_message` | terminal |
| T | `prompt_snapshot` | terminal |

**Exactly two writes per job.** Append on create (returns `updatedRange` → persist the row index in Redis), single `values.update` on terminal state. All writes serialised behind `lock:sheets:write`.

---

## 3. Redis Keyspace

Requires AOF (`appendfsync everysec`) and a mounted volume — Redis is authoritative for live reads.

| Key | Type | TTL | Contents |
|-----|------|-----|----------|
| `job:{job_id}` | hash | 48h | Full job record — see §4 |
| `job:{job_id}:row` | string | 48h | Sheets `JobLog` row index for the terminal update |
| `jobs:recent` | zset | — | `job_id` scored by `created_at` epoch. Capped at 500 |
| `dedupe:{content_hash}` | string | `DEDUPE_WINDOW_SECONDS` (24h) | `job_id` of a prior `succeeded` job |
| `idem:{api_key_name}:{idempotency_key}` | string | 24h | `job_id` |
| `matrix:data` | string | `MATRIX_CACHE_TTL` (300s) | Serialised matrix, JSON |
| `matrix:version` | string | — | Hash of matrix contents; copied to jobs as `matrix_version` |
| `lock:matrix:refresh` | string | 30s | Prevents thundering-herd refresh |
| `lock:sheets:write` | string | 15s | Serialises all Sheets writes |
| `spend:{YYYY-MM-DD}` | string | 48h | Counter of billable generations today |
| `ratelimit:{api_key_name}:{minute}` | string | 120s | Request counter |
| `arq:*` | — | — | Managed by ARQ. Do not touch |

**Key rule:** `job:{job_id}` TTL is 48h, but the Sheets `JobLog` row is permanent. Fetching a job older than 48h returns `410 Gone` with a pointer to the log — it is **not** a 404.

---

## 4. Job Record

Stored as a Redis hash at `job:{job_id}`. All values serialised as strings; nested values as JSON.

### Identity & request
| Field | Type | Set at | Notes |
|-------|------|--------|-------|
| `job_id` | uuid4 str | create | Primary key |
| `api_key_name` | str | create | Owner. Enforced on every read |
| `service` | ServiceType | create | Required from client |
| `jewelry_type_requested` | JewelryType \| null | create | Null → classify |
| `mock` | bool | create | Mock jobs never call the provider |
| `callback_url` | str \| null | create | Stored, unused in v1 |
| `idempotency_key` | str \| null | create | From header |
| `content_hash` | sha256 hex | create | See business-rules §Hashing |
| `deduplicated_from` | job_id \| null | create | Set when returning a prior result |

### Lifecycle
| Field | Type | Set at | Notes |
|-------|------|--------|-------|
| `status` | JobStatus | create, each transition | |
| `created_at` | ISO8601 UTC | create | |
| `updated_at` | ISO8601 UTC | each transition | |
| `completed_at` | ISO8601 UTC \| null | terminal | |
| `deadline_at` | ISO8601 UTC | create | `created_at + JOB_DEADLINE_SECONDS`. Never null |
| `attempt_count` | int | each retry | |

### Source image
| Field | Type | Set at | Notes |
|-------|------|--------|-------|
| `source_ref` | str | ingest | Opaque `storage_ref`. **Never a URL** |
| `source_bytes` | int | ingest | |
| `source_mime` | str | ingest | |

### Classification
| Field | Type | Set at | Notes |
|-------|------|--------|-------|
| `jewelry_type_final` | JewelryType \| null | classify/resolve | Used for matrix lookup |
| `type_source` | TypeSource | classify/resolve | |
| `confidence` | float \| null | classify | 0.0–1.0 |
| `candidate_types` | JSON list \| null | classify | Populated on `needs_input` |

### Matrix snapshot — immutable once set
| Field | Type | Set at | Notes |
|-------|------|--------|-------|
| `prompt_snapshot` | str | resolve | Verbatim. **Never mutated** |
| `negative_prompt_snapshot` | str \| null | resolve | |
| `reference_url_snapshot` | str | resolve | |
| `provider_params_snapshot` | JSON \| null | resolve | |
| `matrix_version` | str | resolve | Ties output to a matrix state |

### Generation
| Field | Type | Set at | Notes |
|-------|------|--------|-------|
| `provider` | str | submit | e.g. `higgsfield`, `fake` |
| `submission_token` | uuid4 str | **before** submit | Written pre-call. Recovery handle |
| `provider_job_id` | str \| null | submit | Null while `submitting` |
| `billable` | bool | submit | False for mock and deduped jobs |

### Result
| Field | Type | Set at | Notes |
|-------|------|--------|-------|
| `asset_refs` | JSON list of `storage_ref` | store | Index order = asset index in the API |
| `error_code` | ErrorCode \| null | failure | Machine-readable |
| `error_message` | str \| null | failure | Human-readable. Never contains secrets |

---

## 5. API Keys

Not in Sheets — the client can read that spreadsheet.

`API_KEYS` env var, format `name:plaintext_key,name2:plaintext_key2`. Hashed with SHA-256 at startup into an in-memory map; the plaintext is never logged or persisted. `ADMIN_API_KEY` is separate and grants `/admin/*`.

Every job read/write verifies `api_key_name` matches the caller. A valid key for client A requesting client B's `job_id` gets **404**, not 403 — do not leak existence.

---

## 6. Environment Variables

| Var | Required | Default | Purpose |
|-----|----------|---------|---------|
| `ENV` | yes | `local` | `local` \| `staging` \| `prod` |
| `LOG_LEVEL` | no | `INFO` | |
| `API_KEYS` | yes | — | `name:key` pairs |
| `ADMIN_API_KEY` | yes | — | Admin routes |
| `REDIS_URL` | yes | — | Must point at an AOF-enabled instance |
| `GOOGLE_SHEET_ID` | yes | — | Spreadsheet holding both tabs |
| `GOOGLE_SERVICE_ACCOUNT_JSON` | yes | — | Base64 of the service account key |
| `GDRIVE_FOLDER_ID` | yes | — | Destination folder |
| `STORAGE_BACKEND` | no | `local` | `local` \| `drive` \| `supabase`. Phase 2 adapter selector |
| `SUPABASE_URL` | required if `STORAGE_BACKEND=supabase` | — | Supabase project URL |
| `SUPABASE_SERVICE_ROLE_KEY` | required if `STORAGE_BACKEND=supabase` | — | Service role key (bypasses RLS on the storage bucket) |
| `SUPABASE_STORAGE_BUCKET` | required if `STORAGE_BACKEND=supabase` | — | Private bucket name for source images and generated assets |
| `GEMINI_API_KEY` | yes | — | |
| `GEMINI_MODEL` | no | `gemini-3.1-flash-lite` | See `docs/ai-integration.md` §1 for why — `gemini-2.5-flash`/`-flash-lite` are 404 "no longer available to new users" on this project's API key, confirmed via live smoke test |
| `CLASSIFIER_CONFIDENCE_THRESHOLD` | no | `0.75` | Below → `needs_input` |
| `HIGGSFIELD_API_KEY` | yes (prod) | — | |
| `HIGGSFIELD_BASE_URL` | no | `https://api.higgsfield.ai` | **Unconfirmed placeholder** — see `phases/phase-4-provider-integration.md` → Manual Verification |
| `PROVIDER` | no | `higgsfield` | `higgsfield` \| `fake` |
| `MAX_IMAGE_BYTES` | no | `15728640` | 15 MB |
| `JOB_DEADLINE_SECONDS` | no | `900` | 15 min |
| `DEDUPE_WINDOW_SECONDS` | no | `86400` | 24h |
| `MATRIX_CACHE_TTL` | no | `300` | |
| `WORKER_CONCURRENCY` | no | `4` | Must respect the provider's cap |
| `DAILY_GENERATION_CAP` | no | `200` | Billable jobs per day |
| `RATE_LIMIT_PER_MINUTE` | no | `60` | Per key |
| `SENTRY_DSN` | no | — | Phase 8 |

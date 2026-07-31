# API Routes

Base path: `/api/v1`. All responses are JSON except asset delivery.

**Auth:** `X-API-Key` header on everything under `/api/v1` except `/health`. Admin routes require `ADMIN_API_KEY` in the same header.

**Contract stability:** these shapes are frozen at the end of Phase 1. Adding optional fields is allowed; renaming, removing, or changing the type of an existing field is a breaking change requiring `/api/v2`.

---

## Error Envelope

Every non-2xx response, without exception:

```json
{
  "error": {
    "code": "MATRIX_MISS",
    "message": "No active prompt row for ANKLET x LIFESTYLE.",
    "job_id": "e2b1...",
    "request_id": "req_8f3a..."
  }
}
```

`code` is always an `ErrorCode` from `docs/schema.md` §1, or one of the transport-level codes below. Clients branch on `code`, never on `message`.

Transport-level codes: `UNAUTHORIZED`, `FORBIDDEN`, `NOT_FOUND`, `GONE`, `VALIDATION_ERROR`, `RATE_LIMITED`, `PAYLOAD_TOO_LARGE`.

---

## Generation

### `POST /api/v1/generate`
Submit an image for generation. Returns immediately.

**Auth:** client key
**Content-Type:** `multipart/form-data` — base64 JSON bodies are rejected with 415

| Part | Type | Required | Notes |
|------|------|----------|-------|
| `image` | file | yes | JPEG/PNG/WebP, ≤ `MAX_IMAGE_BYTES` |
| `service` | text | yes | A v1 `ServiceType`. v2 values → 422 |
| `jewelry_type` | text | no | Omit to trigger classification |
| `callback_url` | text | no | Stored for v2 webhooks; unused |
| `mock` | text | no | `true` → `FakeProvider`, no spend |

**Headers:** `Idempotency-Key` (optional, recommended). Replaying the same key within 24h returns the original job.

**202 Accepted**
```json
{
  "job_id": "e2b1c9a4-...",
  "status": "queued",
  "poll_url": "/api/v1/jobs/e2b1c9a4-...",
  "deduplicated": false,
  "created_at": "2026-07-25T10:14:03Z"
}
```

`deduplicated: true` means an identical prior job succeeded within the dedupe window; `job_id` points at that job and nothing new was generated.

**Errors:** 400 `INVALID_IMAGE` · 401 `UNAUTHORIZED` · 413 `IMAGE_TOO_LARGE` · 415 `UNSUPPORTED_FORMAT` · 422 `VALIDATION_ERROR` (bad/v2 service, unknown jewelry_type) · 429 `RATE_LIMITED` · 429 `BUDGET_EXCEEDED`

---

### `POST /api/v1/classify-preview`

**Showcase-UI-only. Not part of the frozen v1 job contract** — additive tooling, not something the Flutter ERP integration needs or should call. Lets a human confirm both `jewelry_type` and traditional/modern styling *before* a job (and its fixed `service`) is created. Does not create a job, does not touch Redis job state or Sheets, is not billable, and does not count against `DAILY_GENERATION_CAP`.

**Auth:** client key
**Content-Type:** `multipart/form-data`

| Part | Type | Required | Notes |
|------|------|----------|-------|
| `image` | file | yes | Same validation as `POST /generate`: JPEG/PNG/WebP, ≤ `MAX_IMAGE_BYTES`, ≥ 256×256 |

**200 OK**
```json
{
  "is_jewelry": true,
  "jewelry_type_predictions": [
    { "jewelry_type": "NECKLACE", "confidence": 0.92 },
    { "jewelry_type": "BRACELET", "confidence": 0.05 },
    { "jewelry_type": "BANGLE", "confidence": 0.03 }
  ],
  "style_predictions": [
    { "style": "TRADITIONAL", "confidence": 0.88 },
    { "style": "MODERN", "confidence": 0.12 }
  ]
}
```

Once the human confirms a `jewelry_type` and a style (TRADITIONAL/MODERN) in the UI, the UI combines the confirmed style with the category it already knows (FEMALE_MODEL/MALE_MODEL/MANNEQUIN/PRODUCT_STYLING) to build the real `service` value, then calls `POST /generate` with that `service` and the confirmed `jewelry_type` explicit (R11: a client-supplied type is trusted, skipping re-classification and its cost).

**Note:** unlike every other AI-backed route in this system, this one responds synchronously (~7-10s observed) rather than job+poll — see `app/api/v1/classify.py`'s module docstring for why that's a deliberate, narrowly-scoped exception to the async convention.

**Errors:** 400 `INVALID_IMAGE` · 401 `UNAUTHORIZED` · 413 `IMAGE_TOO_LARGE` · 415 `UNSUPPORTED_FORMAT` · 422 `VALIDATION_ERROR` · 429 `RATE_LIMITED` · 502 `CLASSIFIER_ERROR`

---

## Jobs

### `GET /api/v1/jobs/{job_id}`
Poll job status. **This is the hot path** — served entirely from Redis, never Sheets.

**Auth:** client key. Must own the job, else 404.

**200 OK**
```json
{
  "job_id": "e2b1c9a4-...",
  "status": "generating",
  "service": "MODEL_SHOT",
  "jewelry_type": "ANKLET",
  "type_source": "CLASSIFIED",
  "confidence": 0.94,
  "mock": false,
  "created_at": "2026-07-25T10:14:03Z",
  "updated_at": "2026-07-25T10:15:41Z",
  "completed_at": null,
  "deadline_at": "2026-07-25T10:29:03Z",
  "assets": [],
  "candidate_types": null,
  "error": null
}
```

On `succeeded`, `assets` is an ordered list:
```json
"assets": [
  { "index": 0, "url": "/api/v1/jobs/e2b1c9a4-.../assets/0", "mime": "image/png" }
]
```

On `needs_input`, `candidate_types` is populated and `error.code` is `LOW_CONFIDENCE`:
```json
"candidate_types": [
  { "jewelry_type": "ANKLET", "confidence": 0.52 },
  { "jewelry_type": "BRACELET", "confidence": 0.41 }
]
```

On `failed` / `needs_review`, `error` carries `code` and `message`.

**Recommended poll interval: 5s.** Do not poll faster; it counts against the rate limit.

**Errors:** 401 · 404 `NOT_FOUND` (unknown *or* not yours) · 410 `GONE` (older than 48h — the record lives in the Sheets `JobLog`)

### `GET /api/v1/jobs`
Recent jobs for the calling key. Backs the showcase page and debugging.

**Auth:** client key
**Query:** `limit` (default 20, max 100), `status` (optional filter)

**200 OK** — `{ "jobs": [ ...same shape as single job... ], "count": 12 }`

Reads `jobs:recent`; capped at 500 entries and 48h of history by design.

### `GET /api/v1/jobs/{job_id}/assets/{index}`
Fetch a generated asset. **The only supported way to retrieve output.**

**Auth:** client key. Must own the job.

Resolves the `storage_ref` at position `index` through the storage adapter and either streams the bytes or issues a 302 to a short-lived signed URL. **No raw Drive URL is ever exposed** — this is what keeps the storage backend swappable.

**200 OK** — image bytes, correct `Content-Type`, `Cache-Control: private, max-age=3600`
**Errors:** 401 · 404 (unknown job, not yours, index out of range, or job not `succeeded`) · 410 · 502 `STORAGE_ERROR`

### `POST /api/v1/jobs/{job_id}/resolve`
Supply the jewellery type for a job parked in `needs_input`, resuming it.

**Auth:** client key. Must own the job.
**Body:** `{ "jewelry_type": "ANKLET" }`

Sets `jewelry_type_final`, `type_source: RESOLVED`, transitions `needs_input → resolving`, and re-enqueues from the resolve stage. Classification is **not** re-run and the client is **not** charged twice for it.

**200 OK** — the updated job object
**Errors:** 401 · 404 · 409 `VALIDATION_ERROR` (job is not in `needs_input`) · 422 (unknown type)

---

## Matrix

### `GET /api/v1/matrix`
List available Type × Service combinations. The showcase UI uses this to populate its dropdowns instead of hardcoding — so a client sheet edit is reflected without a code change.

**Auth:** client key

**200 OK**
```json
{
  "matrix_version": "a3f9c1...",
  "cached_at": "2026-07-25T10:10:00Z",
  "combinations": [
    { "jewelry_type": "ANKLET", "service": "MODEL_SHOT" }
  ],
  "jewelry_types": ["ANKLET", "BRACELET"],
  "services": ["MODEL_SHOT", "CATALOG_WHITE"]
}
```

Returns only `active` rows. **Never returns prompt text** — prompts are the client's IP and are not exposed through the client API.

---

## Admin

> **Phase 1 status:** both routes are real, typed, admin-gated endpoints — built in Phase 1 Step 6 solely so the frozen contract has all ten routes (`app/api/v1/admin.py`). `matrix/refresh` is a stub returning the fixed worker-internal `current_matrix_version()` with `rows_loaded: 0, changed: false` (nothing to force-reload yet — Phase 3 replaces this with a real Sheets re-read). `admin/jobs/{id}` returns the full real job record from Redis; already not ownership-scoped as designed.

### `POST /api/v1/admin/matrix/refresh`
Force an immediate re-read of the `PromptMatrix` tab, bypassing the TTL. Give this to the client's operator so a prompt edit can be made live on demand.

**Auth:** admin key
**200 OK** — `{ "matrix_version": "...", "rows_loaded": 48, "changed": true }`

### `GET /api/v1/admin/jobs/{job_id}`
Full job record including `prompt_snapshot`, `provider_job_id`, and `submission_token`. For debugging and for resolving `needs_review` jobs. Not ownership-scoped.

**Auth:** admin key

### `POST /api/v1/admin/keys/reload`

**Phase 7.** Re-reads `API_KEYS` from the process environment and atomically swaps the in-memory client-key hash map `require_client_key` looks up on every request — so a client key can be added or revoked without restarting the API process. The deploy platform must update the `API_KEYS` env var and then call this route; the app does not poll for env changes on its own.

Scoped to `API_KEYS` only — does **not** rotate `ADMIN_API_KEY`, `GOOGLE_SERVICE_ACCOUNT_JSON`, `GEMINI_API_KEY`, or `HIGGSFIELD_API_KEY` (those need a real process restart; see `docs/schema.md` §5's admin-key review for why the admin key specifically isn't hashed/rotatable the same way). API-process-only — the ARQ worker never checks client keys, so it has nothing to reload.

**Auth:** admin key
**200 OK** — `{ "keys_loaded": 2 }`
**Errors:** 401 · 422 `VALIDATION_ERROR` (`API_KEYS` unset in the process environment)

---

## Health

### `GET /health`
Liveness. **No auth.** Returns 200 `{"status":"ok"}` if the process is up. Never touches a dependency — this is what the platform's healthcheck hits.

### `GET /health/deep`
Readiness. **Admin key.** Checks Redis ping, Sheets read, storage-adapter reachability, queue depth, and today's spend against the daily cap.

> **Phase 8 status:** all four subsystem checks are real. `redis` — `PING` with latency. `sheets` — reuses the cached matrix-version mechanism (`app/services/matrix.py`), so a cache hit costs nothing and a cache miss is exactly the "is Sheets reachable" question this check answers. `storage` — a reachability probe through whichever `StorageAdapter` `STORAGE_BACKEND` currently resolves to (calls `exists()` with a sentinel ref that never exists — no file is ever uploaded/downloaded by this check). `queue` — real ARQ queue depth via a direct read of the `arq:queue` sorted set (`ZCARD`/`ZRANGE`), no ARQ-internal API needed. `spend` is not a pass/fail check — it surfaces `app/services/budget.py`'s existing `spend:{date}` counter against `DAILY_GENERATION_CAP` so an operator notices "180/200" before clients start seeing `429 BUDGET_EXCEEDED`. Only a Redis failure returns 503; a Sheets/storage blip degrades the response to `"degraded"` at 200 instead — a blip in either shouldn't make the platform's health-check restart an otherwise-fine process.

```json
{
  "status": "degraded",
  "checks": {
    "redis": {"ok": true, "latency_ms": 2},
    "sheets": {"ok": true, "matrix_version": "047b0bc2a85cb9e7"},
    "storage": {"ok": false, "backend": "supabase", "error": "quota exceeded"},
    "queue": {"ok": true, "depth": 3, "oldest_job_age_s": 12},
    "spend": {"today": 37, "cap": 200, "remaining": 163}
  }
}
```

200 when `ok`/`degraded`, 503 when the Redis check fails.

---

## Route Summary

| Method | Path | Auth | Purpose |
|--------|------|------|---------|
| POST | `/api/v1/generate` | client | Submit a job |
| POST | `/api/v1/classify-preview` | client | Showcase-UI-only: preview jewelry_type + style before submitting |
| GET | `/api/v1/jobs` | client | List recent jobs |
| GET | `/api/v1/jobs/{id}` | client | Poll status |
| GET | `/api/v1/jobs/{id}/assets/{index}` | client | Fetch output |
| POST | `/api/v1/jobs/{id}/resolve` | client | Resume a `needs_input` job |
| GET | `/api/v1/matrix` | client | Available combinations |
| POST | `/api/v1/admin/matrix/refresh` | admin | Force matrix reload |
| GET | `/api/v1/admin/jobs/{id}` | admin | Full record |
| POST | `/api/v1/admin/keys/reload` | admin | Reload `API_KEYS` without a restart |
| GET | `/health` | none | Liveness |
| GET | `/health/deep` | admin | Readiness |

**Not in v1:** job cancellation, webhook delivery, asset deletion, re-run of an existing job. Do not add these without updating this file and the roadmap.

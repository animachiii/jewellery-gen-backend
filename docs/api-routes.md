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

---

## Health

### `GET /health`
Liveness. **No auth.** Returns 200 `{"status":"ok"}` if the process is up. Never touches a dependency — this is what the platform's healthcheck hits.

### `GET /health/deep`
Readiness. **Admin key.** Checks Redis ping, Sheets read, Drive reachability, queue depth.

> **Phase 1 status:** Redis is checked for real (`PING`, with latency). `sheets`/`drive`/`queue` are hardcoded `{"ok": true}` stubs (Sheets and Drive have no live-reachability check wired up yet; real queue-depth introspection is Phase 8) — enriched incrementally as those subsystems come online. Returns 503 only when the Redis check fails.

```json
{
  "status": "degraded",
  "checks": {
    "redis": {"ok": true, "latency_ms": 2},
    "sheets": {"ok": true, "matrix_rows": 48},
    "drive": {"ok": false, "error": "quota exceeded"},
    "queue": {"ok": true, "depth": 3, "oldest_job_age_s": 12}
  }
}
```

200 when `ok`/`degraded`, 503 when any critical check fails.

---

## Route Summary

| Method | Path | Auth | Purpose |
|--------|------|------|---------|
| POST | `/api/v1/generate` | client | Submit a job |
| GET | `/api/v1/jobs` | client | List recent jobs |
| GET | `/api/v1/jobs/{id}` | client | Poll status |
| GET | `/api/v1/jobs/{id}/assets/{index}` | client | Fetch output |
| POST | `/api/v1/jobs/{id}/resolve` | client | Resume a `needs_input` job |
| GET | `/api/v1/matrix` | client | Available combinations |
| POST | `/api/v1/admin/matrix/refresh` | admin | Force matrix reload |
| GET | `/api/v1/admin/jobs/{id}` | admin | Full record |
| GET | `/health` | none | Liveness |
| GET | `/health/deep` | admin | Readiness |

**Not in v1:** job cancellation, webhook delivery, asset deletion, re-run of an existing job. Do not add these without updating this file and the roadmap.

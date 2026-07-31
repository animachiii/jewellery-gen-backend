# Business Rules

Rules that must never be broken, and the calculations behind them. When implementation and this file disagree, one of them is a bug — resolve it, don't work around it.

---

## 1. Money Rules

Generation costs real money per call. These rules exist to stop the system spending it twice or by accident.

### R1 — A paid submit is never automatically retried
The provider submit stage runs with `max_tries=1`. If a worker dies or the outcome is unknown, the job moves to `needs_review` with `error_code=ORPHANED_SUBMIT` and waits for a human. One manual check is cheaper than one silent double-charge.

Free stages — classify, poll, download, store — retry with exponential backoff (3 attempts, 2s/8s/30s).

### R2 — `submission_token` is written before the provider is called
Order is fixed and non-negotiable:

1. Generate `submission_token` (uuid4)
2. Write it to Redis with `status=submitting`
3. **Then** call the provider, passing the token as idempotency key / metadata if supported
4. On response, write `provider_job_id`

A worker that boots and finds a job in `submitting` must **never** resubmit. It either looks the job up by `submission_token` (if the provider supports it) or parks it in `needs_review`.

### R3 — Identical requests are deduplicated for 24 hours
```
content_hash = sha256(
    image_bytes
    + b"|" + service.encode()
    + b"|" + (jewelry_type or "AUTO").encode()
)
```
On submit, look up `dedupe:{content_hash}`. If it points at a `succeeded` job, return that `job_id` with `deduplicated: true`, `billable: false`, and generate nothing. Only `succeeded` jobs populate the dedupe key — failures must be retryable.

**Mock jobs are excluded from dedupe entirely.** Note the hash above deliberately does *not* include `mock` — so a mock job and a real request for the same image/service/type collide on one key. Dedupe exists to avoid paying twice; a mock job costs nothing and produces a `FakeProvider` placeholder, so it must never satisfy a real request. Enforced in two places:

- `app/services/dedupe.py`'s `record_dedupe(..., mock=...)` refuses to write a mock job's key.
- `app/api/v1/generate.py` refuses any dedupe hit whose job's `mock` differs from the request's, so a key written before that guard existed still can't leak.

Consequence: mock submits never dedupe against anything and always run fresh through the full state machine, which is what R6 wants anyway.

> Regression origin: found in manual showcase-UI testing — unchecking `mock` kept returning the previous mock run's beige placeholder in ~10s instead of a real generation, because the real request hit the mock job's dedupe key.

### R4 — `Idempotency-Key` is honoured for 24 hours
Scoped per API key. A replay returns the original `job_id` unchanged, even if the job failed. This is distinct from R3: R3 dedupes by *content*, R4 dedupes by *client intent*. Check R4 first.

### R5 — Daily generation cap
`spend:{YYYY-MM-DD}` increments on every **billable** submit. At `DAILY_GENERATION_CAP`, new submits are rejected with 429 `BUDGET_EXCEEDED` until UTC midnight. Mock and deduplicated jobs are not billable and do not increment.

### R6 — Mock jobs never touch the provider
`mock=true` routes to `FakeProvider`: ~10s simulated latency, a placeholder asset, full state machine traversal. It must never call Higgsfield, never increment spend, and must be visibly flagged `mock: true` in every response.

A mock job's result must also never reach a real request via the dedupe path — see R3's "Mock jobs are excluded from dedupe entirely".

**`PROVIDER=fake` is a second, deployment-level mock switch.** `get_provider()` resolves `FakeProvider` when *either* the per-job `mock` flag is true **or** `PROVIDER=fake` — so with `PROVIDER=fake` set, unchecking `mock` on a request still yields a placeholder. That's intended (it's the safety default while no real provider credential exists), but it is a distinct lever from the per-job flag and the two are easy to confuse when debugging "why is my real job returning a placeholder".

---

## 2. Correctness Rules

### R7 — Prompts are used verbatim
The `prompt` column is copied to `prompt_snapshot` and sent to the provider **unmodified**. No templating, no LLM rewriting, no appending "high quality, 4k", no whitespace normalisation. Prompt authorship belongs to the client; silently altering it makes their sheet edits untestable.

### R8 — The matrix snapshot is immutable
Once `prompt_snapshot`, `reference_url_snapshot`, and `matrix_version` are written at the resolve stage, they are never updated — including on retry. A retried job re-uses its snapshot rather than re-reading the sheet, so a mid-flight client edit cannot produce a job whose output doesn't match its record.

### R9 — A matrix miss fails; it never falls back
No active row for `(jewelry_type, service)` → `failed` with `MATRIX_MISS`. Never substitute a similar type, a default prompt, or another service's row. A missing combination is a content gap for the client to fill, and a wrong-but-plausible image is worse than an error.

### R10 — Low confidence parks the job; it never guesses
```
confidence >= CLASSIFIER_CONFIDENCE_THRESHOLD (0.75)  → proceed
confidence <  threshold                               → needs_input
is_jewelry == false                                   → failed / NOT_JEWELRY
```
`needs_input` returns the top 3 candidates with scores. The client resolves via `POST /jobs/{id}/resolve`; classification is not re-run and not re-charged.

### R11 — A client-supplied `jewelry_type` is trusted absolutely
If `jewelry_type` is present on submit, the classifier is skipped entirely — no verification call, no override. `type_source=PROVIDED`. The ERP knows its own catalogue better than a vision model does, and skipping the call is faster and cheaper.

### R12 — Every job has a deadline and is reapable
`deadline_at = created_at + JOB_DEADLINE_SECONDS` (900s), set at creation, never null. The sweeper runs every 60s, finds non-terminal jobs past their deadline, and terminates them:

- past deadline in `submitting` → `needs_review` / `ORPHANED_SUBMIT` (money may have moved)
- past deadline in any other non-terminal state → `failed` / `PROVIDER_TIMEOUT`

Without this, one dead worker leaves the client polling a ghost forever.

---

## 3. Data Rules

### R13 — Redis is the read path; Sheets is the durable log
No request handler may read from Google Sheets to serve job status. Sheets receives exactly two writes per job: append at creation, single update at terminal state. Intermediate transitions are Redis-only.

### R14 — A Sheets write failure never fails the job
Sheets is the log, not the source of truth. If a write fails, log `SHEETS_ERROR`, increment a metric, retry in the background — but the job proceeds and the API keeps serving from Redis. Never surface a Sheets outage to the client as a job failure.

### R15 — Job ownership is enforced on every access
Every job read compares the caller's `api_key_name` against the job's. A mismatch returns **404, not 403** — do not confirm that another client's `job_id` exists.

### R16 — 48-hour Redis window, permanent Sheets record
`job:{job_id}` expires after 48h. A request for an expired job returns **410 Gone**, not 404 — the job existed and its record is in the `JobLog` tab. These are different facts and the client may need to act on them differently.

### R17 — Storage references are opaque
Jobs store `storage_ref` (a Drive file ID today), never a URL. Assets are served only through `/api/v1/jobs/{id}/assets/{index}`. This is what makes the Drive → object-storage migration a non-event.

---

## 4. Input Rules

### R18 — Upload constraints
- `multipart/form-data` only. A JSON body with base64 → 415.
- Max `MAX_IMAGE_BYTES` (15 MB), enforced **streaming** — reject on threshold, don't buffer the whole file first.
- JPEG, PNG, WebP only, validated by **magic bytes**, not by `Content-Type` or file extension.
- Dimensions must be ≥ 256×256.

### R19 — v2 services are rejected explicitly
`REMOVE_BG`, `CHANGE_BG`, `MIX_PIECES` → 422 with a message naming them as not-yet-supported. Do not silently ignore or fall back to a v1 service.

### R20 — Rate limit
**Resolved in Phase 7** with two separate fixed-window buckets per API key, each its own Redis counter (`ratelimit:{bucket}:{key}:{minute}`) so one never eats into the other's budget:

- `RATE_LIMIT_PER_MINUTE` (60) — every `/api/v1` route **except** the single-job poll below. This includes `POST /generate` — previously a real gap: the route had no rate-limit dependency wired in at all (fixed in Phase 7; `app/api/v1/generate.py` now depends on `rate_limit` like every other client route).
- `POLLING_RATE_LIMIT_PER_MINUTE` (180) — `GET /jobs/{job_id}` only, the documented hot, cheap, read-only path. 10 concurrent jobs polling at the recommended 5s interval is 120/min; 180 clears that with headroom for a few extra in-flight jobs, rather than leaving the tension undecided.

`GET /jobs` (the list route), `GET /jobs/{id}/assets/{index}`, and `POST /jobs/{id}/resolve` stay on the default 60/min bucket — none of them are the repeated-polling hot path the way single-job status checks are.

### R21 — CORS fails closed
`CORS_ALLOWED_ORIGINS` (Phase 7) defaults to empty — no cross-origin access at all. A cross-origin client (a real Flutter web build, a separately-hosted showcase page) must be added to the list explicitly; there is no implicit same-origin assumption. `allow_credentials` is always `false` — auth is the `X-API-Key` header, not a cookie, so there is nothing for credentialed CORS to protect, and enabling it would only widen the attack surface for no benefit. Methods/headers exposed to cross-origin callers are the minimal set every route actually uses (`GET`/`POST`; `X-API-Key`, `Idempotency-Key`, `Content-Type`) — never wildcarded.

---

## 5. Calculations

**Content hash** — see R3.

**Matrix version** — `sha256` of all active matrix rows serialised in sorted key order, truncated to 16 hex chars. Changes when any active row changes; used to detect whether a refresh actually altered anything.

**Deadline** — `created_at + 900s`.

**Duration** — `completed_at - created_at`, seconds, written to `JobLog` column J.

**Daily spend** — `INCR spend:{UTC date}` on billable submit only, with a 48h TTL. Billable = `not mock and not deduplicated`.

**Backoff** — free stages: attempt 1 immediate, then 2s, 8s, 30s. Then fail with the stage's retryable error code.

---

## 6. Deferred to v2

Do not build these without an explicit decision:

- Webhook delivery (`callback_url` is stored but never called)
- Job cancellation
- Re-running an existing job with a new prompt
- Per-client prompt overrides
- Asset deletion / retention policy
- Multi-image input (`MIX_PIECES`)
- Migration of the matrix from Sheets to Supabase

---

## Matrix Coverage (as of Phase 0a, 2026-07-26)

> ⚠️ **Snapshot of a work-in-progress sheet.** The client is still actively building out `Sheet1` — this grid, the two data-quality issues below, and the "zero variants" gaps are a point-in-time read, not a finished deliverable. Re-run `python scripts/validate_matrix.py` before Phase 3 (matrix reading) and again before Phase 6 (client benchmark/handover) rather than trusting this table as final.

> **Phase 3 update:** `app/services/matrix.py` now reads `Sheet1` for real (via `SheetsClient.read_all_rows`, parsed by the shared `app/services/matrix_parser.py`, cached in Redis with a TTL and version hash — see `docs/schema.md` §3 and `docs/business-rules.md` §5), replacing the Phase 1 stub. This build sandbox has no network access, so the table below is still the Phase 0a snapshot, not a live re-read. **Re-running `python scripts/validate_matrix.py` against the current live sheet to refresh this snapshot is a pending manual step for the project owner** — do this before trusting the coverage grid below, and again before Phase 6.

Produced by `python scripts/validate_matrix.py` against the client's real `Sheet1`. `x` = at least one prompt variant exists; `.` = `MATRIX_MISS` if requested. Blank combinations below are expected, not bugs — the client has not authored prompts for them yet.

```
jewelry_type  FEMALE_MODEL_MODERN  FEMALE_MODEL_TRADITIONAL  MALE_MODEL_MODERN  MANNEQUIN_MODERN  PRODUCT_STYLING_MODERN  PRODUCT_STYLING_TRADITIONAL
ANKLET        x                    x                         .                  .                  .                        .
BANGLE        x                    x                         .                  .                  x                        x
BRACELET      x                    .                         x                  .                  .                        .
EARRING       x                    x                         .                  .                  x                        .
HIPBELT       .                    x                         .                  .                  x                        x
NECKLACE      x                    x                         .                  x                  x                        .
RING          x                    x                         .                  .                  x                        .
```

`MALE_MODEL_TRADITIONAL` and `MANNEQUIN_TRADITIONAL` currently have **zero** variants for every jewelry type — any job requesting either fails `MATRIX_MISS` until the client fills them in. `MANNEQUIN_MODERN` is populated for `NECKLACE` only, matching the sheet's own "only in necklace" note.

**Two data-quality issues found in the live sheet, flagged for the client, not silently worked around:**

- **Row 16, `BRACELET` × `MALE_MODEL_MODERN`**: the cell contains only a Google Drive URL with no prompt text. This variant will resolve to a reference image but an empty `prompt_snapshot` — likely unusable until the client fills it in.
- **Row 23, `BANGLE` × `PRODUCT_STYLING_TRADITIONAL`**: the cell has prompt text but no trailing Drive URL — `reference_url_snapshot` cannot be resolved for this variant.

Re-run `scripts/validate_matrix.py` after the client edits the sheet to confirm both are fixed before Phase 3 relies on them.

# Phase 1 — API Contract & Mock Pipeline

## Objective
Deliver the complete, frozen HTTP contract plus an end-to-end mock pipeline: a job submitted with `mock=true` traverses the real state machine through ARQ, produces a placeholder asset, and is retrievable through the real asset route. No Gemini, no Higgsfield, no Drive — those arrive in Phases 2–4 behind adapters this phase defines.

## Context
Phase 0a produced the scaffold, config, Docker Compose, logging, and enums. Phase 0b produced the `Job` model, state machine, Redis store, Sheets write-behind log, dedupe/idempotency helpers, rehydration, and the sweeper cron. The worker runs but has no job task registered.

This phase replaces the last three unbuilt seams with stubs so the pipeline can run end to end:

- **Classifier** → `StubClassifier` returning a fixed high-confidence prediction (real Gemini: Phase 3)
- **Matrix** → `StubMatrix` serving fixture rows from `tests/fixtures/matrix.json` (real Sheets: Phase 3)
- **Storage** → `LocalStorage` writing to a mounted volume (real Drive: Phase 2)

**Read first:** `docs/api-routes.md` in full, `docs/business-rules.md` R3–R6 and R18–R20, `docs/conventions.md` → Error Handling and API Design.

> **The contract freezes at the end of this phase.** After sign-off, adding optional fields is fine; renaming, removing, or retyping anything requires `/api/v2`.

---

## Step 1 — Auth, Rate Limiting & Error Envelope

### What to do
Implement `app/api/errors.py`: the `AppError` base class from `docs/conventions.md` → Error Handling, subclasses carrying an `ErrorCode` and HTTP status, plus handlers registered on the app that render **every** error as the envelope in `docs/api-routes.md` → Error Envelope. Include handlers for `AppError`, FastAPI's `RequestValidationError` (→ 422 `VALIDATION_ERROR`), and bare `Exception` (→ 500 `INTERNAL_ERROR`, generic message, full detail to logs only).

`request_id` comes from the Phase 0a middleware. `job_id` is included when the error concerns a specific job.

Implement `app/api/deps.py`:
- `require_client_key` — reads `X-API-Key`, hashes it, looks it up in `settings`, returns the `api_key_name`. Missing or unknown → 401 `UNAUTHORIZED`. Constant-time comparison.
- `require_admin_key` — same against `ADMIN_API_KEY`.
- `rate_limit` — `INCR ratelimit:{key_name}:{minute}` with a 120s TTL; over `RATE_LIMIT_PER_MINUTE` → 429 `RATE_LIMITED` (R20).
- `load_owned_job(job_id, key_name)` — fetches the job, returns 404 if missing **or owned by another key** (R15 — never 403), and 410 `GONE` if the Sheets row exists but the Redis key has expired (R16).

### Checkpoint 1
- [ ] A request with no `X-API-Key` returns 401 with body `{"error":{"code":"UNAUTHORIZED",...}}` including a `request_id`
- [ ] A valid key for client `a` fetching a job owned by `b` returns **404**, and the response body is byte-identical to a genuinely unknown `job_id`
- [ ] `RATE_LIMIT_PER_MINUTE=2`: the third request within the same minute returns 429 `RATE_LIMITED`, and the counter resets the following minute
- [ ] An unhandled exception raised in a route returns 500 `INTERNAL_ERROR` with no stack trace or internal detail in the body, while the full traceback appears in the logs
- [ ] `/health` is reachable with no key; every `/api/v1/*` route rejects an absent key

---

## Step 2 — Request Schemas & Upload Validation

### What to do
Implement `app/models/schemas.py` with explicit Pydantic models for every request and response in `docs/api-routes.md`: `GenerateResponse`, `JobResponse`, `JobListResponse`, `AssetRef`, `CandidateType`, `ResolveRequest`, `MatrixResponse`, `ErrorEnvelope`. Every route declares `response_model` — this is what generates the OpenAPI spec, a handover deliverable.

Implement upload validation in `app/api/v1/generate.py` per R18:
- Accept `multipart/form-data` only. A JSON body → 415 `UNSUPPORTED_FORMAT` with a message naming multipart (R18 — the ERP will try base64 first; the error must tell them why).
- Enforce `MAX_IMAGE_BYTES` **while streaming**. Read in chunks and abort at the threshold — do not buffer the file and then check its size.
- Validate format by **magic bytes** via Pillow, not by `Content-Type` or filename. JPEG/PNG/WebP only.
- Reject images smaller than 256×256.
- `service` must be in `V1_SERVICES`. A `V2_SERVICES` value → 422 naming it explicitly as not yet supported (R19). An unknown value → 422 listing the valid ones.

### Checkpoint 2
- [ ] Posting `application/json` with a base64 field returns 415, and the message mentions `multipart/form-data`
- [ ] A 20 MB upload returns 413 `IMAGE_TOO_LARGE`, and process memory does not grow by 20 MB during the request (verify the streaming abort)
- [ ] A `.jpg`-named file whose bytes are actually a PDF returns 415 `UNSUPPORTED_FORMAT`
- [ ] A valid 100×100 PNG returns 422; a valid 512×512 PNG is accepted
- [ ] `service=REMOVE_BG` returns 422 with a message naming it as a v2 feature; `service=BANANA` returns 422 listing the four valid v1 services

---

## Step 3 — Submit Path

### What to do
Implement `POST /api/v1/generate` per `docs/api-routes.md`, in this exact order — the ordering is the money-safety logic (R3, R4, R5):

1. Auth + rate limit
2. Validate upload and form fields (Step 2)
3. **Idempotency check first** (R4): if `Idempotency-Key` is present and `idem:{key_name}:{value}` exists, return the original job immediately — including if it failed
4. Read image bytes, compute `content_hash` (R3)
5. **Dedupe check** (R3): if `dedupe:{hash}` points at a `succeeded` job, return it with `deduplicated: true`, `billable: false`. Generate nothing
6. **Budget check** (R5): if `spend:{today}` ≥ `DAILY_GENERATION_CAP` and the job is billable, return 429 `BUDGET_EXCEEDED`
7. Persist source bytes via the storage adapter → `source_ref`
8. Create the `Job` with `status=queued`, `deadline_at = now + JOB_DEADLINE_SECONDS` (R12 — never null), `billable = not mock`
9. `append_job_row` → store the row index (Phase 0b)
10. Record idempotency key if supplied
11. Enqueue the ARQ job
12. Return 202

Note the sequencing: dedupe and idempotency are checked **before** anything is persisted or enqueued, and the budget check happens before the job is created — a rejected submit leaves no trace.

### Checkpoint 3
- [ ] A valid submit returns 202 with `job_id`, `status: "queued"`, `poll_url`, and `deduplicated: false`; `GET` on the returned `poll_url` resolves
- [ ] Submitting the identical image + service twice after the first succeeds returns the **same** `job_id` with `deduplicated: true`, and no second ARQ job is enqueued
- [ ] Submitting the same image after the first job **failed** creates a new job (failures are not dedupe-recorded, R3)
- [ ] Replaying the same `Idempotency-Key` returns the original `job_id` even when that job failed, and does not enqueue again
- [ ] With `DAILY_GENERATION_CAP=1`, the second billable submit returns 429 `BUDGET_EXCEEDED`, while a `mock=true` submit still succeeds and does not increment `spend:{today}` (R6)
- [ ] Every created job has a non-null `deadline_at` exactly `JOB_DEADLINE_SECONDS` after `created_at`

---

## Step 4 — Read Routes

### What to do
Implement per `docs/api-routes.md`:

- `GET /api/v1/jobs/{job_id}` — the hot path. Served **entirely from Redis**; add a test asserting the Sheets client is never called (R13). Shape varies by status: `assets` populated on `succeeded`, `candidate_types` on `needs_input`, `error` on `failed`/`needs_review`.
- `GET /api/v1/jobs` — `limit` (default 20, max 100), optional `status` filter, owner-scoped.
- `GET /api/v1/jobs/{job_id}/assets/{index}` — resolve `asset_refs[index]` through the storage adapter and stream the bytes with the correct `Content-Type` and `Cache-Control: private, max-age=3600`. 404 for an out-of-range index or a job not in `succeeded`.
- `POST /api/v1/jobs/{job_id}/resolve` — 409 unless the job is in `needs_input`; sets `jewelry_type_final`, `type_source=RESOLVED`, transitions `needs_input → resolving`, and re-enqueues **from the resolve stage** so classification is not re-run (R10).
- `GET /api/v1/matrix` — combinations from the matrix source. **Must not return prompt text** (client IP).

### Checkpoint 4
- [ ] Polling a job returns exactly the field set documented in `docs/api-routes.md`, with `assets: []` while running and populated on success
- [ ] A test asserts zero Sheets client calls across 50 consecutive polls (R13)
- [ ] `GET /jobs?limit=5` returns at most 5, newest first, none owned by another key; `?status=succeeded` filters correctly
- [ ] Fetching `/assets/0` on a succeeded mock job returns image bytes with the right `Content-Type`; `/assets/9` returns 404; fetching assets on a `queued` job returns 404
- [ ] `POST /resolve` on a `queued` job returns 409; on a `needs_input` job it returns the updated job in `resolving` and re-enqueues without a second classification call
- [ ] `GET /matrix` returns combinations, `jewelry_types`, `services`, and `matrix_version`, and the response contains **no** prompt text

---

## Step 5 — Storage & Provider Adapters

### What to do
Define both seams now, so Phases 2 and 4 are drop-ins.

`app/storage/base.py` — `StorageAdapter` protocol: `put(data: bytes, filename: str, mime: str) -> str` (returns `storage_ref`), `get(ref) -> tuple[bytes, str]`, `exists(ref) -> bool`.
`app/storage/local.py` — `LocalStorage` writing to a mounted volume, `storage_ref` = an opaque UUID key, **not** a path. A ref that leaks filesystem structure defeats the abstraction.

`app/providers/base.py` — the `GenerationProvider` protocol and the `GenerationRequest` / `ProviderSubmission` / `ProviderStatus` / `ProviderAsset` dataclasses exactly as specified in `docs/ai-integration.md` §2.
`app/providers/fake.py` — `FakeProvider` per `docs/ai-integration.md` §3: ~10s simulated latency (configurable to ~0 for tests), a deterministic generated placeholder image, and `FAKE_FAIL_MODE=submit|poll|timeout|none` failure injection.

Both resolved through factories reading `settings`. Enforce the boundary: **no module outside `app/providers/` imports a concrete provider; none outside `app/storage/` imports a concrete adapter** (`docs/conventions.md` → Adapters).

### Checkpoint 5
- [ ] `LocalStorage.put` → `get` round-trips bytes and mime; the returned `storage_ref` contains no path separators and no filename
- [ ] `FakeProvider` implements every method of the protocol; a `mypy --strict` run confirms it satisfies `GenerationProvider`
- [ ] `FAKE_FAIL_MODE=submit` produces a submit failure; `=poll` produces a `failed` poll state; `=timeout` never reaches a terminal provider state
- [ ] `grep -rn "higgsfield\|drive" app/ --include="*.py"` matches only inside `app/providers/` and `app/storage/`
- [ ] Switching `PROVIDER=fake` vs `PROVIDER=higgsfield` changes the resolved provider with no other code change (the Higgsfield class may be a `NotImplementedError` stub in this phase)

---

## Step 6 — Mock Pipeline End-to-End

### What to do
Implement `app/worker/tasks.py` as the **staged** pipeline from `docs/business-rules.md` R1/R2. Each stage commits state via `transition()` before the next begins; no stage boundary lives in memory.

```
classify → resolve → submit → poll → store → finalize
```

- `classify` — skipped when `jewelry_type_requested` is set (R11). Otherwise calls the classifier port; `StubClassifier` returns a fixed high-confidence result for now. Wire the threshold branch to `needs_input` (R10) so the path is real even though the stub never triggers it.
- `resolve` — looks up the matrix port; writes `prompt_snapshot`, `reference_url_snapshot`, `matrix_version` **once, immutably** (R8). A miss → `failed` / `MATRIX_MISS` (R9).
- `submit` — **`max_tries=1`** (R1). Write `submission_token` to Redis **before** calling the provider (R2). Register with ARQ so this stage never auto-retries.
- `poll` — retries freely (2s/8s/30s), respects `deadline_at`.
- `store` — downloads assets, pushes through the storage adapter, records `asset_refs` in index order.
- `finalize` — `succeeded`, records the dedupe key (R3, successes only), triggers the single Sheets terminal update.

Register the pipeline and the Phase 0b sweeper cron in `WorkerSettings`, with `max_jobs = WORKER_CONCURRENCY`.

Finally: export the OpenAPI spec to `docs/openapi.json` via a make target or script, and freeze the contract.

### Checkpoint 6
- [ ] A `mock=true` submit reaches `succeeded` within ~15s, and the logged status sequence is exactly `queued → classifying → resolving → submitting → generating → storing → succeeded`
- [ ] The resulting asset is fetchable via `/assets/0` and is a valid image
- [ ] The whole mock run produces **exactly two** Sheets writes and does not increment `spend:{today}` (R6)
- [ ] Killing the worker mid-`generating` and restarting it: the job resumes polling and completes, without a second provider submit (inspect `provider_job_id` — unchanged)
- [ ] With `FAKE_FAIL_MODE=submit`, the job lands in a terminal state and ARQ records **one** attempt of the submit stage — never two (R1)
- [ ] A job whose `deadline_at` passes while `generating` is terminated by the sweeper as `failed` / `PROVIDER_TIMEOUT`
- [ ] `prompt_snapshot` is unchanged after a retried `poll` stage (R8)
- [ ] `docs/openapi.json` is generated and lists all ten routes from `docs/api-routes.md` → Route Summary

---

## Self-Audit Instruction

Before declaring this phase complete, you must:

1. Re-read every checkpoint in this phase file.
2. Test each one: call the route with `curl`, inspect the Redis hash, read the worker logs for the status sequence, kill and restart containers, count fake-client calls.
3. Return a structured report:
   - ✅ [Checkpoint] — Pass
   - ⚠️ [Checkpoint] — Partial: [specific reason]
   - ❌ [Checkpoint] — Fail: [specific reason]
4. Fix all failures and partials before reporting phase complete.
5. If anything in this phase changed the schema, routes, or business rules from what's documented in `docs/`, update the relevant `docs/*.md` file now — before declaring the phase complete. `claude.md` and `docs/` must reflect reality, not the original plan.
6. Only say "Phase 1 Complete" when every checkbox is green and docs are in sync.

## Final Phase 1 Checklist
- [ ] API-key auth, ownership scoping (404 not 403), rate limiting, and a uniform error envelope on every failure path
- [ ] Multipart-only upload with streaming size enforcement and magic-byte format validation
- [ ] Submit path implementing idempotency → dedupe → budget in the specified order
- [ ] All read routes including assets, resolve, and matrix; polling never touches Sheets
- [ ] Storage and provider adapter seams defined, with `LocalStorage` and `FakeProvider` implemented and boundary-enforced
- [ ] Staged worker pipeline with `max_tries=1` on submit and pre-write of `submission_token`
- [ ] Mock job runs end to end and its asset is retrievable through the real route
- [ ] `docs/openapi.json` exported; **contract frozen and signed off**
- [ ] Self-audit passed with all green
- [ ] `docs/` updated to match what was actually built
- [ ] Manual verification done by architect

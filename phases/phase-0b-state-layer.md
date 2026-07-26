# Phase 0b — State Layer

## Objective
Build the persistence and lifecycle machinery every later phase depends on: the job state machine, the Redis live store, the Sheets write-behind log, boot-time rehydration, and the stuck-job sweeper. No HTTP routes and no external AI calls — this phase is exercised entirely through tests.

## Context
Phase 0a produced the repo, config, Docker Compose (Redis with AOF verified), structured logging, and the domain enums including `LEGAL_TRANSITIONS`. The `worker` container starts but registers no tasks. The client's real matrix has been validated and `docs/schema.md` §1 reconciled against it.

**Read first:** `docs/schema.md` §3 and §4 (Redis keyspace, job record), `docs/business-rules.md` R12–R17, `docs/conventions.md` → State Machine.

---

## Step 1 — Job Model & State Machine

### What to do
Implement `app/models/job.py` with a `Job` dataclass (or Pydantic model) containing **every field in `docs/schema.md` §4**, with `to_redis_hash()` / `from_redis_hash()` handling the string serialisation — `None` as absent keys, nested values as JSON, booleans as `"1"`/`"0"`, timestamps as ISO-8601 UTC with `Z`.

Implement `app/core/state.py`:

```python
async def transition(job: Job, to: JobStatus, *, error_code=None,
                     error_message=None, **fields) -> Job
```

It must:
- Validate the move against `LEGAL_TRANSITIONS`; raise `IllegalTransition` on anything not in the table
- Set `updated_at` on every transition; set `completed_at` when moving to a terminal status
- Require `error_code` when moving to `failed`, `needs_review`, or `needs_input`
- Apply `**fields` atomically with the status change (a single Redis `HSET`)
- Emit one log line per transition: `job.status.changed` with `job_id`, `from`, `to`, `error_code`

**No other module may assign `status` directly** (`docs/conventions.md` → State Machine).

### Checkpoint 1
- [ ] `transition(job, QUEUED → GENERATING)` raises `IllegalTransition`; `SUBMITTING → GENERATING` succeeds
- [ ] Transitioning to `failed` without an `error_code` raises; with one, it persists both code and message
- [ ] Any terminal transition sets `completed_at`; non-terminal transitions leave it `None`
- [ ] `Job.from_redis_hash(job.to_redis_hash())` round-trips every field in `docs/schema.md` §4, including `None`s, the `asset_refs` list, and `candidate_types`
- [ ] `grep -rn "\.status = " app/ --include="*.py"` returns matches only inside `core/state.py`

---

## Step 2 — Redis Store

### What to do
Implement `app/store/redis_store.py` as the only module that touches job keys. Redis client comes from the shared `lifespan`/worker connection — never constructed per call.

Functions:
- `create_job(job) -> None` — `HSET job:{id}`, set 48h TTL, `ZADD jobs:recent` scored by `created_at` epoch, then `ZREMRANGEBYRANK` to cap at 500. All in one pipeline.
- `get_job(job_id) -> Job | None`
- `update_job(job_id, **fields)` — partial `HSET`, refreshes TTL
- `list_recent(api_key_name, limit, status=None) -> list[Job]` — reads `jobs:recent` descending, filters by owner
- `set_row_index(job_id, row)` / `get_row_index(job_id)` for `job:{id}:row`
- `find_expired(now) -> list[Job]` — non-terminal jobs past `deadline_at`, for the sweeper

Also implement `app/services/dedupe.py`:
- `content_hash(image_bytes, service, jewelry_type) -> str` exactly per `docs/business-rules.md` R3
- `check_dedupe(hash) -> job_id | None` and `record_dedupe(hash, job_id)` — **only `succeeded` jobs are recorded** (R3)
- `check_idempotency(key_name, idem_key)` / `record_idempotency(...)`, 24h TTL, scoped per key (R4)

Use key names exactly as written in `docs/schema.md` §3. Do not invent variants.

### Checkpoint 2
- [ ] `create_job` then `get_job` returns an equal `Job`; `TTL job:{id}` is within a few seconds of 172800
- [ ] Creating 505 jobs leaves `ZCARD jobs:recent == 500`, and the 5 oldest are the ones evicted
- [ ] `list_recent` for key `erp` never returns a job owned by key `other`
- [ ] `content_hash` is stable across calls and changes when any of image bytes, service, or jewelry_type changes; `jewelry_type=None` and `"AUTO"` hash identically
- [ ] `record_dedupe` raises or no-ops for a non-`succeeded` job; `check_dedupe` returns `None` after the TTL expires (test with a 1s override)
- [ ] `find_expired` returns a job whose `deadline_at` is in the past and excludes terminal jobs past their deadline

---

## Step 3 — Sheets Job Log

### What to do
Implement `app/store/sheets_store.py`. The Google SDK is synchronous — every call is wrapped in `asyncio.to_thread()` (`docs/conventions.md` → Async).

Two functions only, matching the two-writes-per-job rule (R13):

- `append_job_row(job) -> int` — appends columns A–G per `docs/schema.md` §2, parses the returned `updatedRange` to extract the row index, returns it. Caller persists it via `set_row_index`.
- `update_job_row(job, row_index) -> None` — single `values.update` writing columns H–T for a terminal job.

Both acquire `lock:sheets:write` (Redis `SET NX EX 15`, with retry) so concurrent workers cannot clobber (R13).

**Failure handling is the critical part** (R14): a Sheets failure must never fail the job. Wrap both in a `safe_` layer that catches, logs `sheets.write.failed` at WARNING with `job_id`, and returns without raising. Add a `sheets_write_failures` counter for Phase 8 to expose.

Write tests against a `FakeSheetsClient` — no test may hit the real API (`docs/conventions.md` → Testing).

### Checkpoint 3
- [ ] `append_job_row` returns the correct integer row index parsed from a realistic `updatedRange` like `JobLog!A47:G47`
- [ ] `update_job_row` writes only columns H–T and leaves A–G untouched (assert on the fake's recorded range)
- [ ] Two concurrent `append_job_row` calls serialise — the fake records two sequential calls, never overlapping, and the lock is released after each
- [ ] Injecting an exception in the fake client causes a WARNING log and **no raised exception**; the caller proceeds normally
- [ ] A full job lifecycle (create → succeed) produces **exactly two** calls to the fake Sheets client

---

## Step 4 — Rehydration & Sweeper

### What to do
**Rehydration** — `app/store/rehydrate.py`, run from the worker's `on_startup`. Redis is authoritative but not immortal; if it is flushed or lost, in-flight jobs vanish while their Sheets rows say they never finished. Read the `JobLog` tab for rows with a blank terminal `status` (column H) created within the last 48h; for any whose `job:{id}` key is missing from Redis, reconstruct a minimal `Job` and place it in `needs_review` with `ORPHANED_SUBMIT`. **Never auto-resume a rehydrated job** — it may have already been paid for (R1, R2).

**Sweeper** — `app/worker/sweeper.py`, registered as an ARQ `cron` job running every 60s. ARQ has no beat process, so this registration in `WorkerSettings.cron_jobs` is the only thing that makes it run; if it is missing, nothing fails loudly and stuck jobs accumulate silently.

Per R12:
- non-terminal and past `deadline_at`, status == `submitting` → `needs_review`, `ORPHANED_SUBMIT`
- non-terminal and past `deadline_at`, any other status → `failed`, `PROVIDER_TIMEOUT`

Each termination goes through `transition()` (so the Sheets terminal write fires) and logs `job.swept` with the previous status.

### Checkpoint 4
- [ ] The sweeper is registered in `WorkerSettings.cron_jobs` and a running worker executes it within 60s (confirm via log line)
- [ ] A job in `generating` with `deadline_at` in the past becomes `failed` / `PROVIDER_TIMEOUT` on the next sweep
- [ ] A job in `submitting` past deadline becomes `needs_review` / `ORPHANED_SUBMIT` — **not** `failed`
- [ ] A `succeeded` job with a past `deadline_at` is untouched by the sweeper
- [ ] Sweeping a job triggers exactly one Sheets terminal update
- [ ] With a `JobLog` row for job X non-terminal and `job:X` absent from Redis, worker startup recreates X in `needs_review`; running rehydration twice does not duplicate or alter it

---

## Self-Audit Instruction

Before declaring this phase complete, you must:

1. Re-read every checkpoint in this phase file.
2. Test each one: run the test, inspect the Redis key with `redis-cli`, check the fake Sheets client's recorded calls, watch the worker log for the cron line.
3. Return a structured report:
   - ✅ [Checkpoint] — Pass
   - ⚠️ [Checkpoint] — Partial: [specific reason]
   - ❌ [Checkpoint] — Fail: [specific reason]
4. Fix all failures and partials before reporting phase complete.
5. If anything in this phase changed the schema, routes, or business rules from what's documented in `docs/`, update the relevant `docs/*.md` file now — before declaring the phase complete. `claude.md` and `docs/` must reflect reality, not the original plan.
6. Only say "Phase 0b Complete" when every checkbox is green and docs are in sync.

## Final Phase 0b Checklist
- [ ] `Job` model round-trips every documented field; state machine rejects every illegal transition and is the sole writer of `status`
- [ ] Redis store implements the exact keyspace in `docs/schema.md` §3, with TTLs, the capped recent index, and owner-scoped listing
- [ ] Dedupe and idempotency helpers implemented per R3/R4, recording only successful jobs
- [ ] Sheets log writes exactly twice per job, serialised by lock, and never fails a job
- [ ] Sweeper runs as an ARQ cron and terminates stuck jobs with the correct status split for `submitting`
- [ ] Rehydration recovers orphaned jobs into `needs_review` and is idempotent
- [ ] Self-audit passed with all green
- [ ] `docs/` updated to match what was actually built
- [ ] Manual verification done by architect

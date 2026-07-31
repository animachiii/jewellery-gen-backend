# Phase 8 — Observability & Ops

## Objective
Give the operator (and eventually the client's ops staff) real visibility into a system that currently fails silently in several places: `SENTRY_DSN` exists as a config field but is never initialized, `/health/deep` hardcodes `sheets`/`drive`/`queue` checks to `{"ok": true}` regardless of actual reachability, and nothing notifies anyone when a job lands in `needs_review` (a possible orphaned paid charge) or when the sweeper reaps a stuck job. This phase wires up **free-tier Sentry** for error tracking and needs_review/stuck-job alerting, makes `/health/deep` check what it claims to check, and confirms log correlation actually holds end-to-end — no paid infrastructure, no new services to run.

## Context
**Decision, made explicitly rather than defaulted into:** Sentry's free "Developer" tier (5K events/month, unlimited projects, no card) is used instead of a paid plan or a self-hosted alternative (GlitchTip). This system's error volume is inherently low — async job failures, not per-request traffic — so 5K/month has wide headroom. `sentry-sdk`'s API is what a self-hosted GlitchTip instance would also speak (same DSN-based client, protocol-compatible), so if this project ever needs to drop the Sentry-the-company dependency, it's a DSN swap, not a rewrite. Raw log storage/search is **not** Sentry's job here — `structlog` already emits structured JSON to stdout, and Railway/Render both include a searchable log viewer in their base hosting price. Sentry is specifically for **error aggregation + alerting**, not general log observability.

**Read first:** `app/config.py` (`sentry_dsn` field, currently unused — line ~126), `app/api/errors.py` (`register_error_handlers`'s unhandled-exception path, the one place every unhandled exception already funnels through), `app/main.py` (`lifespan`, `/health/deep`'s current stub checks), `app/worker/settings.py` (`on_startup`/`on_shutdown` — the ARQ worker is a **separate process** from the API and needs its own Sentry init), `app/worker/sweeper.py` (`sweep()` — where stuck jobs get reaped, the natural hook point for stuck-job alerting), `app/core/logging.py` (`bind_job`/`_job_id_var` — the existing job_id-correlation mechanism Sentry's scope should reuse, not duplicate), `docs/api-routes.md` → Health (the documented `/health/deep` response shape this phase has to actually fulfill), `docs/schema.md` §3 (Redis keyspace — note `arq:*` is "Managed by ARQ. Do not touch": this phase reads `arq:queue` for depth/oldest-age via `ZCARD`/`ZRANGE`, which is read-only introspection, not a mutation, and stays within that constraint).

**What's already solid — do not rebuild:**
- Structured JSON logging via `structlog` with automatic `job_id` injection (`app/core/logging.py`'s `_inject_job_id` + `bind_job` context manager) and per-request `request_id` (`app/main.py`'s middleware).
- `GET /health` liveness check — deliberately dependency-free, correct as-is.
- The sweeper (`app/worker/sweeper.py`) already correctly reaps stuck jobs into `failed`/`needs_review` on schedule (R12) — this phase adds *notification* of that event, not new reaping logic.
- `app/services/budget.py`'s `spend:{date}` counter already exists — this phase *exposes* it, doesn't rebuild spend tracking.

---

## Step 1 — Sentry initialization (both processes)

### What to do
- Add `sentry-sdk[fastapi]` to `pyproject.toml`'s main dependencies (not `dev` — this runs in production).
- New `app/core/observability.py`: a single `init_sentry()` function, no-op if `settings.sentry_dsn` is falsy (so local dev with no DSN configured behaves exactly as today — this must never become a hard requirement). Call it from **two** places, since API and worker are separate processes with separate Python interpreters:
  - `app/main.py`'s `lifespan`, before anything else in startup.
  - `app/worker/settings.py`'s `on_startup`.
- Configure `traces_sample_rate=0` (no APM/performance tracing — free tier's event quota is for errors, not spans; this project has no need for distributed tracing) and `send_default_pii=False` (never let Sentry's own defaults leak request bodies/headers — this is the same spirit as HR8/HR9, "never log image bytes, API keys, or full base64 payloads," applied to error reports too).
- Confirm via a manual local check (Manual Verification) that a deliberately-raised exception actually appears in the Sentry dashboard — this is the one thing that can't be proven by a unit test against a fake transport, since it requires a real Sentry project + DSN.

## Step 2 — Wire Sentry into the existing error path + job/request context

### What to do
- In `app/api/errors.py`'s `_handle_unhandled_exception`, after the existing `log.exception(...)` call, also call `sentry_sdk.capture_exception()` (or let Sentry's own FastAPI integration auto-capture it — pick whichever avoids double-reporting; verify with a test that exactly one Sentry capture happens per unhandled exception, not zero and not two).
- **Do not** report `AppError` subclasses to Sentry as exceptions — those are expected, typed, already-logged business outcomes (a 404, a 422, a `MATRIX_MISS`), not bugs. Only the true `Exception` catch-all (500 `INTERNAL_ERROR`) path reports to Sentry. Reporting every 4xx would blow through the free tier's quota on normal traffic within days and bury the signal that actually matters.
- Bind `job_id` (when present) and `request_id` onto the Sentry scope the same way `app/core/logging.py`'s `bind_job` already does for structlog — reuse the existing `_job_id_var` contextvar rather than inventing a second job-id-threading mechanism. A Sentry error report with no `job_id` is much harder to correlate back to the job log.

## Step 3 — Stuck-job / `needs_review` alerting via Sentry

### What to do
- In `app/worker/sweeper.py`'s `sweep()`, after a job is reaped into `NEEDS_REVIEW` specifically (not the `FAILED`/`PROVIDER_TIMEOUT` branch — that's a normal, self-explanatory terminal state, not something requiring human intervention), call `sentry_sdk.capture_message(...)` at `warning` level with `job_id` and `previous_status` attached. This is what "alerting" means in this phase — reusing the already-wired Sentry channel rather than standing up a second notification path (Slack webhook, email) that would cost additional setup and, per the brief, additional-service sprawl this phase is explicitly avoiding.
- Also call the same capture in `app/worker/tasks.py`'s `_submit`'s except-branch (the *other* place a job can land in `NEEDS_REVIEW`/`ORPHANED_SUBMIT` — a live submit failure, not just a stale-deadline sweep). Both paths converge on the same "a human needs to check a possible orphaned charge" event; both should alert identically.
- Do **not** alert on every `FAILED` job — that would be normal-operation noise (a real `MATRIX_MISS` or a genuine `NOT_JEWELRY` classification isn't an incident). `NEEDS_REVIEW` specifically is the signal, because it's the one status that means "money may have moved and nobody has confirmed what happened."

## Step 4 — Real `/health/deep` checks

### What to do
Replace `app/main.py`'s hardcoded `{"ok": true}` stubs with real, cheap checks. Keep the existing "only Redis failing returns 503" contract (`docs/api-routes.md`: "Returns 503 only when the Redis check fails") — the other three degrade the response to `"degraded"` at 200, they don't flip the whole service to 503, since a Sheets/Drive blip shouldn't make the platform's health-check restart a otherwise-fine process.

- **`sheets`**: a cheap read — reuse `app/services/matrix.py`'s existing cached-matrix-version mechanism (`current_matrix_version`) rather than issuing a fresh Sheets API call on every `/health/deep` hit; report `{"ok": true, "matrix_rows": N}` on success, `{"ok": false, "error": "..."}` (message only, never a stack trace or credential detail, per conventions' error-handling rules) on failure.
- **`drive`/`storage`**: a lightweight reachability probe through whichever `StorageAdapter` `STORAGE_BACKEND` currently resolves to (`app/storage/factory.py`) — for Supabase, a cheap bucket-metadata call; do not upload/download a real file on every health check (that's Phase 2's separate manual smoke test, not a per-request health check cost). If the active backend has no cheap reachability primitive, that's a real finding — document it rather than faking a check.
- **`queue`**: real ARQ queue depth via a direct, read-only `ZCARD`/`ZRANGE` against the `arq:queue` sorted set (`arq.constants.default_queue_name`) — the score is the enqueue/defer timestamp in ms, so `oldest_job_age_s` falls out of the same read. This is introspection only, consistent with `docs/schema.md` §3's "Managed by ARQ. Do not touch" (which is about *writes*, not read-only depth checks).

## Step 5 — Expose spend against the daily cap

### What to do
- Add a `spend` block to `/health/deep`'s response (admin-only route already, so no new exposure risk): `{"today": N, "cap": settings.daily_generation_cap, "remaining": cap - today}`, reading the existing `spend:{date}` key (`app/services/budget.py`) — no new tracking logic, just surfacing what already exists. This is what lets an operator notice "we're at 180/200 today" before clients start seeing `429 BUDGET_EXCEEDED`, rather than discovering the cap only when it's already been hit.

## Step 6 — Log correlation review

### What to do
- This is a verification step, not new code, unless it finds a real gap. Confirm, end-to-end, that every log line touching a job — across both the API process and the ARQ worker process — carries `job_id` (HR8/`docs/conventions.md`'s "every log line touching a job carries job_id" rule), and that `request_id` correlates an API request to whatever job it created. Specifically check: does the ARQ worker's `run_job_pipeline` entry point call `bind_job` at all, or does it rely on `_transition_and_persist`'s underlying `transition()` call passing `job_id=job.job_id` explicitly to every `log.info(...)` (which is what actually happens today — verify this is equivalent in practice, not simply "close enough")? If a genuine gap is found (a worker log line missing `job_id` because it's outside the request-scoped explicit-kwarg pattern), fix it; if not, record that the review happened and found the existing explicit-kwarg approach sufficient — don't introduce `bind_job` calls into the worker purely for symmetry with the API if the explicit-kwarg pattern already satisfies the rule everywhere that matters.

## Self-Audit
- [x] `pytest -q` passes, full suite green (402), including new tests for Steps 1–6.
- [x] `ruff check`, `ruff format --check`, `mypy app/` all pass.
- [x] `init_sentry()` is a true no-op when `SENTRY_DSN` is unset — confirmed by the existing test suite still passing with no DSN configured (it already doesn't have one).
- [x] Sentry is initialized in both the API process (`app/main.py`) and the worker process (`app/worker/settings.py`) — not just one.
- [x] Only true unhandled exceptions (500 `INTERNAL_ERROR`) are reported to Sentry as errors; `AppError` subclasses are not, and a test proves this (mock Sentry capture, hit a 404 and a 500, assert capture count).
- [x] `needs_review` transitions from both the sweeper and `_submit`'s except-branch (and the resumed-in-`SUBMITTING` branch) trigger a Sentry alert; ordinary `FAILED` terminal states do not.
- [x] `/health/deep`'s `sheets`/`storage`/`queue` checks are real, each independently testable via a fake, and the "only Redis failure returns 503" contract from `docs/api-routes.md` still holds.
- [x] `/health/deep` surfaces today's spend against `DAILY_GENERATION_CAP`.
- [x] Log correlation review completed and recorded — a real gap was found (provider/classifier logs missing `job_id`) and fixed via `bind_job()`.
- [x] `docs/api-routes.md`'s `/health/deep` example response updated to match what's actually returned; `docs/schema.md`'s `SENTRY_DSN` row updated from "Phase 8" (placeholder) to a real description.

## Manual Verification
- **Done, 2026-07-30.** Project owner created a free Sentry "Developer" project and added its DSN to `.env`. Verified two ways: (1) `app/core/observability.init_sentry()` run directly against the real DSN logged `sentry.enabled`, then `sentry_sdk.capture_exception()` on a deliberately-raised `RuntimeError` returned a real event ID (`ef1aefa9...`) with a clean `client.flush()` (no transport errors); (2) confirmed visually in the Sentry dashboard — the `RuntimeError` issue is present. Local `uvicorn` run via a temporary `jewellery-gen-backend-api` launch config also confirmed `sentry.enabled` fires correctly on real app startup.
- Confirm the free-tier event quota (5K/month) is configured with email alerting enabled in the Sentry project settings, so a `needs_review` spike actually reaches a human inbox and isn't just sitting in a dashboard nobody's watching. Default Sentry issue-alert rule (email on every new issue) applies out of the box — project owner to double check their account email is verified so delivery actually works.

## Results

**All 6 steps complete** (2026-07-30). Full suite: 402 passed, `ruff check`/`ruff format --check`/`mypy app/` clean.

- **Step 1 (Sentry init)**: `app/core/observability.py`'s `init_sentry()` — true no-op without `SENTRY_DSN` (confirmed: the whole suite runs with no DSN configured and behaves identically to before this phase). `traces_sample_rate=0`, `send_default_pii=False`. Called from both `app/main.py`'s `lifespan` and `app/worker/settings.py`'s `on_startup` — API and worker are separate processes, each needs its own init. `sentry-sdk[fastapi]==2.19.2` added as a main (not dev) dependency. Tests: `tests/test_observability.py` (6 tests).
- **Step 2 (wired into error path)**: `app/api/errors.py`'s unhandled-exception handler now calls `sentry_sdk.capture_exception(exc)` after its existing `log.exception(...)`. Deliberately **not** wired into `AppError`/`RequestValidationError` handlers — only the true 500 `INTERNAL_ERROR` catch-all reports to Sentry, confirmed by a test asserting exact capture counts (1 for `/crash`, 0 for a 404, 0 for a 422). `bind_sentry_job_scope()` tags `job_id` onto the Sentry scope, reusing `app/core/logging.py`'s existing `_job_id_var` (via a new public `current_job_id()` accessor) rather than a second job-id mechanism. Tests: 3 new cases added to `tests/test_errors.py`.
- **Step 3 (needs_review alerting)**: `capture_needs_review(job_id, reason=...)` called from all three places a job lands in `NEEDS_REVIEW`: `app/worker/sweeper.py`'s stale-deadline reap, and `app/worker/tasks.py`'s `_submit` except-branch and `_continue_pipeline`'s resumed-in-`SUBMITTING` branch. Deliberately **not** called for ordinary `FAILED` terminal states (that's normal-operation noise, not an incident). Tests: `tests/test_needs_review_alerting.py` (2 tests) + 2 new cases in `tests/test_sweeper.py`.
- **Step 4 (real `/health/deep` checks)**: replaced all three hardcoded `{"ok": true}` stubs. `sheets` reuses the cached matrix-version mechanism (no fresh Sheets call on a cache hit). `storage` calls `adapter.exists("__health_check_sentinel__")` on whichever backend `STORAGE_BACKEND` resolves to — a real reachability probe with zero upload/download cost. `queue` reads real ARQ queue depth via `ZCARD`/`ZRANGE` on the `arq:queue` sorted set — no new ARQ API needed, and consistent with `docs/schema.md` §3's "do not touch" (that's about writes). Kept the documented "only Redis failure returns 503" contract; each other check independently degrades to `"degraded"` at 200. `docs/api-routes.md`'s `/health/deep` section rewritten to match, including renaming the `drive` check to `storage` (the adapter is swappable — calling it `drive` was already wrong given Supabase is the active backend). Tests: `tests/test_health_deep.py` (8 tests, includes Step 5's cases).
- **Step 5 (spend vs. cap)**: added `app/services/budget.py`'s `today_spend()` (a thin public wrapper around the existing private spend-key helpers) and surfaced it in `/health/deep`'s new `spend: {today, cap, remaining}` block — no new tracking logic, just exposing what `DAILY_GENERATION_CAP`/`spend:{date}` already tracked.
- **Step 6 (log correlation review) — real gap found and fixed**: `app/providers/higgsfield.py`'s `"higgsfield.submit"` log line and `app/services/classifier.py`'s `"classifier.gemini.call_*"` lines never carried `job_id`, violating both `docs/conventions.md` ("every log line touching a job carries job_id") and `docs/ai-integration.md` §4 ("Always log per AI call: job_id, ..."). Root cause: every log call *inside* `app/worker/tasks.py` passes `job_id` explicitly, but nothing propagated it into the services/providers that module calls into. Fix: `app/worker/tasks.py`'s `_continue_pipeline` now wraps its whole dispatch in `bind_job(job.job_id)` (the existing, previously-unused-in-the-worker contextvar mechanism from `app/core/logging.py`) — every log line anywhere in that call tree now gets `job_id` auto-injected, with zero changes needed in `higgsfield.py` or `classifier.py` themselves. Tests: `tests/test_log_correlation.py` (2 tests) verify the mechanism directly.

**Real gaps found and fixed in this pass, not just tested around:** the two items above (missing `job_id` in provider/classifier logs; all three hardcoded `/health/deep` stubs) — both were things the docs already claimed were true and weren't.

# Phase 4 — Provider Integration

## Objective
Replace the `HiggsfieldProvider` stub (`app/providers/higgsfield.py`, currently `NotImplementedError` on every method) with a real implementation of the `GenerationProvider` Protocol, behind the exact same interface `app/worker/tasks.py` already calls (`get_provider()` from `app/providers/factory.py`). No orchestration, state-machine, or worker-stage change — Phase 1 already built and froze the full submit/poll/store pipeline against `FakeProvider`; this phase only swaps what answers `submit`/`poll`/`fetch_assets` when `PROVIDER=higgsfield`.

## Context
There is no official Higgsfield Python SDK pinned in `pyproject.toml` — `httpx` (already a dependency, already used the same way in `app/storage/supabase.py`) is the HTTP client. This environment has no network access to call the real Higgsfield API, and there is no confirmed API reference document in this repo for Higgsfield's actual request/response shapes (unlike Gemini in Phase 3, where the installed SDK could be introspected directly). Build `HiggsfieldProvider` against the contract already specified in `docs/ai-integration.md` §2 (the only source of truth this session has), isolate the actual HTTP calls behind a small internally-injectable client so the whole provider is testable with a fake transport, and flag the real endpoint/payload shape as a **manual verification item** exactly like Phase 2's Drive smoke test and Phase 3's Gemini/Sheets smoke tests — do not guess at real Higgsfield endpoint paths or payload field names beyond what's already documented, and clearly mark any such guess as a placeholder the project owner must confirm/correct against Higgsfield's real docs before production use.

**Read first:** `docs/ai-integration.md` §2 in full (interface, input, output, failure-handling table, poll interval), `docs/business-rules.md` R1 (never auto-retry a submit), R2 (submission_token ordering — already implemented in `app/worker/tasks.py`'s `_submit`, this phase does not touch it), R12 (deadline/sweeper — already implemented), `app/providers/base.py` (the frozen Protocol and dataclasses — **do not change these shapes**), `app/providers/fake.py` (existing sibling implementation, same Protocol, good reference for style/docstring conventions), `app/providers/factory.py` (the only call site that picks `HiggsfieldProvider` vs `FakeProvider`), `app/worker/tasks.py`'s `_submit`/`_poll`/`_store` (the only three call sites of the Protocol methods — confirm exactly what they pass in and expect back), `app/storage/supabase.py` (existing httpx-based adapter — mirror its construction pattern: `httpx.AsyncClient` per call vs. shared, error mapping into typed exceptions, timeout handling).

> **Important scope note**: Because there is no live Higgsfield access in this session, "done" for this phase means: (a) `HiggsfieldProvider` correctly implements the documented contract against a fake HTTP transport, with the exact failure-handling table from `docs/ai-integration.md` §2 verified test-by-test, and (b) every place a real Higgsfield-specific detail was *assumed* rather than confirmed (endpoint URL, auth header name, exact JSON field names, idempotency mechanism) is called out explicitly in code comments and in this file's Manual Verification section. Do not present assumed wire details as confirmed.

---

## Step 1 — HTTP client seam

### What to do
- Add a small `HiggsfieldClient` (or similar) wrapping the actual HTTP calls, constructor-injectable into `HiggsfieldProvider` — mirror `app/storage/supabase.py`'s pattern (a thin class around `httpx.AsyncClient`, or a client object passed into `__init__` with a default real one constructed if none given). This is what lets tests substitute a fake transport (`httpx.MockTransport`, already usable with plain `httpx` — no extra dependency needed) without touching `HiggsfieldProvider`'s own logic.
- Base URL, API key: read from `settings.higgsfield_api_key` (`app/config.py` already has this field). Add `HIGGSFIELD_BASE_URL` to `app/config.py` + `.env.example` with a clearly-labeled placeholder default (e.g. `https://api.higgsfield.ai` — **placeholder, confirm against real docs**) since the real base URL isn't confirmed in this repo.
- Auth: send the API key as a bearer token (`Authorization: Bearer {key}`) — the common convention, but flag as unconfirmed for this specific provider in a code comment.
- Timeout: use `httpx.Timeout` with a submit-call timeout distinct from poll/fetch (submit is the expensive, non-retried call — keep it reasonably generous, e.g. 30s connect+read; poll/fetch can be shorter, e.g. 15s, since they retry freely per `docs/ai-integration.md` §2).

## Step 2 — `submit`

### What to do
- `POST` to a `.../generate` (or similar, placeholder) endpoint with a JSON body built from `GenerationRequest`: `reference_image_url`, `prompt` (verbatim, R7 — never touched or logged in a way that could look like a rewrite), `negative_prompt`, `params` (passthrough dict, spread into the body or nested under a `params` key — placeholder, pick one and document it), and `submission_token` passed as the idempotency mechanism. Per `docs/ai-integration.md` §2: "If Higgsfield supports neither [idempotency key nor request metadata], record that fact here explicitly." Since this session cannot confirm which (if either) Higgsfield actually supports, implement it as an `Idempotency-Key` HTTP header (common REST convention) **and** document in this file's Manual Verification section that this must be confirmed against real Higgsfield docs — if unsupported, `submission_token` becomes purely a local recovery breadcrumb with no server-side effect, which is the least-bad fallback (R2's local-side ordering still holds either way).
- `source_image` (raw bytes) must reach the provider somehow — Higgsfield's real upload mechanism (multipart body vs. a separate pre-signed upload step vs. a data URI) isn't confirmed. Implement as `multipart/form-data` (the same convention this API's own client-facing contract uses, per R9 in `docs/business-rules.md` — a reasonable default) with the JSON fields as additional form fields, and flag this as the single most likely-to-be-wrong assumption in the Manual Verification section.
- **Response → `ProviderSubmission(provider_job_id=...)`**. Field name for the returned job id isn't confirmed — read it defensively (try a couple of plausible keys, e.g. `id` / `job_id`, and raise a clear error if neither is present) rather than silently defaulting to `None`.
- **Failure handling** (`docs/ai-integration.md` §2 table — implement exactly, do not add extra retry logic here; retries for submit are explicitly forbidden by R1 and the worker's `_submit` already treats any raised exception as an orphaned-submit signal):
  - A definite 4xx HTTP response (client error, no ambiguity about whether the provider accepted the job) → raise a distinguishable exception (e.g. `HiggsfieldSubmitRejected`) carrying enough info that a future caller *could* map it to `PROVIDER_SUBMIT_FAILED` instead of `ORPHANED_SUBMIT` if `app/worker/tasks.py`'s `_submit` is ever refactored to distinguish the two — **but do not change `_submit` itself in this phase**; today it catches `Exception` uniformly and always maps to `ORPHANED_SUBMIT`/`needs_review`, which is the conservative, currently-correct behavior per R1 ("never resubmit... park it in `needs_review` for a human"). Keep the distinction available for a future phase, not wired in now, since `docs/schema.md`'s error table lists `PROVIDER_SUBMIT_FAILED` as a separate code but `_submit`'s current catch-all doesn't discriminate — flag this exact gap in the Self-Audit rather than silently "fixing" worker orchestration logic that's out of this phase's stated scope.
  - Timeout / connection error / any ambiguous failure → let the exception propagate as-is (already handled correctly and conservatively by `_submit`'s catch-all → `needs_review`/`ORPHANED_SUBMIT`).

## Step 3 — `poll`

### What to do
- `GET` a status endpoint keyed by `provider_job_id` (placeholder path, e.g. `.../generate/{provider_job_id}` — confirm against real docs).
- Map the response's status field (name unconfirmed — read defensively, same approach as Step 2) to the `ProviderStatus` Protocol's `Literal["pending", "running", "succeeded", "failed"]`. If the real API uses different string values, this mapping is the one place that needs updating later — isolate it in a small pure function (`_map_status(raw: str) -> Literal[...]`) so that update is a one-function change, not a rewrite.
- `progress`, `error` — pass through if present, `None` otherwise.
- **Poll failures are free to retry** (`docs/ai-integration.md` §2 — "Polling is free — retry it freely"); the worker's `_poll` already wraps each poll call in `retry_free`. `HiggsfieldProvider.poll()` itself should raise on any HTTP/network error rather than swallowing it — mirror `FakeProvider`'s pattern where failure surfaces as an exception the caller's retry wrapper handles, not a synthetic `"failed"` status (a real network blip is not the same thing as the provider reporting genuine generation failure).

## Step 4 — `fetch_assets`

### What to do
- `GET` the completed job's asset list/URLs (placeholder endpoint, e.g. `.../generate/{provider_job_id}/assets` or the asset URLs may already be embedded in the terminal `poll` response — since this is unconfirmed, implement the two-step "poll tells you it's done, then a separate fetch downloads bytes" version to match the Protocol's existing three-method shape, and note in Manual Verification that if Higgsfield actually returns asset URLs directly in the poll response, this can be simplified later).
- Download each asset's bytes (a second HTTP `GET` per URL, or decode from response if Higgsfield returns bytes/base64 directly — placeholder: assume URLs requiring a follow-up `GET`, most common REST pattern) and wrap each into `ProviderAsset(data=..., mime=...)`. Determine `mime` from the response `Content-Type` header if present, else default to `image/png` (mirrors `FakeProvider`'s convention).
- **Failure handling**: per `docs/ai-integration.md` §2, asset download failure retries 3× then `STORAGE_ERROR` — this retry already lives in the worker's `_store` via `retry_free`; `fetch_assets` itself should just raise on failure, not retry internally (same "single retry layer" principle enforced in Phase 3 for the classifier — do not duplicate retry logic inside the provider).

### Checkpoint (Steps 1–4)
- [ ] `HiggsfieldProvider` satisfies the `GenerationProvider` Protocol (typed assignment test, mirroring `FakeProvider`'s and other adapters' `test_*_satisfies_*_protocol` pattern — check `tests/` for the existing convention and follow it)
- [ ] `submit()` against a fake HTTP transport: builds the expected request (assert on the captured request — headers, body/multipart fields, `submission_token` present) and correctly parses a fake success response into `ProviderSubmission`
- [ ] `submit()` against a fake 4xx response raises (do not need to test the currently-unwired `PROVIDER_SUBMIT_FAILED` distinction beyond confirming the exception is raised and distinguishable in principle)
- [ ] `submit()` against a fake timeout/connection-error raises (letting `_submit`'s existing catch-all map it to `ORPHANED_SUBMIT` — confirm this end-to-end through a worker-level test using `HiggsfieldProvider` with a failing fake transport, if practical, otherwise unit-level is sufficient)
- [ ] `poll()` correctly maps each of the real API's assumed status strings to the four `ProviderStatus.state` values via the isolated `_map_status` function (unit-test the mapping function directly)
- [ ] `poll()` against a fake network error raises (not swallowed into a synthetic `"failed"`)
- [ ] `fetch_assets()` against a fake terminal response downloads and wraps bytes correctly, mime inferred from `Content-Type`
- [ ] `fetch_assets()` against a fake download failure raises
- [ ] `submission_token` never appears in any log line as anything other than an opaque id (no image bytes, no full payload logged) — grep test or manual review, per `docs/ai-integration.md` §4
- [ ] Full existing test suite still passes — no regressions to `_submit`/`_poll`/`_store` worker logic (this phase changes nothing about `app/worker/tasks.py`)
- [ ] `ruff check` and `mypy --strict` pass on `app/providers/higgsfield.py` and its test file

---

## Self-Audit Instruction

Before declaring this phase complete:
1. Re-read every checkpoint above; verify each with a real test run (`pytest`, `ruff check`, `mypy --strict`), not by inspection alone.
2. Confirm `app/worker/tasks.py` is byte-for-byte unchanged — this phase is a pure adapter swap behind the frozen `GenerationProvider` Protocol, same pattern as Phase 2's storage swap and Phase 3's classifier/matrix swap.
3. Explicitly list, in this file's own text, every assumed-not-confirmed Higgsfield wire detail (base URL, auth header, submit endpoint/method/body shape, idempotency mechanism, poll endpoint/status field values, asset-fetch mechanism) so the project owner can correct them in one pass against Higgsfield's real API docs before the first live call.
4. Note the `PROVIDER_SUBMIT_FAILED` vs `ORPHANED_SUBMIT` discrimination gap in `app/worker/tasks.py`'s `_submit` (it currently treats all submit exceptions as orphaned/needs_review, never distinguishing a confirmed-no-charge 4xx) as an out-of-scope observation for a future phase — do not silently fix it here.
5. Update `phases/phase-roadmap.md` (status → Complete, with the manual verification explicitly flagged pending) and `claude.md` if needed.
6. Only say "Phase 4 Complete" when every checkpoint is green and all assumed wire details are flagged for manual confirmation.

## Manual Verification (fill in after real Higgsfield access is available)
- [ ] Confirm real base URL, auth mechanism, and whether `HIGGSFIELD_API_KEY` is a bearer token or something else.
- [ ] Confirm the real submit endpoint, method, and body shape (JSON vs multipart; how `source_image` bytes and `reference_image_url` are actually passed; where `prompt`/`negative_prompt`/`params` go).
- [ ] Confirm whether Higgsfield supports an idempotency key or submission metadata at all, and if so, the correct header/field name — update `submit()` accordingly if the current `Idempotency-Key` header guess is wrong.
- [ ] Confirm the real poll endpoint and the exact set of status strings it returns; update `_map_status` if they differ from the assumed pending/running/succeeded/failed set.
- [ ] Confirm whether completed asset URLs arrive embedded in the poll response or require the separate fetch endpoint assumed here; simplify `fetch_assets`/`poll` if the former.
- [ ] Run one real end-to-end job with `mock=false` and a real product photo, confirm `provider_job_id` round-trips correctly, `asset_refs` end up populated with real generated images, and that a forced submit failure (e.g. temporarily invalid API key) correctly parks the job in `needs_review`/`ORPHANED_SUBMIT` rather than crash-looping.

## Final Phase 4 Checklist
- [ ] `HiggsfieldProvider` implements `GenerationProvider` fully, no `NotImplementedError` left
- [ ] All HTTP calls isolated behind an injectable client seam, testable via fake transport, no live network needed for the test suite
- [ ] Failure handling matches `docs/ai-integration.md` §2's table at the provider level; retry counts/backoffs remain solely the worker's responsibility (no duplicate retry logic inside the provider)
- [ ] Every assumed (unconfirmed) wire detail flagged in this file's text and/or code comments
- [ ] `app/worker/tasks.py` unchanged
- [ ] Full test suite green, `ruff`/`mypy --strict` clean
- [ ] `docs/` and roadmap updated to match reality; manual verification explicitly pending

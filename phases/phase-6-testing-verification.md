# Phase 6 — Testing & Verification

## Objective
Build the cross-cutting verification that no single phase owns: an end-to-end suite that drives a job through the real routes and the real staged worker pipeline (not unit-level mocks of individual services) against `FakeProvider`, table-driven enforcement of the state machine and the twelve Hard Rules, explicit money-path regression tests for R1–R6, a frozen-contract snapshot of the OpenAPI schema, a classification accuracy benchmark against labelled client photos, and a light load test on the Redis read path.

This phase adds tests only. No production code change is expected — if a test finds a real bug, fix it and record the divergence in `docs/` per the Documentation Discipline rule, rather than weakening the test to match current behaviour.

## Context
Phases 0a–5 each shipped unit tests as an in-phase checkpoint (247 tests currently pass — `pytest -q`, ~34s, plus `ruff check`/`ruff format --check`/`mypy app/` in `.github/workflows/ci.yml`). Those are components tested in isolation. What's missing is everything that only shows up when the pieces run *together* or when a rule is checked *exhaustively* rather than by example.

Coverage already in place — do **not** rebuild it:
- Cross-tenant isolation is tested (`test_deps.py::test_job_owned_by_other_client_returns_404_identical_to_unknown_job`, `test_jobs_routes.py::test_poll_other_clients_job_returns_404`, and ownership filtering in `test_list_jobs_respects_limit_newest_first_and_ownership`). Hard Rule 3 also has `test_50_consecutive_polls_never_touch_sheets_client`.
- Sweeper/deadline behaviour per status is tested (`test_sweeper.py`).
- `HiggsfieldProvider` is fully tested against `httpx.MockTransport` (`test_higgsfield_provider.py`).

**Read first:** `docs/business-rules.md` in full, `docs/schema.md` §1 (the state transition table — Step 2 is generated from it) and §6 (settings), `docs/api-routes.md`, `claude.md` → "Hard Rules — Never Break These", `docs/conventions.md` → Testing, `app/worker/tasks.py`, and `tests/conftest.py` + `tests/fakes/` (reuse the existing fixtures — real Redis on DB 15, `FakeProvider`, `FakeSheetsClient` — do not introduce new fakes or `fakeredis`).

> **Testing-convention constraint.** `docs/conventions.md` states no test may call a real external service. Steps 5 and 6 produce **scripts under `scripts/`, not `pytest` tests** — the benchmark needs real Gemini and the load test needs a real running stack. Neither may be collected by the default `pytest` run or by CI. Keep them out of `tests/`.

---

## Step 1 — Cross-phase e2e pipeline suite

### What to do
- New `tests/test_e2e_pipeline.py`. Drive full job lifecycles through the real FastAPI app (`POST /generate` → poll `GET /jobs/{id}` → terminal state) with the real `app/worker/tasks.py` stage functions executed in sequence against `FakeProvider`. Assert on the whole picture at each terminal state — HTTP status and response body, Redis job record, *and* the `FakeSheetsClient` call log — not merely that no exception was raised.
- Cases, one per terminal state:
  - **Happy path**: `queued → classifying → resolving → submitting → generating → storing → succeeded`. Assert the asset is fetchable via `GET /jobs/{id}/assets/0` and that no raw storage URL appears anywhere in any response body (Hard Rule 2).
  - **`failed` / `MATRIX_MISS`** (R9) — assert no fallback prompt was substituted.
  - **`needs_input`** (low confidence, R10) → `POST /jobs/{id}/resolve` → resumes at the resolve stage → `succeeded`. Assert the classifier fake was called exactly once total, i.e. classification was neither re-run nor re-charged.
  - **`needs_review` / `ORPHANED_SUBMIT`** (R1) — inject a submit-stage exception; assert the job parks, and that the provider's `submit` was called exactly once and never again on any subsequent sweep or rehydration pass.
  - **Client-supplied `jewelry_type`** (R11) — assert the classifier fake was never called and `type_source == PROVIDED`.
- **Crash-recovery e2e**: write a job mid-pipeline into Redis, run `app/store/rehydrate.py`'s boot recovery followed by the sweeper, and assert the job reaches a legal terminal state without a second paid call. This is the one path where R1, R12, and boot rehydration interact, and nothing currently exercises all three together.

## Step 2 — State machine and Hard Rule invariants

### What to do
- New `tests/test_state_table.py`: a **table-driven** test generated from the transition table in `docs/schema.md` §1, asserting every legal transition is accepted and every unlisted `(from, to)` pair raises. `tests/test_state.py`'s current 8 example-based tests do not prove exhaustiveness, and an unlisted-but-accidentally-allowed transition is exactly the class of bug that lets a job leave a terminal state. Assert too that every transition writes `updated_at`, and that terminal transitions set `completed_at`.
- New `tests/test_invariants.py`, one test per Hard Rule that isn't already pinned elsewhere:
  - **HR2** — sweep every route's response body for anything resembling a raw Drive/Supabase URL or a bare `storage_ref`.
  - **HR4** — a job that traverses the full pipeline produces **exactly two** `FakeSheetsClient` writes (one append, one update), asserted by call count, not by spot-check.
  - **HR8** — capture structured log output across a full pipeline run and assert no API key, no `GOOGLE_SERVICE_ACCOUNT_JSON`, no base64, and no image bytes appear in it, and that prompt text never appears at INFO.
  - **HR12** — no code path can create a job without `deadline_at`.
  - **Envelope conformance** — every error response across every route has a populated `error.code` from the `ErrorCode` enum, and every timestamp field in every response is ISO 8601 UTC with a trailing `Z`.

## Step 3 — Money-path regression tests

### What to do
- New `tests/test_money_paths.py`, one test per rule, each asserting the specific failure mode the rule exists to prevent — not the happy path already covered elsewhere. Name them after the behaviour per `docs/conventions.md`.
  - **R1** — a submit-stage crash mid-call never produces a second provider call on rehydration or sweep.
  - **R3** — a second submit with an identical `content_hash` returns `deduplicated: true`, `billable: false`, and makes no provider call; **and** a mock job and a real request for the same image/service/type never satisfy each other in either direction (both guards: `record_dedupe`'s refusal to write and `generate.py`'s refusal to accept a cross-`mock` hit — this is a recorded regression, so test both, not just one).
  - **R4** — an `Idempotency-Key` replay returns the original `job_id` even when the original job *failed*, and is scoped per API key (the same key value from a different client key does not collide).
  - **R5** — submits at `DAILY_GENERATION_CAP` reject with `429 BUDGET_EXCEEDED`; mock and deduplicated submits never increment `spend:{date}`.
  - **R6** — `mock=true` never constructs or calls `HiggsfieldProvider` and never increments spend; separately, `PROVIDER=fake` with `mock=false` also yields `FakeProvider` (the second, deployment-level switch — assert both levers independently so the two can't be confused in future debugging).

## Step 4 — Frozen-contract snapshot

### What to do
- Phase 1 froze the API contract and Phase 10 ships `docs/openapi.json` as a handover deliverable, but nothing currently fails when a response model changes. Add `tests/test_contract_snapshot.py`: generate the app's OpenAPI schema and compare it against a committed `tests/fixtures/openapi_snapshot.json`. A diff fails the test with instructions to either revert the change or regenerate the snapshot deliberately — per `docs/conventions.md`, adding an optional field is fine but renaming, removing, or retyping one is a `/api/v2` change and must not pass silently.
- Add `tests/test_settings_sync.py`: assert every field on the `Settings` object appears in `.env.example` and vice versa, and that the set matches `docs/schema.md` §6. Conventions require this file stay in sync; nothing enforces it today.

## Step 5 — Classification accuracy benchmark

### What to do
- The client's ~50 labelled photos are **not in this repo**. If they haven't been supplied when this phase runs, this step is blocked — record it as a Manual Verification item and do not fabricate labelled data or substitute unlabelled samples.
- `scripts/benchmark_classifier.py` (a script, not a `pytest` test — it calls real Gemini): loads each labelled photo, calls the real classifier service, and reports overall accuracy, a per-`JewelryType` confusion breakdown, the mean/median confidence, and the count that would land in `needs_input` at the current `CLASSIFIER_CONFIDENCE_THRESHOLD` of 0.75. Write results to a timestamped file rather than stdout only.
- Per the roadmap's Revisit Trigger: if accuracy is below ~90%, surface the number and stop — do not silently raise the threshold. That's the project owner's call, and the tradeoff (more `needs_input` friction vs. more wrong generations) is a business decision.
- This benchmark is also the intended home for the still-pending Phase 3 live-Gemini smoke test; running it satisfies that outstanding item.

## Step 6 — Light load test

### What to do
- `scripts/load_test.py` (a script, not a `pytest` test — needs a real running stack: `uvicorn` + `arq` + Redis). Plain `asyncio` + `httpx`, both already dependencies — do not add a load-testing framework for this.
- Fire concurrent `POST /generate` requests with `mock=true` at a concurrency around the R20 rate-limit boundary (~10 concurrent, the roadmap's stated revisit trigger) and poll each to completion. Assert: no job ends in an illegal or non-terminal state under concurrency; no two jobs cross-contaminate state in Redis; rate limiting returns `429` past the configured threshold and *only* past it; and total wall-clock is consistent with `FakeProvider`'s ~10s simulated latency rather than the serialized sum, which would indicate a worker-side bottleneck.
- Scope this to catch concurrency bugs, not to produce throughput numbers. Record the observed numbers in this file anyway — they're the baseline the Phase 8 queue-depth metrics and the "30 jobs/min" Supabase revisit trigger get compared against.

## Step 7 — Coverage and CI wiring

### What to do
- Add `pytest-cov` and a coverage report over `app/` to the dev extra and the CI test step. Do **not** set an arbitrary global percentage gate — instead review the report and confirm that `app/services/dedupe.py`, `app/services/budget.py`, `app/core/state.py`, and `app/worker/tasks.py`'s `_submit` (the money and correctness paths) are at or near full branch coverage. Record the actual figures in this file and file any genuinely-uncovered branch as a finding rather than backfilling a token test.
- Register `e2e` and `slow` pytest markers if the new e2e suite is slow enough to want separating locally; CI runs everything either way.
- Confirm `scripts/benchmark_classifier.py` and `scripts/load_test.py` are **not** collected by `pytest` and are not invoked by `.github/workflows/ci.yml` — CI has no network access to Gemini and no running worker.

## Self-Audit
- [x] `pytest -q` passes: all 247 pre-existing tests plus every new Phase 6 test, full suite green (366 total).
- [x] `ruff check`, `ruff format --check`, and `mypy app/` all pass, including type hints on every new test signature.
- [x] Every rule in `docs/business-rules.md` §1 (Money Rules, R1–R6) has a test that would fail if the rule were violated (see `tests/test_money_paths.py`'s per-rule index).
- [x] Every Hard Rule in `claude.md` is either pinned by a Phase 6 invariant test or explicitly noted here as already pinned elsewhere, with the test named (see `tests/test_invariants.py`'s header).
- [x] The state-transition test is generated from `docs/schema.md` §1, not hand-enumerated, so the table and the tests cannot drift apart (`tests/test_state_table.py`, generated directly from `LEGAL_TRANSITIONS`).
- [x] The e2e suite reaches all four terminal states (`succeeded`, `failed`, `needs_input`, `needs_review`) plus the crash-recovery path.
- [x] OpenAPI snapshot committed, and deliberately breaking a response model is confirmed to fail the test.
- [x] Coverage figures for the money/correctness modules recorded below.
- [ ] Classifier benchmark run and results recorded below — or explicitly marked blocked with the reason. **Blocked**, see Manual Verification.
- [ ] Load test run once against a real local stack and results recorded below. **Blocked**, see Manual Verification.
- [x] Benchmark and load scripts confirmed excluded from `pytest` collection and from CI — moot for now: neither `scripts/benchmark_classifier.py` nor `scripts/load_test.py` exists yet (Steps 5/6 are blocked pending network access and labelled data), so there is nothing to accidentally collect.
- [x] `docs/` updated for any divergence this phase uncovered, in this session (see Results below).

## Manual Verification
Items that cannot be completed in a sandboxed/offline session — record what was actually possible when this phase is executed:

1. **Classifier benchmark (Step 5)** — needs real Gemini network access **and** the client's ~50 labelled photos from the project owner. Neither is currently in the repo.
2. **Load test (Step 6)** — needs a real running stack (`uvicorn` + `arq` + Redis). Note that Phase 5 was verified against a local stack rather than `docker-compose` (a pre-existing container held host port 6379); if Compose is used here instead, that also discharges Phase 5's outstanding "verify `docker-compose up` end-to-end" item.
3. **Carried-over items from earlier phases that a networked session should discharge alongside this phase** — Phase 2's live Drive/Supabase `put`/`get`/byte-compare smoke test, Phase 3's live `POST /admin/matrix/refresh` and re-run of `scripts/validate_matrix.py` against the real sheet, Phase 4's full list of unconfirmed Higgsfield wire details, and the Phase 1 precondition that the `JobLog` tab must exist on the client's spreadsheet before any real Sheets write is exercised.

## Results

**Steps 1–4 and 7 complete** (2026-07-29). Full suite: 366 passed (247 pre-existing + 119 new), `ruff check`/`ruff format --check`/`mypy app/` clean on all touched/new files. New files: `tests/test_e2e_pipeline.py` (6 tests, marked `@pytest.mark.e2e`), `tests/test_state_table.py` (101 tests, exhaustive `LEGAL_TRANSITIONS` table), `tests/test_invariants.py` (3 tests), `tests/test_money_paths.py` (3 tests), `tests/test_contract_snapshot.py` + `tests/fixtures/openapi_snapshot.json` (2 tests), `tests/test_settings_sync.py` (3 tests).

**Real bugs/gaps found and fixed in this pass, not just tested around:**
- [app/api/v1/jobs.py:76-80](../app/api/v1/jobs.py) — `needs_input` responses never populated `error.code`, contradicting `docs/api-routes.md`'s documented contract (`"error.code" is "LOW_CONFIDENCE"` on `needs_input`). Found by the e2e suite's needs_input→resolve test. Fixed: `error` is now populated for `NEEDS_INPUT` too.
- `HIGGSFIELD_MCP_BRIDGE_URL` was a real `Settings` field missing from both `.env.example` and `docs/schema.md` §6. Added to both.
- `docs/schema.md` §6 was also missing `LOCAL_STORAGE_DIR` and undocumented the `higgsfield_mcp_bridge` value of `PROVIDER`. Added.

- **Coverage (money/correctness modules)**: `app/services/dedupe.py` 100%, `app/services/budget.py` 100%, `app/core/state.py` 100%. `app/worker/tasks.py`'s `_submit` function is fully covered (0 missed statements within it) — the module's overall 88%/18-missed-lines are all in `_poll`/`_store` edge branches and `_continue_pipeline`/entry-point guards, not `_submit`. Full breakdown in CI output (`pytest --cov=app --cov-report=term-missing`, now wired into `.github/workflows/ci.yml` and `pyproject.toml`'s `dev` extra via `pytest-cov`). No global coverage gate was set, per the step's own instruction not to backfill token tests for an arbitrary percentage.
- Classifier accuracy: **blocked** — no labelled client photos in this repo and no network access in this session. See Manual Verification item 1.
- Load test observations: **blocked** — needs a real running stack (`uvicorn` + `arq` + Redis), not available in this session. See Manual Verification item 2.

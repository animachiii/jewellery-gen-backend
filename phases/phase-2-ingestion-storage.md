# Phase 2 — Ingestion & Storage

## Objective
Replace `LocalStorage` with a real Google Drive-backed `StorageAdapter`, without changing the `StorageAdapter` protocol, the `storage_ref` contract, or anything downstream of it (D6, D7, R17). Source-image retention and asset delivery move onto real Drive, quota/error handling included. No route, schema, or business-rule change — this phase is a pure adapter swap plus its operational edges.

## Context
Phase 1 built the seam (`app/storage/base.py`'s `StorageAdapter` Protocol) and `LocalStorage` as the only implementation, factory-resolved via `get_storage_adapter()` in `app/storage/local.py`. `docs/conventions.md` → Adapters is explicit: "No code outside `app/storage/` may import a concrete provider... Adding a provider means adding one file and one factory line — nothing else." This phase is the proof of that promise.

`GOOGLE_SERVICE_ACCOUNT_JSON` and `GDRIVE_FOLDER_ID` are already configured in `.env` (confirmed by the project owner: the service account has Editor access on the target folder). This environment has no network access to call the real Drive API — build and test entirely against a `FakeDriveClient` per `docs/conventions.md` → Testing ("No test may call a real external service"). A manual smoke check against the real API happens outside this session, in an environment with network access.

**Read first:** `docs/schema.md` §4 (`source_ref`, `asset_refs`), `docs/business-rules.md` R17, `docs/conventions.md` → Adapters and Async, `app/storage/base.py`, `app/storage/local.py` (the interface and ref-opacity contract you're reproducing), `app/api/v1/jobs.py` (the assets route you're changing the backing store for, not its response shape).

---

## Step 1 — `DriveStorage` Adapter

### What to do
Implement `app/storage/drive.py`:

- A `DriveClient` `Protocol` (mirroring `app/store/sheets_store.py`'s `SheetsClient` pattern — sync surface, wrapped in `asyncio.to_thread` by the adapter, never called directly from async code): methods for uploading bytes to a folder, downloading bytes by file ID, and checking existence — design the minimal surface `DriveStorage` actually needs, not the full Drive API.
- `GoogleDriveClient` — the real implementation, built on `google-api-python-client`'s `drive` service (already a pinned dependency, same auth pattern as `app/store/sheets_store.py`'s `GoogleSheetsClient`: `google.oauth2.service_account.Credentials.from_service_account_info(settings.google_service_account_info, scopes=[...])`). Use the `drive.file` scope (least privilege sufficient for uploading/reading files this service account creates — do not request `drive` full-account scope). Uploads go into `settings.gdrive_folder_id`.
- `DriveStorage` implementing `StorageAdapter` exactly:
  - `put(data, filename, mime) -> str` — uploads to Drive, returns an **opaque `storage_ref`**. Per R17 and the existing `LocalStorage` precedent, this must not be a raw Drive file ID exposed as a URL anywhere — but unlike `LocalStorage`'s synthetic uuid4, here the natural opaque handle *is* the Drive file ID (a Drive file ID is already an opaque string with no path structure and no filename embedded — it satisfies the "no path separators, no filename" rule as-is). Confirm this reasoning holds and use the Drive file ID directly as `storage_ref`, OR wrap it if you find a reason not to (e.g. needing to store mime type alongside it, similar to `LocalStorage`'s `.mime` sidecar) — Drive files carry their own `mimeType` metadata natively, so prefer setting that at upload time and reading it back via `get()` rather than inventing a sidecar file. Document whichever choice you make.
  - `get(ref) -> tuple[bytes, str]` — downloads by file ID, returns `(data, mime)` read from Drive's stored `mimeType` metadata.
  - `exists(ref) -> bool` — cheap metadata check (`files.get` with `fields=id`), not a full download.
  - Never raise an exception exposing a Drive URL, path, or the service account's identity in a message that could reach a client-facing error later (mirror `StorageRefNotFoundError`'s existing discipline in `app/storage/local.py`).
- **Quota and error handling**: Drive's API returns 403 (quota/rate limit) and 5xx transient errors. Wrap `put`/`get` calls with a small retry (reuse the pattern/constants from `app/worker/retry.py`'s `retry_free` helper built in Phase 1 if it fits, or a local equivalent) on retryable failures (403 rate-limit, 5xx) — NOT on definite 4xx errors (404, 400, permission-denied-not-quota). On exhausted retries, raise a clear `DriveStorageError` (or reuse `StorageRefNotFoundError`-style typed exception, but this is a different failure class — not "ref not found", it's "operation failed") that the caller (worker `store` stage, or the `assets` route) maps to the existing `STORAGE_ERROR` `ErrorCode` / `StorageError` `AppError` (both already exist from Phase 1 — `app/models/enums.py`'s `ErrorCode.STORAGE_ERROR`, `app/api/errors.py`'s `StorageError`).

### `FakeDriveClient` (test-only, alongside the real one)
Per `docs/conventions.md` → Testing, build `FakeDriveClient` implementing the same `DriveClient` Protocol — an in-memory dict standing in for Drive. Used by every `DriveStorage` test and by any later phase's tests that touch storage in a Drive-configured mode. Put it in `tests/fakes/fake_drive_client.py`, mirroring `tests/fakes/fake_sheets_client.py`'s existing pattern exactly (check that file for the house style before writing this one).

### Factory update
`app/storage/local.py`'s `get_storage_adapter()` currently unconditionally returns `LocalStorage`, with a comment: "Phase 2 will branch on a new setting (e.g. STORAGE_BACKEND) to add Drive." Add that setting now: `storage_backend: Literal["local", "drive"] = Field(default="local", alias="STORAGE_BACKEND")` in `app/config.py` (add to `.env.example` too, and — since this changes `docs/schema.md` §6's env var table — add the row there in your docs-update pass at the end). Default stays `"local"` so nothing in Phase 1's existing test suite or any not-yet-Drive-configured environment breaks by default; production `.env` should set `STORAGE_BACKEND=drive` once this phase is verified live. Move the factory (or keep it in `app/storage/local.py`, or relocate to a neutral `app/storage/__init__.py` / new `app/storage/factory.py` matching `app/providers/factory.py`'s existing precedent — prefer the latter for symmetry) to branch on this setting.

### Checkpoint 1
- [x] `DriveStorage.put` → `get` round-trips bytes and mime through `FakeDriveClient`
- [x] `storage_ref` returned by `put()` contains no path separators and no filename (same assertion style as Phase 1's `LocalStorage` test)
- [x] `exists()` returns `True`/`False` correctly without triggering a full download (assert `FakeDriveClient`'s call-count/method used, not just the boolean result)
- [x] A simulated quota/5xx failure retries the configured number of times then raises a typed error mapping to `STORAGE_ERROR`; a simulated definite-404 does NOT retry
- [x] `get_storage_adapter()` returns `LocalStorage` when `STORAGE_BACKEND=local` (default) and `DriveStorage` when `STORAGE_BACKEND=drive`, with no other code change required (mirrors Phase 1 Checkpoint 5's provider-factory test pattern)
- [x] `grep -rn "googleapiclient\|drive" app/ --include="*.py"` (excluding `app/store/sheets_store.py`'s pre-existing Sheets-API use of the same client library) matches only inside `app/storage/`

---

## Step 2 — Source Image Retention

### What to do
Currently (Phase 1), the uploaded source image is written via `get_storage_adapter().put(...)` at submit time (`app/api/v1/generate.py`) and referenced as `job.source_ref` — this already works generically through the adapter and needs **no route-level change**. This step is about confirming/adding retention semantics specific to Drive:

- Decide and implement a retention policy for source images: do they live in the same `GDRIVE_FOLDER_ID` as generated assets, or a separate subfolder/folder (e.g. `GDRIVE_FOLDER_ID` for outputs, a new `GDRIVE_SOURCE_FOLDER_ID` for inputs)? `docs/schema.md` §6 only defines one `GDRIVE_FOLDER_ID`. Simplest, most consistent-with-existing-scope choice: **reuse the same folder for both**, distinguished only by filename prefix or Drive metadata (e.g. `put(data, filename=f"source_{job_id}.{ext}", mime=...)` vs `put(data, filename=f"{job_id}_{i}.{ext}", mime=...)` — check what `app/api/v1/generate.py` and `app/worker/tasks.py` currently pass as `filename` and keep it distinguishable). Don't invent a second env var/folder unless you find a concrete reason retention *policy* (e.g. different lifecycle/deletion rules) requires physical separation — `docs/business-rules.md` §6 "Deferred to v2" already lists "Asset deletion / retention policy" as explicitly out of scope, so there is no differential retention rule to implement yet; keep this step minimal.
- Confirm `source_bytes`/`source_mime` on the `Job` record (already populated at Phase 1 submit time) remain accurate and untouched by this phase — no schema change needed here.

### Checkpoint 2
- [x] A submitted job's source image round-trips through `DriveStorage` exactly like a generated asset does (same adapter, same guarantees) — one test proving `source_ref` set at submit resolves via `get_storage_adapter().get()` after switching `STORAGE_BACKEND=drive`
- [x] No new required env var was introduced without a concrete retention-policy reason (self-check against `docs/business-rules.md` §6)

---

## Step 3 — Asset Streaming / Signed-URL Delivery

### What to do
`docs/api-routes.md` → `GET /jobs/{id}/assets/{index}`: "Resolves the storage_ref... and either streams the bytes or issues a 302 to a short-lived signed URL." Phase 1's route (`app/api/v1/jobs.py`) always streams bytes directly (`Response(content=data, media_type=mime, ...)`) — correct for `LocalStorage`, which has no concept of a signed URL. For Drive, decide:

- **Streaming-through-the-API (recommended default, keep it simple)**: the route keeps doing exactly what it does now — `DriveStorage.get(ref)` downloads bytes server-side, the route streams them back with the same `Content-Type`/`Cache-Control: private, max-age=3600` headers already in place. This preserves D7 ("Asset delivery; no raw Drive URLs ever") trivially, since no Drive URL is ever constructed or exposed. **No route code changes needed at all** — the adapter swap alone satisfies this.
- Signed-URL delivery (302 redirect to a short-lived Drive link) is an optimization for large files / reduced server bandwidth, not a correctness requirement — D7 explicitly frames it as "no raw Drive URL ever appears in a response," and a short-lived, scope-limited signed URL technically still isn't the permanent raw Drive URL, but it's meaningfully more complex (URL expiry handling, Drive's actual signed-URL/webContentLink mechanics, cache-control interaction with a redirect) for a benefit (bandwidth) this project doesn't need yet at its scale. **Default to streaming-through-the-API for this phase** and leave signed-URL delivery as a documented future optimization, not a checkpoint here — this is a deliberate scope-narrowing decision Phase 2 makes explicit, matching how Phase 1 scoped `LocalStorage` minimally. If a later phase's quota/bandwidth pressure revisits this, it's a `Revisit Trigger` candidate for `phases/phase-roadmap.md`, not a silent gap.
- Do add one thing routes currently lack: **Drive-specific failure mapping**. If `DriveStorage.get()` raises the quota/retry-exhausted error from Step 1, the assets route must map it to `502 STORAGE_ERROR` (the `StorageError` `AppError` already exists from Phase 1 — confirm the route's existing `except` clause around `storage.get()` actually catches your new Drive-specific exception type, not just `LocalStorage`'s `StorageRefNotFoundError`; widen/adjust the except clause if needed).

### Checkpoint 3
- [x] `GET /assets/{index}` against a `succeeded` job with `STORAGE_BACKEND=drive` streams bytes correctly through `FakeDriveClient` with the right `Content-Type` and `Cache-Control`
- [x] A simulated Drive quota/retry-exhaustion failure on `get()` surfaces as `502 STORAGE_ERROR` through the route, not a raw 500
- [x] The decision to default to streaming (not signed-URL redirect) is recorded in this file's audit notes and, if warranted, added to `phases/phase-roadmap.md` → Revisit Triggers

---

## Self-Audit Instruction

Before declaring this phase complete:
1. Re-read every checkpoint above; verify each with a real test run (`pytest`, `ruff check`, `mypy --strict`), not by inspection alone.
2. Confirm `LocalStorage` still passes its full Phase 1 test suite unmodified (default `STORAGE_BACKEND=local` must remain a fully working, regression-free path — Drive is additive, not a replacement of the interface).
3. Confirm the boundary rule: `grep -rn "googleapiclient" app/ --include="*.py"` shows Drive-API usage confined to `app/storage/`, Sheets-API usage confined to `app/store/` — no crossover.
4. Update `docs/schema.md` §6 (new `STORAGE_BACKEND` var) and `phases/phase-roadmap.md` (status → Complete, plus a Revisit Trigger note on signed-URL delivery if you added one) in the same session.
5. Leave a clear note (in this file's own "Manual Verification" section below, filled in by whoever runs it) for the required **real** smoke test against live Drive, since this session cannot perform one: upload a small file via `DriveStorage.put` against real credentials, confirm it lands in the configured `GDRIVE_FOLDER_ID`, `get()` it back, confirm byte-for-byte match, then manually delete the test file from Drive.
6. Only say "Phase 2 Complete" when every checkpoint is green, docs are in sync, and the manual-verification note is filled in (or explicitly still pending, clearly flagged as the one item this sandboxed session could not close).

## Manual Verification (fill in after a real run against live Drive)
- [x] Upload smoke test: **RUN, FAILED — not a code defect.** `scripts/smoke_test_drive.py` was run by the project owner against real credentials. Sequence of real failures encountered and fixed up to a point:
  1. Drive API disabled on the GCP project → enabled it.
  2. `GDRIVE_FOLDER_ID` was still the `.env.example` placeholder → corrected to a real Drive folder ID.
  3. **Hard blocker, unresolvable within this setup**: `storageQuotaExceeded` — "Service Accounts do not have storage quota." Google does not allow a bare service account to own file storage in a personal (non-Workspace) Google Drive; Shared Drives (the standard workaround) require Google Workspace, which this project does not have.
  - **Decision**: switched the active storage backend to **Supabase Storage** instead (see Addendum below). `DriveStorage` is left in place, fully built and tested against `FakeDriveClient`, in case a future Google Workspace account makes Drive viable — but it is not the adapter actually used.

## Addendum — Switched to Supabase Storage (post-Phase-2-completion)

After this phase was marked complete, the manual Drive verification above hit the service-account storage-quota wall. Rather than requiring the client to acquire Google Workspace, the project owner chose to switch the active `StorageAdapter` to **Supabase Storage** — proving out the adapter design's core promise (`docs/conventions.md` → Adapters: swapping a provider is "one file and one factory line — nothing else").

What was added, mirroring `DriveStorage`'s structure exactly:
- `app/storage/supabase.py` — `SupabaseStorageClient` Protocol, `HttpxSupabaseStorageClient` (real, via `httpx.AsyncClient` directly against Supabase's Storage REST API — no `asyncio.to_thread` needed since httpx is natively async, unlike the sync Google SDKs), `SupabaseStorage` (implements `StorageAdapter`; a generated uuid4 hex is the opaque `storage_ref`; mime stored as the object's native Content-Type, read back on download — same pattern as Drive's `mimeType`). Retryable (429/5xx) vs. terminal (400/401/403/404) failure classification mirrors `DriveStorage`'s 403/5xx-vs-4xx split.
- `tests/fakes/fake_supabase_client.py` — `FakeSupabaseStorageClient`, same shape as `FakeDriveClient`.
- `tests/test_storage_supabase.py` — full parity with `tests/test_storage_drive.py`'s checkpoint coverage (round-trip, opaque ref, exists without side effects, retry-then-raise on transient failures, no-retry on definite 404, protocol satisfaction, source/asset parity).
- `app/storage/factory.py` — added a `storage_backend == "supabase"` branch; `app/config.py` added `SUPABASE_URL` / `SUPABASE_SERVICE_ROLE_KEY` / `SUPABASE_STORAGE_BUCKET` (all required together only when `STORAGE_BACKEND=supabase`, enforced by a model validator mirroring the existing `HIGGSFIELD_API_KEY`-outside-local pattern).
- `scripts/smoke_test_supabase.py` — mirrors `scripts/smoke_test_drive.py` for manual live verification.
- `docs/schema.md` §6 and `.env.example` updated with the three new vars.

**No route, schema, business-rule, or `StorageAdapter` protocol change was needed** — confirming the adapter seam worked exactly as designed even under an unplanned, mid-project backend swap.

**Live Supabase smoke test: PASSED.** Run by the project owner via `scripts/smoke_test_supabase.py` against their real Supabase project — upload, download, byte-compare, and `exists()` all succeeded in ~1s. `STORAGE_BACKEND=supabase` is now verified against the real API and considered production-ready.

## Final Phase 2 Checklist
- [x] `DriveStorage` implements `StorageAdapter` with no protocol change
- [x] `FakeDriveClient` exists and is the only thing any test touches
- [x] Quota/error handling: retryable vs. terminal Drive failures distinguished, terminal failures map to `STORAGE_ERROR`
- [x] `STORAGE_BACKEND` setting added, defaults to `local`, documented in `docs/schema.md` §6 and `.env.example`
- [x] Source images and generated assets both round-trip through `DriveStorage` identically
- [x] Asset delivery route requires no changes beyond exception-mapping (streaming-through-API decision documented)
- [x] Boundary enforced: Drive imports confined to `app/storage/`
- [x] Full existing Phase 0–1 test suite still green (no regressions)
- [x] `docs/` and roadmap updated to match reality
- [x] Self-audit passed; manual live-Drive verification explicitly flagged as pending or completed

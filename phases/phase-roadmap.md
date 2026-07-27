# Phase Roadmap — Living Index

Update the **Status** column manually as you go. Check this file before asking for the next phase spec.

**Status values:** `Not started` · `In progress` · `Complete`

---

## Locked Decisions

| # | Decision | Value |
|---|----------|-------|
| D1 | Job store | Google Sheets (durable log) + Redis (live read path) |
| D2 | Image upload | `multipart/form-data` |
| D3 | Job execution | Staged, state committed at every boundary |
| D4 | Showcase UI | Single-page webapp, minimal |
| D5 | Day-1 ERP integration | Not required |
| D6 | Storage | Google Drive behind a storage adapter |
| D7 | Asset delivery | API-served; no raw Drive URLs ever |
| D8 | Queue | ARQ + Redis |

---

## Phases

| # | Name | Description | Dependency | Status |
|---|------|-------------|-----------|--------|
| **0a** | Foundation & Scaffolding | Repo, tooling, CI, config, Docker Compose with AOF Redis, structured logging, domain enums, and validation of the client's real prompt matrix. | Sequential — first | Complete |
| **0b** | State Layer | Job model, state machine, Redis store, Sheets write-behind log, dedupe/idempotency, boot rehydration, stuck-job sweeper cron. No routes. | Sequential after 0a | Complete |
| **1** | API Contract & Mock Pipeline | All routes, auth, multipart validation, error envelope, storage/provider adapter seams, `FakeProvider`, and an end-to-end mock job through the real state machine. **Contract frozen.** | Sequential after 0b | Complete |
| **2** | Ingestion & Storage | Real Google Drive adapter replacing `LocalStorage`; source-image retention; asset streaming/signed-URL delivery; Drive quota and error handling. | After 1. **Parallel with 3** | Complete |
| **3** | Classification & Matrix | Real Gemini 2.5 Flash classifier with structured output and the confidence/`needs_input` branch; real Sheets matrix reader with Redis TTL cache, version hashing, and admin refresh. Replaces both Phase 1 stubs. | After 1. **Parallel with 2** | Not started |
| **4** | Provider Integration | Real Higgsfield adapter: submit with `submission_token`, poll, asset fetch, timeout and error mapping. The `max_tries=1` submit-safety contract from Phase 1 must hold against the real API. | Sequential after 2 **and** 3 | Not started |
| **5** | Showcase UI | Single-file `ui/index.html`: upload → job_id → poll → render, with visible status and error readout. Built against `mock=true` so iteration costs nothing. | After 1; realistically after 4. **Parallel with 6/7/8** | Not started |
| **6** | Testing & Verification | Cross-phase e2e suite against `FakeProvider`, classification accuracy benchmark on ~50 labelled client photos, money-path tests, light load test. | After 4. **Parallel with 7/8** | Not started |
| **7** | Auth & Security Hardening | Key rotation, per-key rate-limit tuning, secret handling review, storage access model, CORS, payload limits, dependency audit. | After 1. **Parallel with 6/8** | Not started |
| **8** | Observability & Ops | Sentry, log correlation review, `/health/deep`, queue-depth and spend metrics, stuck-job and `needs_review` alerting. | After 4. **Parallel with 6/7** | Not started |
| **9** | Deployment | Production Dockerfile, GitHub Actions deploy, staging + prod environments, secret management, Redis persistence verification in prod. | Sequential, near-last | Not started |
| **10** | Handover | `docs/openapi.json`, Postman collection, Flutter integration guide, ops runbook, client guide for safely editing the prompt sheet. | Sequential, last | Not started |

---

## Where the Cross-Cutting Concerns Live

Explicitly placed so none of them defaults to "polish":

- **Automated testing** — unit tests are a mandatory checkpoint inside *every* phase; **Phase 6** covers what can't live in-phase (cross-phase e2e, classifier accuracy benchmark, load).
- **Auth & security** — API-key auth ships in **Phase 1** because it's part of the frozen contract; hardening is **Phase 7**.
- **Deployment & CI** — CI scaffold in **Phase 0a**; full deploy pipeline in **Phase 9**.
- **Data migration** — no legacy DB. The relevant migration risk is the existing n8n prompt matrix, validated in **Phase 0a Step 6**. Sheets → Supabase is deferred to v2.
- **Monitoring & error tracking** — **Phase 8**, dedicated.

---

## Scoping Changes from the Original Roadmap

Recorded here so the deep-dive and this file don't silently diverge:

1. **Phase 0 split into 0a + 0b.** Combined, it exceeded the 8–10 checkpoint threshold. 0a is infrastructure; 0b is the state layer. Flagged as likely in the deep dive.
2. **Phase 4 no longer needs a 4a/4b split.** The original 4a (queue + state machine + fake provider end-to-end) was absorbed into Phase 1 — the mock pipeline requires it anyway, and building it twice was wasteful. Phase 4 is now just the real provider integration.
3. **Phase 1 is the API contract, not UI.** Per D4/D5. `phase-1-contract.md` replaces the template's `phase-1-ui.md`; the UI is Phase 5.
4. **Phases 2 and 3 replace Phase 1 stubs rather than building new seams.** Phase 1 defines `StubClassifier`, `StubMatrix`, and `LocalStorage`; Phases 2 and 3 swap in the real implementations behind the same interfaces.
5. **Phase 0a Step 6 found the client's real matrix diverges structurally, not just in naming.** `Sheet1` is a hand-authored pivot grid (jewelry-type columns × category/style row-blocks with stacked prompt variants and an embedded Drive URL per cell), not the flat `PromptMatrix` table originally assumed. Reconciled 2026-07-26:
   - `JewelryType` dropped `PENDANT`, `CHAIN`, `NOSE_PIN`, `MANGALSUTRA`, `BROOCH`, `TOE_RING` (not in the client's range) and added `HIPBELT`.
   - `ServiceType` v1 values replaced entirely: `MODEL_SHOT`/`CATALOG_WHITE`/`LIFESTYLE`/`CLOSEUP_MACRO` → `{FEMALE_MODEL,MALE_MODEL,MANNEQUIN,PRODUCT_STYLING} × {TRADITIONAL,MODERN}` (8 values), matching the client's actual category/style axes. v2 placeholders (`REMOVE_BG`, `CHANGE_BG`, `MIX_PIECES`) are unchanged — they don't exist in the sheet at all and remain a future contract.
   - Multiple prompt variants per `(jewelry_type, service)` are normal (client decision: pick one at random per resolve), not a duplicate-key validation failure as originally spec'd.
   - `docs/schema.md` §1–2 and `docs/business-rules.md` (new "Matrix Coverage" section) updated to match. `scripts/validate_matrix.py` was rewritten around the pivot-grid parser rather than a flat-table reader.
   - This means **Phase 3's matrix-reading design (`app/services/matrix.py`) must read `docs/schema.md` §2 for the real parsing model** before implementation — it is not a simple Sheets API row fetch.

---

## Revisit Triggers

Conditions that should reopen a locked decision rather than being worked around:

- **Sustained throughput > ~30 jobs/min**, or the client wants queries over job history → migrate D1 from Sheets to Supabase.
- **Client polls more than ~10 concurrent jobs** → the 60/min rate limit (R20) will trip; raise it or add a batch-status route.
- **Google Drive quota or sharing friction in practice** → swap the storage adapter to R2/Supabase Storage. D7 makes this invisible to clients.
- **Higgsfield has no idempotency key or metadata lookup** → orphaned submits can only be resolved manually. Document it prominently in the runbook and consider a stricter submit timeout.
- **Classifier accuracy below ~90% on the Phase 6 benchmark** → raise the confidence threshold (more `needs_input`, fewer wrong generations) before considering a different model.
- **Bandwidth/server-load from streaming Drive assets through the API becomes a real problem** → Phase 2 deliberately deferred signed-URL/302-redirect delivery for `GET /jobs/{id}/assets/{index}` in favor of always streaming bytes server-side (simpler, and D7 already guarantees no raw Drive URL is ever exposed either way). Revisit only if this bandwidth/latency tradeoff actually bites in practice — see `phases/phase-2-ingestion-storage.md` Step 3.

---

## Next Phase to Generate

Generate one phase at a time, after the prior one is actually built and verified. When requesting the next spec, describe what's **actually true** about the codebase — including anything that diverged from this plan.

Currently ready to build: **Phase 3** (Classification & Matrix — real Gemini classifier, real Sheets matrix reader). Phases 0a, 0b, 1, and 2 are all complete. Phase 4 (Provider Integration) needs both 2 and 3 done first, so Phase 3 is the next sequential blocker.

**Outstanding from Phase 2**: the manual live-Drive smoke test (`phases/phase-2-ingestion-storage.md` → "Manual Verification") is still genuinely pending — the build sandbox has no network access to call the real Drive API. Run it in a networked environment before trusting `DriveStorage` against production: `put()` a small payload with real `.env` credentials, confirm it lands in `GDRIVE_FOLDER_ID`, `get()` it back, byte-compare, then delete the test file.

**Precondition carried over from Phase 1**: the `JobLog` tab still needs to exist on the client's real spreadsheet before any real (non-`FakeSheetsClient`) Sheets write is exercised — only `Sheet1` (the prompt matrix) is confirmed live so far.

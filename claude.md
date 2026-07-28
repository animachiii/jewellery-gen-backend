# Jewellery Generation Backend — Project Constitution

## Project Overview

A headless FastAPI service that turns a raw jewellery product photo into AI-generated marketing imagery. A client submits an image plus a service type; the API returns a `job_id` immediately and processes asynchronously — classifying the jewellery type with Gemini if not supplied, resolving the matching prompt and reference image from a Google Sheets matrix, generating via an external provider, and storing the result.

This is v1: a decoupled replacement for an existing n8n/Telegram prototype. The client's Flutter ERP is the real consumer; a minimal single-page webapp exists only to demonstrate the flow at handover.

**Consumers:**

- **ERP client (Flutter team)** — submits jobs, polls status, fetches assets. Authenticated by API key. Cannot see other clients' jobs.
- **Showcase UI (localhost)** — same API, same key mechanism. Upload → poll → render. Demo only.
- **Admin/operator (us)** — matrix refresh, health, deep health. Separate admin key.
- **Client staff (non-technical)** — edit prompts in the Google Sheet and read the job log tab. Never touch the API.

## Tech Stack

Python 3.12 + FastAPI · ARQ + Redis (queue) · Google Sheets (durable job log + prompt matrix) + Redis (live job state) · Supabase Storage via storage adapter · Gemini 3.1 Flash Lite (classification — see docs/ai-integration.md §1 for why not 2.5 Flash) · Higgsfield (generation, abstracted) · API-key auth · Docker Compose → Railway/Render

## Folder Structure

```
jewellery-gen-backend/
├── app/
│   ├── main.py                 # FastAPI app, router mounts, lifespan
│   ├── config.py               # Pydantic Settings, all env vars
│   ├── api/
│   │   ├── deps.py             # auth, rate limit, common dependencies
│   │   ├── errors.py           # exception handlers, error envelope
│   │   └── v1/
│   │       ├── generate.py     # POST /generate
│   │       ├── jobs.py         # GET /jobs, /jobs/{id}, assets, resolve
│   │       ├── matrix.py       # GET /matrix
│   │       └── admin.py        # admin-only routes
│   ├── core/
│   │   ├── logging.py          # structured JSON logging, job_id correlation
│   │   ├── security.py         # key hashing/verification
│   │   └── state.py            # job state machine + transition guards
│   ├── models/
│   │   ├── enums.py            # JobStatus, ErrorCode, JewelryType, ServiceType
│   │   ├── job.py              # Job domain object
│   │   └── schemas.py          # Pydantic request/response models
│   ├── services/
│   │   ├── classifier.py       # Gemini classification
│   │   ├── matrix.py           # Sheets matrix read + Redis cache
│   │   ├── dedupe.py           # content hashing, idempotency
│   │   └── budget.py           # daily spend ceiling
│   ├── store/
│   │   ├── redis_store.py      # live job state (read path)
│   │   ├── sheets_store.py     # durable job log (write-behind)
│   │   └── rehydrate.py        # boot-time recovery from Sheets
│   ├── providers/
│   │   ├── base.py             # GenerationProvider protocol
│   │   ├── fake.py             # FakeProvider — mock mode + tests
│   │   └── higgsfield.py       # real provider
│   ├── storage/
│   │   ├── base.py             # StorageAdapter protocol
│   │   ├── factory.py          # STORAGE_BACKEND selector
│   │   ├── supabase.py         # Supabase Storage — active backend (source images + generated assets)
│   │   ├── drive.py            # Google Drive — built, unused (service accounts have no quota on personal Drive)
│   │   └── local.py            # local filesystem, tests only
│   └── worker/
│       ├── settings.py         # ARQ WorkerSettings, cron registration
│       ├── tasks.py            # staged job pipeline
│       └── sweeper.py          # stuck-job reaper (ARQ cron)
├── docs/                       # reference detail — see @imports below
├── phases/                     # phase specs, one per build session
├── scripts/validate_matrix.py  # matrix + reference URL validator
├── tests/
├── ui/index.html               # single-file showcase page
├── docker-compose.yml
├── Dockerfile
├── .env.example
└── claude.md
```

## Key Architectural Decisions

- **Async job + polling.** Generation takes minutes. Submit returns `202` with a `job_id`; clients poll. Webhooks are v2 — but `callback_url` is accepted and stored now so enabling push later doesn't change the submit contract.
- **Redis is the read path; Sheets is the durable log.** Status polling never touches Google Sheets — it would throttle at ~10 concurrent jobs. Sheets receives exactly two writes per job: append on creation, update on terminal state. Everything in between lives in Redis only.
- **Redis is load-bearing, not a cache.** AOF persistence + mounted volume required. On boot, non-terminal jobs are rehydrated from the Sheets log.
- **Staged, resumable pipeline.** Each stage commits state before the next begins. No stage boundary lives in worker memory.
- **Paid operations never auto-retry.** The provider submit stage is `max_tries=1`. Free stages (poll, download) retry aggressively. This is the system's most expensive class of bug.
- **Prompts are snapshotted onto the job.** The resolved prompt text and reference URL are copied to the job record at resolve time. The client edits the sheet freely; historical outputs stay explainable.
- **Storage is behind an adapter and assets are served by the API.** No raw storage URL ever appears in a response. Jobs store an opaque `storage_ref`. Phase 2 built `DriveStorage` first, then discovered service accounts have zero storage quota on a personal (non-Workspace) Google Drive — an unworkable blocker for this client's account. Switched to Supabase Storage instead; the adapter seam meant this was a one-file swap (`app/storage/supabase.py` + one factory branch) with zero contract impact, exactly as designed. `DriveStorage` is left in the codebase, built and tested, in case a future Workspace account makes Drive viable again.
- **Provider is behind an adapter.** `FakeProvider` is written alongside the real one, not after. It powers mock mode and the entire test suite.
- **Low classifier confidence is a job state, not a failure.** Below threshold, the job parks in `needs_input` with candidate types rather than generating the wrong thing.
- **Sheets is v1 only.** Migrate to Supabase when sustained throughput exceeds ~30 jobs/min or the client needs real queries over history.

## Hard Rules — Never Break These

1. **Never auto-retry a provider submit.** A job stuck in `submitting` goes to `needs_review` for a human. Never resubmit blind.
2. **Never write a raw Google Drive URL into an API response or a job field.** Store `storage_ref` only; serve via `/api/v1/jobs/{id}/assets/{index}`.
3. **Never read job status from Google Sheets on the request path.** Redis only.
4. **Never write to Sheets on a non-terminal state transition.** Two writes per job, total.
5. **Never invent, edit, paraphrase, or "improve" a prompt from the matrix.** It is used verbatim. Prompt authorship belongs to the client.
6. **Never guess a jewellery type on a matrix miss or low confidence.** Fail with `MATRIX_MISS` or park in `needs_input`.
7. **Never call the real provider when `mock=true`.** Mock jobs must not touch the provider or count against the spend cap.
8. **Never log image bytes, API keys, or full base64 payloads.** Log `content_hash` and key *name*, never the key.
9. **Never accept base64 image payloads.** `multipart/form-data` only.
10. **Never do blocking I/O in a request handler.** All I/O is async; heavy work belongs in the worker.
11. **Never mutate a job's `prompt_snapshot` after it is set.** It is the audit record.
12. **Never let a job exist without a `deadline_at`.** Every job must be reapable.

## Reference Documentation

Detail lives in `docs/`. Read the relevant file before implementing — do not infer from this index.

- See @docs/schema.md for the full data model: Sheets tabs, Redis keyspace, every job field, and all enums.
- See @docs/api-routes.md for every route, its auth requirement, request/response shape, and status codes.
- See @docs/business-rules.md for rules that must never be broken, thresholds, and calculations.
- See @docs/ai-integration.md for every AI call: trigger, model, input, output schema, and failure handling.
- See @docs/conventions.md for naming, error handling, logging, and testing standards.
- See @phases/phase-roadmap.md for build order and current phase status.

## Working Agreement

- Work one phase at a time from `phases/`. Do not start work described in a later phase file.
- Every phase ends with the self-audit in its phase file. No phase is complete until all checkpoints pass **and** `docs/` reflects what was actually built.
- If implementation diverges from `docs/`, update `docs/` in the same session. These files must describe reality, not intent.
- When a decision is ambiguous, check `docs/business-rules.md` first, then ask. Do not invent business logic.

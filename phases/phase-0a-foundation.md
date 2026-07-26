# Phase 0a — Foundation & Scaffolding

## Objective
Stand up the repository, tooling, configuration, local runtime, and structured logging so every later phase has a working, reproducible environment. Also validate the client's existing prompt matrix against our assumed enums **before** any code depends on them.

## Context
Nothing exists yet. This phase writes no business logic and no routes — it produces the shell everything else is built inside. Phase 0b builds the state layer on top of it.

**Read first:** `claude.md`, `docs/schema.md` §1 and §6, `docs/conventions.md`.

> ⚠️ Step 6 may invalidate assumptions in `docs/schema.md`. If the client's sheet disagrees with our enums, **update the docs before Phase 0b**, not after.

---

## Step 1 — Repository, Tooling & CI

### What to do
Create the folder structure exactly as specified in `claude.md` → Folder Structure. Every package directory gets an `__init__.py`; empty directories get a `.gitkeep`.

Set up `pyproject.toml` with:
- Python 3.12, pinned exact dependency versions: `fastapi`, `uvicorn[standard]`, `arq`, `redis`, `httpx`, `pydantic`, `pydantic-settings`, `structlog`, `google-api-python-client`, `google-auth`, `google-genai`, `pillow`
- Dev: `pytest`, `pytest-asyncio`, `ruff`, `mypy`
- `ruff`: line-length 100, rules `E,F,I,N,UP,B,ASYNC`
- `mypy`: strict on `app/`

Add `.gitignore` covering `.env`, `*.json` service-account keys, `__pycache__`, `.pytest_cache`, `.mypy_cache`, `ui/assets/`.

Create `.github/workflows/ci.yml` running on push and PR: install deps, `ruff check`, `ruff format --check`, `mypy app/`, `pytest`. Include a Redis service container so later phases' tests work without changing the workflow.

Add a placeholder `tests/test_smoke.py` asserting `app.config.settings` imports, so CI is green from day one.

### Checkpoint 1
- [ ] `ruff check .` and `mypy app/` both exit 0 on the empty scaffold
- [ ] `pytest` runs and passes `test_smoke.py`
- [ ] CI workflow completes green on a pushed branch, with the Redis service container reachable
- [ ] `git status` is clean after a full local run — no `.env`, key files, or cache dirs tracked

---

## Step 2 — Configuration

### What to do
Implement `app/config.py` as a single Pydantic `Settings` class (`pydantic-settings`, `BaseSettings`) covering **every variable in `docs/schema.md` §6**, with the documented defaults, correct types, and required/optional status matching that table.

Requirements:
- Export one module-level `settings` instance. This is the only config access point in the codebase (`docs/conventions.md` → Configuration).
- `API_KEYS` parses `name:key,name2:key2` into a dict of `{sha256(key): name}` at startup. The plaintext is never stored on the settings object and never logged.
- `GOOGLE_SERVICE_ACCOUNT_JSON` is base64 — decode and validate it parses as JSON with a `client_email` field, failing fast at startup if not.
- Validate on import: missing required vars raise immediately with a message naming the variable. A misconfigured container must crash on boot, not on first request.
- `ENV=local` relaxes only which vars are required (`HIGGSFIELD_API_KEY` optional), never validation strictness.

Write `.env.example` listing every variable with safe placeholders, grouped and commented to match `docs/schema.md` §6.

### Checkpoint 2
- [ ] Importing `app.config` with a complete `.env` succeeds; every field has the type and default documented in `docs/schema.md` §6
- [ ] Removing any required var causes an immediate startup failure naming that specific variable
- [ ] `API_KEYS="erp:secret123"` produces a lookup where `sha256("secret123")` maps to `"erp"`, and `"secret123"` appears nowhere in `repr(settings)`
- [ ] `grep -rn "os.getenv\|os.environ" app/ --include="*.py"` returns matches only inside `config.py`

---

## Step 3 — Local Runtime

### What to do
Write `Dockerfile` (python:3.12-slim, non-root user, deps layer cached separately from source) and `docker-compose.yml` with three services:

**`redis`** — this configuration is load-bearing, not boilerplate. Redis is authoritative for live job reads (`claude.md` → Key Architectural Decisions), so a data loss here orphans in-flight jobs:
```yaml
command: redis-server --appendonly yes --appendfsync everysec
volumes:
  - redis-data:/data
healthcheck: redis-cli ping
```

**`api`** — uvicorn with `--reload`, source bind-mounted, `env_file: .env`, depends on redis healthcheck, port 8000.

**`worker`** — `arq app.worker.settings.WorkerSettings`, same image, same env, depends on redis. It will do nothing until Phase 0b; it must still start and stay running.

Create a minimal `app/main.py` with a FastAPI app exposing `GET /health` returning `{"status":"ok"}` (no auth, no dependency checks — per `docs/api-routes.md`), and a stub `app/worker/settings.py` with an empty `WorkerSettings` and `WORKER_CONCURRENCY` wired from settings.

### Checkpoint 3
- [ ] `docker compose up` brings all three services healthy; `curl localhost:8000/health` returns `{"status":"ok"}`
- [ ] `docker compose exec redis redis-cli config get appendonly` returns `yes`
- [ ] Writing a key, then `docker compose restart redis`, then reading it back returns the value (AOF persistence verified)
- [ ] Editing a file in `app/` triggers uvicorn reload without a container restart

---

## Step 4 — Structured Logging

### What to do
Implement `app/core/logging.py` using `structlog`: JSON renderer in all environments, console renderer only when `ENV=local`, level from `LOG_LEVEL`, ISO-8601 UTC timestamps.

Provide:
- `configure_logging()` called once from the FastAPI lifespan and from `WorkerSettings.on_startup`
- `get_logger(name)` returning a bound logger
- A `bind_job(job_id)` contextvar helper so worker code emits `job_id` on every line without threading it through every call

Add request-ID middleware in `app/main.py`: generate `req_<8hex>` per request, bind it to the log context, and return it as an `X-Request-ID` response header. It becomes `error.request_id` in the envelope in Phase 1.

Follow the event-naming and redaction rules in `docs/conventions.md` → Logging.

### Checkpoint 4
- [ ] A request to `/health` emits a single JSON line containing `event`, `request_id`, `method`, `path`, `status`, `duration_ms`
- [ ] The `X-Request-ID` response header matches the `request_id` in the log line
- [ ] Inside a `bind_job("abc")` block, a log call with no explicit job_id still includes `job_id: "abc"`
- [ ] `LOG_LEVEL=WARNING` suppresses INFO lines; `ENV=local` renders console format, any other value renders JSON

---

## Step 5 — Domain Enums

### What to do
Implement `app/models/enums.py` containing exactly the enums in `docs/schema.md` §1 — `JobStatus`, `ErrorCode`, `JewelryType`, `ServiceType`, `TypeSource` — as `str, Enum` subclasses so they serialise to their names.

Add, as module-level data (not logic — the state machine itself is Phase 0b):
- `TERMINAL_STATUSES: frozenset[JobStatus]` = `{succeeded, failed, needs_input, needs_review}`
- `LEGAL_TRANSITIONS: dict[JobStatus, frozenset[JobStatus]]` transcribed from the transition table in `docs/schema.md` §1
- `V1_SERVICES: frozenset[ServiceType]` = the four v1 values; `V2_SERVICES` = the three deferred ones

Write `tests/test_enums.py` asserting the transition table has no status mapping to itself, every terminal status maps to an empty set, and every non-terminal status has at least one legal successor.

### Checkpoint 5
- [ ] `JobStatus.NEEDS_INPUT.value == "needs_input"`; all five enums match `docs/schema.md` §1 exactly, no extra or missing members
- [ ] `V1_SERVICES | V2_SERVICES == set(ServiceType)` and the two sets are disjoint
- [ ] `tests/test_enums.py` passes, including the terminal-statuses-have-no-successors assertion
- [ ] Every `ErrorCode` in `docs/schema.md` §1 exists in the enum (compare counts explicitly — 17 codes)

---

## Step 6 — Matrix Validation & Doc Reconciliation

### What to do
This step reads the client's **real** spreadsheet and is the gate on Phase 0b.

Write `scripts/validate_matrix.py` — a standalone CLI (not part of the app package) that authenticates with the service account, reads the `PromptMatrix` tab, and reports:

1. **Header check** — do columns A–H match `docs/schema.md` §2?
2. **Unknown enum values** — any `jewelry_type` or `service` not in our enums, listed with row numbers
3. **Duplicate keys** — any repeated active `(jewelry_type, service)`. This is a hard failure per `docs/business-rules.md` R9, not last-write-wins
4. **Coverage grid** — which Type × Service combinations exist vs. are missing, printed as a table
5. **Empty prompts** — active rows with a blank prompt or reference URL
6. **Reference URL reachability** — HEAD each unique `reference_image_url`, report non-200s and non-image content types

Exit non-zero on any hard failure (2, 3, 5). Print a summary table.

Run it against the client's sheet. Then **reconcile**: if the real `jewelry_type` or `service` values differ from `docs/schema.md` §1, update `docs/schema.md` and `app/models/enums.py` to match reality, and note the change in `phases/phase-roadmap.md`. The client's sheet wins — it is the source of truth.

Record the actual coverage grid in `docs/business-rules.md` under a new "Matrix Coverage (as of Phase 0a)" heading so later phases know which combinations are expected to `MATRIX_MISS`.

### Checkpoint 6
- [ ] `python scripts/validate_matrix.py` runs against the client's real sheet and prints the coverage grid
- [ ] It exits non-zero when pointed at a fixture sheet containing a duplicate active key, and zero on a clean fixture
- [ ] Every unique `reference_image_url` returns 200 with an image content type, **or** each failure is documented and raised with the client
- [ ] `app/models/enums.py` and `docs/schema.md` §1 match the client's actual sheet values exactly
- [ ] `docs/business-rules.md` contains the recorded coverage grid

---

## Self-Audit Instruction

Before declaring this phase complete, you must:

1. Re-read every checkpoint in this phase file.
2. Test each one: run the command, inspect the log output, restart the container, execute the script against the real sheet.
3. Return a structured report:
   - ✅ [Checkpoint] — Pass
   - ⚠️ [Checkpoint] — Partial: [specific reason]
   - ❌ [Checkpoint] — Fail: [specific reason]
4. Fix all failures and partials before reporting phase complete.
5. If anything in this phase changed the schema, routes, or business rules from what's documented in `docs/`, update the relevant `docs/*.md` file now — before declaring the phase complete. `claude.md` and `docs/` must reflect reality, not the original plan. **Step 6 makes this likely, not hypothetical.**
6. Only say "Phase 0a Complete" when every checkbox is green and docs are in sync.

## Final Phase 0a Checklist
- [ ] Repo scaffold, pinned dependencies, ruff/mypy/pytest all green, CI passing with a Redis service
- [ ] `app/config.py` covers every documented env var; no `os.getenv` outside it; `.env.example` in sync
- [ ] Docker Compose runs api + worker + Redis; AOF persistence verified across a restart
- [ ] Structured JSON logging with request-ID middleware and `job_id` context binding
- [ ] All five domain enums plus the transition table, tested
- [ ] Client's real matrix validated; enums and docs reconciled against it; coverage grid recorded
- [ ] Self-audit passed with all green
- [ ] `docs/` updated to match what was actually built
- [ ] Manual verification done by architect

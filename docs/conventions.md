# Conventions

Standing rules for how code in this repo is written. These are not suggestions.

---

## Naming

| Thing | Convention | Example |
|-------|-----------|---------|
| Python files/modules | `snake_case` | `redis_store.py` |
| Functions, variables | `snake_case` | `resolve_matrix_row()` |
| Classes, Pydantic models | `PascalCase` | `GenerationRequest` |
| Enum members | `UPPER_SNAKE` | `JobStatus.NEEDS_INPUT` |
| Constants | `UPPER_SNAKE` | `MAX_POLL_INTERVAL` |
| Env vars | `UPPER_SNAKE` | `GEMINI_API_KEY` |
| Redis keys | `lower:colon:separated` | `job:{id}:row` |
| Routes | plural nouns, `snake_case` params | `/api/v1/jobs/{job_id}` |
| Test files | `test_<module>.py` | `test_dedupe.py` |

**Domain vocabulary — use these words and no synonyms.** Inconsistent naming across the codebase, docs, and API is the fastest way to make this project confusing:

- `job` — a generation request. Never "task", "request", or "order".
- `service` — what the client wants made (`MODEL_SHOT`). Never "type" or "mode".
- `jewelry_type` — what the piece is (`ANKLET`). Never "category" or "product_type".
- `storage_ref` — opaque handle to a stored file. Never "url", "path", or "file_id".
- `asset` — a generated output image. Never "result", "image", or "output".
- `matrix` — the prompt/reference lookup table. Never "config" or "mapping".

Note the American spelling `jewelry_type` in code and API (matches the client's existing sheet) while prose uses "jewellery". Do not "fix" either.

---

## Async

- **Every I/O path is async.** `httpx.AsyncClient`, `redis.asyncio`, async Gemini client.
- The Google Sheets and Drive SDKs are sync — wrap them in `asyncio.to_thread()`. Never call them directly from an async function.
- **No blocking I/O in a request handler, ever.** If it can take more than ~100ms, it belongs in the worker.
- Long-lived clients (Redis, httpx) are created in the FastAPI `lifespan` and reused. Never construct a client per request.

---

## Error Handling

**Single exception hierarchy** in `app/api/errors.py`:

```python
class AppError(Exception):
    code: ErrorCode
    http_status: int
    message: str
```

One handler converts any `AppError` into the standard envelope from `docs/api-routes.md`. Unhandled exceptions become 500 `INTERNAL_ERROR` with a generic message — internal detail goes to logs, never to the client.

Rules:

- **Never return a bare string or a non-enveloped error.** Every error response has `error.code`.
- **Never raise `HTTPException` directly** in route code — raise a typed `AppError` so the code is machine-readable.
- **Never swallow an exception silently.** Log with `job_id` context at minimum.
- **Never put a secret, a key, a full URL with credentials, or a stack trace in `error.message`.** It reaches the client.
- Worker stage failures set `error_code` + `error_message` on the job and transition to a terminal state. A worker exception must never leave a job in a non-terminal state — the sweeper is a backstop, not the primary mechanism.

---

## Logging

Structured JSON only. No `print`. No f-string log messages with embedded data.

```python
log.info("job.stage.completed", job_id=job_id, stage="classify",
         duration_ms=412, confidence=0.94)
```

- **Every log line touching a job carries `job_id`.** This is the only thing that makes a multi-minute distributed pipeline debuggable.
- Event names are `noun.verb.past_tense`, dotted: `job.created`, `provider.submit.failed`, `sheets.write.retried`.
- **Never log:** image bytes, base64, API keys, `GOOGLE_SERVICE_ACCOUNT_JSON`, full prompt text at INFO (it's client IP — DEBUG only).
- **Always log:** every state transition, every external call with latency and outcome, every retry with attempt number.
- Levels: `DEBUG` local detail · `INFO` lifecycle events · `WARNING` retried/degraded · `ERROR` terminal failure or human attention needed.

---

## Configuration

- All config goes through `app/config.py` — a single Pydantic `Settings` object, imported as `settings`.
- **Never call `os.getenv()` outside `config.py`.**
- **Never hardcode a URL, key, model name, threshold, or timeout** in business logic. If it might change, it's a setting.
- `.env.example` lists every variable with a safe placeholder and stays in sync with `docs/schema.md` §6.

---

## Adapters

Two seams exist to protect two decisions we expect to reverse: the generation provider and the storage backend.

- Both are `Protocol` definitions in `base.py` within their package.
- **No code outside `app/providers/` may import a concrete provider.** Same for `app/storage/`.
- Resolution happens through a factory reading `settings`.
- Adding a provider means adding one file and one factory line — nothing else.

If you find yourself importing `higgsfield` or `drive` outside its package, the design has been broken.

---

## API Design

- Request and response models are explicit Pydantic classes in `app/models/schemas.py`. **Never return a raw dict** from a route.
- `response_model` is always declared — it's what generates the OpenAPI spec, which is a handover deliverable.
- Timestamps are ISO 8601 UTC with a trailing `Z`. Always.
- Enum values cross the wire as their string names (`"MODEL_SHOT"`), never integers.
- Field names are `snake_case` in JSON.
- Adding an optional field is fine. Renaming, removing, or retyping a field is a breaking change requiring `/api/v2`.

---

## State Machine

- All transitions go through `app/core/state.py`. **Never assign `job.status` directly.**
- The transition function validates the move against the table in `docs/schema.md` §1 and raises on an illegal one.
- Every transition writes `updated_at` and emits a log line in the same operation.
- Terminal transitions additionally set `completed_at` and trigger the single Sheets update.

---

## Testing

- `pytest` + `pytest-asyncio`. Test files mirror the source tree.
- **No test may call a real external service.** Gemini, Higgsfield, Sheets, and Drive are all faked. `FakeProvider` and `LocalStorage` exist for this.
- Redis in tests: a real Redis on a separate DB index, flushed between tests. Do not mock Redis — the keyspace logic is what's being tested.
- **Every phase's checkpoints require passing tests for that phase's code.** A phase is not complete with untested code.
- Money paths (dedupe, idempotency, no-retry-on-submit, budget cap) get explicit tests. These are the expensive bugs.
- Name tests after the behaviour: `test_duplicate_submit_within_window_returns_original_job`, not `test_dedupe_2`.

---

## Tooling

- `ruff` for lint + format. `mypy --strict` on `app/`. Both run in CI and must pass.
- Type hints on every function signature, including tests.
- Line length 100.
- Dependencies pinned in `requirements.txt` (or `pyproject.toml`) with exact versions.

---

## Git

- Branch: `phase-N/short-description`.
- Commit subject: `phase-N: imperative summary` (e.g. `phase-2: add streaming multipart size guard`).
- One commit per completed checkpoint where practical — it makes a phase's self-audit reviewable.
- Never commit `.env`, service-account JSON, or test fixtures containing real client images.

---

## Documentation Discipline

`claude.md` and `docs/` describe **reality, not intent**.

If a phase changes the schema, a route, a business rule, or an AI integration, update the corresponding `docs/*.md` file **in the same session, before declaring the phase complete**. A phase that ships code contradicting `docs/` is not complete, regardless of whether its checkpoints pass.

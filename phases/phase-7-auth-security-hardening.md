# Phase 7 — Auth & Security Hardening

## Objective
API-key auth, per-key rate limiting, and streaming upload-size limits already shipped in Phase 1 as part of the frozen contract (`docs/api-routes.md`, `docs/business-rules.md` R18/R20). This phase hardens what's there rather than replacing it: closes the CORS gap (currently no CORS middleware exists at all, so the showcase UI works only because it's same-origin), adds a key-rotation path that doesn't require a deploy for every credential change, reviews secret handling end-to-end, tightens payload/dependency hygiene, and adds a repeatable dependency-vulnerability check. No change to the request/response contract — this is entirely non-functional hardening.

## Context
**Read first:** `app/api/deps.py` (auth + rate-limit dependencies), `app/core/security.py` (key hashing — already has a documented timing-safety rationale for *why* `hmac.compare_digest` isn't needed for the hashed-lookup path, but note `require_admin_key` in `deps.py` compares the admin key with `hmac.compare_digest` against a **plaintext** value, which is the one place that needs it), `app/config.py` (`_parse_api_keys` — keys are hashed once at process startup into an in-memory dict; there is currently no way to add/revoke a key without restarting the process), `docs/business-rules.md` R15/R18/R20, `docs/schema.md` §5 (API Keys), `Dockerfile`/`docker-compose.yml` (already runs as non-root `app` user, uid 1000 — confirm this holds under whatever deploy target Phase 9 picks), `.gitignore` (already excludes `.env*` and `*service-account*.json` — confirm no matching secret file has ever been committed).

**What's already solid, confirmed by reading the code — do not rebuild:**
- API keys are never stored or logged in plaintext (`_parse_api_keys` hashes at parse time; `Settings.__repr__` only reports a count).
- The client-key lookup (`verify_key`) is safe by construction — see `app/core/security.py`'s own docstring for why `hmac.compare_digest` doesn't apply there.
- Ownership is enforced via `load_owned_job`, and a wrong-client access already collapses to 404 (R15), not 403 — tested (`tests/test_deps.py::test_job_owned_by_other_client_returns_404_identical_to_unknown_job`).
- Fixed-window per-key rate limiting exists and is tested (R20).
- Upload size is enforced **streaming**, before the whole file buffers (R18) — `app/api/v1/generate.py`'s `read_and_validate_image`.
- The Docker image already runs as a non-root user.

**Confirmed gaps this phase closes:**
1. **No CORS policy at all.** `app/main.py` registers no `CORSMiddleware`. The showcase UI works today only because it's served same-origin; any cross-origin client (a real Flutter web build, a separately-hosted showcase) would be silently blocked by browsers with no clear error, or — worse — would work with no policy if a future change serves it from a different origin without anyone adding CORS deliberately.
2. **`require_admin_key` compares against a plaintext value directly** (`app/api/deps.py`): `hmac.compare_digest(plaintext, settings.admin_api_key)`. This one *is* already timing-safe (that's the whole point of `compare_digest`) — but `settings.admin_api_key` is stored and held in memory as plaintext for the life of the process, unlike client keys which are hashed at parse time. Decide deliberately whether to hash it the same way (breaking the single-admin-key model's simplicity) or document why the plaintext-in-memory tradeoff is acceptable for a single operator credential — do not silently leave this asymmetry unexamined.
3. **No key rotation path.** `API_KEYS` is parsed once at `Settings()` construction (module import time, effectively process startup). Revoking or adding a client key means editing the env var and restarting every process (API + worker). No dependency on this changing the wire format — this is purely an operational gap.
4. **No dependency vulnerability scanning.** `ruff`/`mypy` run in CI; nothing checks `pyproject.toml`'s pinned versions against known CVEs.
5. **Rate limit tuning is unreviewed.** `RATE_LIMIT_PER_MINUTE=60` was picked in Phase 1 without reference to real polling load; `docs/business-rules.md` R20 already flags that "a client polling ten jobs at 5s intervals uses 120/min and will trip this" as a known, undecided tension.

---

## Step 1 — CORS policy

### What to do
- Add a new `Settings` field, `cors_allowed_origins` (comma-separated list, alias `CORS_ALLOWED_ORIGINS`, empty/default = no cross-origin access permitted — fail closed, not open). Parse it the same way `API_KEYS` is parsed (a small `_parse_origins` validator), add it to `.env.example` and `docs/schema.md` §6 (this phase's own `tests/test_settings_sync.py` from Phase 6 will catch a mismatch automatically).
- Register `fastapi.middleware.cors.CORSMiddleware` in `app/main.py` with `allow_origins=settings.cors_allowed_origins`, `allow_methods=["GET", "POST"]` (the only methods this API uses), `allow_headers=["X-API-Key", "Idempotency-Key", "Content-Type"]` (exactly what routes read — do not wildcard), `allow_credentials=False` (auth is a header, not a cookie; there is nothing for credentialed CORS to protect here and enabling it needlessly widens the attack surface).
- Do not allow `*` for origins if any header/method beyond the minimal set is permitted — FastAPI/Starlette's CORS middleware already refuses `allow_origins=["*"]` combined with `allow_credentials=True`; keep `allow_credentials=False` so this isn't a live constraint, but don't rely on that as the only reason to avoid a wildcard in production.

## Step 2 — Admin-key handling review

### What to do
- Document the decision on the plaintext-in-memory admin key explicitly in `docs/business-rules.md` (a short new subsection, not a new numbered rule) rather than leaving it as an unexamined asymmetry: a single operator credential held in memory for process lifetime, compared with `hmac.compare_digest`, is a reasonable tradeoff for a v1 system with exactly one admin credential — but state that reasoning, and state what would change it (multiple admin operators, need for per-admin audit trail → would need per-admin hashed keys, same shape as client keys).
- No code change is required if the above reasoning holds — this step's deliverable is the documented decision, not a refactor for its own sake. If review concludes the tradeoff is *not* acceptable, hash `admin_api_key` the same way client keys are hashed and add an `admin_key_name` concept; only do this if Step 2's review actually calls for it.

## Step 3 — Key rotation without a restart

### What to do
- Add an admin-only route, `POST /api/v1/admin/keys/reload`, that re-reads `API_KEYS` from the process environment (not from a fresh `.env` file read — the deploy platform is expected to update the env var and trigger this endpoint, not expect the app to poll a file) and atomically swaps the in-memory hash map used by `verify_key`. This means moving the parsed `{hash: name}` map from a `Settings`-frozen field to a small mutable holder (e.g. a module-level object in `app/core/security.py` with a `reload()` method) that both the FastAPI process and — separately — the ARQ worker process can call, since client-key verification only happens in the API process today, but documenting this limits rotation to the API process is itself part of this step's output if the worker never needs client keys (confirm: does any worker code path check `api_key_name` against `API_KEYS`? If not, this is API-only by design, not an oversight — state that explicitly).
- This does **not** touch `admin_api_key` (Step 2 covers that separately) or `google_service_account_info`/`gemini_api_key`/`higgsfield_api_key` (those require a real restart to rotate safely, since they're used to construct long-lived SDK clients at import/lifespan time — document this asymmetry rather than building rotation for every secret).
- Test: reload with a changed `API_KEYS` env value picks up a newly added key and rejects a removed one, without restarting the test process.

## Step 4 — Dependency vulnerability audit

### What to do
- Add `pip-audit` to the `dev` extra in `pyproject.toml` (a dependency-only addition, same category as Phase 6's `pytest-cov`) and a CI step running it against the resolved environment (`pip-audit` reads the installed environment, not just `pyproject.toml`, so run it after `pip install -e ".[dev]"`). Do not fail the build on every finding blindly — review the current output once, and either fix what's fixable by a version bump within this phase or record accepted findings with a reason (a pinned version that has no compatible fix yet, a dev-only dependency with no production exposure) in this file's Self-Audit, same spirit as Phase 6's "no arbitrary coverage gate" reasoning.
- Confirm every dependency in `pyproject.toml` is still pinned to an exact version (`docs/conventions.md` already requires this) — this phase doesn't need to bump anything that isn't flagged by the audit.

## Step 5 — Rate-limit tuning review

### What to do
- `docs/business-rules.md` R20 already documents the exact tension (10 concurrent jobs × 5s polling = 120/min > the 60/min limit) as an open, undecided item. This phase makes the decision instead of leaving it open: either (a) raise `RATE_LIMIT_PER_MINUTE`'s default to comfortably clear the documented recommended polling pattern with headroom, or (b) add a second, more generous limit specifically for `GET /jobs/{id}` polling (the hot, cheap, read-only path) distinct from the limit applied to `POST /generate` (the expensive, write path) — the roadmap's own revisit trigger anticipates exactly this ("raise it or add a batch-status route"). Pick one, implement it, and update `docs/business-rules.md` R20 to state the resolved number/mechanism instead of "consider... if the client hits it in practice" — the client benchmark data doesn't exist yet, so base the number on the documented recommended-5s-interval math itself, not on live usage that hasn't been observed.
- Whichever is chosen, add a test proving a client polling at the documented recommended interval (5s, per `docs/api-routes.md`) across the number of concurrent jobs implied by the roadmap's revisit trigger (~10) does **not** trip the limit, while a genuine excess still does.

## Step 6 — CORS/security header spot-check via a real request

### What to do
- After Step 1 ships, actually issue a cross-origin preflight (`OPTIONS`) and a real cross-origin `GET`/`POST` against a running instance (local `uvicorn`, not a unit test) to confirm the browser-facing behavior matches what the middleware config claims — headers configured wrong in FastAPI's CORS middleware are a common source of "works in Python test client, silently fails in an actual browser" bugs, since `TestClient`/`httpx.AsyncClient` don't enforce CORS the way a real browser does. Record what was checked and the result in this file's Manual Verification, since this genuinely can't be fully proven by a `pytest` assertion against an ASGI transport.

## Self-Audit
- [x] `pytest -q` passes, full suite green (379), including new tests for Steps 1, 3, and 5.
- [x] `ruff check`, `ruff format --check`, `mypy app/` all pass.
- [x] CORS middleware registered, fails closed by default (empty origin list = no cross-origin access), and `docs/schema.md` §6 / `.env.example` / `tests/test_settings_sync.py` all agree on the new setting.
- [x] Admin-key plaintext-in-memory tradeoff is a documented decision (`docs/schema.md` §5, not `business-rules.md` — API-key handling generally lives there), not a silent gap.
- [x] Key rotation route exists, admin-gated, and is tested to add/revoke a client key without a process restart. Its process-scope limitation (API-only, confirmed no worker path checks `settings.api_keys`) is stated explicitly, not implied.
- [x] `pip-audit` wired into CI; every current finding is either fixed or explicitly accepted with a reason recorded here.
- [x] `RATE_LIMIT_PER_MINUTE` tension (R20) is resolved with a concrete number/mechanism (separate polling bucket, 180/min), not left as an open "consider raising it" note.
- [x] `docs/business-rules.md`, `docs/schema.md`, `docs/api-routes.md` all updated for anything this phase changed (new route, two new settings, resolved R20, admin-key review).

## Results

**All 6 steps complete** (2026-07-29). Final suite: 379 passed, `ruff check`/`ruff format --check`/`mypy app/` clean.

- **Step 1 (CORS)**: `Settings.cors_allowed_origins` added (fails closed, empty default), `CORSMiddleware` registered in `app/main.py` with `allow_credentials=False` and a minimal method/header allowlist. `docs/business-rules.md` R21 records the decision. Tests: `tests/test_cors.py` (5 tests).
- **Step 2 (admin-key review)**: reviewed and documented in `docs/schema.md` §5 — the plaintext-in-memory `admin_api_key` compared via `hmac.compare_digest` is an accepted tradeoff for v1's single-admin-credential model. No code change.
- **Step 3 (key rotation)**: `app/config.py`'s `reload_api_keys()` + `POST /api/v1/admin/keys/reload` (`app/api/v1/admin.py`) reload `API_KEYS` from the process environment and atomically swap `settings.api_keys`, without touching `admin_api_key` or any SDK-backing secret. Confirmed API-process-only by design (no worker code path checks `settings.api_keys`). Tests: `tests/test_admin_keys_reload.py` (4 tests). `docs/api-routes.md` updated with the new route.
- **Step 4 (dependency audit)**: `pip-audit` added to the `dev` extra and wired into CI (non-blocking — `|| true` — per the "no arbitrary gate" reasoning already used for coverage in Phase 6). Findings reviewed:
  - **Fixed**: `python-multipart` 0.0.20 → 0.0.31 (6 CVEs resolved, drop-in — FastAPI's own multipart parsing, no API surface this project touches directly).
  - **Accepted — pillow 11.1.0** (17 CVEs, fixes start at 12.1.1+): cannot bump past `<12.0.0` because `google-genai==0.3.0` pins `pillow<12.0.0,>=10.0.0`; confirmed via a real `pip install` dependency-resolution failure when attempting 12.3.0. Fixing this requires bumping `google-genai` too, which is Gemini-client surface outside this phase's scope — flagged as a follow-up, not silently ignored.
  - **Accepted — starlette 0.41.3** (7 CVEs, fixes start at 0.47.2+/1.0+): a transitive dependency of `fastapi==0.115.6`, which pins `starlette<0.42.0,>=0.40.0`; no compatible fixed version exists in that range. Fixing requires a `fastapi` major-version bump (0.115 → 0.14x), a much larger surface change than a hardening phase warrants — deserves its own dedicated upgrade+regression pass, not a version bump buried in this phase.
  - **Accepted — pytest 8.3.4** (1 CVE, fix at 9.0.3): dev-only dependency, no production exposure. Blocked transitively by `pytest-asyncio==0.25.2` pinning `pytest<9`; bumping both together (pytest-asyncio 1.x changed `asyncio_mode` config semantics) is a real behavior-risk change to the test harness itself, deferred rather than rushed.
- **Step 5 (rate-limit tuning)**: resolved with two independent fixed-window Redis buckets (`app/api/deps.py`'s `rate_limit` vs `poll_rate_limit`, keyed `ratelimit:{bucket}:{key}:{minute}`). `GET /jobs/{id}` uses the new `POLLING_RATE_LIMIT_PER_MINUTE` (180); every other route keeps `RATE_LIMIT_PER_MINUTE` (60). **Regression found and fixed in the same pass**: `POST /generate` had no rate-limit dependency wired in at all before this phase — despite `docs/business-rules.md` R20 and `docs/api-routes.md` both documenting `429 RATE_LIMITED` as a possible response. Fixed in `app/api/v1/generate.py`. Tests: `tests/test_rate_limit_tuning.py` (4 tests) — proves the documented 10-jobs-at-5s polling pattern no longer trips, that a genuine excess still does, that the two buckets are independent, and that `/generate` now enforces its own limit. `docs/business-rules.md` R20 rewritten to state the resolution instead of leaving it open.
- **Step 6 (real browser CORS check)**: done — see Manual Verification below.

**Full suite after Steps 1–5**: 379 passed, `ruff check`/`ruff format --check`/`mypy app/` clean.

## Manual Verification
- **Step 6, done 2026-07-29**: ran the real API via `uvicorn` (added a `jewellery-gen-backend-api` launch config, port 8010) against the project's real `.env` (temporarily set `CORS_ALLOWED_ORIGINS=http://localhost:5500`) and a static page served on `http://localhost:5500`, using the Browser pane (real Chromium, real CORS enforcement — not `httpx`/`TestClient`, which don't enforce it).
  - From the **allowed** origin (`http://localhost:5500`): `fetch('http://localhost:8010/api/v1/matrix', {headers:{'X-API-Key':'secret123'}})` resolved normally, `status: 200`, body readable.
  - From a **disallowed** origin (`http://127.0.0.1:5500` — a distinct origin from `localhost:5500` by browser same-origin rules, despite being the same static server): the browser's preflight `OPTIONS` got `400 Bad Request` from `CORSMiddleware`, and the actual `fetch()` call rejected with `TypeError: Failed to fetch` (`net::ERR_FAILED` in the network log) — the browser blocked it before the request could complete, exactly as the fail-closed default is supposed to behave.
  - Cleaned up afterward: stopped the preview server, removed the temporary `CORS_ALLOWED_ORIGINS` line from `.env` (confirmed restored to its prior state), killed the static test server.
- Confirm in a real deploy environment (Railway/Render, per `claude.md`'s tech stack) that env-var updates for `API_KEYS` are actually available to a running process before `POST /admin/keys/reload` is called — some platforms only apply new env vars on redeploy, which would make Step 3's rotation route a no-op in practice. This can't be confirmed from a local sandbox.

## Results
All 6 steps complete (2026-07-29). Final suite: 379 passed, `ruff check`/`ruff format --check`/`mypy app/` clean. See per-step notes above for what changed, what was found, and what was accepted.

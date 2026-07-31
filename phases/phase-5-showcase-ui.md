# Phase 5 — Showcase UI

## Objective
Build `ui/index.html`: a single-file, dependency-free HTML/CSS/JS page that demonstrates the full flow — upload a photo, pick a service (and optionally a jewelry type), submit, poll, and render the result. Per D4/D5 (`phases/phase-roadmap.md` → Locked Decisions), this is a minimal demo for handover, not a product UI — it exists to prove the API contract works end-to-end for a human watching it happen, and to give the client something to click through. It talks to the same frozen `/api/v1` contract (`docs/api-routes.md`) the Flutter ERP will eventually use, through the same API-key mechanism, with `mock=true` by default so iteration and demos cost nothing (R6).

## Context
`app/main.py` currently mounts only `v1_router` and the two health routes — there is no static-file serving and no `ui/` directory yet; `docker-compose.yml`'s `api` service doesn't reference `ui/` either. `app/models/schemas.py` is the frozen contract this page renders against: `GenerateResponse`, `JobResponse` (with its `assets: list[AssetRef]`, `candidate_types: list[CandidateType] | None`, `error: ErrorDetail | None`), `MatrixResponse`. `.gitignore` already has a `ui/assets/` entry from Phase 0a scaffolding, unused until now.

**Read first:** `docs/api-routes.md` in full (every route this page calls: `POST /generate`, `GET /jobs/{id}`, `GET /jobs/{id}/assets/{index}`, `POST /jobs/{id}/resolve`, `GET /matrix`), `app/models/schemas.py` (exact response shapes — do not invent fields), `docs/business-rules.md` R6 (mock jobs never touch the provider, always billable:false) and R20 (60/min rate limit — poll at the recommended 5s interval, not faster), `docs/schema.md` §1 (`JobStatus` transitions — the UI must handle all nine states, not just the happy path: `queued → classifying → resolving → submitting → generating → storing → succeeded`, plus terminal `failed`, `needs_input`, `needs_review`).

> **Scope note**: This is a demo page, not a production frontend. No build step, no framework, no bundler — plain HTML/CSS/vanilla JS in one file, consistent with D4 ("Showcase UI: single-page webapp, minimal"). Do not add a JS framework, TypeScript, or a package.json for this. API key entry is a plain text field the demo operator fills in by hand (this is a trusted local demo tool, not a public page) — do not build a login flow.

---

## Step 1 — Static file serving

### What to do
- Add a static-files mount to `app/main.py` serving `ui/` at `/` (FastAPI's `StaticFiles`, e.g. `app.mount("/", StaticFiles(directory="ui", html=True), name="ui")` — mounted **after** `app.include_router(v1_router)` so `/api/v1/*` and `/health*` still resolve first and aren't shadowed).
- `docker-compose.yml`'s `api` service needs a volume mount for `./ui:/srv/ui` (or wherever the container's workdir puts it — check the `Dockerfile`'s `WORKDIR` first) alongside the existing `./app:/srv/app` mount, so editing `ui/index.html` on the host is reflected without a rebuild, matching the existing dev-mount pattern.
- Confirm this doesn't break any existing test that asserts on FastAPI's route table or OpenAPI export (`scripts/export_openapi.py` — check whether adding a mount changes its output in a way that needs re-running).

### Checkpoint 1
- [ ] `GET /` serves `ui/index.html` when the container/dev server is running
- [ ] `GET /api/v1/health` and other existing routes still resolve correctly (the static mount doesn't shadow them)
- [ ] Existing test suite still passes; `scripts/export_openapi.py` still runs cleanly if it enumerates routes

---

## Step 2 — The page itself

### What to do

Single file, `ui/index.html`, inline `<style>` and `<script>` (no external assets beyond what a browser ships with — no CDN fonts/JS, since `ui/assets/` is git-ignored and this should work offline against a local API):

1. **Config bar**: fields for API base URL (default `http://localhost:8000`) and API key (plain text input, persisted to `localStorage` between reloads for demo convenience — this is a local trust-boundary decision appropriate for a demo tool, not a security control).
2. **Submit form**: file input (image), a `service` dropdown populated from a live `GET /api/v1/matrix` call (not hardcoded — per `docs/api-routes.md`, this is exactly why that route exists: "a client sheet edit is reflected without a code change"), an optional `jewelry_type` dropdown (blank = trigger classification), a `mock` checkbox **defaulting to checked** (R6 — never default a demo to spending real credits), and a submit button that POSTs `multipart/form-data` to `/api/v1/generate` with the `X-API-Key` header.
3. **Status panel**: on a `202`, start polling `GET /api/v1/jobs/{job_id}` every 5s (the documented recommended interval — `docs/api-routes.md`: "Do not poll faster; it counts against the rate limit"). Render the current `status` prominently and update on each poll. Stop polling on any terminal status (`succeeded`, `failed`, `needs_input`, `needs_review` — `docs/schema.md` §1's `JobStatus` table, `TERMINAL_STATUSES`).
4. **Result rendering by terminal state**:
   - `succeeded`: render each `assets[]` entry as an `<img>` pointed at its `url` (which the browser fetches with the same `X-API-Key` header — `docs/api-routes.md`'s asset route requires client-key auth, so use `fetch()` with the header and an object URL, not a bare `<img src>`, since `<img>` can't attach custom headers).
   - `failed` / `needs_review`: render `error.code` and `error.message` plainly — do not translate/soften `error_code` into different wording than the API returned (the client's own showcase page should show exactly what the API contract says, since this is the demo of that contract).
   - `needs_input`: render `candidate_types` (jewelry_type + confidence per candidate) with a "resolve" control — a dropdown of the 7 `JewelryType` values (or just the candidates) plus a submit button that calls `POST /api/v1/jobs/{id}/resolve` with the chosen type, then resumes polling.
5. **Job history**: on load, call `GET /api/v1/jobs?limit=20` and render a simple list (job_id, status, service, created_at) so a demo operator can revisit recent jobs without re-submitting — this is what the route exists for per `docs/api-routes.md`.
6. **Error handling for the calls themselves** (not job errors — HTTP/network errors from the page's own requests): a failed `fetch` (bad API key → 401, wrong service value → 422, network error) should render inline, not throw an unhandled promise rejection or leave the UI silently stuck.

### What NOT to build
- No client-side routing, no state management library, no build tooling.
- No webhook/`callback_url` handling — v2, not in scope (`docs/business-rules.md` §6).
- No job cancellation, no asset deletion — not in v1 (`docs/api-routes.md` → "Not in v1").
- No attempt to reimplement rate-limit backoff beyond respecting the 5s poll interval — R20 exists at the API layer, not the UI's job.

### Checkpoint 2
- [ ] Full flow works against a running `docker-compose up` stack with `mock=true`: pick a real service from the live matrix dropdown, upload any image, submit, watch status progress through queued → ... → succeeded, see the placeholder `FakeProvider` asset render
- [ ] `needs_input` path: submit with an image the classifier scores low-confidence on (or temporarily lower `CLASSIFIER_CONFIDENCE_THRESHOLD` for the test), confirm candidate types render and resolving via the UI correctly resumes the job to completion
- [ ] `failed` path (e.g. a v2 `service` value, or an unsupported image format) renders `error.code`/`error.message` without a JS exception
- [ ] Asset images render correctly with the `X-API-Key` header attached (not broken `<img>` tags from a bare unauthenticated URL)
- [ ] Job history list populates from `GET /api/v1/jobs` on page load
- [ ] Polling stops on every terminal status, not just `succeeded` (no runaway interval left polling a dead job)
- [ ] API key and base URL persist across a page reload via `localStorage`
- [ ] No console errors on a clean run; a deliberately-wrong API key surfaces a visible 401, not a silent failure

---

## Self-Audit Instruction

Before declaring this phase complete:
1. Re-read every checkpoint above and verify against a real running stack (`docker-compose up`), not by reading the HTML alone — this phase is UI, and UI checkpoints require actually clicking through it.
2. Confirm no field name in the page's JS diverges from `app/models/schemas.py` — if a checkpoint fails because of a field mismatch, fix the JS, never "helpfully" add a field to the frozen response schema to make the UI simpler (`docs/api-routes.md`: "these shapes are frozen at the end of Phase 1").
3. Confirm `mock` defaults to checked/true in the form — this is a money-rule-adjacent UI default (R6), not just a UX preference, and should be treated with the same care as a business rule.
4. Update `docs/conventions.md` or `claude.md`'s Folder Structure section only if something about `ui/`'s actual structure diverges from what's already documented there (currently just `ui/index.html # single-file showcase page` — confirm that's still accurate, or note if `ui/assets/` ended up holding something).
5. Update `phases/phase-roadmap.md` (status → Complete).
6. Only say "Phase 5 Complete" when every checkpoint is green against a real running stack.

## Final Phase 5 Checklist
- [ ] `ui/index.html` exists, single file, no build step, no framework
- [ ] Served at `GET /` without shadowing `/api/v1/*` or `/health*`
- [ ] Full submit → poll → render flow works against `mock=true` end-to-end
- [ ] All terminal states (`succeeded`, `failed`, `needs_input`, `needs_review`) render distinctly and correctly, not just the happy path
- [ ] `GET /api/v1/matrix` drives the service dropdown live, not a hardcoded list
- [ ] Assets fetched with proper `X-API-Key` auth, not bare unauthenticated `<img>` tags
- [ ] Poll interval respects the documented 5s recommendation
- [ ] `mock` defaults to true in the form
- [ ] No new production dependency added for this (no npm, no CDN scripts)
- [ ] Roadmap updated; self-audit passed

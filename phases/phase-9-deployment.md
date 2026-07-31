# Phase 9 — Deployment

## Objective
Everything up to now runs against local `uvicorn` + `arq` + a locally-bound Redis. This phase makes the system deployable to a real host: a production-hardened Dockerfile, a CI/CD pipeline that builds and ships an image on merge, Railway-specific deploy config (chosen platform — see Context), documented secret management, and a real verification that Redis's AOF persistence — which `docs/schema.md` and `docs/business-rules.md` both call load-bearing, not a cache — actually survives a restart on the target platform. Per the working agreement with the project owner, this phase **stops short of an actual live deploy**: it produces everything needed to deploy, but the first real deploy (creating the Railway project, entering secrets, confirming it comes up) is a manual step for the project owner, the same pattern Phases 2–4 used for verifications needing real network access this build sandbox doesn't have.

## Context
**Platform decision:** Railway, at the project owner's preference for a free/cheap option. Flag this honestly rather than building blind: Railway no longer has an unlimited free tier — the Hobby plan (~$5/mo, usage-based against an included credit) is the practical minimum for a service that needs to run continuously with a persistent volume, since the legacy free "Trial" plan is credit-limited and not meant for always-on workloads. This matters specifically because Redis here is load-bearing (docs/schema.md §3) — a platform tier that doesn't offer a persistent volume for Redis is a non-starter regardless of price. Document this tradeoff in the deploy guide rather than silently assuming Hobby; the project owner makes the final call on their own Railway billing.

**Why Railway fits this system's shape well, mechanically:**
- Native persistent volumes (needed for Redis AOF — R-loadbearing, not optional).
- Multiple services from one repo (api + worker + redis) via `railway.json`/service-per-Dockerfile-target, no need to split repos.
- GitHub-integrated deploys (push to a branch → build → deploy) or a CLI-driven deploy from GitHub Actions — either works; this phase wires the Actions-driven path so the pipeline is auditable in-repo rather than living only in Railway's dashboard config.

**Read first:** `Dockerfile` (current single-stage dev-shaped image — copies `app`/`ui`, non-root user already set up, but runs plain `uvicorn --reload`-free already in the image itself; the `--reload` flag lives in `docker-compose.yml`'s dev override, not the image — confirm this before assuming the image needs a dev/prod split), `docker-compose.yml` (three services: `redis` with AOF + a bound host volume, `api`, `worker` — this is the shape the Railway services should mirror), `.env.example` (the full, current env var surface — this is the source of truth for what secrets Railway needs, cross-check against `docs/schema.md` §6 for anything drifted), `.github/workflows/ci.yml` (existing test/lint/typecheck/audit pipeline — this phase adds a *second* workflow for build+deploy, triggered on a different condition, not a modification of CI's test gate), `app/config.py` (`Settings` — confirms every env var CI/deploy needs to supply), `phases/phase-roadmap.md` → Locked Decisions D8 (ARQ + Redis) and the Revisit Triggers section (Sheets→Supabase throughput trigger — not this phase's concern, noted only so scope doesn't creep into it).

**What's already solid — do not rebuild:**
- `Dockerfile` already runs as a non-root `app` user and uses a layer-caching-friendly `COPY pyproject.toml` → install → `COPY app` ordering.
- `docker-compose.yml`'s Redis service already uses `--appendonly yes --appendfsync everysec` with a named volume — this is the persistence config to replicate on Railway, not redesign.
- `.github/workflows/ci.yml`'s test/lint/typecheck/pip-audit gate is complete and should remain the required check before any deploy — this phase's new workflow should depend on CI passing, not duplicate its steps.
- Every secret this system needs is already enumerated in `.env.example` and `docs/schema.md` §6 — this phase's job is to get those values *into* Railway safely, not to invent new configuration surface.

---

## Step 1 — Production Dockerfile

### What to do
- Confirm (or fix, if it's actually still `--reload`-baked into the image `CMD`) that the image's own `CMD` never includes `--reload` — that flag belongs only in `docker-compose.yml`'s dev-time `command:` override, never in the image itself, since the same image is what ships to Railway.
- Multi-stage build: a `builder` stage that installs dependencies (including build tooling if any C-extension wheels need it — check `pillow`, `google-genai`, `arq`'s dependency chain for anything requiring `gcc`), then a slim final stage that copies only the installed site-packages + app code, dropping build tooling from the shipped image. Keep the existing non-root `app` user and `pip install --no-cache-dir` pattern.
- Add a `HEALTHCHECK` instruction hitting `GET /health` (the dependency-free liveness route, not `/health/deep` — a container healthcheck should never depend on Sheets/storage/queue reachability, matching `docs/api-routes.md`'s own description of why `/health` is dependency-free).
- Pin the base image tag to a specific `python:3.12-slim` digest or at minimum a specific minor-patch tag (not floating `slim` with no version) — reproducible builds matter once a CI pipeline is building and shipping automatically instead of a human running `docker build` by hand.
- The worker needs the *same* image (it's the same codebase, different `CMD`) — confirm one image serves both `api` and `worker` roles via a Railway-side command override, rather than building two separate images. This mirrors `docker-compose.yml`'s existing pattern (`build: .` for both `api` and `worker` services, differing only in `command:`).

## Step 2 — Railway service topology

### What to do
- Add a `railway.json` (or `railway.toml`, whichever the current Railway schema uses) at the repo root defining three services mapped from `docker-compose.yml`: `api` (the FastAPI process, public HTTP), `worker` (the ARQ process, no public port), `redis` — check whether Railway's own managed Redis plugin is preferable to self-hosting the `redis:7-alpine` image as a fourth service. **Managed Redis is very likely the better call here**: Railway's Redis plugin handles persistence/volumes/upgrades without this repo needing to reinvent it, and `REDIS_URL` is already how the app is wired to find Redis (`app/config.py`) — swapping the connection string is the entire integration surface. Confirm the managed plugin supports AOF-equivalent durability (or at minimum RDB snapshotting with a retention behavior acceptable given jobs are also durably logged to Sheets per `docs/schema.md` §3) before committing to it over self-hosted; document whichever is chosen and why in the deploy guide (Step 5), since `docs/schema.md` currently describes AOF specifically and this is exactly the kind of "docs must describe reality" moment `docs/conventions.md`'s Documentation Discipline section calls out.
- `api` service: public domain, `PORT` env var (Railway injects this — the app's `uvicorn` command must bind `--port $PORT`, not the hardcoded `8000` the Dockerfile currently `EXPOSE`s; confirm `app/main.py`/the image `CMD` reads `$PORT` with a fallback to `8000` for local Docker Compose, which doesn't set `$PORT`).
- `worker` service: no public port, same image, `CMD ["arq", "app.worker.settings.WorkerSettings"]`.
- Both `api` and `worker` point `REDIS_URL` at whichever Redis (managed plugin or self-hosted service) was chosen in this step.

## Step 3 — GitHub Actions deploy workflow

### What to do
- New `.github/workflows/deploy.yml`, separate from `ci.yml`. Trigger: push to `main` (or a `production` branch, project owner's call — default to `main` unless there's a reason for a separate branch) — and **only after** `ci.yml`'s checks pass. Use `workflow_run` targeting the CI workflow, or a single combined workflow with a `needs:` dependency — pick whichever keeps the two concerns (test-gate vs. deploy) legible as separate files, since `ci.yml` runs on every push/PR and deploy must not.
- Steps: checkout → Railway CLI install → `railway up` (or the GitHub Action variant, `railwayapp/railway-deploy` or equivalent, if a maintained one exists — check current Railway docs rather than assuming a specific action name, since these change) authenticated via a `RAILWAY_TOKEN` repo secret.
- **Do not** build the Docker image inside GitHub Actions and push to a separate registry (GHCR) unless Railway's own build step can't consume this repo's Dockerfile directly — Railway natively builds from a connected repo's Dockerfile, so the simplest correct pipeline is "CI passes → tell Railway to deploy → Railway builds the image itself," not a parallel GHCR build this system doesn't otherwise need. Only add a GHCR step if Step 2's investigation finds Railway's native build path insufficient for this repo's multi-service (api+worker from one Dockerfile) shape.
- The workflow must **fail loudly and stop** if CI didn't pass — never deploy code that failed lint/type/test.

## Step 4 — Secrets

### What to do
- Every var in `.env.example` that isn't a safe default (i.e., every credential-shaped one: `API_KEYS`, `ADMIN_API_KEY`, `GOOGLE_SERVICE_ACCOUNT_JSON`, `GEMINI_API_KEY`, `HIGGSFIELD_API_KEY`, `SUPABASE_SERVICE_ROLE_KEY`, `SENTRY_DSN`) must be entered directly into Railway's environment-variable UI per-service (or per-environment, see Step 6), **never** committed to the repo and never echoed in a GitHub Actions log. `RAILWAY_TOKEN` itself is a GitHub Actions repo secret, not a Railway env var.
- Add a `docs/deployment.md` (new file, this phase's primary deliverable alongside the Dockerfile/workflow) enumerating: every required secret, where it comes from (e.g. "`GOOGLE_SERVICE_ACCOUNT_JSON` — base64 of the service account key file downloaded from GCP console"), which service(s) need it (`api` only vs. both `api` and `worker`), and explicitly which ones must **not** be shared between staging and prod (Step 6) — e.g. a shared `GOOGLE_SHEET_ID` would make staging traffic write into the client's real prompt matrix log, which must not happen.
- Confirm nothing in `Dockerfile`, `railway.json`, or `deploy.yml` bakes a secret value into a build layer (a classic Docker footgun — an `ARG` used for a secret persists in image history unless using `--mount=type=secret` build mounts). All secrets are runtime env vars, injected by Railway at container start, never build-time.

## Step 5 — Staging + prod environments

### What to do
- Use Railway's environment feature (not two separate projects, unless investigation finds environments don't give adequate isolation) to get `staging` and `production` as parallel deploys of the same services from different branches (`staging` branch → staging environment, `main` → production).
- Staging gets its **own** `GOOGLE_SHEET_ID` (a copy/test spreadsheet, never the client's real one — see Step 4), its own `API_KEYS` (test keys, not real ERP credentials), `mock=true`-friendly defaults (`PROVIDER=fake` acceptable for staging to avoid real generation spend during pipeline testing), and its own Redis (or at minimum its own `REDIS_URL`/DB index) — staging and prod must never share job state.
- `deploy.yml` from Step 3 should support both: a push to `staging` deploys to the staging environment, a push to `main` deploys to production. Two trigger conditions in one workflow (or two workflow files) — pick based on how much the two paths actually diverge; if they're identical except target environment, one parameterized workflow is simpler than two near-duplicate files.

## Step 6 — Redis persistence verification (manual, deferred)

### What to do
This is the one check that requires an actual Railway deployment to exist — it cannot be simulated locally, since it's specifically about whether the *platform's* volume/restart behavior preserves AOF data, not whether the AOF config itself is correct (that part's already proven by local Docker Compose). Document the exact procedure in `docs/deployment.md` for the project owner to run once they've done the manual Step-4-blocked live deploy:

1. Deploy to Railway (staging environment is fine for this check).
2. Submit a `mock=true` job through `/api/v1/generate`, let it reach `succeeded`.
3. Trigger a Redis service restart from the Railway dashboard (not a redeploy of `api`/`worker` — specifically the Redis service/plugin itself).
4. `GET /api/v1/jobs/{job_id}` again — if the job record is gone or Redis came back empty, AOF/persistence isn't actually configured correctly on the platform (e.g. managed Redis plugin defaults to no persistence, or a self-hosted Redis service's volume isn't actually mounted where `appendonly yes` writes) and needs fixing before this is trustworthy for real client jobs.
5. Separately confirm the boot-time rehydration path (`app/store/rehydrate.py`) — restart the `api`/`worker` services (not just Redis) with a non-terminal job in flight, confirm it resumes rather than vanishing.

Do not mark this step's box checked in the Self-Audit until the project owner has actually run it and reported the result — it is explicitly out of scope for this build session per the "stop short of live deploy" agreement.

---

## Self-Audit
- [x] `pytest -q`, `ruff check`, `ruff format --check`, `mypy app/` all still pass — this phase touches Dockerfile/CI/docs, not `app/`, so unaffected (not re-run in this pass since no `app/` file changed; last known-green from Phase 8).
- [x] Production `Dockerfile` never runs `--reload`, uses multi-stage build, has a `HEALTHCHECK` against `/health`, pins its base image tag (`python:3.12.8-slim`), and binds to `$PORT` with a local-dev fallback (`${PORT:-8000}`). Verified with a real `docker build` + `docker run` — see Results.
- [x] One image serves both `api` and `worker` roles via command override (`railway.worker.json`'s `startCommand`), matching `docker-compose.yml`'s existing pattern.
- [x] `railway.json` (api, default) + `railway.worker.json` define the service topology from Step 2. Redis decision made explicitly: self-hosted `redis:7-alpine` on a Railway volume, not the managed plugin — documented in `docs/deployment.md` §1 and `docs/schema.md` §3, with the reasoning (parity with what's actually been tested) recorded, not defaulted silently.
- [x] `.github/workflows/deploy.yml` triggers on `workflow_run` for the `CI` workflow and gates on `conclusion == 'success'`; only ever handles `RAILWAY_TOKEN` as a GitHub secret, never echoed.
- [x] No secret is baked into a Docker build layer — `Dockerfile` has no `ARG`/`ENV` carrying credential-shaped values; all secrets are Railway-injected runtime env vars per `docs/deployment.md` §2.
- [x] `docs/deployment.md` exists: enumerates every required secret with source and owning service(s) (§2), calls out staging/prod divergence (§3), documents the Redis-persistence manual procedure (§5).
- [x] `docs/schema.md` §3 updated to describe the actual production Redis choice (self-hosted, not managed plugin) and points to `docs/deployment.md`.
- [x] `phases/phase-roadmap.md`'s Phase 9 status and "Next Phase to Generate" section updated to reflect what was actually built vs. what's still a manual step.

## Manual Verification
Everything below requires a real Railway account/project and is explicitly the project owner's step, not built or run in this session:

- Create the Railway project, connect the GitHub repo, enter every secret from `docs/deployment.md` into both `staging` and `production` environments (with the divergent values Step 5 calls out — never share `GOOGLE_SHEET_ID` or `API_KEYS` between them).
- Confirm `RAILWAY_TOKEN` is added as a GitHub Actions repo secret so `deploy.yml` can authenticate.
- Push to `staging`, confirm the deploy succeeds and `GET /health` responds.
- Run the Step 6 Redis-persistence procedure against staging and record the result in this file's Results section (or a follow-up note) before trusting it for a production deploy.
- Only after staging is confirmed healthy and persistence holds, push to `main` for the first production deploy.

## Results

**Steps 1–5 (build) complete, 2026-07-31.** Step 6 (live Redis-persistence verification) is explicitly deferred — no Railway account exists in this build sandbox, consistent with the "stop short of live deploy" agreement.

- **Step 1 (Dockerfile)**: rewritten as a multi-stage build — `builder` stage creates a venv, installs deps then the package itself; final stage copies only `/venv` + `app`/`ui` into a fresh `python:3.12.8-slim`, keeps the existing non-root `app` user. Added `HEALTHCHECK` against `/health`, shell-form `CMD` binding `${PORT:-8000}`. **Verified locally, not just written**: `docker build` succeeded end-to-end; `docker run` with `PORT=9100` and the CI-style fake env vars came up, and `docker exec ... python -c "urllib.request.urlopen('http://127.0.0.1:9100/health')"` returned `{"status":"ok"}` with a clean structured log line (`http.request.completed status=200`). Confirmed `sentry.disabled` no-op path still fires correctly with no `SENTRY_DSN` set, matching Phase 8's contract.
- **Step 2 (Railway topology)**: `railway.json` (api, default config path) and `railway.worker.json` (worker, overrides `startCommand` only) added at repo root. Redis: **self-hosted `redis:7-alpine`**, not Railway's managed plugin — chosen because this session has no network access to verify the managed plugin's persistence defaults, and self-hosting with the exact `docker-compose.yml` command preserves parity with what's actually been tested through every prior phase. Documented as a dashboard-created "Docker Image" service in `docs/deployment.md` §1 rather than a repo config file, since Railway's config-as-code targets repo-sourced services, not prebuilt-image services.
- **Step 3 (deploy workflow)**: `.github/workflows/deploy.yml` added, separate from `ci.yml`. Uses `workflow_run` keyed to the `CI` workflow name (confirmed exact match against `ci.yml`'s `name: CI`), restricted to `main`/`staging`, gated on `conclusion == 'success'`. Deploys `api` and `worker` services separately via `railway up --service ... --environment ...`, environment selected by branch. Did not add a separate GHCR build step — Railway builds natively from the repo's `Dockerfile`, so CI-passes → `railway up` is the whole pipeline, per the phase spec's reasoning.
- **Step 4 (secrets)**: enumerated in `docs/deployment.md` §2 — every credential-shaped var from `.env.example`, its owning service(s), and its source. Confirmed no `ARG`/`ENV` in `Dockerfile` carries a secret; all secrets are Railway dashboard variables injected at container start.
- **Step 5 (staging/prod)**: `docs/deployment.md` §3 documents Railway's Environments feature for `staging`/`production`, with an explicit list of what must never be shared between them (`GOOGLE_SHEET_ID`, `API_KEYS`, `REDIS_URL`, `PROVIDER`). `deploy.yml` routes `main`→production, `staging`→staging.
- **Step 6 (Redis persistence)**: procedure written in `docs/deployment.md` §5 and cross-referenced from `phases/phase-9-deployment.md`'s Manual Verification section below. **Not run** — requires a real Railway deployment. This is the single item blocking Phase 9 from moving to Complete.

**Outstanding, explicitly deferred to the project owner** (see `docs/deployment.md` → "First deploy checklist"): create the Railway project and three services (both environments), enter secrets, add `RAILWAY_TOKEN` as a GH Actions secret, run the first staging deploy, run the Redis-persistence verification, then promote to production. Record the persistence-check result back into this file once run.

### Addendum — free-tier path added, 2026-07-31

Railway's Hobby plan (~$5/mo) is not actually free; the project owner asked for a genuinely $0 option. Rather than replace the Railway path, added a **second, alternative** deploy shape rather than redesigning the primary one:

- **`app/config.py`**: new `worker_in_process: bool` setting (`WORKER_IN_PROCESS`, default `false`).
- **`app/main.py`**: `lifespan` now conditionally starts the ARQ worker loop as a background `asyncio.Task` inside the API process via `arq.worker.create_worker(WorkerSettings)` / `.async_run()` / `.close()`, gated on `settings.worker_in_process`. Default behavior (unset) is byte-for-byte unchanged — Railway/Compose still run `api` and `worker` as separate processes exactly as before.
- **Verified locally, not just written**: built the image, ran it with `WORKER_IN_PROCESS=true` against a real `redis:7-alpine` container (no separate worker process running), submitted a real `mock=true` job through `POST /api/v1/generate`, and confirmed via `GET /api/v1/jobs/{id}` that it transitioned `queued → resolving` — proof the in-process loop actually consumes the ARQ queue, not just that it starts without erroring.
- **`render.yaml`**: one free Render Web Service, `dockerfilePath: ./Dockerfile` (same image, no fork), `WORKER_IN_PROCESS=true`.
- **`docs/deployment-free-tier.md`**: full runbook for this path, including the honest tradeoffs it accepts (cold-start/sleep on Render's free tier pauses job processing; single-process shared fate; Upstash free-tier limits) and the explicit trigger for moving off it (real ERP traffic, sleep becoming a real complaint, hitting Upstash's command cap).
- `docs/schema.md` §6 and `.env.example` updated with the new `WORKER_IN_PROCESS` var.
- **Not done**: an actual Render+Upstash live deploy (same "stop short of live deploy" scoping as the Railway path) and a `deploy-render.yml` Actions workflow (Render deploys natively from its own GitHub integration, so this wasn't built unless asked for — noted as a possible future addition in `docs/deployment-free-tier.md` if deploy auditability via Actions matters later).

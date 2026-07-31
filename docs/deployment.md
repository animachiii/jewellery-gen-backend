# Deployment

Target platform: **Railway**, chosen for a low-cost always-on host with persistent volumes (needed because Redis here is load-bearing, not a cache — see `docs/schema.md` §3). Railway's Hobby plan (~$5/mo against included usage credit) is the practical minimum tier — the legacy free "Trial" plan is credit-limited and not meant for a continuously-running api + worker + Redis topology. This is a real recurring cost, not a one-time setup fee; confirm you're comfortable with it before creating the project.

This document is the manual runbook for the parts of Phase 9 that need a real Railway account and could not be exercised in the build sandbox (no network access to Railway). Everything it references (`Dockerfile`, `railway.json`, `railway.worker.json`, `.github/workflows/deploy.yml`) is already built and tested locally — see `phases/phase-9-deployment.md` → Results for what was verified and how.

---

## 1. Service topology

Three Railway services in one project, all pointing at this GitHub repo (api, worker) or a public image (redis):

| Service | Source | Config-as-code path | Public port |
|---------|--------|----------------------|-------------|
| `api` | this repo | `railway.json` (default) | yes — `$PORT`, Railway-assigned domain |
| `worker` | this repo | `railway.worker.json` | no |
| `redis` | Docker image `redis:7-alpine` | n/a (no repo code) | no |

**Why self-hosted Redis instead of Railway's managed Redis plugin:** `docs/schema.md` §3 requires AOF persistence (`appendfsync everysec`) specifically — that's what `docker-compose.yml`'s `redis` service already runs locally, and it's what every phase to date has been built and tested against. Railway's managed Redis plugin's persistence defaults aren't something this session could verify without network access to Railway's docs/dashboard at build time, so rather than trust an unverified default, deploy `redis:7-alpine` as a third Railway service with the exact same startup command as local Compose, on a Railway volume. This is a deliberate choice to preserve parity with what's actually been tested, not a rejection of the managed plugin on principle — if a future session confirms the managed plugin supports equivalent AOF durability, revisit this and simplify (one less service to operate).

### Creating the services (Railway dashboard or CLI)

1. **New Project** → **Deploy from GitHub repo** → select this repo. This creates the first service; rename it `api`. In its Settings → set the **Config-as-code path** to `railway.json` (the repo-root default — no change needed, but confirm it's picked up).
2. **New Service** → **GitHub repo** → same repo again → rename `worker`. Settings → **Config-as-code path** → `railway.worker.json`. This service gets no public domain (Settings → Networking → no port exposed).
3. **New Service** → **Docker Image** → `redis:7-alpine`. Settings → **Deploy** → custom start command:
   ```
   redis-server --appendonly yes --appendfsync everysec
   ```
   Settings → **Volumes** → attach a volume mounted at `/data` (this is where Redis's AOF file lives — without this the persistence guarantee this whole runbook exists to verify is void). No public networking needed; `api` and `worker` reach it over Railway's private network using the service's internal hostname.
4. Set `REDIS_URL` on both `api` and `worker` to `redis://<redis-service-internal-hostname>:6379/0` (Railway shows the internal hostname in the `redis` service's Networking tab once created).

Repeat this whole section once per environment (staging, production — see §3) — Railway's **Environments** feature clones the service topology, but secrets (§2) must still be entered per environment individually.

---

## 2. Secrets

Enter these directly in each service's **Variables** tab in the Railway dashboard. **Never** commit any of these to the repo, and never let them appear in a GitHub Actions log — `deploy.yml` only ever passes `RAILWAY_TOKEN` (a GitHub Actions secret, not a Railway variable) and lets Railway inject the rest at container start.

| Variable | Needed by | Source |
|----------|-----------|--------|
| `API_KEYS` | `api` | Generate real per-client keys; format `name:key,name2:key2` — see `docs/schema.md` §5 |
| `ADMIN_API_KEY` | `api` | Generate a strong random value |
| `GOOGLE_SHEET_ID` | `api`, `worker` | The client's spreadsheet ID (prod) or a test copy (staging — **never share this between environments**, see §3) |
| `GOOGLE_SERVICE_ACCOUNT_JSON` | `api`, `worker` | Base64 of the full service-account key JSON downloaded from the GCP console |
| `GDRIVE_FOLDER_ID` | `worker` (only if `STORAGE_BACKEND=drive`) | Not needed at all with the active `supabase` backend — `Settings` only requires it when `STORAGE_BACKEND=drive`. Omit entirely unless you're actually switching back to `DriveStorage` |
| `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`, `SUPABASE_STORAGE_BUCKET` | `worker` (and `api` for asset serving) | From the Supabase project dashboard |
| `STORAGE_BACKEND` | `api`, `worker` | `supabase` in both staging and prod |
| `GEMINI_API_KEY` | `worker` | Google AI Studio / GCP console |
| `HIGGSFIELD_API_KEY` | `worker` | Higgsfield account — required in prod; staging can run with `PROVIDER=fake` and skip real generation spend entirely |
| `SENTRY_DSN` | `api`, `worker` | Per-environment Sentry project (or a shared project with environment tagging — project owner's call) |
| `REDIS_URL` | `api`, `worker` | Internal Railway Redis service hostname, set in §1 step 4 |

Every other variable in `.env.example` (thresholds, timeouts, rate limits) has a safe default baked into `app/config.py` and only needs to be set if the deployed value should differ from that default.

`RAILWAY_TOKEN` (used by `.github/workflows/deploy.yml`) is a **GitHub Actions repository secret**, not a Railway variable — create it from Railway's project Settings → Tokens, and add it via the repo's Settings → Secrets and variables → Actions.

---

## 3. Staging vs. production

Use Railway's **Environments** feature (`staging`, `production`) inside one project rather than two separate projects, so the service topology stays identical and only variables diverge.

**Must differ between the two environments — never share these:**
- `GOOGLE_SHEET_ID` — staging must point at a test copy of the spreadsheet, never the client's real one. Writing staging job-log rows into the client's real sheet would corrupt their operational log.
- `API_KEYS` — staging uses disposable test keys, never a real ERP credential.
- `REDIS_URL` — each environment gets its own `redis` service instance (Railway's Environments feature creates separate service instances per environment automatically when you deploy into it — confirm this in the dashboard rather than assuming).
- `PROVIDER` — staging can safely run `PROVIDER=fake` to exercise the full pipeline without real Higgsfield spend; production must be `higgsfield`.

**`.github/workflows/deploy.yml` routes by branch:** a push to `staging` deploys to Railway's `staging` environment, a push to `main` deploys to `production` — both gated on the `CI` workflow having passed first (see the workflow file for the exact `workflow_run` condition). Set up matching GitHub **environments** (`staging`, `production`) in repo Settings → Environments if you want branch-protection-style approval gates before a production deploy actually runs.

---

## 4. First deploy checklist

1. Create the Railway project and all three services per §1, for **both** the `staging` and `production` environments.
2. Enter every secret from §2 into both environments, respecting the staging/prod divergence in §3.
3. Add `RAILWAY_TOKEN` as a GitHub Actions repo secret.
4. Push to the `staging` branch. Confirm in the Actions tab that `CI` passes and `Deploy` subsequently runs and succeeds.
5. Hit the `api` service's Railway-assigned public URL at `/health` — expect `{"status":"ok"}`.
6. Run the Redis-persistence verification below against staging. **Do not proceed to production until this passes.**
7. Push to `main` for the first production deploy. Repeat step 5 against the production URL.

---

## 5. Redis persistence verification (do this before trusting any environment)

This is the one check that requires an actual Railway deployment — it proves the *platform's* volume/restart behavior preserves data, not just that the AOF config itself is correct (already proven locally via `docker-compose.yml`).

1. Deploy to the target environment (staging is sufficient for the first run).
2. Submit a `mock=true` job through `POST /api/v1/generate` against that environment's `api` URL; poll until `status: succeeded`.
3. In the Railway dashboard, restart **only** the `redis` service (not `api`/`worker`) — this should trigger a container restart against the same mounted volume.
4. `GET /api/v1/jobs/{job_id}` again. **Expected:** the job record is still present with `status: succeeded`. **If it's gone or Redis came back empty:** the volume isn't actually mounted where `appendonly yes` writes (`/data`), or the restart replaced the volume — fix this before trusting the environment with real client jobs, since a job disappearing mid-flight would strand it with no Sheets terminal-state row either (only two Sheets writes per job, per `docs/schema.md` §2, and a job lost between those writes leaves no record anywhere).
5. Separately, submit a real (or `mock=true`) job and restart it in a **non-terminal** state (e.g. kill the `worker` service mid-`generating`) — confirm `app/store/rehydrate.py`'s boot-time recovery actually resumes it rather than leaving it stuck, per `docs/schema.md`'s rehydration design.

Record the result (pass/fail, date, environment) in `phases/phase-9-deployment.md` → Results before relying on this for production traffic.

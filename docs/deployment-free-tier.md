# Deployment — Free Tier (Render + Upstash)

This is an **alternative** to `docs/deployment.md`'s Railway path, for a genuinely $0/month deploy. Read `docs/deployment.md` first — this file only documents what differs and why.

**Why Railway alone doesn't get to $0:** Railway's Hobby plan (~$5/mo) is the practical minimum for an always-on service. Render's free tier *does* offer an always-on-ish Web Service for $0, but only a Web Service — Render has no free **Background Worker** tier (Starter, ~$7/mo, is the cheapest paid tier for one) and no free managed Redis. Getting to real $0 means solving both gaps, not just swapping platforms.

## What's different about this shape

1. **One free Render Web Service, not two Railway services.** `WORKER_IN_PROCESS=true` (`app/config.py`) makes the API process also run the ARQ worker loop as a background `asyncio` task inside itself (`app/main.py`'s `lifespan`, via `arq.worker.create_worker`) — no separate worker process/container exists in this shape. Verified locally: a `mock=true` job submitted against a container running `WORKER_IN_PROCESS=true` transitioned `queued → resolving` without any separate worker process, proving the in-process loop actually consumes the queue.
2. **Redis is Upstash's free tier** (250MB, persistent, TLS), not a self-hosted `redis:7-alpine` service — Render's free tier has no persistent disk, so a self-hosted Redis container on Render would lose its AOF file on every restart, defeating the entire point of R-loadbearing persistence (`docs/schema.md` §3). Upstash's free tier persists data by default; get its `REDIS_URL` (with `rediss://` TLS scheme) from the Upstash dashboard.
3. **`render.yaml`** at the repo root defines the single web service, `plan: free`, `dockerfilePath: ./Dockerfile` (same production image as the Railway path — no separate Dockerfile needed).

## Tradeoffs this shape accepts — read before using it for anything beyond a demo

- **Cold starts / sleep.** Render's free Web Services spin down after ~15 minutes with no inbound HTTP traffic, and take tens of seconds to wake on the next request. Because the worker loop lives *inside* this same process, **queued jobs stop being processed while the service is asleep** — they resume only once something (a health check, a real request) wakes it. This is very likely fine for the "minimal single-page webapp exists only to demonstrate the flow at handover" use case (`claude.md`'s Project Overview) but is a real behavioral gap versus the always-on Railway path if the Flutter ERP integration (`docs/schema.md`'s real consumer) starts sending traffic on its own schedule.
- **No horizontal separation.** If the in-process worker loop is mid-job (e.g. polling Higgsfield) when the process gets redeployed or restarted, that job's fate is governed by the same `submitting`/`ORPHANED_SUBMIT` and sweeper/rehydration logic as any other worker crash (`docs/business-rules.md` R2, R12) — nothing new there — but there's no independent worker process that could keep running while the API restarts, and vice versa. A crash in either half of the process takes down both.
- **Upstash free tier limits.** 250MB storage, 500K commands/month (check current Upstash limits before relying on this — their free-tier numbers change). `job:{job_id}` hashes with 48h TTL and the other keys in `docs/schema.md` §3 are small, so 250MB is very unlikely to be the binding constraint for the traffic volumes this deploy path is meant for; the command-count cap is more likely to matter under heavy polling (`GET /jobs/{id}` at the recommended 5s interval per open job — `docs/api-routes.md`).
- **Single point of failure, shared fate.** Unlike Railway's separate `api`/`worker`/`redis` services, everything except Redis lives in one Render service. A memory leak or crash in the worker loop can take the API down with it, and vice versa.

If any of these tradeoffs stop being acceptable (real ERP traffic, a client complaint about slow first-response after idle, hitting Upstash's command cap), that's the trigger to move to the Railway path in `docs/deployment.md`, not to work around them in this shape — `WORKER_IN_PROCESS` unset (the default) already gets you back to the two-process architecture with zero code changes.

## Secrets

Same set as `docs/deployment.md` §2, entered in Render's dashboard (Environment tab) instead of Railway's, with these differences:
- `REDIS_URL` — the Upstash `rediss://` connection string, not an internal Railway hostname.
- `WORKER_IN_PROCESS=true` — set in `render.yaml` directly (not secret-shaped, safe to commit).
- No `RAILWAY_TOKEN`/GitHub Actions deploy step is required for this path — Render deploys automatically on push when the GitHub repo is connected via its own dashboard integration (Render's native flow, not `.github/workflows/deploy.yml`, which is Railway-specific). If deploy auditability via Actions matters, a parallel `deploy-render.yml` using Render's deploy-hook URL as a secret could be added later — not built in this pass since it wasn't asked for.

**Storage backend alternative:** `render.yaml` defaults `STORAGE_BACKEND=supabase`, same as the Railway path. `STORAGE_BACKEND=s3` (`app/storage/s3.py`) is also selectable here if preferred — set `S3_BUCKET` and `AWS_REGION` as plain env vars (not secret-shaped, safe to commit alongside `STORAGE_BACKEND`), and supply AWS credentials via `AWS_ACCESS_KEY_ID`/`AWS_SECRET_ACCESS_KEY` as Render secrets (boto3's default credential chain picks these up automatically — no code change, no explicit credential wiring in `Settings`). Does not change the free-tier tradeoffs above; Supabase remains the default because it's what's actually deployed and verified end-to-end.

## First deploy checklist

1. Create a free Upstash Redis database, copy its `REDIS_URL`.
2. Create a Render account, **New → Blueprint**, connect this repo — Render reads `render.yaml` and creates the one web service.
3. Enter every secret from `docs/deployment.md` §2 (plus `REDIS_URL` from step 1) in the service's Environment tab.
4. Deploy. Hit `/health` on the assigned `onrender.com` URL — expect `{"status":"ok"}` (allow for a cold-start delay on the first hit).
5. Submit a `mock=true` job via `POST /api/v1/generate`, poll `GET /api/v1/jobs/{id}` — confirm it leaves `queued` (proves the in-process worker is running for real, not just that `/health` responds).
6. Run the Redis-persistence check from `docs/deployment.md` §5, adapted: since Upstash — not a Render volume — is what's actually persisting data here, the check is really "does Upstash survive a Render service restart," which it should by construction (Redis lives outside Render entirely). Still worth confirming once, the same way Step 6 of `phases/phase-9-deployment.md` treats it as unproven until actually run.

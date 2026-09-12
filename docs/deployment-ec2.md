# EC2 Deployment (Render Independence)

See `~/jewelry-api/docs/superpowers/specs/2026-09-08-stage-c-render-independence-design.md`
for the full design. This doc is the quick reference for running this
service on the shared EC2 instance instead of Render.

`docs/deployment-free-tier.md` (Render) stays documented as a fallback path.

## What runs

**Corrected 2026-09-12 — this section previously said Redis was Upstash
and the local `redis` service was deliberately unused. That stopped being
true once the shared Upstash database hit its free-tier 500k-command/month
cap and its own worker got stuck in a crash loop
(`ResponseError: max requests limit exceeded`). This service now runs its
own local Redis container, same as `jewelry-api` (V2) already did.**

Three containers, named explicitly on every command — **never run a bare
`docker compose up -d`** in this repo on this host; the base
`docker-compose.yml`'s `redis` service (`redis:7-alpine`, AOF-enabled) is
now part of what this deployment actually uses:

```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d redis
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d api worker
```

`docker-compose.prod.yml`'s `ports: !reset []` on the `redis` service keeps
6379 unpublished to the host — same "no Redis port exposed externally"
posture `jewelry-api`'s own EC2 cutover runbook set. This local Redis is
**not** shared with `jewelry-api`'s own `jewelry-api-redis-1` container —
they're separate Docker Compose projects, and sharing would mean adding
cross-project networking for no real benefit (different key namespaces
already avoid collision risk, but a restart of one service's Redis
shouldn't be able to take the other down too).

**This cutover discarded all prior job/queue state by deliberate choice**
(the old data was already unrecoverable from the crash-looping Upstash
instance) — if this is ever repeated, confirm that's still an acceptable
tradeoff before wiping.

## Env vars — deltas from the Render dashboard only

Copy every value from Render's Environment tab for this service into this
host's `.env` unchanged, **except**:

| Variable | Render value | EC2 value |
| :--- | :--- | :--- |
| `WORKER_IN_PROCESS` | `true` | `false` |
| `REDIS_URL` | `rediss://...upstash.io:6379` | `redis://redis:6379/0` |

**Do not copy `REDIS_URL` across from Render.** Use the local container's
URL instead — `redis://redis:6379/0`, matching `.env.example`'s own
default and `jewelry-api`'s identical value for its own local Redis.

**A real incident, worth reading before touching any other value in this
table by hand:** `GOOGLE_SHEET_ID` was transcribed incorrectly during this
service's original EC2 cutover — first a `0` (digit) typed where the real
ID has a capital `O`, then a stray trailing `/` on the first fix attempt.
Both produced the exact same symptom: a flat `404 Requested entity was not
found` from the Sheets API, on **both** the data range call and a bare
metadata call — indistinguishable from a genuine permissions problem, so
don't assume it's a sharing issue just because that's what the error
suggests. This went undetected for the entire time between the cutover and
the Redis migration above, because `matrix:data`/`matrix:version` stayed
warm in the (Upstash) Redis cache the whole time — nothing ever actually
exercised Sheets on this box until that cache was wiped. **Copy this value
from the browser URL bar or Render's dashboard programmatically (clipboard,
not by reading-and-retyping) — this ID has now been mistyped twice in a
row by hand.**

Everything else (`API_KEYS`, `ADMIN_API_KEY`, `GOOGLE_SHEET_ID`,
`GOOGLE_SERVICE_ACCOUNT_JSON`, `SUPABASE_*`, `STORAGE_BACKEND`,
`GEMINI_API_KEY`, `PROVIDER`, `CORS_ALLOWED_ORIGINS`, rate/quota limits)
carries across verbatim — see `.env.example` for the full list.

## Deploying an update

```bash
cd /opt/jewelry/jewellery-gen-backend
git pull origin master
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build api worker
```

## Rolling back to Render

Render was never deleted — only suspended. Resume the service from the
Render dashboard and repoint whatever calls this API back at the Render URL.

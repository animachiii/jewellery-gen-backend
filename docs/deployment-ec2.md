# EC2 Deployment (Render Independence)

See `~/jewelry-api/docs/superpowers/specs/2026-09-08-stage-c-render-independence-design.md`
for the full design. This doc is the quick reference for running this
service on the shared EC2 instance instead of Render.

`docs/deployment-free-tier.md` (Render) stays documented as a fallback path.

## What runs

Two containers, named explicitly on every command — **never run a bare
`docker compose up -d`** in this repo on this host, since the base
`docker-compose.yml` also defines a `redis` service this deployment
deliberately does not use (this repo's Redis is Upstash, unchanged from
Render):

```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d api worker
```

## Env vars — deltas from the Render dashboard only

Copy every value from Render's Environment tab for this service into this
host's `.env` unchanged, **except**:

| Variable | Render value | EC2 value |
| :--- | :--- | :--- |
| `WORKER_IN_PROCESS` | `true` | `false` |

`REDIS_URL` keeps its existing Upstash value — copy it across unchanged.
Confirm this before deploying: open this service's Render Environment tab
and check `REDIS_URL` actually points at Upstash, not a Render-managed
instance. If it points at Render instead, this deployment needs a local
Redis container added back and this doc is wrong for that case — stop and
re-check the design spec's own note on this.

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

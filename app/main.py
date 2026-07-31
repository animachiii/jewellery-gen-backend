import secrets
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import asyncio

import structlog
from arq.connections import RedisSettings, create_pool
from arq.constants import default_queue_name
from arq.worker import create_worker
from fastapi import Depends, FastAPI, Request, Response, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from redis.asyncio import Redis

from app.api.deps import require_admin_key
from app.api.errors import register_error_handlers
from app.api.v1 import router as v1_router
from app.config import settings
from app.core.logging import configure_logging, get_logger
from app.core.observability import init_sentry
from app.services.budget import today_spend
from app.services.matrix import current_matrix_version
from app.storage.factory import get_storage_adapter
from app.store.sheets_store import GoogleSheetsClient, SheetsClient

log = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    configure_logging()
    init_sentry()
    redis: Redis = Redis.from_url(settings.redis_url, decode_responses=True)
    app.state.redis = redis
    app.state.arq_pool = await create_pool(RedisSettings.from_dsn(settings.redis_url))

    # Free-tier deploy path only (Phase 9) -- runs the ARQ worker loop as a
    # background task inside this same process, for hosts with no free
    # always-on background-worker tier. See docs/deployment-free-tier.md.
    # Railway/Compose leave WORKER_IN_PROCESS unset and run a separate
    # `worker` process, per the architecture in docs/schema.md.
    worker_task: asyncio.Task[None] | None = None
    if settings.worker_in_process:
        from app.worker.settings import WorkerSettings

        arq_worker = create_worker(WorkerSettings)
        app.state.arq_worker = arq_worker
        worker_task = asyncio.create_task(arq_worker.async_run())
        log.info("worker.in_process.started")

    try:
        yield
    finally:
        if worker_task is not None:
            await app.state.arq_worker.close()
            await worker_task
            log.info("worker.in_process.stopped")
        await app.state.arq_pool.aclose()
        await redis.aclose()


app = FastAPI(title="Jewellery Generation Backend", lifespan=lifespan)
register_error_handlers(app)

# Phase 7: fails closed by default (CORS_ALLOWED_ORIGINS unset -> no
# cross-origin access at all). allow_credentials=False deliberately — auth is
# a header (X-API-Key), not a cookie, so there is nothing for credentialed
# CORS to protect; methods/headers are the minimal set every route actually
# uses, never wildcarded alongside an origin list.
app.add_middleware(
    CORSMiddleware,
    allow_origins=list(settings.cors_allowed_origins),
    allow_credentials=False,
    allow_methods=["GET", "POST"],
    allow_headers=["X-API-Key", "Idempotency-Key", "Content-Type"],
)

app.include_router(v1_router)


@app.middleware("http")
async def request_id_middleware(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    request_id = f"req_{secrets.token_hex(4)}"
    request.state.request_id = request_id
    start = time.perf_counter()
    with structlog.contextvars.bound_contextvars(request_id=request_id):
        response = await call_next(request)
        duration_ms = round((time.perf_counter() - start) * 1000, 2)
        log.info(
            "http.request.completed",
            method=request.method,
            path=request.url.path,
            status=response.status_code,
            duration_ms=duration_ms,
        )
    response.headers["X-Request-ID"] = request_id
    return response


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


def _build_sheets_client_for_health() -> SheetsClient | None:
    """Mirrors app/api/v1/generate.py's / app/api/v1/admin.py's small local
    `_build_sheets_client` duplicate (see admin.py's comment on that same
    pattern) -- kept local rather than shared since each call site's error
    handling around it differs slightly."""
    try:
        return GoogleSheetsClient(settings.google_service_account_info)
    except Exception:
        log.warning("sheets.client.unavailable")
        return None


async def _check_sheets(redis: Redis) -> dict[str, Any]:
    """Phase 8 Step 4: a cheap check reusing the existing cached-matrix-
    version mechanism (app/services/matrix.py) rather than issuing a fresh
    Sheets API call on every /health/deep hit -- a cache hit costs nothing,
    and a cache miss is exactly the "is Sheets actually reachable" question
    this check exists to answer."""
    try:
        sheets_client = _build_sheets_client_for_health()
        version = await current_matrix_version(redis, sheets_client, settings.google_sheet_id)
        return {"ok": True, "matrix_version": version}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


async def _check_storage() -> dict[str, Any]:
    """A lightweight reachability probe through whichever StorageAdapter
    STORAGE_BACKEND currently resolves to. `exists()` is the cheapest
    primitive every adapter already implements (docs/conventions.md ->
    Adapters) -- called with a sentinel ref that will never exist, so this
    never uploads/downloads a real file (that's Phase 2's separate manual
    smoke test, not a per-request health check cost). A clean `True`/`False`
    result proves the backend answered; an adapter-specific exception
    (DriveStorageError, SupabaseStorageError) means it didn't."""
    try:
        adapter = get_storage_adapter()
        await adapter.exists("__health_check_sentinel__")
        return {"ok": True, "backend": settings.storage_backend}
    except Exception as exc:
        return {"ok": False, "backend": settings.storage_backend, "error": str(exc)}


async def _check_queue(redis: Redis) -> dict[str, Any]:
    """Real ARQ queue depth via a direct, read-only ZCARD/ZRANGE against the
    `arq:queue` sorted set -- consistent with docs/schema.md §3's "Managed by
    ARQ. Do not touch" (which is about writes, not read-only introspection).
    The score is the enqueue/defer timestamp in ms, so oldest_job_age_s falls
    out of the same read."""
    try:
        depth = await redis.zcard(default_queue_name)
        oldest_job_age_s = 0
        if depth:
            oldest = await redis.zrange(default_queue_name, 0, 0, withscores=True)
            if oldest:
                _job_id, score_ms = oldest[0]
                age_s = datetime.now(UTC).timestamp() - (float(score_ms) / 1000)
                oldest_job_age_s = max(0, round(age_s))
        return {"ok": True, "depth": depth, "oldest_job_age_s": oldest_job_age_s}
    except Exception as exc:
        return {"ok": False, "depth": 0, "oldest_job_age_s": 0, "error": str(exc)}


@app.get("/health/deep")
async def health_deep(request: Request, _admin: None = Depends(require_admin_key)) -> JSONResponse:
    """Readiness. Redis failing is the only thing that flips this to 503
    (docs/api-routes.md); Sheets/storage/queue degrade the response to
    "degraded" at 200 instead, since a Sheets or storage blip shouldn't make
    the platform's health-check restart an otherwise-fine process."""
    redis = request.app.state.redis
    redis_ok = True
    latency_ms = 0.0
    start = time.perf_counter()
    try:
        await redis.ping()
    except Exception:
        redis_ok = False
    finally:
        latency_ms = round((time.perf_counter() - start) * 1000, 2)

    sheets_check = await _check_sheets(redis)
    storage_check = await _check_storage()
    queue_check = await _check_queue(redis)
    try:
        spend_today = await today_spend(redis)
    except Exception:
        spend_today = 0

    checks: dict[str, Any] = {
        "redis": {"ok": redis_ok, "latency_ms": latency_ms},
        "sheets": sheets_check,
        "storage": storage_check,
        "queue": queue_check,
        "spend": {
            "today": spend_today,
            "cap": settings.daily_generation_cap,
            "remaining": max(0, settings.daily_generation_cap - spend_today),
        },
    }

    if redis_ok:
        overall_status = "ok" if sheets_check["ok"] and storage_check["ok"] else "degraded"
    else:
        overall_status = "degraded"
    http_status = status.HTTP_200_OK if redis_ok else status.HTTP_503_SERVICE_UNAVAILABLE
    return JSONResponse(
        status_code=http_status, content={"status": overall_status, "checks": checks}
    )


# Mounted last, at "/", so it never shadows /api/v1/* or /health* above --
# Starlette resolves routes in registration order. Phase 5's single-file
# demo page (docs/api-routes.md's contract is what it talks to; this mount
# just serves the static HTML/CSS/JS).
_UI_DIR = Path(__file__).resolve().parent.parent / "ui"
if _UI_DIR.is_dir():
    app.mount("/", StaticFiles(directory=_UI_DIR, html=True), name="ui")

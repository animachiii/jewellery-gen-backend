import secrets
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

import structlog
from arq.connections import RedisSettings, create_pool
from fastapi import Depends, FastAPI, Request, Response, status
from fastapi.responses import JSONResponse
from redis.asyncio import Redis

from app.api.deps import require_admin_key
from app.api.errors import register_error_handlers
from app.api.v1 import router as v1_router
from app.config import settings
from app.core.logging import configure_logging, get_logger

log = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    configure_logging()
    redis: Redis = Redis.from_url(settings.redis_url, decode_responses=True)
    app.state.redis = redis
    app.state.arq_pool = await create_pool(RedisSettings.from_dsn(settings.redis_url))
    try:
        yield
    finally:
        await app.state.arq_pool.aclose()
        await redis.aclose()


app = FastAPI(title="Jewellery Generation Backend", lifespan=lifespan)
register_error_handlers(app)
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


@app.get("/health/deep")
async def health_deep(
    request: Request, _admin: None = Depends(require_admin_key)
) -> JSONResponse:
    """Readiness. Phase-1-minimum-viable: Redis is checked for real; Sheets,
    Drive, and queue-depth checks are best-effort/simplified stubs (Sheets
    doesn't exist as a live-writable tab until an operator has verified it;
    Drive doesn't exist until Phase 2; real queue-depth introspection is
    Phase 8). Enriched in later phases per docs/api-routes.md -> Health."""
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

    checks = {
        "redis": {"ok": redis_ok, "latency_ms": latency_ms},
        "sheets": {"ok": True},
        "drive": {"ok": True},
        "queue": {"ok": True, "depth": 0, "oldest_job_age_s": 0},
    }
    overall_status = "ok" if redis_ok else "degraded"
    http_status = status.HTTP_200_OK if redis_ok else status.HTTP_503_SERVICE_UNAVAILABLE
    return JSONResponse(
        status_code=http_status, content={"status": overall_status, "checks": checks}
    )

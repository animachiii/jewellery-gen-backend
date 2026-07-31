"""FastAPI dependency functions: auth, rate limiting, ownership-scoped job load."""

import hmac
from datetime import UTC, datetime

from fastapi import Depends, Request
from redis.asyncio import Redis

from app.api.errors import NotFoundError, RateLimitedError, UnauthorizedError
from app.config import settings
from app.core.security import verify_key
from app.models.job import Job
from app.store.redis_store import get_job

RATE_LIMIT_KEY_TTL_SECONDS = 120


def _get_redis(request: Request) -> Redis:
    redis: Redis = request.app.state.redis
    return redis


async def require_client_key(request: Request) -> str:
    plaintext = request.headers.get("X-API-Key")
    if not plaintext:
        raise UnauthorizedError("Missing X-API-Key header.")
    key_name = verify_key(plaintext, settings.api_keys)
    if key_name is None:
        raise UnauthorizedError("Invalid API key.")
    return key_name


async def require_admin_key(request: Request) -> None:
    plaintext = request.headers.get("X-API-Key")
    if not plaintext or not hmac.compare_digest(plaintext, settings.admin_api_key):
        raise UnauthorizedError("Invalid admin API key.")


async def _check_rate_limit(
    request: Request, key_name: str, *, bucket: str, limit_per_minute: int
) -> None:
    """Shared fixed-window rate-limit check. `bucket` namespaces the Redis
    key so the polling-path limit and the default limit never share a
    counter — a client hitting the generous polling limit must not eat into
    their budget for POST /generate, and vice versa (Phase 7 Step 5)."""
    redis = _get_redis(request)
    minute = datetime.now(UTC).strftime("%Y%m%d%H%M")
    key = f"ratelimit:{bucket}:{key_name}:{minute}"
    count = await redis.incr(key)
    await redis.expire(key, RATE_LIMIT_KEY_TTL_SECONDS)
    if count > limit_per_minute:
        raise RateLimitedError("Rate limit exceeded for this API key.")


async def rate_limit(request: Request, key_name: str = Depends(require_client_key)) -> None:
    """Default limit (`RATE_LIMIT_PER_MINUTE`) — applies to every `/api/v1`
    route except the single-job poll (see `poll_rate_limit` below)."""
    await _check_rate_limit(
        request, key_name, bucket="default", limit_per_minute=settings.rate_limit_per_minute
    )


async def poll_rate_limit(request: Request, key_name: str = Depends(require_client_key)) -> None:
    """Generous limit (`POLLING_RATE_LIMIT_PER_MINUTE`) for `GET
    /jobs/{job_id}` only — docs/api-routes.md's documented hot, cheap,
    read-only path. Resolves R20's tension between the recommended 5s poll
    interval and the default 60/min limit tripping at ~10 concurrent jobs."""
    await _check_rate_limit(
        request,
        key_name,
        bucket="poll",
        limit_per_minute=settings.polling_rate_limit_per_minute,
    )


async def load_owned_job(
    request: Request, job_id: str, key_name: str = Depends(require_client_key)
) -> Job:
    redis = _get_redis(request)
    job = await get_job(redis, job_id)
    # NOTE(Phase 1 Step 4+): a job older than 48h expires from Redis and should
    # return 410 GONE (docs/schema.md R16), distinguishable from "never
    # existed"/"not yours" (404) via a Sheets JobLog lookup. That lookup isn't
    # wired up yet (no real Sheets writes happen before Phase 1 exercises the
    # JobLog tab), so for now an expired-or-nonexistent-or-not-owned job all
    # collapse to 404, which is a safe (if slightly imprecise) default per R15.
    if job is None or job.api_key_name != key_name:
        raise NotFoundError("Job not found.")
    return job

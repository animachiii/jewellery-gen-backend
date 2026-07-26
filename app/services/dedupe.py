import hashlib
from typing import cast

from redis.asyncio import Redis

from app.config import settings
from app.models.enums import JobStatus

IDEMPOTENCY_TTL_SECONDS = 24 * 60 * 60


def content_hash(image_bytes: bytes, service: str, jewelry_type: str | None) -> str:
    """docs/business-rules.md R3. jewelry_type=None hashes identically to 'AUTO'."""
    type_component = (jewelry_type or "AUTO").encode()
    hasher = hashlib.sha256()
    hasher.update(image_bytes)
    hasher.update(b"|")
    hasher.update(service.encode())
    hasher.update(b"|")
    hasher.update(type_component)
    return hasher.hexdigest()


def _dedupe_key(content_hash_: str) -> str:
    return f"dedupe:{content_hash_}"


def _idem_key(api_key_name: str, idempotency_key: str) -> str:
    return f"idem:{api_key_name}:{idempotency_key}"


async def check_dedupe(redis: Redis, content_hash_: str) -> str | None:
    return cast(str | None, await redis.get(_dedupe_key(content_hash_)))


async def record_dedupe(
    redis: Redis,
    content_hash_: str,
    job_id: str,
    status: JobStatus,
    *,
    ttl_seconds: int | None = None,
) -> None:
    """Only succeeded jobs may be recorded (R3) — failures must stay retryable."""
    if status is not JobStatus.SUCCEEDED:
        return
    ttl = ttl_seconds if ttl_seconds is not None else settings.dedupe_window_seconds
    await redis.set(_dedupe_key(content_hash_), job_id, ex=ttl)


async def check_idempotency(redis: Redis, api_key_name: str, idempotency_key: str) -> str | None:
    return cast(str | None, await redis.get(_idem_key(api_key_name, idempotency_key)))


async def record_idempotency(
    redis: Redis, api_key_name: str, idempotency_key: str, job_id: str
) -> None:
    await redis.set(_idem_key(api_key_name, idempotency_key), job_id, ex=IDEMPOTENCY_TTL_SECONDS)

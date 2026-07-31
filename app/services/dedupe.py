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
    mock: bool = False,
    ttl_seconds: int | None = None,
) -> None:
    """Only succeeded, non-mock jobs may be recorded.

    - Only succeeded (R3) — failures must stay retryable.
    - Never mock (R6). Dedupe exists purely to avoid paying twice for
      identical work; a mock job costs nothing and produces a FakeProvider
      placeholder, so it has no business in the money-saving index.
      `content_hash` deliberately excludes `mock` (R3's formula), so a
      recorded mock job would collide with a later *real* request for the
      same image+service+jewelry_type and serve it the placeholder as a
      `deduplicated` result. `app/api/v1/generate.py` additionally refuses
      any dedupe hit whose `mock` doesn't match the request's, so a key
      written before this guard existed can't leak either.
    """
    if status is not JobStatus.SUCCEEDED or mock:
        return
    ttl = ttl_seconds if ttl_seconds is not None else settings.dedupe_window_seconds
    await redis.set(_dedupe_key(content_hash_), job_id, ex=ttl)


async def check_idempotency(redis: Redis, api_key_name: str, idempotency_key: str) -> str | None:
    return cast(str | None, await redis.get(_idem_key(api_key_name, idempotency_key)))


async def record_idempotency(
    redis: Redis, api_key_name: str, idempotency_key: str, job_id: str
) -> None:
    await redis.set(_idem_key(api_key_name, idempotency_key), job_id, ex=IDEMPOTENCY_TTL_SECONDS)

from datetime import UTC, datetime
from typing import cast

from redis.asyncio import Redis

from app.models.enums import TERMINAL_STATUSES
from app.models.job import Job

JOB_TTL_SECONDS = 48 * 60 * 60
RECENT_CAP = 500


def _job_key(job_id: str) -> str:
    return f"job:{job_id}"


def _row_key(job_id: str) -> str:
    return f"job:{job_id}:row"


async def create_job(redis: Redis, job: Job) -> None:
    async with redis.pipeline(transaction=True) as pipe:
        pipe.hset(_job_key(job.job_id), mapping=job.to_redis_hash())
        pipe.expire(_job_key(job.job_id), JOB_TTL_SECONDS)
        pipe.zadd("jobs:recent", {job.job_id: job.created_at.timestamp()})
        pipe.zremrangebyrank("jobs:recent", 0, -(RECENT_CAP + 1))
        await pipe.execute()


async def get_job(redis: Redis, job_id: str) -> Job | None:
    data = cast(dict[str, str], await redis.hgetall(_job_key(job_id)))  # type: ignore[misc]
    if not data:
        return None
    return Job.from_redis_hash(data)


async def update_job(redis: Redis, job_id: str, **fields: object) -> None:
    hash_updates: dict[str, str] = {}
    for key, value in fields.items():
        if value is None:
            continue
        if hasattr(value, "value"):  # Enum
            hash_updates[key] = cast(str, value.value)
        elif isinstance(value, bool):
            hash_updates[key] = "1" if value else "0"
        elif isinstance(value, datetime):
            hash_updates[key] = value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
        else:
            hash_updates[key] = str(value)

    async with redis.pipeline(transaction=True) as pipe:
        if hash_updates:
            pipe.hset(_job_key(job_id), mapping=hash_updates)
        pipe.expire(_job_key(job_id), JOB_TTL_SECONDS)
        await pipe.execute()


async def list_recent(
    redis: Redis, api_key_name: str, limit: int = 20, status: str | None = None
) -> list[Job]:
    job_ids: list[str] = await redis.zrevrange("jobs:recent", 0, -1)
    results: list[Job] = []
    for job_id in job_ids:
        if len(results) >= limit:
            break
        job = await get_job(redis, job_id)
        if job is None or job.api_key_name != api_key_name:
            continue
        if status is not None and job.status.value != status:
            continue
        results.append(job)
    return results


async def set_row_index(redis: Redis, job_id: str, row: int) -> None:
    await redis.set(_row_key(job_id), str(row), ex=JOB_TTL_SECONDS)


async def get_row_index(redis: Redis, job_id: str) -> int | None:
    value = await redis.get(_row_key(job_id))
    return int(value) if value is not None else None


async def find_expired(redis: Redis, now: datetime) -> list[Job]:
    job_ids: list[str] = await redis.zrange("jobs:recent", 0, -1)
    expired: list[Job] = []
    for job_id in job_ids:
        job = await get_job(redis, job_id)
        if job is None:
            continue
        if job.status in TERMINAL_STATUSES:
            continue
        if job.deadline_at < now:
            expired.append(job)
    return expired

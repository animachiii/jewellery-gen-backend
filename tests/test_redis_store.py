from datetime import UTC, datetime, timedelta

from redis.asyncio import Redis

from app.models.enums import JobStatus, ServiceType
from app.models.job import Job
from app.store import redis_store


def _make_job(job_id: str, api_key_name: str = "erp", **overrides: object) -> Job:
    # Millisecond-aligned: to_redis_hash truncates to milliseconds, so a raw
    # datetime.now() microsecond value would fail an equality round-trip.
    raw_now = datetime.now(UTC)
    now = raw_now.replace(microsecond=(raw_now.microsecond // 1000) * 1000)
    defaults: dict[str, object] = dict(
        job_id=job_id,
        api_key_name=api_key_name,
        service=ServiceType.FEMALE_MODEL_TRADITIONAL,
        mock=False,
        content_hash=f"hash-{job_id}",
        created_at=now,
        updated_at=now,
        deadline_at=now + timedelta(seconds=900),
        source_ref="ref",
        source_bytes=10,
        source_mime="image/png",
    )
    defaults.update(overrides)
    return Job(**defaults)  # type: ignore[arg-type]


async def test_create_then_get_returns_equal_job(redis: Redis) -> None:
    job = _make_job("job-1")
    await redis_store.create_job(redis, job)
    fetched = await redis_store.get_job(redis, "job-1")
    assert fetched == job


async def test_ttl_is_close_to_48_hours(redis: Redis) -> None:
    job = _make_job("job-1")
    await redis_store.create_job(redis, job)
    ttl = await redis.ttl("job:job-1")
    assert abs(ttl - redis_store.JOB_TTL_SECONDS) < 10


async def test_recent_index_capped_at_500_and_evicts_oldest(redis: Redis) -> None:
    base = datetime.now(UTC)
    for i in range(505):
        job = _make_job(f"job-{i}", created_at=base + timedelta(seconds=i))
        await redis_store.create_job(redis, job)

    card = await redis.zcard("jobs:recent")
    assert card == 500

    remaining = await redis.zrange("jobs:recent", 0, -1)
    for i in range(5):
        assert f"job-{i}" not in remaining
    for i in range(5, 505):
        assert f"job-{i}" in remaining


async def test_list_recent_never_returns_another_keys_job(redis: Redis) -> None:
    await redis_store.create_job(redis, _make_job("job-erp", api_key_name="erp"))
    await redis_store.create_job(redis, _make_job("job-other", api_key_name="other"))

    results = await redis_store.list_recent(redis, "erp", limit=20)
    ids = {j.job_id for j in results}
    assert "job-erp" in ids
    assert "job-other" not in ids


async def test_set_and_get_row_index(redis: Redis) -> None:
    await redis_store.set_row_index(redis, "job-1", 47)
    assert await redis_store.get_row_index(redis, "job-1") == 47


async def test_find_expired_includes_past_deadline_excludes_terminal(redis: Redis) -> None:
    now = datetime.now(UTC)
    past = now - timedelta(seconds=10)
    future = now + timedelta(seconds=900)

    expired_job = _make_job(
        "job-expired", status=JobStatus.GENERATING, deadline_at=past, created_at=now
    )
    terminal_but_past = _make_job(
        "job-terminal", status=JobStatus.SUCCEEDED, deadline_at=past, created_at=now
    )
    healthy_job = _make_job(
        "job-healthy", status=JobStatus.GENERATING, deadline_at=future, created_at=now
    )

    for j in (expired_job, terminal_but_past, healthy_job):
        await redis_store.create_job(redis, j)

    expired = await redis_store.find_expired(redis, now)
    ids = {j.job_id for j in expired}
    assert ids == {"job-expired"}

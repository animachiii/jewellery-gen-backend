import asyncio

from redis.asyncio import Redis

from app.models.enums import JobStatus
from app.services import dedupe


def test_content_hash_stable_across_calls() -> None:
    h1 = dedupe.content_hash(b"imgbytes", "FEMALE_MODEL_TRADITIONAL", "RING")
    h2 = dedupe.content_hash(b"imgbytes", "FEMALE_MODEL_TRADITIONAL", "RING")
    assert h1 == h2


def test_content_hash_changes_with_image_bytes() -> None:
    h1 = dedupe.content_hash(b"a", "FEMALE_MODEL_TRADITIONAL", "RING")
    h2 = dedupe.content_hash(b"b", "FEMALE_MODEL_TRADITIONAL", "RING")
    assert h1 != h2


def test_content_hash_changes_with_service() -> None:
    h1 = dedupe.content_hash(b"a", "FEMALE_MODEL_TRADITIONAL", "RING")
    h2 = dedupe.content_hash(b"a", "MALE_MODEL_TRADITIONAL", "RING")
    assert h1 != h2


def test_content_hash_changes_with_jewelry_type() -> None:
    h1 = dedupe.content_hash(b"a", "FEMALE_MODEL_TRADITIONAL", "RING")
    h2 = dedupe.content_hash(b"a", "FEMALE_MODEL_TRADITIONAL", "NECKLACE")
    assert h1 != h2


def test_none_and_auto_jewelry_type_hash_identically() -> None:
    h1 = dedupe.content_hash(b"a", "FEMALE_MODEL_TRADITIONAL", None)
    h2 = dedupe.content_hash(b"a", "FEMALE_MODEL_TRADITIONAL", "AUTO")
    assert h1 == h2


async def test_record_dedupe_noops_for_non_succeeded_job(redis: Redis) -> None:
    await dedupe.record_dedupe(redis, "hash-1", "job-1", JobStatus.FAILED)
    assert await dedupe.check_dedupe(redis, "hash-1") is None


async def test_record_dedupe_records_succeeded_job(redis: Redis) -> None:
    await dedupe.record_dedupe(redis, "hash-1", "job-1", JobStatus.SUCCEEDED)
    assert await dedupe.check_dedupe(redis, "hash-1") == "job-1"


async def test_record_dedupe_noops_for_mock_job(redis: Redis) -> None:
    """R6 money-path regression: a mock job never touches the provider and is
    never billable, so it must stay entirely outside the dedupe index.
    `content_hash` deliberately excludes `mock` (R3), so a recorded mock job
    collides with a later *real* request for the same image+service+type and
    would serve that request FakeProvider's placeholder as a `deduplicated`
    result. Found in manual testing: unchecking `mock` in the showcase UI kept
    returning the previous mock run's beige placeholder in ~10s."""
    await dedupe.record_dedupe(redis, "hash-mock", "job-mock", JobStatus.SUCCEEDED, mock=True)
    assert await dedupe.check_dedupe(redis, "hash-mock") is None


async def test_record_dedupe_records_real_job_when_mock_flag_false(redis: Redis) -> None:
    await dedupe.record_dedupe(redis, "hash-real", "job-real", JobStatus.SUCCEEDED, mock=False)
    assert await dedupe.check_dedupe(redis, "hash-real") == "job-real"


async def test_check_dedupe_returns_none_after_ttl_expires(redis: Redis) -> None:
    await dedupe.record_dedupe(redis, "hash-1", "job-1", JobStatus.SUCCEEDED, ttl_seconds=1)
    assert await dedupe.check_dedupe(redis, "hash-1") == "job-1"
    await asyncio.sleep(1.2)
    assert await dedupe.check_dedupe(redis, "hash-1") is None


async def test_idempotency_round_trip_scoped_per_key(redis: Redis) -> None:
    await dedupe.record_idempotency(redis, "erp", "idem-1", "job-1")
    assert await dedupe.check_idempotency(redis, "erp", "idem-1") == "job-1"
    assert await dedupe.check_idempotency(redis, "other", "idem-1") is None

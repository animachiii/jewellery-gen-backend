from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
import sentry_sdk
from redis.asyncio import Redis

from app.models.enums import ErrorCode, JobStatus, ServiceType
from app.models.job import Job
from app.store import redis_store
from app.worker.sweeper import sweep
from tests.fakes.fake_sheets_client import FakeSheetsClient

SHEET_ID = "sheet-1"
TAB = "JobLog"


def _make_job(job_id: str, status: JobStatus, deadline_at: datetime) -> Job:
    # Millisecond-aligned: to_redis_hash truncates to milliseconds, so a raw
    # datetime.now() microsecond value would fail an equality round-trip.
    raw_now = datetime.now(UTC)
    now = raw_now.replace(microsecond=(raw_now.microsecond // 1000) * 1000)
    return Job(
        job_id=job_id,
        api_key_name="erp",
        service=ServiceType.FEMALE_MODEL_TRADITIONAL,
        mock=False,
        content_hash=f"hash-{job_id}",
        status=status,
        created_at=now - timedelta(seconds=1000),
        updated_at=now - timedelta(seconds=1000),
        deadline_at=deadline_at,
        source_ref="ref",
        source_bytes=1,
        source_mime="image/png",
    )


@pytest.fixture
def sentry_capture_count() -> Iterator[list[int]]:
    """Phase 8 Step 3 -- same pattern as tests/test_errors.py: a real Sentry
    client against a fake DSN, `before_send` counts and suppresses sends so
    nothing reaches sentry.io (docs/conventions.md -> Testing)."""
    counts = [0]

    def _before_send(event: object, hint: object) -> None:
        counts[0] += 1
        return None

    sentry_sdk.init(
        dsn="https://fake_public_key@fake.ingest.sentry.io/123456",
        before_send=_before_send,
    )
    try:
        yield counts
    finally:
        sentry_sdk.get_global_scope().set_client(None)


async def test_generating_past_deadline_becomes_failed_provider_timeout(redis: Redis) -> None:
    past = datetime.now(UTC) - timedelta(seconds=10)
    job = _make_job("job-1", JobStatus.GENERATING, past)
    await redis_store.create_job(redis, job)

    swept = await sweep(redis, None, SHEET_ID, TAB)

    assert swept == ["job-1"]
    updated = await redis_store.get_job(redis, "job-1")
    assert updated is not None
    assert updated.status == JobStatus.FAILED
    assert updated.error_code == ErrorCode.PROVIDER_TIMEOUT


async def test_submitting_past_deadline_becomes_needs_review_not_failed(redis: Redis) -> None:
    past = datetime.now(UTC) - timedelta(seconds=10)
    job = _make_job("job-2", JobStatus.SUBMITTING, past)
    await redis_store.create_job(redis, job)

    swept = await sweep(redis, None, SHEET_ID, TAB)

    assert swept == ["job-2"]
    updated = await redis_store.get_job(redis, "job-2")
    assert updated is not None
    assert updated.status == JobStatus.NEEDS_REVIEW
    assert updated.error_code == ErrorCode.ORPHANED_SUBMIT


async def test_needs_review_sweep_triggers_a_sentry_alert(
    redis: Redis, sentry_capture_count: list[int]
) -> None:
    past = datetime.now(UTC) - timedelta(seconds=10)
    job = _make_job("job-alert", JobStatus.SUBMITTING, past)
    await redis_store.create_job(redis, job)

    await sweep(redis, None, SHEET_ID, TAB)

    assert sentry_capture_count == [1]


async def test_ordinary_failed_sweep_does_not_trigger_a_sentry_alert(
    redis: Redis, sentry_capture_count: list[int]
) -> None:
    """A genuine PROVIDER_TIMEOUT is normal-operation noise, not an incident
    -- only NEEDS_REVIEW (a possible orphaned charge) alerts."""
    past = datetime.now(UTC) - timedelta(seconds=10)
    job = _make_job("job-no-alert", JobStatus.GENERATING, past)
    await redis_store.create_job(redis, job)

    await sweep(redis, None, SHEET_ID, TAB)

    assert sentry_capture_count == [0]


async def test_succeeded_job_past_deadline_is_untouched(redis: Redis) -> None:
    raw_past = datetime.now(UTC) - timedelta(seconds=10)
    past = raw_past.replace(microsecond=(raw_past.microsecond // 1000) * 1000)
    job = _make_job("job-3", JobStatus.SUCCEEDED, past)
    await redis_store.create_job(redis, job)

    swept = await sweep(redis, None, SHEET_ID, TAB)

    assert swept == []
    unchanged = await redis_store.get_job(redis, "job-3")
    assert unchanged == job


async def test_sweeping_triggers_exactly_one_sheets_terminal_update(redis: Redis) -> None:
    past = datetime.now(UTC) - timedelta(seconds=10)
    job = _make_job("job-4", JobStatus.GENERATING, past)
    await redis_store.create_job(redis, job)
    await redis_store.set_row_index(redis, "job-4", 12)

    client = FakeSheetsClient()
    await sweep(redis, client, SHEET_ID, TAB)

    assert len(client.calls) == 1
    assert client.calls[0].method == "update"
    assert client.calls[0].range_ == "JobLog!H12:T12"

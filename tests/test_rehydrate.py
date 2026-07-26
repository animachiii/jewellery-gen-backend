from datetime import UTC, datetime, timedelta

from redis.asyncio import Redis

from app.models.enums import ErrorCode, JobStatus
from app.store import redis_store
from app.store.rehydrate import rehydrate
from tests.fakes.fake_sheets_client import FakeSheetsClient

SHEET_ID = "sheet-1"
TAB = "JobLog"

HEADER = [
    "job_id",
    "created_at",
    "api_key_name",
    "service",
    "jewelry_type",
    "mock",
    "content_hash",
    "status",
]


def _row(job_id: str, created_at: datetime, status: str = "") -> list[str]:
    return [
        job_id,
        created_at.isoformat().replace("+00:00", "Z"),
        "erp",
        "FEMALE_MODEL_TRADITIONAL",
        "RING",
        "0",
        f"hash-{job_id}",
        status,
    ]


async def test_recreates_orphaned_non_terminal_row_missing_from_redis(redis: Redis) -> None:
    client = FakeSheetsClient()
    created_at = datetime.now(UTC) - timedelta(hours=1)
    client.rows = [HEADER, _row("job-x", created_at, status="")]

    recreated = await rehydrate(redis, client, SHEET_ID, TAB)

    assert recreated == ["job-x"]
    job = await redis_store.get_job(redis, "job-x")
    assert job is not None
    assert job.status == JobStatus.NEEDS_REVIEW
    assert job.error_code == ErrorCode.ORPHANED_SUBMIT


async def test_skips_rows_with_terminal_status(redis: Redis) -> None:
    client = FakeSheetsClient()
    created_at = datetime.now(UTC) - timedelta(hours=1)
    client.rows = [HEADER, _row("job-done", created_at, status="succeeded")]

    recreated = await rehydrate(redis, client, SHEET_ID, TAB)

    assert recreated == []
    assert await redis_store.get_job(redis, "job-done") is None


async def test_skips_rows_already_present_in_redis(redis: Redis) -> None:
    from app.models.enums import ServiceType
    from app.models.job import Job

    now = datetime.now(UTC)
    existing = Job(
        job_id="job-live",
        api_key_name="erp",
        service=ServiceType.FEMALE_MODEL_TRADITIONAL,
        mock=False,
        content_hash="hash-job-live",
        status=JobStatus.GENERATING,
        created_at=now,
        updated_at=now,
        deadline_at=now + timedelta(seconds=900),
        source_ref="ref",
        source_bytes=1,
        source_mime="image/png",
    )
    await redis_store.create_job(redis, existing)

    client = FakeSheetsClient()
    client.rows = [HEADER, _row("job-live", now, status="")]

    recreated = await rehydrate(redis, client, SHEET_ID, TAB)
    assert recreated == []


async def test_rehydration_is_idempotent_across_two_runs(redis: Redis) -> None:
    client = FakeSheetsClient()
    created_at = datetime.now(UTC) - timedelta(hours=1)
    client.rows = [HEADER, _row("job-x", created_at, status="")]

    first = await rehydrate(redis, client, SHEET_ID, TAB)
    job_after_first = await redis_store.get_job(redis, "job-x")

    second = await rehydrate(redis, client, SHEET_ID, TAB)
    job_after_second = await redis_store.get_job(redis, "job-x")

    assert first == ["job-x"]
    assert second == []
    assert job_after_first == job_after_second


async def test_skips_rows_outside_the_48h_window(redis: Redis) -> None:
    client = FakeSheetsClient()
    created_at = datetime.now(UTC) - timedelta(hours=72)
    client.rows = [HEADER, _row("job-old", created_at, status="")]

    recreated = await rehydrate(redis, client, SHEET_ID, TAB)
    assert recreated == []

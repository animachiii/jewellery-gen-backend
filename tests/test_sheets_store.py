import asyncio
import threading
import time
from datetime import UTC, datetime, timedelta

from redis.asyncio import Redis

from app.models.enums import JobStatus, ServiceType
from app.models.job import Job
from app.store import sheets_store
from tests.fakes.fake_sheets_client import FakeSheetsClient

SHEET_ID = "sheet-1"
TAB = "JobLog"


def _make_job(job_id: str = "job-1", **overrides: object) -> Job:
    now = datetime.now(UTC)
    defaults: dict[str, object] = dict(
        job_id=job_id,
        api_key_name="erp",
        service=ServiceType.FEMALE_MODEL_TRADITIONAL,
        mock=False,
        content_hash="hash",
        created_at=now,
        updated_at=now,
        deadline_at=now + timedelta(seconds=900),
        source_ref="ref",
        source_bytes=10,
        source_mime="image/png",
    )
    defaults.update(overrides)
    return Job(**defaults)  # type: ignore[arg-type]


async def test_append_job_row_returns_parsed_row_index(redis: Redis) -> None:
    client = FakeSheetsClient()
    client._next_row = 47  # force a realistic 'JobLog!A47:G47' updatedRange
    job = _make_job()
    row = await sheets_store.append_job_row(redis, client, SHEET_ID, TAB, job)
    assert row == 47


async def test_update_job_row_writes_only_h_through_t(redis: Redis) -> None:
    client = FakeSheetsClient()
    job = _make_job(status=JobStatus.SUCCEEDED, completed_at=datetime.now(UTC))
    await sheets_store.update_job_row(redis, client, SHEET_ID, TAB, job, row_index=47)
    assert len(client.calls) == 1
    call = client.calls[0]
    assert call.range_ == "JobLog!H47:T47"


async def test_concurrent_appends_never_overlap(redis: Redis) -> None:
    lock = threading.Lock()
    active = 0
    max_active = 0

    class TrackingClient(FakeSheetsClient):
        def append_row(self, sheet_id: str, tab: str, values: list[str]) -> str:
            nonlocal active, max_active
            with lock:
                active += 1
                max_active = max(max_active, active)
            time.sleep(0.05)
            result = super().append_row(sheet_id, tab, values)
            with lock:
                active -= 1
            return result

    client = TrackingClient()
    job_a = _make_job("job-a")
    job_b = _make_job("job-b")

    await asyncio.gather(
        sheets_store.append_job_row(redis, client, SHEET_ID, TAB, job_a),
        sheets_store.append_job_row(redis, client, SHEET_ID, TAB, job_b),
    )

    assert max_active == 1
    assert len(client.calls) == 2

    lock_remaining_ttl = await redis.ttl(sheets_store.LOCK_KEY)
    assert lock_remaining_ttl in (-2, -1)  # released, not left dangling


async def test_safe_append_logs_warning_and_does_not_raise(redis: Redis, caplog) -> None:  # type: ignore[no-untyped-def]
    client = FakeSheetsClient()
    client.fail_next = True
    job = _make_job()

    result = await sheets_store.safe_append_job_row(redis, client, SHEET_ID, TAB, job)

    assert result is None
    assert sheets_store.sheets_write_failures() >= 1


async def test_full_lifecycle_produces_exactly_two_sheets_calls(redis: Redis) -> None:
    client = FakeSheetsClient()
    job = _make_job()

    row = await sheets_store.append_job_row(redis, client, SHEET_ID, TAB, job)

    completed_job = Job(
        **{**job.__dict__, "status": JobStatus.SUCCEEDED, "completed_at": datetime.now(UTC)}
    )
    await sheets_store.update_job_row(redis, client, SHEET_ID, TAB, completed_job, row_index=row)

    assert len(client.calls) == 2
    assert client.calls[0].method == "append"
    assert client.calls[1].method == "update"

import asyncio
from datetime import UTC, datetime, timedelta

from redis.asyncio import Redis

from app.config import settings
from app.core.logging import get_logger
from app.models.enums import ErrorCode, JewelryType, JobStatus, ServiceType, TypeSource
from app.models.job import Job
from app.store import redis_store
from app.store.sheets_store import SheetsClient

log = get_logger(__name__)

REHYDRATION_WINDOW = timedelta(hours=48)

# JobLog column indices (0-based), matching docs/schema.md §2
_COL_JOB_ID = 0
_COL_CREATED_AT = 1
_COL_API_KEY_NAME = 2
_COL_SERVICE = 3
_COL_JEWELRY_TYPE = 4
_COL_MOCK = 5
_COL_CONTENT_HASH = 6
_COL_STATUS = 7


def _cell(row: list[str], index: int) -> str:
    return row[index] if index < len(row) else ""


def _parse_created_at(raw: str) -> datetime:
    return datetime.fromisoformat(raw.replace("Z", "+00:00"))


async def rehydrate(redis: Redis, client: SheetsClient, sheet_id: str, tab: str) -> list[str]:
    """Reconstructs jobs whose JobLog row is non-terminal but whose job:{id} key is
    missing from Redis (Redis flushed/lost mid-flight). Recreated jobs land in
    needs_review/ORPHANED_SUBMIT — never auto-resumed (R1, R2). Idempotent: a job
    already present in Redis (including one this function already recreated) is
    left untouched.
    """
    rows = await asyncio.to_thread(client.read_all_rows, sheet_id, tab)
    now = datetime.now(UTC)
    recreated: list[str] = []

    for row in rows[1:]:  # skip header
        job_id = _cell(row, _COL_JOB_ID)
        if not job_id:
            continue

        status = _cell(row, _COL_STATUS)
        if status:  # terminal write already happened
            continue

        created_at_raw = _cell(row, _COL_CREATED_AT)
        if not created_at_raw:
            continue
        created_at = _parse_created_at(created_at_raw)
        if now - created_at > REHYDRATION_WINDOW:
            continue

        existing = await redis_store.get_job(redis, job_id)
        if existing is not None:
            continue

        jewelry_type_raw = _cell(row, _COL_JEWELRY_TYPE)
        job = Job(
            job_id=job_id,
            api_key_name=_cell(row, _COL_API_KEY_NAME),
            service=ServiceType(_cell(row, _COL_SERVICE)),
            jewelry_type_requested=JewelryType(jewelry_type_raw)
            if jewelry_type_raw and jewelry_type_raw != "AUTO"
            else None,
            jewelry_type_final=JewelryType(jewelry_type_raw)
            if jewelry_type_raw and jewelry_type_raw != "AUTO"
            else None,
            type_source=TypeSource.PROVIDED if jewelry_type_raw not in ("", "AUTO") else None,
            mock=_cell(row, _COL_MOCK) == "1",
            content_hash=_cell(row, _COL_CONTENT_HASH),
            status=JobStatus.NEEDS_REVIEW,
            created_at=created_at,
            updated_at=now,
            completed_at=now,
            deadline_at=created_at + timedelta(seconds=settings.job_deadline_seconds),
            error_code=ErrorCode.ORPHANED_SUBMIT,
            error_message="Recovered on worker startup: Redis record missing for a "
            "non-terminal JobLog row.",
        )

        await redis_store.create_job(redis, job)
        recreated.append(job_id)
        log.warning("job.rehydrated", job_id=job_id)

    return recreated

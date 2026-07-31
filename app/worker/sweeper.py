from datetime import UTC, datetime

from redis.asyncio import Redis

from app.core.logging import get_logger
from app.core.observability import capture_needs_review
from app.core.state import transition
from app.models.enums import ErrorCode, JobStatus
from app.store import redis_store
from app.store.sheets_store import SheetsClient, safe_update_job_row

log = get_logger(__name__)


async def sweep(
    redis: Redis,
    sheets_client: SheetsClient | None,
    sheet_id: str,
    sheets_tab: str,
) -> list[str]:
    """Terminates non-terminal jobs past their deadline_at (R12). Runs as an ARQ
    cron job every 60s — see WorkerSettings.cron_jobs. If that registration is
    missing, this never runs and stuck jobs accumulate silently.
    """
    now = datetime.now(UTC)
    expired = await redis_store.find_expired(redis, now)
    swept: list[str] = []

    for job in expired:
        previous_status = job.status
        if previous_status is JobStatus.SUBMITTING:
            updated = await transition(
                job, JobStatus.NEEDS_REVIEW, error_code=ErrorCode.ORPHANED_SUBMIT
            )
            # Phase 8 Step 3: NEEDS_REVIEW means money may have moved and
            # nobody has confirmed what happened -- alert, unlike an
            # ordinary FAILED terminal state (below), which is normal-
            # operation noise, not an incident.
            capture_needs_review(job.job_id, reason="deadline exceeded while submitting")
        else:
            updated = await transition(job, JobStatus.FAILED, error_code=ErrorCode.PROVIDER_TIMEOUT)

        await redis_store.update_job(
            redis,
            job.job_id,
            status=updated.status,
            updated_at=updated.updated_at,
            completed_at=updated.completed_at,
            error_code=updated.error_code,
            error_message=updated.error_message,
        )

        if sheets_client is not None:
            row_index = await redis_store.get_row_index(redis, job.job_id)
            if row_index is not None:
                await safe_update_job_row(
                    redis, sheets_client, sheet_id, sheets_tab, updated, row_index
                )

        log.info(
            "job.swept",
            job_id=job.job_id,
            previous_status=previous_status.value,
            new_status=updated.status.value,
        )
        swept.append(job.job_id)

    return swept

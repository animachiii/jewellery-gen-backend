from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

from app.core.logging import get_logger
from app.models.enums import LEGAL_TRANSITIONS, TERMINAL_STATUSES, ErrorCode, JobStatus
from app.models.job import Job

log = get_logger(__name__)

_ERROR_REQUIRED_STATUSES = frozenset(
    {JobStatus.FAILED, JobStatus.NEEDS_REVIEW, JobStatus.NEEDS_INPUT}
)


class IllegalTransition(Exception):  # noqa: N818 — name mandated by phases/phase-0b-state-layer.md
    def __init__(self, from_status: JobStatus, to_status: JobStatus) -> None:
        self.from_status = from_status
        self.to_status = to_status
        super().__init__(f"Illegal transition: {from_status.value} -> {to_status.value}")


async def transition(
    job: Job,
    to: JobStatus,
    *,
    error_code: ErrorCode | None = None,
    error_message: str | None = None,
    **fields: Any,
) -> Job:
    """The only sanctioned way to change a job's status. Validates against
    LEGAL_TRANSITIONS, stamps updated_at/completed_at, and applies extra fields
    atomically with the status change. Persistence is the caller's job (see
    app/store/redis_store.py) — this only returns the updated in-memory Job.
    """
    from_status = job.status

    if to not in LEGAL_TRANSITIONS.get(from_status, frozenset()):
        raise IllegalTransition(from_status, to)

    if to in _ERROR_REQUIRED_STATUSES and error_code is None:
        raise ValueError(f"error_code is required when transitioning to {to.value}")

    now = datetime.now(UTC)
    updates: dict[str, Any] = {
        "status": to,
        "updated_at": now,
        **fields,
    }
    if error_code is not None:
        updates["error_code"] = error_code
    if error_message is not None:
        updates["error_message"] = error_message
    if to in TERMINAL_STATUSES:
        updates["completed_at"] = now

    updated = replace(job, **updates)

    log.info(
        "job.status.changed",
        job_id=job.job_id,
        **{
            "from": from_status.value,
            "to": to.value,
            "error_code": error_code.value if error_code else None,
        },
    )

    return updated

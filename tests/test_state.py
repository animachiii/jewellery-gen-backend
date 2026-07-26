from datetime import UTC, datetime

import pytest

from app.core.state import IllegalTransition, transition
from app.models.enums import ErrorCode, JobStatus, ServiceType
from app.models.job import Job


def _make_job(status: JobStatus = JobStatus.QUEUED) -> Job:
    now = datetime(2026, 7, 26, 12, 0, 0, tzinfo=UTC)
    return Job(
        job_id="job-1",
        api_key_name="erp",
        service=ServiceType.FEMALE_MODEL_TRADITIONAL,
        mock=False,
        content_hash="abc123",
        status=status,
        created_at=now,
        updated_at=now,
        deadline_at=now,
        source_ref="drive-file-1",
        source_bytes=1024,
        source_mime="image/png",
    )


async def test_illegal_transition_raises() -> None:
    job = _make_job(JobStatus.QUEUED)
    with pytest.raises(IllegalTransition):
        await transition(job, JobStatus.GENERATING)


async def test_legal_transition_succeeds() -> None:
    job = _make_job(JobStatus.SUBMITTING)
    updated = await transition(job, JobStatus.GENERATING)
    assert updated.status == JobStatus.GENERATING


async def test_failed_without_error_code_raises() -> None:
    job = _make_job(JobStatus.QUEUED)
    with pytest.raises(ValueError, match="error_code"):
        await transition(job, JobStatus.FAILED)


async def test_failed_with_error_code_persists_code_and_message() -> None:
    job = _make_job(JobStatus.QUEUED)
    updated = await transition(
        job, JobStatus.FAILED, error_code=ErrorCode.INVALID_IMAGE, error_message="bad image"
    )
    assert updated.error_code == ErrorCode.INVALID_IMAGE
    assert updated.error_message == "bad image"


async def test_terminal_transition_sets_completed_at() -> None:
    job = _make_job(JobStatus.STORING)
    updated = await transition(job, JobStatus.SUCCEEDED)
    assert updated.completed_at is not None


async def test_non_terminal_transition_leaves_completed_at_none() -> None:
    job = _make_job(JobStatus.QUEUED)
    updated = await transition(job, JobStatus.RESOLVING)
    assert updated.completed_at is None


async def test_needs_input_requires_error_code() -> None:
    job = _make_job(JobStatus.CLASSIFYING)
    with pytest.raises(ValueError, match="error_code"):
        await transition(job, JobStatus.NEEDS_INPUT)
    updated = await transition(job, JobStatus.NEEDS_INPUT, error_code=ErrorCode.LOW_CONFIDENCE)
    assert updated.status == JobStatus.NEEDS_INPUT


async def test_needs_review_requires_error_code() -> None:
    job = _make_job(JobStatus.SUBMITTING)
    with pytest.raises(ValueError, match="error_code"):
        await transition(job, JobStatus.NEEDS_REVIEW)
    updated = await transition(job, JobStatus.NEEDS_REVIEW, error_code=ErrorCode.ORPHANED_SUBMIT)
    assert updated.status == JobStatus.NEEDS_REVIEW

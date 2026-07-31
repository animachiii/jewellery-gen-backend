"""Phase 6 Step 2 — exhaustive transition-table coverage.

tests/test_state.py covers illustrative examples (one legal, one illegal
transition, error-code requirements). Neither proves exhaustiveness: an
unlisted-but-accidentally-allowed transition is exactly the class of bug that
lets a job leave a terminal state undetected. This file is generated directly
from `LEGAL_TRANSITIONS` (the single source of truth `app/core/state.py`
already validates against) so the table and the tests cannot drift apart.
"""

from datetime import UTC, datetime

import pytest

from app.core.state import IllegalTransition, transition
from app.models.enums import (
    LEGAL_TRANSITIONS,
    TERMINAL_STATUSES,
    ErrorCode,
    JobStatus,
    ServiceType,
)
from app.models.job import Job

# Mirrors app/core/state.py's own _ERROR_REQUIRED_STATUSES — duplicated here
# deliberately so this file doesn't import a private name from app.core.state,
# and so a change to that private set is caught by a diff in two places.
_ERROR_REQUIRED_STATUSES = frozenset(
    {JobStatus.FAILED, JobStatus.NEEDS_REVIEW, JobStatus.NEEDS_INPUT}
)

_ALL_PAIRS = [(frm, to) for frm in JobStatus for to in JobStatus]


def _make_job(status: JobStatus) -> Job:
    now = datetime(2026, 7, 26, 12, 0, 0, tzinfo=UTC)
    return Job(
        job_id="job-table",
        api_key_name="erp",
        service=ServiceType.FEMALE_MODEL_TRADITIONAL,
        mock=False,
        content_hash="abc123",
        status=status,
        created_at=now,
        updated_at=now,
        deadline_at=now,
        source_ref="ref-1",
        source_bytes=1024,
        source_mime="image/png",
    )


def _error_kwargs(to: JobStatus) -> dict[str, object]:
    if to in _ERROR_REQUIRED_STATUSES:
        return {"error_code": ErrorCode.INTERNAL_ERROR}
    return {}


@pytest.mark.parametrize(
    "frm,to",
    [(f, t) for f, t in _ALL_PAIRS if t in LEGAL_TRANSITIONS.get(f, frozenset())],
)
async def test_legal_transition_is_accepted(frm: JobStatus, to: JobStatus) -> None:
    job = _make_job(frm)
    updated = await transition(job, to, **_error_kwargs(to))
    assert updated.status == to
    assert updated.updated_at is not None
    if to in TERMINAL_STATUSES:
        assert updated.completed_at is not None
    else:
        assert updated.completed_at is None


@pytest.mark.parametrize(
    "frm,to",
    [(f, t) for f, t in _ALL_PAIRS if t not in LEGAL_TRANSITIONS.get(f, frozenset())],
)
async def test_illegal_transition_is_rejected(frm: JobStatus, to: JobStatus) -> None:
    job = _make_job(frm)
    with pytest.raises(IllegalTransition):
        await transition(job, to, **_error_kwargs(to))


def test_every_terminal_status_has_no_legal_outbound_transitions_except_needs_input() -> None:
    """needs_input is the one documented exception (docs/schema.md §1: it can
    resume to `resolving` via POST /jobs/{id}/resolve). Every other terminal
    status must be a dead end."""
    for status in TERMINAL_STATUSES:
        outbound = LEGAL_TRANSITIONS.get(status, frozenset())
        if status is JobStatus.NEEDS_INPUT:
            assert outbound == frozenset({JobStatus.RESOLVING})
        else:
            assert outbound == frozenset()

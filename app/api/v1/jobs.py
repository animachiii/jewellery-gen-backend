"""GET /api/v1/jobs, /jobs/{id}, /jobs/{id}/assets/{index}, POST /jobs/{id}/resolve.

`GET /jobs/{id}` is the hot polling path (R13) — it must never touch Google
Sheets. `load_owned_job` (app/api/deps.py) already collapses "job doesn't
exist" / "not yours" / "expired from Redis" into a single 404 per its own
documented Step-1 decision; the 410-GONE-via-JobLog-lookup case stays
deferred here too — building it would need a Sheets read on (part of) this
route's error path, which is more scope than this step needs, and the
decision to defer it is already recorded in `app/api/deps.py`.

Contract for Step 6 (the worker pipeline):
- `"run_job_pipeline"(ctx, job_id: str)` — used by POST /generate. Starts at
  classify (if `jewelry_type_requested` is null) or resolve (if it's set).
- `"run_job_pipeline_from_resolve"(ctx, job_id: str)` — used by POST
  /jobs/{id}/resolve. Always starts at the resolve stage, skipping classify
  entirely, since classification must not be re-run or re-charged after a
  client supplies `jewelry_type` via /resolve (R10).
"""

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import Response
from redis.asyncio import Redis

from app.api.deps import load_owned_job, poll_rate_limit, rate_limit, require_client_key
from app.api.errors import JobNotResolvableError, NotFoundError, StorageError
from app.core.state import transition
from app.models.enums import JobStatus, TypeSource
from app.models.job import Job, _iso
from app.models.schemas import (
    AssetRef,
    CandidateType,
    ErrorDetail,
    JobListResponse,
    JobResponse,
    ResolveRequest,
)
from app.storage.base import StorageAdapter
from app.storage.drive import DriveStorageError
from app.storage.factory import get_storage_adapter
from app.storage.local import StorageRefNotFoundError
from app.storage.supabase import SupabaseStorageError
from app.store.redis_store import list_recent, update_job

router = APIRouter()

# Hardcoded rather than resolved from storage (which would mean an extra
# storage read on the hot polling path just to learn a mime type). Every
# asset produced in Phase 1 is a PNG (FakeProvider's placeholder — see
# app/providers/fake.py). If a future provider ever returns non-PNG assets,
# add a stored per-asset mime field to Job instead of guessing here.
_ASSET_MIME = "image/png"


def _job_to_response(job: Job) -> JobResponse:
    jewelry_type = job.jewelry_type_final if job.jewelry_type_final else job.jewelry_type_requested

    assets: list[AssetRef] = []
    if job.status == JobStatus.SUCCEEDED:
        assets = [
            AssetRef(index=i, url=f"/api/v1/jobs/{job.job_id}/assets/{i}", mime=_ASSET_MIME)
            for i in range(len(job.asset_refs))
        ]

    candidate_types: list[CandidateType] | None = None
    if job.status == JobStatus.NEEDS_INPUT and job.candidate_types is not None:
        candidate_types = [
            CandidateType(
                jewelry_type=c["jewelry_type"],  # type: ignore[arg-type]
                confidence=c["confidence"],  # type: ignore[arg-type]
            )
            for c in job.candidate_types
        ]

    error: ErrorDetail | None = None
    if (
        job.status in (JobStatus.FAILED, JobStatus.NEEDS_REVIEW, JobStatus.NEEDS_INPUT)
        and job.error_code is not None
    ):
        error = ErrorDetail(
            code=job.error_code.value, message=job.error_message or "", job_id=job.job_id
        )

    return JobResponse(
        job_id=job.job_id,
        status=job.status,
        service=job.service,
        jewelry_type=jewelry_type,
        type_source=job.type_source.value if job.type_source else None,
        confidence=job.confidence,
        mock=job.mock,
        created_at=_iso(job.created_at),
        updated_at=_iso(job.updated_at),
        completed_at=_iso(job.completed_at) if job.completed_at else None,
        deadline_at=_iso(job.deadline_at),
        assets=assets,
        candidate_types=candidate_types,
        error=error,
    )


@router.get("/jobs/{job_id}", response_model=JobResponse)
async def get_job_route(
    job: Annotated[Job, Depends(load_owned_job)],
    _rate_limited: Annotated[None, Depends(poll_rate_limit)],
) -> JobResponse:
    return _job_to_response(job)


@router.get("/jobs", response_model=JobListResponse)
async def list_jobs_route(
    request: Request,
    key_name: Annotated[str, Depends(require_client_key)],
    _rate_limited: Annotated[None, Depends(rate_limit)],
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    status_filter: Annotated[JobStatus | None, Query(alias="status")] = None,
) -> JobListResponse:
    redis: Redis = request.app.state.redis
    jobs = await list_recent(
        redis, key_name, limit=limit, status=status_filter.value if status_filter else None
    )
    return JobListResponse(jobs=[_job_to_response(j) for j in jobs], count=len(jobs))


@router.get("/jobs/{job_id}/assets/{index}")
async def get_job_asset_route(
    index: int,
    job: Annotated[Job, Depends(load_owned_job)],
    _rate_limited: Annotated[None, Depends(rate_limit)],
) -> Response:
    if job.status != JobStatus.SUCCEEDED:
        raise NotFoundError("Job not found.")
    if index < 0 or index >= len(job.asset_refs):
        raise NotFoundError("Job not found.")

    storage: StorageAdapter = get_storage_adapter()
    try:
        data, mime = await storage.get(job.asset_refs[index])
    except (StorageRefNotFoundError, DriveStorageError, SupabaseStorageError) as exc:
        raise StorageError("Stored asset could not be retrieved.") from exc

    return Response(
        content=data,
        media_type=mime,
        headers={"Cache-Control": "private, max-age=3600"},
    )


@router.post("/jobs/{job_id}/resolve", response_model=JobResponse)
async def resolve_job_route(
    body: ResolveRequest,
    request: Request,
    job: Annotated[Job, Depends(load_owned_job)],
    _rate_limited: Annotated[None, Depends(rate_limit)],
) -> JobResponse:
    if job.status != JobStatus.NEEDS_INPUT:
        raise JobNotResolvableError()

    redis: Redis = request.app.state.redis
    updated = await transition(
        job,
        JobStatus.RESOLVING,
        jewelry_type_final=body.jewelry_type,
        type_source=TypeSource.RESOLVED,
    )
    await update_job(
        redis,
        job.job_id,
        status=updated.status,
        updated_at=updated.updated_at,
        jewelry_type_final=updated.jewelry_type_final,
        type_source=updated.type_source,
    )

    arq_pool = request.app.state.arq_pool
    await arq_pool.enqueue_job("run_job_pipeline_from_resolve", job.job_id)

    return _job_to_response(updated)

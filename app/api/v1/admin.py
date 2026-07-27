"""Admin-only routes (docs/api-routes.md -> Admin).

Phase-1-minimum-viable: these two routes exist so Checkpoint 6's "all ten
routes in the exported OpenAPI spec" requirement is satisfiable now, ahead of
their real implementations (Phase 3 for matrix refresh, general debugging
tooling for the job dump). Both are stubbed but real, typed, admin-gated
endpoints — not placeholders that will need a route-shape rewrite later.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel

from app.api.deps import require_admin_key
from app.api.errors import NotFoundError
from app.config import settings
from app.core.logging import get_logger
from app.models.job import Job, _iso
from app.services.matrix import refresh_matrix
from app.store.redis_store import get_job
from app.store.sheets_store import GoogleSheetsClient, SheetsClient

router = APIRouter()
log = get_logger(__name__)


class MatrixRefreshResponse(BaseModel):
    matrix_version: str
    rows_loaded: int
    changed: bool


class AdminJobResponse(BaseModel):
    job_id: str
    status: str
    api_key_name: str
    service: str
    jewelry_type_requested: str | None
    jewelry_type_final: str | None
    type_source: str | None
    confidence: float | None
    candidate_types: list[dict[str, object]] | None
    mock: bool
    created_at: str
    updated_at: str
    completed_at: str | None
    deadline_at: str
    prompt_snapshot: str | None
    negative_prompt_snapshot: str | None
    reference_url_snapshot: str | None
    matrix_version: str | None
    provider: str | None
    submission_token: str | None
    provider_job_id: str | None
    billable: bool | None
    asset_refs: list[str]
    error_code: str | None
    error_message: str | None


def _build_sheets_client() -> SheetsClient | None:
    """Mirrors app/api/v1/generate.py's `_build_sheets_client` — kept as a
    small local duplicate rather than a shared import, per this codebase's
    established convention for this exact 5-line pattern (see that module's
    comment on the same function)."""
    try:
        return GoogleSheetsClient(settings.google_service_account_info)
    except Exception:
        log.warning("sheets.client.unavailable")
        return None


@router.post("/admin/matrix/refresh", response_model=MatrixRefreshResponse)
async def refresh_matrix_route(
    request: Request,
    _admin: Annotated[None, Depends(require_admin_key)],
) -> MatrixRefreshResponse:
    """Forces an immediate re-read of Sheet1, bypassing MATRIX_CACHE_TTL
    (docs/api-routes.md)."""
    redis = request.app.state.redis
    sheets_client = _build_sheets_client()
    matrix_version, rows_loaded, changed = await refresh_matrix(
        redis, sheets_client, settings.google_sheet_id, force=True
    )
    return MatrixRefreshResponse(
        matrix_version=matrix_version, rows_loaded=rows_loaded, changed=changed
    )


@router.get("/admin/jobs/{job_id}", response_model=AdminJobResponse)
async def get_admin_job(
    job_id: str,
    request: Request,
    _admin: Annotated[None, Depends(require_admin_key)],
) -> AdminJobResponse:
    redis = request.app.state.redis
    job = await get_job(redis, job_id)
    if job is None:
        raise NotFoundError("Job not found.")
    return _to_admin_response(job)


def _to_admin_response(job: Job) -> AdminJobResponse:
    return AdminJobResponse(
        job_id=job.job_id,
        status=job.status.value,
        api_key_name=job.api_key_name,
        service=job.service.value,
        jewelry_type_requested=job.jewelry_type_requested.value
        if job.jewelry_type_requested
        else None,
        jewelry_type_final=job.jewelry_type_final.value if job.jewelry_type_final else None,
        type_source=job.type_source.value if job.type_source else None,
        confidence=job.confidence,
        candidate_types=job.candidate_types,
        mock=job.mock,
        created_at=_iso(job.created_at),
        updated_at=_iso(job.updated_at),
        completed_at=_iso(job.completed_at) if job.completed_at else None,
        deadline_at=_iso(job.deadline_at),
        prompt_snapshot=job.prompt_snapshot,
        negative_prompt_snapshot=job.negative_prompt_snapshot,
        reference_url_snapshot=job.reference_url_snapshot,
        matrix_version=job.matrix_version,
        provider=job.provider,
        submission_token=job.submission_token,
        provider_job_id=job.provider_job_id,
        billable=job.billable,
        asset_refs=job.asset_refs,
        error_code=job.error_code.value if job.error_code else None,
        error_message=job.error_message,
    )

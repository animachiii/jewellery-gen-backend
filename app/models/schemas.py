"""Explicit Pydantic v2 request/response models — every shape in docs/api-routes.md.

Per docs/conventions.md -> API Design: routes never return a raw dict, and
`response_model` is always declared. Timestamps are typed as `str` here, not
`datetime` — job records already carry pre-formatted ISO-8601-with-`Z` strings
(see `app/models/job.py`'s `_iso` helper), so response models constructed from
a `Job` pass those strings straight through rather than re-parsing/re-formatting.
"""

from pydantic import BaseModel

from app.models.enums import JewelryType, JobStatus, ServiceType, Style


class AssetRef(BaseModel):
    index: int
    url: str
    mime: str


class CandidateType(BaseModel):
    jewelry_type: JewelryType
    confidence: float


class StyleCandidate(BaseModel):
    style: Style
    confidence: float


class ClassifyPreviewResponse(BaseModel):
    """POST /api/v1/classify-preview — showcase-UI-only, not part of the
    frozen v1 job contract (docs/api-routes.md). Lets a human confirm both
    jewelry_type and traditional/modern styling before a job (and its fixed
    `service`) is created; see app/api/v1/classify.py."""

    is_jewelry: bool
    jewelry_type_predictions: list[CandidateType]
    style_predictions: list[StyleCandidate]


class ErrorDetail(BaseModel):
    code: str
    message: str
    job_id: str | None = None


class ErrorEnvelope(BaseModel):
    error: ErrorDetail


class GenerateResponse(BaseModel):
    job_id: str
    status: JobStatus
    poll_url: str
    deduplicated: bool
    created_at: str


class JobResponse(BaseModel):
    """Mirrors the GET /jobs/{job_id} example in docs/api-routes.md.

    `jewelry_type` maps to `Job.jewelry_type_final`, falling back to
    `Job.jewelry_type_requested` when the final type hasn't been set yet
    (e.g. while still `queued`/`classifying`) — this is a read-model
    convenience so callers see the client-provided type immediately rather
    than `null` until classification completes. The route that builds this
    from a `Job` (Step 4) is responsible for applying that fallback; this
    schema only declares the shape.
    """

    job_id: str
    status: JobStatus
    service: ServiceType
    jewelry_type: JewelryType | None
    type_source: str | None
    confidence: float | None
    mock: bool
    created_at: str
    updated_at: str
    completed_at: str | None
    deadline_at: str
    assets: list[AssetRef]
    candidate_types: list[CandidateType] | None
    error: ErrorDetail | None


class JobListResponse(BaseModel):
    jobs: list[JobResponse]
    count: int


class ResolveRequest(BaseModel):
    jewelry_type: JewelryType


class MatrixCombination(BaseModel):
    jewelry_type: JewelryType
    service: ServiceType


class MatrixResponse(BaseModel):
    matrix_version: str
    cached_at: str
    combinations: list[MatrixCombination]
    jewelry_types: list[JewelryType]
    services: list[ServiceType]

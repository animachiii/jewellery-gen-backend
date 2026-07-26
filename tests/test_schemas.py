from app.models.enums import JewelryType, JobStatus, ServiceType
from app.models.schemas import (
    AssetRef,
    CandidateType,
    ErrorDetail,
    ErrorEnvelope,
    GenerateResponse,
    JobListResponse,
    JobResponse,
    MatrixCombination,
    MatrixResponse,
    ResolveRequest,
)


def test_enum_fields_serialize_as_string_names() -> None:
    resp = GenerateResponse(
        job_id="job-1",
        status=JobStatus.QUEUED,
        poll_url="/api/v1/jobs/job-1",
        deduplicated=False,
        created_at="2026-07-25T10:14:03.000Z",
    )
    dumped = resp.model_dump(mode="json")
    assert dumped["status"] == "queued"


def test_job_response_round_trips_full_shape() -> None:
    resp = JobResponse(
        job_id="job-1",
        status=JobStatus.GENERATING,
        service=ServiceType.FEMALE_MODEL_TRADITIONAL,
        jewelry_type=JewelryType.ANKLET,
        type_source="CLASSIFIED",
        confidence=0.94,
        mock=False,
        created_at="2026-07-25T10:14:03.000Z",
        updated_at="2026-07-25T10:15:41.000Z",
        completed_at=None,
        deadline_at="2026-07-25T10:29:03.000Z",
        assets=[AssetRef(index=0, url="/api/v1/jobs/job-1/assets/0", mime="image/png")],
        candidate_types=None,
        error=None,
    )
    dumped = resp.model_dump(mode="json")
    assert dumped["jewelry_type"] == "ANKLET"
    assert dumped["service"] == "FEMALE_MODEL_TRADITIONAL"
    assert dumped["assets"][0]["mime"] == "image/png"


def test_job_response_needs_input_shape() -> None:
    resp = JobResponse(
        job_id="job-2",
        status=JobStatus.NEEDS_INPUT,
        service=ServiceType.FEMALE_MODEL_TRADITIONAL,
        jewelry_type=None,
        type_source=None,
        confidence=None,
        mock=False,
        created_at="2026-07-25T10:14:03.000Z",
        updated_at="2026-07-25T10:15:41.000Z",
        completed_at=None,
        deadline_at="2026-07-25T10:29:03.000Z",
        assets=[],
        candidate_types=[
            CandidateType(jewelry_type=JewelryType.ANKLET, confidence=0.52),
            CandidateType(jewelry_type=JewelryType.BRACELET, confidence=0.41),
        ],
        error=ErrorDetail(code="LOW_CONFIDENCE", message="Ambiguous.", job_id="job-2"),
    )
    dumped = resp.model_dump(mode="json")
    assert dumped["candidate_types"][0]["jewelry_type"] == "ANKLET"
    assert dumped["error"]["code"] == "LOW_CONFIDENCE"


def test_job_list_response_shape() -> None:
    listing = JobListResponse(jobs=[], count=0)
    assert listing.model_dump() == {"jobs": [], "count": 0}


def test_resolve_request_parses_jewelry_type() -> None:
    req = ResolveRequest(jewelry_type=JewelryType.ANKLET)
    assert req.jewelry_type == JewelryType.ANKLET


def test_matrix_response_shape() -> None:
    resp = MatrixResponse(
        matrix_version="a3f9c1",
        cached_at="2026-07-25T10:10:00.000Z",
        combinations=[
            MatrixCombination(
                jewelry_type=JewelryType.ANKLET, service=ServiceType.FEMALE_MODEL_TRADITIONAL
            )
        ],
        jewelry_types=[JewelryType.ANKLET, JewelryType.BRACELET],
        services=[ServiceType.FEMALE_MODEL_TRADITIONAL],
    )
    dumped = resp.model_dump(mode="json")
    assert dumped["combinations"][0]["service"] == "FEMALE_MODEL_TRADITIONAL"


def test_error_envelope_shape() -> None:
    env = ErrorEnvelope(
        error=ErrorDetail(code="MATRIX_MISS", message="No row.", job_id=None),
    )
    dumped = env.model_dump()
    assert dumped == {"error": {"code": "MATRIX_MISS", "message": "No row.", "job_id": None}}

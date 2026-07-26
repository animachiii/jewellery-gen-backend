from datetime import UTC, datetime

from app.models.enums import ErrorCode, JewelryType, JobStatus, ServiceType, TypeSource
from app.models.job import Job


def _make_job(**overrides: object) -> Job:
    now = datetime(2026, 7, 26, 12, 0, 0, tzinfo=UTC)
    defaults: dict[str, object] = dict(
        job_id="job-1",
        api_key_name="erp",
        service=ServiceType.FEMALE_MODEL_TRADITIONAL,
        mock=False,
        content_hash="abc123",
        created_at=now,
        updated_at=now,
        deadline_at=now,
        source_ref="drive-file-1",
        source_bytes=1024,
        source_mime="image/png",
    )
    defaults.update(overrides)
    return Job(**defaults)  # type: ignore[arg-type]


def test_round_trip_minimal_job() -> None:
    job = _make_job()
    restored = Job.from_redis_hash(job.to_redis_hash())
    assert restored == job


def test_round_trip_fully_populated_job() -> None:
    now = datetime(2026, 7, 26, 12, 0, 0, tzinfo=UTC)
    job = _make_job(
        jewelry_type_requested=JewelryType.RING,
        callback_url="https://example.com/cb",
        idempotency_key="idem-1",
        deduplicated_from="job-0",
        status=JobStatus.NEEDS_INPUT,
        completed_at=now,
        attempt_count=2,
        jewelry_type_final=JewelryType.RING,
        type_source=TypeSource.CLASSIFIED,
        confidence=0.42,
        candidate_types=[{"jewelry_type": "RING", "confidence": 0.42}],
        prompt_snapshot="a verbatim prompt",
        negative_prompt_snapshot="no blur",
        reference_url_snapshot="https://drive.google.com/x",
        provider_params_snapshot={"strength": 0.8},
        matrix_version="abc123def456",
        provider="fake",
        submission_token="token-1",
        provider_job_id="prov-1",
        billable=True,
        asset_refs=["ref-1", "ref-2"],
        error_code=ErrorCode.LOW_CONFIDENCE,
        error_message="below threshold",
    )
    restored = Job.from_redis_hash(job.to_redis_hash())
    assert restored == job


def test_none_fields_are_absent_keys_not_empty_strings() -> None:
    job = _make_job()
    hash_ = job.to_redis_hash()
    assert "jewelry_type_requested" not in hash_
    assert "completed_at" not in hash_
    assert "error_code" not in hash_


def test_booleans_serialise_as_1_or_0() -> None:
    job = _make_job(mock=True)
    hash_ = job.to_redis_hash()
    assert hash_["mock"] == "1"
    job2 = _make_job(mock=False)
    assert job2.to_redis_hash()["mock"] == "0"


def test_timestamps_are_iso8601_with_z_suffix() -> None:
    job = _make_job()
    hash_ = job.to_redis_hash()
    assert hash_["created_at"].endswith("Z")
    assert "T" in hash_["created_at"]

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime

from app.models.enums import ErrorCode, JewelryType, JobStatus, ServiceType, TypeSource


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _parse_iso(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=UTC)


@dataclass
class Job:
    # Identity & request
    job_id: str
    api_key_name: str
    service: ServiceType
    mock: bool
    content_hash: str
    jewelry_type_requested: JewelryType | None = None
    callback_url: str | None = None
    idempotency_key: str | None = None
    deduplicated_from: str | None = None

    # Lifecycle
    status: JobStatus = JobStatus.QUEUED
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    deadline_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    completed_at: datetime | None = None
    attempt_count: int = 0

    # Source image
    source_ref: str = ""
    source_bytes: int = 0
    source_mime: str = ""

    # Classification
    jewelry_type_final: JewelryType | None = None
    type_source: TypeSource | None = None
    confidence: float | None = None
    candidate_types: list[dict[str, object]] | None = None

    # Matrix snapshot — immutable once set
    prompt_snapshot: str | None = None
    negative_prompt_snapshot: str | None = None
    reference_url_snapshot: str | None = None
    provider_params_snapshot: dict[str, object] | None = None
    matrix_version: str | None = None

    # Generation
    provider: str | None = None
    submission_token: str | None = None
    provider_job_id: str | None = None
    billable: bool | None = None

    # Result
    asset_refs: list[str] = field(default_factory=list)
    error_code: ErrorCode | None = None
    error_message: str | None = None

    def to_redis_hash(self) -> dict[str, str]:
        raw: dict[str, object | None] = {
            "job_id": self.job_id,
            "api_key_name": self.api_key_name,
            "service": self.service.value,
            "mock": "1" if self.mock else "0",
            "content_hash": self.content_hash,
            "jewelry_type_requested": self.jewelry_type_requested.value
            if self.jewelry_type_requested
            else None,
            "callback_url": self.callback_url,
            "idempotency_key": self.idempotency_key,
            "deduplicated_from": self.deduplicated_from,
            "status": self.status.value,
            "created_at": _iso(self.created_at),
            "updated_at": _iso(self.updated_at),
            "deadline_at": _iso(self.deadline_at),
            "completed_at": _iso(self.completed_at) if self.completed_at else None,
            "attempt_count": str(self.attempt_count),
            "source_ref": self.source_ref,
            "source_bytes": str(self.source_bytes),
            "source_mime": self.source_mime,
            "jewelry_type_final": self.jewelry_type_final.value
            if self.jewelry_type_final
            else None,
            "type_source": self.type_source.value if self.type_source else None,
            "confidence": str(self.confidence) if self.confidence is not None else None,
            "candidate_types": json.dumps(self.candidate_types)
            if self.candidate_types is not None
            else None,
            "prompt_snapshot": self.prompt_snapshot,
            "negative_prompt_snapshot": self.negative_prompt_snapshot,
            "reference_url_snapshot": self.reference_url_snapshot,
            "provider_params_snapshot": json.dumps(self.provider_params_snapshot)
            if self.provider_params_snapshot is not None
            else None,
            "matrix_version": self.matrix_version,
            "provider": self.provider,
            "submission_token": self.submission_token,
            "provider_job_id": self.provider_job_id,
            "billable": ("1" if self.billable else "0") if self.billable is not None else None,
            "asset_refs": json.dumps(self.asset_refs),
            "error_code": self.error_code.value if self.error_code else None,
            "error_message": self.error_message,
        }
        return {k: str(v) for k, v in raw.items() if v is not None}

    @classmethod
    def from_redis_hash(cls, data: dict[str, str]) -> "Job":
        def _opt(key: str) -> str | None:
            return data.get(key)

        def _bool(key: str) -> bool:
            return data.get(key) == "1"

        def _opt_bool(key: str) -> bool | None:
            v = data.get(key)
            return None if v is None else v == "1"

        def _opt_float(key: str) -> float | None:
            v = data.get(key)
            return None if v is None else float(v)

        def _opt_json(key: str) -> object | None:
            v = data.get(key)
            return None if v is None else json.loads(v)

        def _opt_dt(key: str) -> datetime | None:
            v = data.get(key)
            return None if v is None else _parse_iso(v)

        candidate_types = _opt_json("candidate_types")
        provider_params = _opt_json("provider_params_snapshot")

        jewelry_type_requested = _opt("jewelry_type_requested")
        jewelry_type_final = _opt("jewelry_type_final")
        type_source = _opt("type_source")
        error_code = _opt("error_code")

        return cls(
            job_id=data["job_id"],
            api_key_name=data["api_key_name"],
            service=ServiceType(data["service"]),
            mock=_bool("mock"),
            content_hash=data["content_hash"],
            jewelry_type_requested=JewelryType(jewelry_type_requested)
            if jewelry_type_requested
            else None,
            callback_url=_opt("callback_url"),
            idempotency_key=_opt("idempotency_key"),
            deduplicated_from=_opt("deduplicated_from"),
            status=JobStatus(data["status"]),
            created_at=_parse_iso(data["created_at"]),
            updated_at=_parse_iso(data["updated_at"]),
            deadline_at=_parse_iso(data["deadline_at"]),
            completed_at=_opt_dt("completed_at"),
            attempt_count=int(data.get("attempt_count", "0")),
            source_ref=data.get("source_ref", ""),
            source_bytes=int(data.get("source_bytes", "0")),
            source_mime=data.get("source_mime", ""),
            jewelry_type_final=JewelryType(jewelry_type_final) if jewelry_type_final else None,
            type_source=TypeSource(type_source) if type_source else None,
            confidence=_opt_float("confidence"),
            candidate_types=candidate_types,  # type: ignore[arg-type]
            prompt_snapshot=_opt("prompt_snapshot"),
            negative_prompt_snapshot=_opt("negative_prompt_snapshot"),
            reference_url_snapshot=_opt("reference_url_snapshot"),
            provider_params_snapshot=provider_params,  # type: ignore[arg-type]
            matrix_version=_opt("matrix_version"),
            provider=_opt("provider"),
            submission_token=_opt("submission_token"),
            provider_job_id=_opt("provider_job_id"),
            billable=_opt_bool("billable"),
            asset_refs=_opt_json("asset_refs") or [],  # type: ignore[arg-type]
            error_code=ErrorCode(error_code) if error_code else None,
            error_message=_opt("error_message"),
        )

"""POST /api/v1/classify-preview — showcase-UI-only classification preview.

NOT part of the frozen v1 job contract (docs/api-routes.md's "Contract
stability" applies to the job lifecycle routes; this is additive tooling for
the showcase page, not something the Flutter ERP integration needs). Exists
because `service` (category + style, e.g. FEMALE_MODEL_TRADITIONAL) must be
supplied at job creation time, but nothing in the frozen contract lets a
human see Gemini's jewelry_type/style read on a photo *before* committing to
a job. This route runs classify_with_style() directly, synchronously,
without creating a Job, touching Redis job state, or writing to Sheets --
purely a preview. Once the human confirms jewelry_type + style in the UI,
the UI constructs the real `service` value itself and calls the existing
POST /generate with an explicit `jewelry_type` (R11: a client-supplied type
is trusted, skipping re-classification and its cost).

Deliberate departure from docs/conventions.md's Async rule ("no blocking I/O
in a request handler... if it can take more than ~100ms, it belongs in the
worker"): a single Gemini call observed at ~7-10s. Every other AI call in
this system goes through the job+poll pattern specifically to avoid a slow
synchronous response, but this route has no job to attach the result to (by
design -- there's nothing to poll yet, since committing doesn't happen until
after confirmation) and is not billable/gated by DAILY_GENERATION_CAP the
way a real generation is. Classification is documented as "cheap,
retryable" (docs/ai-integration.md), so a several-second synchronous
response for a human actively waiting on a preview is judged an acceptable,
narrowly-scoped exception rather than building a second job-lifecycle
concept just for this.
"""

from fastapi import APIRouter, Depends, Request, status

from app.api.deps import rate_limit, require_client_key
from app.api.errors import DomainError, ValidationAppError
from app.api.v1.generate import (
    _InMemoryUploadFile,
    _parse_multipart_form,
    read_and_validate_image,
)
from app.models.enums import ErrorCode
from app.models.schemas import CandidateType, ClassifyPreviewResponse, StyleCandidate
from app.services.classifier import GeminiClassifier, TypeAndStyleResult
from app.worker.retry import DEFAULT_DELAYS, retry_free

router = APIRouter()

# Module-level (not inlined) so tests can monkeypatch to near-zero and stay
# fast, mirroring app/worker/tasks.py's RETRY_DELAYS pattern.
RETRY_DELAYS = DEFAULT_DELAYS


@router.post(
    "/classify-preview",
    status_code=status.HTTP_200_OK,
    response_model=ClassifyPreviewResponse,
)
async def classify_preview(
    request: Request,
    _key_name: str = Depends(require_client_key),
    _rate_limited: None = Depends(rate_limit),
) -> ClassifyPreviewResponse:
    content_type = request.headers.get("content-type", "")
    body = await request.body()
    fields = _parse_multipart_form(content_type, body)

    image_field = fields.get("image")
    if image_field is None:
        raise ValidationAppError("An 'image' file part is required.")

    image_bytes_raw, _filename = image_field
    image_bytes, _mime = await read_and_validate_image(_InMemoryUploadFile(image_bytes_raw))

    classifier = GeminiClassifier()

    async def _do_classify() -> TypeAndStyleResult:
        return await classifier.classify_with_style(image_bytes)

    try:
        result = await retry_free(_do_classify, delays=RETRY_DELAYS)
    except Exception as exc:
        raise DomainError(
            code=ErrorCode.CLASSIFIER_ERROR,
            http_status=502,
            message="Classification failed after retries.",
        ) from exc

    return ClassifyPreviewResponse(
        is_jewelry=result.is_jewelry,
        jewelry_type_predictions=[
            CandidateType(jewelry_type=p.jewelry_type, confidence=p.confidence)
            for p in result.predictions
        ],
        style_predictions=[
            StyleCandidate(style=s.style, confidence=s.confidence)
            for s in result.style_predictions
        ],
    )

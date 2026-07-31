"""Single exception hierarchy and error-envelope handlers.

Per docs/conventions.md -> Error Handling: route code must never raise
HTTPException directly. Raise an AppError (or a subclass) instead; the handlers
registered here convert it into the standard envelope from docs/api-routes.md.
"""

import sentry_sdk
from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.core.logging import get_logger
from app.core.observability import bind_sentry_job_scope
from app.models.enums import ErrorCode

log = get_logger(__name__)


class AppError(Exception):
    """Base class for all application errors.

    `code` covers both docs/schema.md ErrorCode values (domain/business errors)
    and the transport-level codes from docs/api-routes.md -> Error Envelope
    (UNAUTHORIZED, FORBIDDEN, NOT_FOUND, GONE, VALIDATION_ERROR, RATE_LIMITED,
    PAYLOAD_TOO_LARGE). It's typed as `str` rather than the ErrorCode enum so
    both families are representable without an artificial enum merge.
    """

    def __init__(
        self,
        code: str,
        http_status: int,
        message: str,
        job_id: str | None = None,
    ) -> None:
        self.code = code
        self.http_status = http_status
        self.message = message
        self.job_id = job_id
        super().__init__(message)


class UnauthorizedError(AppError):
    def __init__(self, message: str = "Missing or invalid API key.") -> None:
        super().__init__(
            code="UNAUTHORIZED", http_status=status.HTTP_401_UNAUTHORIZED, message=message
        )


class NotFoundError(AppError):
    """Used for both 'job does not exist' and 'job is not yours' (R15) — the
    response body must be byte-identical in both cases, so no distinguishing
    detail is ever included here."""

    def __init__(self, message: str = "Resource not found.") -> None:
        super().__init__(code="NOT_FOUND", http_status=status.HTTP_404_NOT_FOUND, message=message)


class GoneError(AppError):
    def __init__(self, message: str = "Resource is no longer available.") -> None:
        super().__init__(code="GONE", http_status=status.HTTP_410_GONE, message=message)


class RateLimitedError(AppError):
    def __init__(self, message: str = "Rate limit exceeded.") -> None:
        super().__init__(
            code="RATE_LIMITED", http_status=status.HTTP_429_TOO_MANY_REQUESTS, message=message
        )


class BudgetExceededError(AppError):
    def __init__(self, message: str = "Daily generation cap reached.") -> None:
        super().__init__(
            code=ErrorCode.BUDGET_EXCEEDED.value,
            http_status=status.HTTP_429_TOO_MANY_REQUESTS,
            message=message,
        )


class JobNotResolvableError(AppError):
    """Job exists but is not in `needs_input` when POST /jobs/{id}/resolve is
    called. Not exercised in Step 1 (no real routes yet); provided so the error
    taxonomy already accommodates it per docs/api-routes.md."""

    def __init__(self, message: str = "Job is not awaiting input.") -> None:
        super().__init__(
            code="VALIDATION_ERROR", http_status=status.HTTP_409_CONFLICT, message=message
        )


class UnsupportedFormatError(AppError):
    """415 — either the request Content-Type isn't multipart/form-data, or the
    uploaded image's magic bytes don't identify as JPEG/PNG/WebP (R18)."""

    def __init__(self, message: str = "Unsupported format.") -> None:
        super().__init__(
            code=ErrorCode.UNSUPPORTED_FORMAT.value,
            http_status=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            message=message,
        )


class ImageTooLargeError(AppError):
    def __init__(self, message: str = "Image exceeds the maximum allowed size.") -> None:
        super().__init__(
            code=ErrorCode.IMAGE_TOO_LARGE.value,
            http_status=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            message=message,
        )


class InvalidImageError(AppError):
    """400 — Pillow identifies the format but the pixel data itself is corrupt
    (fails `.load()`/`.verify()`). Distinct from UnsupportedFormatError (415),
    which is for bytes that never identify as JPEG/PNG/WebP at all — see
    Checkpoint 2's ".jpg-named PDF" case, which is 415 because the bytes never
    identify as an image format in the first place."""

    def __init__(self, message: str = "Image file is corrupt or unreadable.") -> None:
        super().__init__(
            code=ErrorCode.INVALID_IMAGE.value,
            http_status=status.HTTP_400_BAD_REQUEST,
            message=message,
        )


class ValidationAppError(AppError):
    """Generic 422 for request-validation failures raised directly from route
    code (as opposed to FastAPI's own RequestValidationError path) — e.g. an
    image below the minimum 256x256 dimension, an unknown `service`/
    `jewelry_type` string, or a v2-only service requested in v1."""

    def __init__(self, message: str) -> None:
        super().__init__(
            code="VALIDATION_ERROR",
            http_status=status.HTTP_422_UNPROCESSABLE_ENTITY,
            message=message,
        )


class StorageError(AppError):
    """502 — a job claims an asset via `asset_refs` but the storage adapter
    can't resolve it (e.g. `StorageRefNotFoundError`). A missing file for a
    job that claims to have it is a storage-layer inconsistency, not a 404."""

    def __init__(self, message: str = "Stored asset could not be retrieved.") -> None:
        super().__init__(
            code=ErrorCode.STORAGE_ERROR.value,
            http_status=status.HTTP_502_BAD_GATEWAY,
            message=message,
        )


class DomainError(AppError):
    """Generic constructor for business/domain ErrorCode values (docs/schema.md
    §1) that don't yet have a dedicated subclass."""

    def __init__(
        self,
        code: ErrorCode,
        http_status: int,
        message: str,
        job_id: str | None = None,
    ) -> None:
        super().__init__(code=code.value, http_status=http_status, message=message, job_id=job_id)


def _request_id(request: Request) -> str | None:
    return getattr(request.state, "request_id", None)


def _envelope(
    code: str, message: str, job_id: str | None, request_id: str | None
) -> dict[str, object]:
    return {
        "error": {
            "code": code,
            "message": message,
            "job_id": job_id,
            "request_id": request_id,
        }
    }


def register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def _handle_app_error(request: Request, exc: AppError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.http_status,
            content=_envelope(exc.code, exc.message, exc.job_id, _request_id(request)),
        )

    @app.exception_handler(RequestValidationError)
    async def _handle_validation_error(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        message = "Request validation failed: " + "; ".join(
            f"{'.'.join(str(loc) for loc in err['loc'])}: {err['msg']}" for err in exc.errors()
        )
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content=_envelope("VALIDATION_ERROR", message, None, _request_id(request)),
        )

    @app.exception_handler(Exception)
    async def _handle_unhandled_exception(request: Request, exc: Exception) -> JSONResponse:
        log.exception(
            "http.request.unhandled_exception",
            request_id=_request_id(request),
            path=request.url.path,
        )
        # Phase 8: only a genuinely unhandled exception (this catch-all, the
        # 500 INTERNAL_ERROR path) is reported to Sentry. AppError subclasses
        # and RequestValidationError are expected, typed, already-logged
        # business outcomes (a 404, a 422, a MATRIX_MISS) -- reporting every
        # 4xx would blow through the free tier's event quota on ordinary
        # traffic and bury the signal that actually matters. No-op if Sentry
        # isn't configured (docs/schema.md §6 -- SENTRY_DSN is optional).
        bind_sentry_job_scope()
        sentry_sdk.capture_exception(exc)
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content=_envelope(
                "INTERNAL_ERROR",
                "An internal error occurred.",
                None,
                _request_id(request),
            ),
        )

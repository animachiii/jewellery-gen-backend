"""POST /api/v1/generate — upload validation (Step 2) + submit logic (Step 3).

Step 3 order (docs/business-rules.md — this is the money-safety logic, do not
reorder): idempotency check (R4) -> dedupe check (R3) -> budget check (R5/R6)
-> persist source image -> create Job -> Redis write -> Sheets append (R14,
never fails the request) -> record idempotency -> enqueue ARQ pipeline ->
increment spend (only for billable, only after a successful enqueue).

NOTE for whoever builds Step 6 (the worker pipeline): the enqueued function
name is exactly "run_job_pipeline", taking `(ctx, job_id: str)`. `enqueue_job`
only needs the name as a string, so this enqueue call is safe to write before
that function exists / is registered in `WorkerSettings.functions`.
"""

import email
import io
from datetime import UTC, datetime, timedelta
from email.message import Message
from uuid import uuid4

from fastapi import APIRouter, Depends, Request, status
from PIL import Image, UnidentifiedImageError
from redis.asyncio import Redis

from app.api.deps import rate_limit, require_client_key
from app.api.errors import (
    BudgetExceededError,
    ImageTooLargeError,
    InvalidImageError,
    UnsupportedFormatError,
    ValidationAppError,
)
from app.config import settings
from app.core.logging import get_logger
from app.models.enums import V1_SERVICES, V2_SERVICES, JewelryType, ServiceType
from app.models.job import Job, _iso
from app.models.schemas import GenerateResponse
from app.services import dedupe
from app.services.budget import check_budget, increment_spend
from app.storage.factory import get_storage_adapter
from app.store.redis_store import create_job, get_job, set_row_index
from app.store.sheets_store import GoogleSheetsClient, SheetsClient, safe_append_job_row

router = APIRouter()
log = get_logger(__name__)

# docs/schema.md §2 — same value as app/worker/settings.py's JOB_LOG_TAB.
# Duplicated here rather than imported to avoid a circular import between
# app.api.v1.generate and app.worker.settings (the worker module pulls in
# app.store.rehydrate and app.worker.sweeper, neither of which this route
# needs).
JOB_LOG_TAB = "JobLog"

_MIME_BY_PIL_FORMAT = {
    "JPEG": "image/jpeg",
    "PNG": "image/png",
    "WEBP": "image/webp",
}

_CHUNK_SIZE = 64 * 1024
_MIN_DIMENSION = 256
_SUPPORTED_PIL_FORMATS = {"JPEG", "PNG", "WEBP"}


class _InMemoryUploadFile:
    """Minimal async-.read()-chunked wrapper around bytes already buffered by
    `_parse_multipart_form`, so `read_and_validate_image`'s streaming-read
    loop works identically whether fed a real Starlette `UploadFile` (Step 3,
    once `python-multipart` is available) or a part parsed by the fallback
    parser below."""

    def __init__(self, data: bytes) -> None:
        self._data = data
        self._pos = 0

    async def read(self, size: int = _CHUNK_SIZE) -> bytes:
        chunk = self._data[self._pos : self._pos + size]
        self._pos += len(chunk)
        return chunk


def _parse_multipart_form(content_type: str, body: bytes) -> dict[str, tuple[bytes, str | None]]:
    """Stdlib-only `multipart/form-data` parser: {field_name: (raw_bytes, filename)}.

    `python-multipart` is now pinned in pyproject.toml (Starlette/FastAPI's
    usual `request.form()` backend), but this dev environment has no network
    access to actually install it, so this step falls back to a stdlib-only
    parser to keep validation testable now: MIME multipart is the same
    boundary-delimited structure `multipart/form-data` uses, so wrapping the
    raw body with a synthetic `Content-Type` header and handing it to
    `email.message_from_bytes` parses it correctly without extra deps.

    KNOWN LIMITATION: unlike Starlette's real UploadFile (which streams from
    the ASGI receive channel into a SpooledTemporaryFile), this reads the
    *entire* request body into memory via `await request.body()` before any
    size check runs — so the true "abort before buffering the whole file"
    guarantee (R18) is only proven at the `read_and_validate_image` unit-test
    level here, not for this route end-to-end. Once `python-multipart` is
    installed, swap `_parse_multipart_form`/`_InMemoryUploadFile` back out for
    `await request.form()` + the real `UploadFile` to restore true streaming.
    """
    synthetic_header = f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode()
    msg: Message = email.message_from_bytes(synthetic_header + body)
    fields: dict[str, tuple[bytes, str | None]] = {}
    if not msg.is_multipart():
        return fields
    payload = msg.get_payload()
    if not isinstance(payload, list):
        return fields
    for part in payload:
        if not isinstance(part, Message):
            continue
        disposition = part.get("Content-Disposition", "")
        name: str | None = None
        filename: str | None = None
        for item in disposition.split(";"):
            item = item.strip()
            if item.startswith("name="):
                name = item[len("name=") :].strip('"')
            elif item.startswith("filename="):
                filename = item[len("filename=") :].strip('"')
        if name is None:
            continue
        part_bytes = part.get_payload(decode=True)
        fields[name] = (part_bytes if isinstance(part_bytes, bytes) else b"", filename)
    return fields


def parse_service(raw: str) -> ServiceType:
    """Parse the `service` form field. Unknown string -> 422 listing v1
    services; a valid-but-v2 value -> 422 naming it as not-yet-supported (R19).
    """
    try:
        service = ServiceType(raw)
    except ValueError as exc:
        valid = ", ".join(sorted(s.value for s in V1_SERVICES))
        raise ValidationAppError(
            f"Unknown service '{raw}'. Valid v1 services are: {valid}."
        ) from exc
    if service in V2_SERVICES:
        raise ValidationAppError(f"Service '{raw}' is a v2 feature and is not yet supported in v1.")
    return service


def parse_jewelry_type(raw: str | None) -> JewelryType | None:
    """Parse the optional `jewelry_type` form field. Omit -> None (triggers
    classification). Invalid string -> 422."""
    if raw is None or raw == "":
        return None
    try:
        return JewelryType(raw)
    except ValueError as exc:
        valid = ", ".join(t.value for t in JewelryType)
        raise ValidationAppError(
            f"Unknown jewelry_type '{raw}'. Valid types are: {valid}."
        ) from exc


async def read_and_validate_image(upload_file: object) -> tuple[bytes, str]:
    """Stream-read `upload_file` (a Starlette UploadFile) in chunks up to
    `settings.max_image_bytes`, aborting the read loop before buffering past
    the cap (413 IMAGE_TOO_LARGE). Then validate the bytes by magic bytes via
    Pillow: undecodable-as-any-image -> 415 UNSUPPORTED_FORMAT (Checkpoint 2's
    ".jpg-named PDF" case); decodable but wrong format -> 415; decodable,
    right format, but corrupt pixel data -> 400 INVALID_IMAGE; decodable,
    right format, but smaller than 256x256 -> 422 (Checkpoint 2).

    Returns `(data, mime)` — `mime` derived from the Pillow-detected format
    (Step 3 needs it for the stored Job's `source_mime`; threading it through
    here avoids re-validating already-valid bytes a second time in the route).
    """
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk: bytes = await upload_file.read(_CHUNK_SIZE)  # type: ignore[attr-defined]
        if not chunk:
            break
        total += len(chunk)
        if total > settings.max_image_bytes:
            raise ImageTooLargeError(
                f"Image exceeds the maximum allowed size of {settings.max_image_bytes} bytes."
            )
        chunks.append(chunk)
    data = b"".join(chunks)

    try:
        img = Image.open(io.BytesIO(data))
        img_format = img.format
    except UnidentifiedImageError as exc:
        raise UnsupportedFormatError(
            "File could not be identified as an image. Only JPEG, PNG, and WebP are supported."
        ) from exc
    except Exception as exc:
        raise UnsupportedFormatError(
            "File could not be identified as an image. Only JPEG, PNG, and WebP are supported."
        ) from exc

    if img_format not in _SUPPORTED_PIL_FORMATS:
        raise UnsupportedFormatError(
            f"Unsupported image format '{img_format}'. Only JPEG, PNG, and WebP are supported."
        )

    try:
        img.load()
    except Exception as exc:
        raise InvalidImageError("Image file is corrupt or could not be decoded.") from exc

    width, height = img.size
    if width < _MIN_DIMENSION or height < _MIN_DIMENSION:
        raise ValidationAppError(
            f"Image dimensions {width}x{height} are below the minimum "
            f"{_MIN_DIMENSION}x{_MIN_DIMENSION}."
        )

    mime = _MIME_BY_PIL_FORMAT[img_format]
    return data, mime


def _build_sheets_client() -> SheetsClient | None:
    """Mirrors app/worker/settings.py::_build_sheets_client — kept as a small
    local duplicate rather than a shared import to avoid coupling this route
    to the worker module (see the JOB_LOG_TAB comment above)."""
    try:
        return GoogleSheetsClient(settings.google_service_account_info)
    except Exception:
        log.warning("sheets.client.unavailable")
        return None


def _parse_mock(raw: bytes | None) -> bool:
    return raw is not None and raw.decode().strip().lower() == "true"


@router.post("/generate", status_code=status.HTTP_202_ACCEPTED, response_model=GenerateResponse)
async def generate(
    request: Request,
    key_name: str = Depends(require_client_key),
    _rate_limited: None = Depends(rate_limit),
) -> GenerateResponse:
    redis: Redis = request.app.state.redis
    content_type = request.headers.get("content-type", "")
    if not content_type.startswith("multipart/form-data"):
        raise UnsupportedFormatError(
            "Content-Type must be multipart/form-data; base64 JSON bodies are not accepted."
        )

    body = await request.body()
    fields = _parse_multipart_form(content_type, body)

    image_field = fields.get("image")
    service_field = fields.get("service")
    jewelry_type_field = fields.get("jewelry_type")
    mock_field = fields.get("mock")
    callback_url_field = fields.get("callback_url")

    if image_field is None:
        raise ValidationAppError("An 'image' file part is required.")
    if service_field is None:
        raise ValidationAppError("A 'service' field is required.")

    image_bytes_raw, _filename = image_field
    service_raw, _ = service_field
    jewelry_type_raw = jewelry_type_field[0].decode() if jewelry_type_field is not None else None
    mock = _parse_mock(mock_field[0] if mock_field is not None else None)
    callback_url = callback_url_field[0].decode() if callback_url_field is not None else None
    idempotency_key = request.headers.get("Idempotency-Key")

    service = parse_service(service_raw.decode())
    jewelry_type = parse_jewelry_type(jewelry_type_raw)

    # --- Step 3: idempotency check first (R4) ---
    if idempotency_key:
        existing_job_id = await dedupe.check_idempotency(redis, key_name, idempotency_key)
        if existing_job_id is not None:
            existing_job = await get_job(redis, existing_job_id)
            if existing_job is not None:
                return _to_response(existing_job, deduplicated=False)
            # Idempotency record points at a job that's since expired from
            # Redis (edge case) — treat as if no idempotency record existed.

    image_bytes, mime = await read_and_validate_image(_InMemoryUploadFile(image_bytes_raw))
    content_hash = dedupe.content_hash(
        image_bytes, service.value, jewelry_type.value if jewelry_type else None
    )

    # --- Step 3: dedupe check (R3) ---
    dedupe_job_id = await dedupe.check_dedupe(redis, content_hash)
    if dedupe_job_id is not None:
        dedupe_job = await get_job(redis, dedupe_job_id)
        if (
            dedupe_job is not None
            and dedupe_job.status.value == "succeeded"
            and dedupe_job.mock == mock
        ):
            return _to_response(dedupe_job, deduplicated=True)
        # A dedupe key pointing at a non-succeeded (or missing) job shouldn't
        # happen — record_dedupe only ever records succeeded jobs — but treat
        # defensively as a cache miss rather than trusting stale state.
        #
        # The `mock` equality check is load-bearing, not defensive: R3's
        # content_hash deliberately excludes `mock`, so a mock job and a real
        # request for the same image+service+jewelry_type hash identically.
        # Without this, a prior mock run's FakeProvider placeholder is served
        # back as `deduplicated: true` for a real, billable request — the
        # client asks for a real generation and silently gets a fake asset in
        # ~10s. record_dedupe now also refuses to write mock jobs, so this
        # branch mainly catches keys written before that guard existed.

    billable = not mock
    if billable and not await check_budget(redis):
        raise BudgetExceededError()

    job_id = str(uuid4())
    ext = mime.split("/", 1)[1]
    storage = get_storage_adapter()
    source_ref = await storage.put(image_bytes, filename=f"{job_id}.{ext}", mime=mime)

    now_utc = datetime.now(UTC)
    job = Job(
        job_id=job_id,
        api_key_name=key_name,
        service=service,
        mock=mock,
        content_hash=content_hash,
        jewelry_type_requested=jewelry_type,
        callback_url=callback_url,
        idempotency_key=idempotency_key,
        created_at=now_utc,
        updated_at=now_utc,
        deadline_at=now_utc + timedelta(seconds=settings.job_deadline_seconds),
        source_ref=source_ref,
        source_bytes=len(image_bytes),
        source_mime=mime,
        billable=billable,
    )

    await create_job(redis, job)

    sheets_client = _build_sheets_client()
    if sheets_client is not None:
        row_index = await safe_append_job_row(
            redis, sheets_client, settings.google_sheet_id, JOB_LOG_TAB, job
        )
        if row_index is not None:
            await set_row_index(redis, job.job_id, row_index)

    if idempotency_key:
        await dedupe.record_idempotency(redis, key_name, idempotency_key, job.job_id)

    arq_pool = request.app.state.arq_pool
    await arq_pool.enqueue_job("run_job_pipeline", job.job_id)

    if billable:
        await increment_spend(redis)

    return _to_response(job, deduplicated=False)


def _to_response(job: Job, *, deduplicated: bool) -> GenerateResponse:
    return GenerateResponse(
        job_id=job.job_id,
        status=job.status,
        poll_url=f"/api/v1/jobs/{job.job_id}",
        deduplicated=deduplicated,
        created_at=_iso(job.created_at),
    )

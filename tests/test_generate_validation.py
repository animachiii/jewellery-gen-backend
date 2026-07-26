import io

import pytest
from PIL import Image

from app.api.errors import (
    ImageTooLargeError,
    InvalidImageError,
    UnsupportedFormatError,
    ValidationAppError,
)
from app.api.v1.generate import parse_jewelry_type, parse_service, read_and_validate_image
from app.config import settings
from app.models.enums import JewelryType, ServiceType


def _png_bytes(width: int, height: int) -> bytes:
    img = Image.new("RGB", (width, height), color="red")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


class _FakeUploadFile:
    """Minimal stand-in for Starlette's UploadFile: async chunked .read()."""

    def __init__(self, data: bytes, chunk_size: int = 64 * 1024) -> None:
        self._data = data
        self._pos = 0
        self._chunk_size = chunk_size

    async def read(self, size: int = -1) -> bytes:
        read_size = size if size and size > 0 else self._chunk_size
        chunk = self._data[self._pos : self._pos + read_size]
        self._pos += len(chunk)
        return chunk


class _AbortingUploadFile:
    """Proves the read loop aborts before buffering the whole file: raises if
    asked to read past the point where the 413 threshold should already have
    fired."""

    def __init__(self, total_size: int, threshold: int, chunk_size: int = 64 * 1024) -> None:
        self._total_size = total_size
        self._threshold = threshold
        self._served = 0
        self._chunk_size = chunk_size

    async def read(self, size: int = -1) -> bytes:
        if self._served > self._threshold + self._chunk_size:
            raise AssertionError(
                "read() was called after the size cap should have aborted the loop "
                "— the implementation is buffering past the threshold instead of "
                "aborting the stream early."
            )
        read_size = size if size and size > 0 else self._chunk_size
        remaining = self._total_size - self._served
        chunk_len = min(read_size, remaining)
        if chunk_len <= 0:
            return b""
        self._served += chunk_len
        return b"\x00" * chunk_len


async def test_valid_512x512_png_passes_validation() -> None:
    data = _png_bytes(512, 512)
    result = await read_and_validate_image(_FakeUploadFile(data))
    assert result == data


async def test_100x100_png_raises_validation_error_422() -> None:
    data = _png_bytes(100, 100)
    with pytest.raises(ValidationAppError) as exc_info:
        await read_and_validate_image(_FakeUploadFile(data))
    assert exc_info.value.http_status == 422


async def test_jpg_named_pdf_bytes_raise_unsupported_format_415() -> None:
    pdf_bytes = b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\nrest of a fake pdf body"
    with pytest.raises(UnsupportedFormatError) as exc_info:
        await read_and_validate_image(_FakeUploadFile(pdf_bytes))
    assert exc_info.value.http_status == 415
    assert exc_info.value.code == "UNSUPPORTED_FORMAT"


async def test_oversized_upload_aborts_stream_and_raises_413() -> None:
    # 20 MB nominal size vs the configured cap — proves the read loop aborts
    # once the running total crosses settings.max_image_bytes rather than
    # reading the full 20 MB into memory first (the _AbortingUploadFile fake
    # raises AssertionError if .read() is called well past the cap).
    total_size = 20 * 1024 * 1024
    fake = _AbortingUploadFile(total_size=total_size, threshold=settings.max_image_bytes)
    with pytest.raises(ImageTooLargeError) as exc_info:
        await read_and_validate_image(fake)
    assert exc_info.value.http_status == 413
    assert exc_info.value.code == "IMAGE_TOO_LARGE"


async def test_corrupt_jpeg_raises_invalid_image_400() -> None:
    img = Image.new("RGB", (300, 300), color="blue")
    buf = io.BytesIO()
    img.save(buf, format="JPEG")
    good_bytes = buf.getvalue()
    # Truncate: keeps the JPEG magic bytes/header (so Pillow's format sniff
    # still succeeds) but corrupts the body so .load() fails.
    truncated = good_bytes[: len(good_bytes) // 2]
    with pytest.raises((InvalidImageError, UnsupportedFormatError)) as exc_info:
        await read_and_validate_image(_FakeUploadFile(truncated))
    # Whichever it raises, it must not be a 2xx/422 — both InvalidImageError
    # (400) and UnsupportedFormatError (415) are acceptable outcomes for
    # truncated bytes depending on how aggressively Pillow's lazy decode
    # detects the corruption at .load() time.
    assert exc_info.value.http_status in (400, 415)


def test_parse_service_accepts_valid_v1_service() -> None:
    assert parse_service("FEMALE_MODEL_TRADITIONAL") == ServiceType.FEMALE_MODEL_TRADITIONAL


def test_parse_service_rejects_v2_service_naming_it_as_unsupported() -> None:
    with pytest.raises(ValidationAppError) as exc_info:
        parse_service("REMOVE_BG")
    assert exc_info.value.http_status == 422
    assert "v2" in exc_info.value.message.lower()
    assert "REMOVE_BG" in exc_info.value.message


def test_parse_service_rejects_unknown_string_listing_v1_services() -> None:
    with pytest.raises(ValidationAppError) as exc_info:
        parse_service("BANANA")
    assert exc_info.value.http_status == 422
    for service in (
        "FEMALE_MODEL_TRADITIONAL",
        "FEMALE_MODEL_MODERN",
        "MALE_MODEL_TRADITIONAL",
        "MALE_MODEL_MODERN",
        "MANNEQUIN_TRADITIONAL",
        "MANNEQUIN_MODERN",
        "PRODUCT_STYLING_TRADITIONAL",
        "PRODUCT_STYLING_MODERN",
    ):
        assert service in exc_info.value.message


def test_parse_jewelry_type_none_returns_none() -> None:
    assert parse_jewelry_type(None) is None


def test_parse_jewelry_type_valid_string() -> None:
    assert parse_jewelry_type("ANKLET") == JewelryType.ANKLET


def test_parse_jewelry_type_invalid_string_raises_422() -> None:
    with pytest.raises(ValidationAppError) as exc_info:
        parse_jewelry_type("NOT_A_TYPE")
    assert exc_info.value.http_status == 422

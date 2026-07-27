import io

import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from PIL import Image
from redis.asyncio import Redis

from app.config import settings
from app.main import app

CLIENT_KEY = "secret123"  # matches .env API_KEYS=erp:secret123


class FakeArqPool:
    async def enqueue_job(self, name: str, *args: object) -> None:
        pass


def _png_bytes(width: int, height: int) -> bytes:
    img = Image.new("RGB", (width, height), color="green")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


@pytest_asyncio.fixture
async def client(redis: Redis) -> AsyncClient:  # noqa: ARG001 - flushes DB 15
    app.state.redis = redis
    # This route's own tests must not depend on whatever left `app.state`
    # (a process-wide singleton) in whatever state a previous test module
    # ran in — always set a working arq_pool explicitly, matching every
    # other test module that exercises /api/v1/generate.
    app.state.arq_pool = FakeArqPool()
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    return AsyncClient(transport=transport, base_url="http://test")


async def test_json_body_returns_415_mentioning_multipart(client: AsyncClient) -> None:
    async with client as ac:
        resp = await ac.post(
            "/api/v1/generate",
            json={"image": "base64stuff", "service": "FEMALE_MODEL_TRADITIONAL"},
            headers={"X-API-Key": CLIENT_KEY},
        )
    assert resp.status_code == 415
    body = resp.json()
    assert body["error"]["code"] == "UNSUPPORTED_FORMAT"
    assert "multipart/form-data" in body["error"]["message"]


async def test_jpg_named_pdf_returns_415_via_route(client: AsyncClient) -> None:
    pdf_bytes = b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\nrest of a fake pdf body"
    async with client as ac:
        resp = await ac.post(
            "/api/v1/generate",
            data={"service": "FEMALE_MODEL_TRADITIONAL"},
            files={"image": ("photo.jpg", pdf_bytes, "image/jpeg")},
            headers={"X-API-Key": CLIENT_KEY},
        )
    assert resp.status_code == 415
    assert resp.json()["error"]["code"] == "UNSUPPORTED_FORMAT"


async def test_100x100_png_returns_422_via_route(client: AsyncClient) -> None:
    data = _png_bytes(100, 100)
    async with client as ac:
        resp = await ac.post(
            "/api/v1/generate",
            data={"service": "FEMALE_MODEL_TRADITIONAL"},
            files={"image": ("photo.png", data, "image/png")},
            headers={"X-API-Key": CLIENT_KEY},
        )
    assert resp.status_code == 422


async def test_valid_512x512_png_does_not_fail_validation(client: AsyncClient) -> None:
    data = _png_bytes(512, 512)
    async with client as ac:
        resp = await ac.post(
            "/api/v1/generate",
            data={"service": "FEMALE_MODEL_TRADITIONAL"},
            files={"image": ("photo.png", data, "image/png")},
            headers={"X-API-Key": CLIENT_KEY},
        )
    # Validation passes; the route is fully implemented as of Phase 1 Step 3,
    # so a valid image now reaches 202 Accepted. What matters here is that
    # the response is NOT one of the image-validation error codes
    # (UNSUPPORTED_FORMAT, IMAGE_TOO_LARGE, INVALID_IMAGE) — proving the
    # image itself passed validation.
    body = resp.json()
    if "error" in body:
        assert body["error"]["code"] not in (
            "UNSUPPORTED_FORMAT",
            "IMAGE_TOO_LARGE",
            "INVALID_IMAGE",
        )
    else:
        assert resp.status_code == 202


async def test_remove_bg_service_returns_422_naming_v2(client: AsyncClient) -> None:
    data = _png_bytes(512, 512)
    async with client as ac:
        resp = await ac.post(
            "/api/v1/generate",
            data={"service": "REMOVE_BG"},
            files={"image": ("photo.png", data, "image/png")},
            headers={"X-API-Key": CLIENT_KEY},
        )
    assert resp.status_code == 422
    assert "v2" in resp.json()["error"]["message"].lower()


async def test_banana_service_returns_422_listing_v1_services(client: AsyncClient) -> None:
    data = _png_bytes(512, 512)
    async with client as ac:
        resp = await ac.post(
            "/api/v1/generate",
            data={"service": "BANANA"},
            files={"image": ("photo.png", data, "image/png")},
            headers={"X-API-Key": CLIENT_KEY},
        )
    assert resp.status_code == 422
    message = resp.json()["error"]["message"]
    assert "FEMALE_MODEL_TRADITIONAL" in message


async def test_missing_api_key_returns_401(client: AsyncClient) -> None:
    data = _png_bytes(512, 512)
    async with client as ac:
        resp = await ac.post(
            "/api/v1/generate",
            data={"service": "FEMALE_MODEL_TRADITIONAL"},
            files={"image": ("photo.png", data, "image/png")},
        )
    assert resp.status_code == 401


async def test_settings_max_image_bytes_matches_15mb_default() -> None:
    assert settings.max_image_bytes == 15_728_640

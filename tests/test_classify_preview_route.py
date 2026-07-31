"""POST /api/v1/classify-preview — showcase-UI-only, see app/api/v1/classify.py.

No test may call a real Gemini API (docs/conventions.md -> Testing) — the
route's `GeminiClassifier()` construction is monkeypatched at the module
level, mirroring how tests/test_generate_route.py fakes out Sheets.
"""

import io

import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from PIL import Image
from pytest import MonkeyPatch
from redis.asyncio import Redis

import app.api.v1.classify as classify_route
from app.main import app
from app.models.enums import JewelryType, Style
from app.services.classifier import Prediction, StylePrediction, TypeAndStyleResult

CLIENT_KEY = "secret123"  # matches .env API_KEYS=erp:secret123


def _png_bytes(width: int = 512, height: int = 512) -> bytes:
    img = Image.new("RGB", (width, height), color="gold")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


class _FakeClassifier:
    def __init__(self, result: TypeAndStyleResult | None = None, *, fail: bool = False) -> None:
        self._result = result or TypeAndStyleResult(
            is_jewelry=True,
            predictions=[
                Prediction(jewelry_type=JewelryType.NECKLACE, confidence=0.9),
                Prediction(jewelry_type=JewelryType.BRACELET, confidence=0.06),
                Prediction(jewelry_type=JewelryType.BANGLE, confidence=0.04),
            ],
            style_predictions=[
                StylePrediction(style=Style.TRADITIONAL, confidence=0.85),
                StylePrediction(style=Style.MODERN, confidence=0.15),
            ],
        )
        self._fail = fail

    async def classify_with_style(self, image_bytes: bytes) -> TypeAndStyleResult:
        if self._fail:
            raise RuntimeError("simulated Gemini failure")
        return self._result


@pytest_asyncio.fixture
async def client(redis: Redis, monkeypatch: MonkeyPatch) -> AsyncClient:  # noqa: ARG001
    app.state.redis = redis
    monkeypatch.setattr(classify_route, "GeminiClassifier", lambda: _FakeClassifier())
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    return AsyncClient(transport=transport, base_url="http://test")


async def test_classify_preview_returns_type_and_style_predictions(client: AsyncClient) -> None:
    async with client as ac:
        resp = await ac.post(
            "/api/v1/classify-preview",
            files={"image": ("photo.png", _png_bytes(), "image/png")},
            headers={"X-API-Key": CLIENT_KEY},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["is_jewelry"] is True
    assert body["jewelry_type_predictions"][0] == {
        "jewelry_type": "NECKLACE",
        "confidence": 0.9,
    }
    assert body["style_predictions"][0] == {"style": "TRADITIONAL", "confidence": 0.85}
    assert len(body["jewelry_type_predictions"]) == 3
    assert len(body["style_predictions"]) == 2


async def test_classify_preview_missing_image_returns_422(client: AsyncClient) -> None:
    async with client as ac:
        resp = await ac.post(
            "/api/v1/classify-preview",
            data={},
            headers={"X-API-Key": CLIENT_KEY},
        )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "VALIDATION_ERROR"


async def test_classify_preview_missing_api_key_returns_401(client: AsyncClient) -> None:
    async with client as ac:
        resp = await ac.post(
            "/api/v1/classify-preview",
            files={"image": ("photo.png", _png_bytes(), "image/png")},
        )
    assert resp.status_code == 401


async def test_classify_preview_does_not_create_a_job(client: AsyncClient, redis: Redis) -> None:
    async with client as ac:
        resp = await ac.post(
            "/api/v1/classify-preview",
            files={"image": ("photo.png", _png_bytes(), "image/png")},
            headers={"X-API-Key": CLIENT_KEY},
        )
    assert resp.status_code == 200
    assert "job_id" not in resp.json()
    keys = await redis.keys("job:*")
    assert keys == []


async def test_classify_preview_classifier_failure_returns_502(
    client: AsyncClient, monkeypatch: MonkeyPatch
) -> None:
    monkeypatch.setattr(classify_route, "RETRY_DELAYS", (0.0, 0.0, 0.0))
    monkeypatch.setattr(classify_route, "GeminiClassifier", lambda: _FakeClassifier(fail=True))
    async with client as ac:
        resp = await ac.post(
            "/api/v1/classify-preview",
            files={"image": ("photo.png", _png_bytes(), "image/png")},
            headers={"X-API-Key": CLIENT_KEY},
        )
    assert resp.status_code == 502
    assert resp.json()["error"]["code"] == "CLASSIFIER_ERROR"

"""GET /api/v1/matrix, backed by a real (fake-client-sourced) Sheets read."""

import json

import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from pytest import MonkeyPatch
from redis.asyncio import Redis

import app.api.v1.matrix as matrix_route
from app.main import app
from tests.fakes.fake_sheets_client import FakeSheetsClient

CLIENT_KEY = "secret123"  # matches .env API_KEYS=erp:secret123

_HEADER = ["", "Anklets", "Necklace", "Earrings", "Bangles", "Bracelets", "Hipbelt", "Ring"]


def _matrix_rows() -> list[list[str]]:
    url = "https://drive.google.com/file/d/abc123/view"
    return [
        _HEADER,
        ["Female Model"],
        ["Traditional"] + [""] * 6 + [f"A ring prompt {url}"],
    ]


class FakeArqPool:
    async def enqueue_job(self, name: str, *args: object) -> None:
        pass


@pytest_asyncio.fixture
async def client(redis: Redis, monkeypatch: MonkeyPatch) -> AsyncClient:
    app.state.redis = redis
    app.state.arq_pool = FakeArqPool()
    # No test may call a real external service (docs/conventions.md ->
    # Testing) — stand in for the route's real GoogleSheetsClient
    # construction with a FakeSheetsClient carrying a synthetic sheet.
    monkeypatch.setattr(
        matrix_route, "_build_sheets_client", lambda: FakeSheetsClient(rows=_matrix_rows())
    )
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    return AsyncClient(transport=transport, base_url="http://test")


async def test_matrix_returns_combinations_types_services_and_version(
    client: AsyncClient,
) -> None:
    async with client as ac:
        resp = await ac.get("/api/v1/matrix", headers={"X-API-Key": CLIENT_KEY})
    assert resp.status_code == 200
    body = resp.json()
    assert body["matrix_version"]
    assert body["cached_at"]
    assert len(body["combinations"]) > 0
    assert len(body["jewelry_types"]) > 0
    assert len(body["services"]) > 0
    for combo in body["combinations"]:
        assert set(combo.keys()) == {"jewelry_type", "service"}


async def test_matrix_never_leaks_prompt_text(client: AsyncClient) -> None:
    async with client as ac:
        resp = await ac.get("/api/v1/matrix", headers={"X-API-Key": CLIENT_KEY})
    assert resp.status_code == 200
    raw_text = json.dumps(resp.json()).lower()
    assert "prompt" not in raw_text


async def test_matrix_missing_api_key_returns_401(client: AsyncClient) -> None:
    async with client as ac:
        resp = await ac.get("/api/v1/matrix")
    assert resp.status_code == 401

"""POST /api/v1/admin/matrix/refresh — real forced re-read (Phase 3 Step 2)."""

import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from pytest import MonkeyPatch
from redis.asyncio import Redis

import app.api.v1.admin as admin_route
from app.main import app
from tests.fakes.fake_sheets_client import FakeSheetsClient

ADMIN_KEY = "local-admin-key"  # matches .env ADMIN_API_KEY

_HEADER = ["", "Anklets", "Necklace", "Earrings", "Bangles", "Bracelets", "Hipbelt", "Ring"]
_URL = "https://drive.google.com/file/d/abc123/view"


def _matrix_rows() -> list[list[str]]:
    return [
        _HEADER,
        ["Female Model"],
        ["Traditional"] + [""] * 6 + [f"A ring prompt {_URL}"],
    ]


class FakeArqPool:
    async def enqueue_job(self, name: str, *args: object) -> None:
        pass


@pytest_asyncio.fixture
async def fake_client() -> FakeSheetsClient:
    return FakeSheetsClient(rows=_matrix_rows())


@pytest_asyncio.fixture
async def client(
    redis: Redis, monkeypatch: MonkeyPatch, fake_client: FakeSheetsClient
) -> AsyncClient:
    app.state.redis = redis
    app.state.arq_pool = FakeArqPool()
    monkeypatch.setattr(admin_route, "_build_sheets_client", lambda: fake_client)
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    return AsyncClient(transport=transport, base_url="http://test")


async def test_admin_refresh_returns_real_version_rows_and_changed(
    client: AsyncClient, fake_client: FakeSheetsClient
) -> None:
    async with client as ac:
        resp = await ac.post(
            "/api/v1/admin/matrix/refresh", headers={"X-API-Key": ADMIN_KEY}
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["matrix_version"]
    assert len(body["matrix_version"]) == 16
    assert body["rows_loaded"] == 1
    assert body["changed"] is True
    assert fake_client.read_all_rows_calls == 1


async def test_admin_refresh_forces_reread_even_when_cache_warm(
    client: AsyncClient, fake_client: FakeSheetsClient
) -> None:
    async with client as ac:
        first = await ac.post(
            "/api/v1/admin/matrix/refresh", headers={"X-API-Key": ADMIN_KEY}
        )
        assert first.status_code == 200
        assert fake_client.read_all_rows_calls == 1

        second = await ac.post(
            "/api/v1/admin/matrix/refresh", headers={"X-API-Key": ADMIN_KEY}
        )
    assert second.status_code == 200
    # Cache is still warm (well within MATRIX_CACHE_TTL) but force=True must
    # bypass it and re-read Sheets anyway.
    assert fake_client.read_all_rows_calls == 2
    assert second.json()["changed"] is False  # identical content -> unchanged


async def test_admin_refresh_missing_admin_key_returns_401(client: AsyncClient) -> None:
    async with client as ac:
        resp = await ac.post("/api/v1/admin/matrix/refresh")
    assert resp.status_code == 401

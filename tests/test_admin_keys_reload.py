"""POST /api/v1/admin/keys/reload — Phase 7 Step 3, key rotation without a restart."""

import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from pytest import MonkeyPatch
from redis.asyncio import Redis

from app.config import settings
from app.main import app

ADMIN_KEY = "local-admin-key"  # matches .env ADMIN_API_KEY
ORIGINAL_CLIENT_KEY = "secret123"  # matches .env API_KEYS=erp:secret123


@pytest_asyncio.fixture
async def client(redis: Redis) -> AsyncClient:
    app.state.redis = redis
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    return AsyncClient(transport=transport, base_url="http://test")


async def test_reload_rejects_missing_admin_key(client: AsyncClient) -> None:
    async with client as ac:
        resp = await ac.post("/api/v1/admin/keys/reload")
    assert resp.status_code == 401


async def test_reload_picks_up_a_newly_added_key_without_restart(
    client: AsyncClient, monkeypatch: MonkeyPatch
) -> None:
    original_keys = dict(settings.api_keys)
    monkeypatch.setenv("API_KEYS", "erp:secret123,newclient:brandnewkey456")

    async with client as ac:
        resp = await ac.post("/api/v1/admin/keys/reload", headers={"X-API-Key": ADMIN_KEY})
        assert resp.status_code == 200
        assert resp.json()["keys_loaded"] == 2

        auth_check = await ac.get("/api/v1/matrix", headers={"X-API-Key": "brandnewkey456"})
    # 200 (or at least not 401) proves the new key is now recognized — the
    # route's own business logic (matrix availability) isn't this test's concern.
    assert auth_check.status_code != 401

    # Restore process-wide settings so later tests in this session aren't
    # affected by this test's env mutation.
    settings.api_keys = original_keys


async def test_reload_rejects_a_removed_key_without_restart(
    client: AsyncClient, monkeypatch: MonkeyPatch
) -> None:
    original_keys = dict(settings.api_keys)
    # Reload with a key set that no longer contains the original client key.
    monkeypatch.setenv("API_KEYS", "onlyclient:onlykey789")

    async with client as ac:
        resp = await ac.post("/api/v1/admin/keys/reload", headers={"X-API-Key": ADMIN_KEY})
        assert resp.status_code == 200

        old_key_check = await ac.get("/api/v1/jobs", headers={"X-API-Key": ORIGINAL_CLIENT_KEY})
    assert old_key_check.status_code == 401

    settings.api_keys = original_keys


async def test_reload_with_no_env_value_returns_422(
    client: AsyncClient, monkeypatch: MonkeyPatch
) -> None:
    monkeypatch.delenv("API_KEYS", raising=False)

    async with client as ac:
        resp = await ac.post("/api/v1/admin/keys/reload", headers={"X-API-Key": ADMIN_KEY})
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "VALIDATION_ERROR"

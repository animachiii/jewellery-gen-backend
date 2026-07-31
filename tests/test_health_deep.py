"""Phase 8 Step 4/5 — GET /health/deep real checks (docs/api-routes.md ->
Health): only a Redis failure returns 503; Sheets/storage degrade the
response to "degraded" at 200 instead. Also Step 5's spend-vs-cap block.
"""

import time
from datetime import UTC, datetime

import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from pytest import MonkeyPatch
from redis.asyncio import Redis

from app.config import settings
from app.main import app
from tests.fakes.fake_sheets_client import FakeSheetsClient

ADMIN_KEY = "local-admin-key"  # matches .env ADMIN_API_KEY


@pytest_asyncio.fixture
async def client(redis: Redis, monkeypatch: MonkeyPatch) -> AsyncClient:
    app.state.redis = redis
    # Keep this route's own tests isolated from whatever real backend .env
    # configures (docs/conventions.md -> Testing: no real external service)
    # -- same pattern as tests/test_storage.py / tests/test_jobs_routes.py.
    monkeypatch.setattr(settings, "storage_backend", "local")
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    return AsyncClient(transport=transport, base_url="http://test")


async def test_all_healthy_returns_200_ok(client: AsyncClient, monkeypatch: MonkeyPatch) -> None:
    import app.main as main_module

    monkeypatch.setattr(
        main_module, "_build_sheets_client_for_health", lambda: FakeSheetsClient(rows=[["h"]])
    )

    async with client as ac:
        resp = await ac.get("/health/deep", headers={"X-API-Key": ADMIN_KEY})
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["checks"]["redis"]["ok"] is True
    assert body["checks"]["sheets"]["ok"] is True
    assert body["checks"]["storage"]["ok"] is True
    assert body["checks"]["storage"]["backend"] == "local"
    assert body["checks"]["queue"]["ok"] is True
    assert body["checks"]["queue"]["depth"] == 0


async def test_redis_failure_returns_503(client: AsyncClient, monkeypatch: MonkeyPatch) -> None:
    import app.main as main_module

    monkeypatch.setattr(
        main_module, "_build_sheets_client_for_health", lambda: FakeSheetsClient(rows=[["h"]])
    )

    class _BrokenRedis:
        async def ping(self) -> None:
            raise ConnectionError("simulated Redis outage")

    app.state.redis = _BrokenRedis()
    async with client as ac:
        resp = await ac.get("/health/deep", headers={"X-API-Key": ADMIN_KEY})
    assert resp.status_code == 503
    assert resp.json()["checks"]["redis"]["ok"] is False


async def test_sheets_failure_degrades_but_stays_200(
    client: AsyncClient, monkeypatch: MonkeyPatch
) -> None:
    import app.main as main_module

    def _broken_sheets_client() -> FakeSheetsClient:
        raise RuntimeError("simulated Sheets outage")

    monkeypatch.setattr(main_module, "_build_sheets_client_for_health", _broken_sheets_client)

    async with client as ac:
        resp = await ac.get("/health/deep", headers={"X-API-Key": ADMIN_KEY})
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "degraded"
    assert body["checks"]["sheets"]["ok"] is False
    assert "error" in body["checks"]["sheets"]


async def test_storage_failure_degrades_but_stays_200(
    client: AsyncClient, monkeypatch: MonkeyPatch
) -> None:
    import app.main as main_module

    monkeypatch.setattr(
        main_module, "_build_sheets_client_for_health", lambda: FakeSheetsClient(rows=[["h"]])
    )

    class _BrokenAdapter:
        async def exists(self, ref: str) -> bool:
            raise RuntimeError("simulated storage outage")

    monkeypatch.setattr(main_module, "get_storage_adapter", lambda: _BrokenAdapter())

    async with client as ac:
        resp = await ac.get("/health/deep", headers={"X-API-Key": ADMIN_KEY})
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "degraded"
    assert body["checks"]["storage"]["ok"] is False
    assert "error" in body["checks"]["storage"]


async def test_queue_depth_reflects_real_arq_queue(
    client: AsyncClient, redis: Redis, monkeypatch: MonkeyPatch
) -> None:
    import app.main as main_module

    monkeypatch.setattr(
        main_module, "_build_sheets_client_for_health", lambda: FakeSheetsClient(rows=[["h"]])
    )

    from arq.constants import default_queue_name

    now_ms = time.time() * 1000
    # Two pending jobs; the older one is 30s in the past.
    await redis.zadd(default_queue_name, {"job-a": now_ms - 30_000, "job-b": now_ms})

    async with client as ac:
        resp = await ac.get("/health/deep", headers={"X-API-Key": ADMIN_KEY})
    body = resp.json()
    assert body["checks"]["queue"]["depth"] == 2
    assert body["checks"]["queue"]["oldest_job_age_s"] >= 29


async def test_missing_admin_key_returns_401(client: AsyncClient) -> None:
    async with client as ac:
        resp = await ac.get("/health/deep")
    assert resp.status_code == 401


async def test_spend_block_reflects_todays_count_against_cap(
    client: AsyncClient, redis: Redis, monkeypatch: MonkeyPatch
) -> None:
    import app.main as main_module

    monkeypatch.setattr(
        main_module, "_build_sheets_client_for_health", lambda: FakeSheetsClient(rows=[["h"]])
    )
    monkeypatch.setattr(settings, "daily_generation_cap", 200)
    today = datetime.now(UTC).strftime("%Y-%m-%d")
    await redis.set(f"spend:{today}", "37")

    async with client as ac:
        resp = await ac.get("/health/deep", headers={"X-API-Key": ADMIN_KEY})
    body = resp.json()
    assert body["checks"]["spend"] == {"today": 37, "cap": 200, "remaining": 163}


async def test_spend_block_defaults_to_zero_when_no_billable_submits_yet(
    client: AsyncClient, monkeypatch: MonkeyPatch
) -> None:
    import app.main as main_module

    monkeypatch.setattr(
        main_module, "_build_sheets_client_for_health", lambda: FakeSheetsClient(rows=[["h"]])
    )
    monkeypatch.setattr(settings, "daily_generation_cap", 200)

    async with client as ac:
        resp = await ac.get("/health/deep", headers={"X-API-Key": ADMIN_KEY})
    body = resp.json()
    assert body["checks"]["spend"] == {"today": 0, "cap": 200, "remaining": 200}

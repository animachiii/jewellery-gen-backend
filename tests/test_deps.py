from collections.abc import AsyncGenerator

import pytest
import pytest_asyncio
from fastapi import Depends, FastAPI, Request
from httpx import ASGITransport, AsyncClient
from redis.asyncio import Redis

from app.api.deps import load_owned_job, rate_limit, require_admin_key, require_client_key
from app.api.errors import register_error_handlers
from app.config import settings
from app.models.enums import ServiceType
from app.models.job import Job
from app.store.redis_store import create_job

CLIENT_KEY = "secret123"  # matches .env API_KEYS=erp:secret123
CLIENT_NAME = "erp"
ADMIN_KEY = settings.admin_api_key


def _build_app(redis: Redis) -> FastAPI:
    app = FastAPI()
    app.state.redis = redis
    register_error_handlers(app)

    import secrets

    @app.middleware("http")
    async def request_id_middleware(request: Request, call_next):  # type: ignore[no-untyped-def]
        request.state.request_id = f"req_{secrets.token_hex(4)}"
        return await call_next(request)

    @app.get("/whoami")
    async def whoami(key_name: str = Depends(require_client_key)) -> dict[str, str]:
        return {"key_name": key_name}

    @app.get("/admin-only")
    async def admin_only(_: None = Depends(require_admin_key)) -> dict[str, bool]:
        return {"ok": True}

    @app.get("/limited")
    async def limited(_: None = Depends(rate_limit)) -> dict[str, bool]:
        return {"ok": True}

    @app.get("/jobs/{job_id}")
    async def get_job_route(job: Job = Depends(load_owned_job)) -> dict[str, str]:  # noqa: B008
        return {"job_id": job.job_id}

    @app.get("/boom")
    async def boom() -> None:
        raise RuntimeError("kaboom")

    return app


@pytest_asyncio.fixture
async def client(redis: Redis) -> AsyncGenerator[AsyncClient, None]:
    app = _build_app(redis)
    # raise_app_exceptions=False: see comment in tests/test_errors.py.
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


async def test_missing_api_key_returns_401_envelope(client: AsyncClient) -> None:
    resp = await client.get("/whoami")
    assert resp.status_code == 401
    body = resp.json()
    assert body["error"]["code"] == "UNAUTHORIZED"
    assert body["error"]["request_id"] is not None


async def test_valid_client_key_returns_key_name(client: AsyncClient) -> None:
    resp = await client.get("/whoami", headers={"X-API-Key": CLIENT_KEY})
    assert resp.status_code == 200
    assert resp.json() == {"key_name": CLIENT_NAME}


async def test_unknown_client_key_returns_401(client: AsyncClient) -> None:
    resp = await client.get("/whoami", headers={"X-API-Key": "not-a-real-key"})
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "UNAUTHORIZED"


async def test_admin_route_rejects_missing_key(client: AsyncClient) -> None:
    resp = await client.get("/admin-only")
    assert resp.status_code == 401


async def test_admin_route_accepts_valid_admin_key(client: AsyncClient) -> None:
    resp = await client.get("/admin-only", headers={"X-API-Key": ADMIN_KEY})
    assert resp.status_code == 200


async def test_admin_route_rejects_client_key(client: AsyncClient) -> None:
    resp = await client.get("/admin-only", headers={"X-API-Key": CLIENT_KEY})
    assert resp.status_code == 401


async def test_job_owned_by_other_client_returns_404_identical_to_unknown_job(
    client: AsyncClient, redis: Redis
) -> None:
    job = Job(
        job_id="job-owned-by-b",
        api_key_name="someone-else",
        service=ServiceType.FEMALE_MODEL_TRADITIONAL,
        mock=False,
        content_hash="hash-1",
    )
    await create_job(redis, job)

    resp_owned_by_other = await client.get(
        "/jobs/job-owned-by-b", headers={"X-API-Key": CLIENT_KEY}
    )
    resp_unknown = await client.get(
        "/jobs/totally-unknown-job-id", headers={"X-API-Key": CLIENT_KEY}
    )

    assert resp_owned_by_other.status_code == 404
    assert resp_unknown.status_code == 404
    # Byte-identical except for the per-request request_id (R15: don't leak
    # whether the job exists via any observable difference).
    body_a = resp_owned_by_other.json()
    body_b = resp_unknown.json()
    body_a["error"].pop("request_id")
    body_b["error"].pop("request_id")
    assert body_a == body_b
    assert body_a["error"]["code"] == "NOT_FOUND"


async def test_job_owned_by_caller_is_returned(client: AsyncClient, redis: Redis) -> None:
    job = Job(
        job_id="job-owned-by-erp",
        api_key_name=CLIENT_NAME,
        service=ServiceType.FEMALE_MODEL_TRADITIONAL,
        mock=False,
        content_hash="hash-2",
    )
    await create_job(redis, job)

    resp = await client.get("/jobs/job-owned-by-erp", headers={"X-API-Key": CLIENT_KEY})
    assert resp.status_code == 200
    assert resp.json() == {"job_id": "job-owned-by-erp"}


async def test_rate_limit_third_request_in_same_minute_returns_429(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "rate_limit_per_minute", 2)

    r1 = await client.get("/limited", headers={"X-API-Key": CLIENT_KEY})
    r2 = await client.get("/limited", headers={"X-API-Key": CLIENT_KEY})
    r3 = await client.get("/limited", headers={"X-API-Key": CLIENT_KEY})

    assert r1.status_code == 200
    assert r2.status_code == 200
    assert r3.status_code == 429
    assert r3.json()["error"]["code"] == "RATE_LIMITED"


async def test_rate_limit_different_minute_bucket_is_unaffected(
    client: AsyncClient, redis: Redis, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "rate_limit_per_minute", 2)

    # Simulate the limit already being hit in a *different* minute bucket by
    # writing directly to a distinct key — proves buckets are isolated by minute
    # without needing a real-time sleep.
    await redis.set("ratelimit:erp:209901010000", "5", ex=120)

    r1 = await client.get("/limited", headers={"X-API-Key": CLIENT_KEY})
    r2 = await client.get("/limited", headers={"X-API-Key": CLIENT_KEY})

    assert r1.status_code == 200
    assert r2.status_code == 200


async def test_unhandled_exception_returns_500_internal_error_no_detail_leaked(
    client: AsyncClient,
) -> None:
    resp = await client.get("/boom")
    assert resp.status_code == 500
    body = resp.json()
    assert body["error"]["code"] == "INTERNAL_ERROR"
    assert body["error"]["message"] == "An internal error occurred."
    assert "kaboom" not in resp.text
    assert "RuntimeError" not in resp.text
    assert body["error"]["request_id"] is not None


async def test_require_client_key_dependency_rejects_absent_key_directly() -> None:
    """Step 1 doesn't build real /api/v1/* routes yet (Steps 2-4 do). This test
    exercises the require_client_key dependency itself — every future
    /api/v1/* route depends on it, so this is the mechanism that will make
    each of them reject an absent key."""
    from fastapi import Request

    from app.api.errors import UnauthorizedError

    scope = {
        "type": "http",
        "headers": [],
        "method": "GET",
        "path": "/api/v1/anything",
    }
    request = Request(scope)
    with pytest.raises(UnauthorizedError):
        await require_client_key(request)

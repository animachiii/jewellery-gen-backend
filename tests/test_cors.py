"""Phase 7 Step 1 — CORS policy (docs/business-rules.md R21).

Fails closed by default: CORS_ALLOWED_ORIGINS unset -> no cross-origin access
at all. httpx's ASGITransport doesn't enforce CORS the way a real browser
does, but Starlette's CORSMiddleware still computes and sets (or omits) the
`Access-Control-Allow-Origin` response header deterministically, which is
what these tests assert on. A real browser-level check is Step 6 (Manual
Verification), since that's the only way to prove enforcement, not just
header presence.
"""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from httpx import ASGITransport, AsyncClient
from redis.asyncio import Redis

from app.api.v1 import router as v1_router
from app.config import settings
from app.main import app as real_app


def _cors_app(*, allowed_origins: tuple[str, ...]) -> FastAPI:
    """Builds a throwaway app registering CORSMiddleware exactly the way
    app/main.py does, parameterized by origin — isolated from the real
    module-level `app` singleton so these tests never mutate shared state
    other test modules depend on."""
    test_app = FastAPI()
    test_app.add_middleware(
        CORSMiddleware,
        allow_origins=list(allowed_origins),
        allow_credentials=False,
        allow_methods=["GET", "POST"],
        allow_headers=["X-API-Key", "Idempotency-Key", "Content-Type"],
    )
    test_app.include_router(v1_router)
    return test_app


async def _preflight(app: FastAPI, redis: Redis, origin: str) -> AsyncClient:
    app.state.redis = redis
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    return AsyncClient(transport=transport, base_url="http://test")


async def test_real_app_default_config_allows_no_cross_origin_access(redis: Redis) -> None:
    """Proves the actual production default (as registered in app/main.py,
    not a reconstructed copy) fails closed."""
    assert settings.cors_allowed_origins == ()
    real_app.state.redis = redis
    transport = ASGITransport(app=real_app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        resp = await ac.options(
            "/api/v1/matrix",
            headers={"Origin": "https://example.com", "Access-Control-Request-Method": "GET"},
        )
    assert "access-control-allow-origin" not in resp.headers


async def test_configured_origin_is_allowed(redis: Redis) -> None:
    app = _cors_app(allowed_origins=("https://erp.example.com",))
    ac = await _preflight(app, redis, "https://erp.example.com")
    async with ac:
        resp = await ac.options(
            "/api/v1/matrix",
            headers={
                "Origin": "https://erp.example.com",
                "Access-Control-Request-Method": "GET",
            },
        )
    assert resp.headers.get("access-control-allow-origin") == "https://erp.example.com"


async def test_non_configured_origin_is_refused(redis: Redis) -> None:
    app = _cors_app(allowed_origins=("https://erp.example.com",))
    ac = await _preflight(app, redis, "https://erp.example.com")
    async with ac:
        resp = await ac.options(
            "/api/v1/matrix",
            headers={
                "Origin": "https://not-allowed.example.com",
                "Access-Control-Request-Method": "GET",
            },
        )
    assert "access-control-allow-origin" not in resp.headers


async def test_credentials_are_never_allowed(redis: Redis) -> None:
    """R21: allow_credentials is always False — auth is a header, not a
    cookie, so there's nothing for credentialed CORS to protect."""
    app = _cors_app(allowed_origins=("https://erp.example.com",))
    ac = await _preflight(app, redis, "https://erp.example.com")
    async with ac:
        resp = await ac.options(
            "/api/v1/matrix",
            headers={
                "Origin": "https://erp.example.com",
                "Access-Control-Request-Method": "GET",
            },
        )
    assert "access-control-allow-credentials" not in resp.headers


async def test_disallowed_method_is_not_permitted(redis: Redis) -> None:
    app = _cors_app(allowed_origins=("https://erp.example.com",))
    ac = await _preflight(app, redis, "https://erp.example.com")
    async with ac:
        resp = await ac.options(
            "/api/v1/matrix",
            headers={
                "Origin": "https://erp.example.com",
                "Access-Control-Request-Method": "DELETE",
            },
        )
    allowed = resp.headers.get("access-control-allow-methods", "")
    assert "DELETE" not in allowed

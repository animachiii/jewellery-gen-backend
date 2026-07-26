import secrets

import pytest
from fastapi import FastAPI, Request
from httpx import ASGITransport, AsyncClient
from pydantic import BaseModel

from app.api.errors import (
    AppError,
    GoneError,
    NotFoundError,
    RateLimitedError,
    UnauthorizedError,
    register_error_handlers,
)


class _Body(BaseModel):
    name: str


def _build_app() -> FastAPI:
    app = FastAPI()
    register_error_handlers(app)

    @app.middleware("http")
    async def request_id_middleware(request: Request, call_next):  # type: ignore[no-untyped-def]
        request.state.request_id = f"req_{secrets.token_hex(4)}"
        return await call_next(request)

    @app.get("/unauthorized")
    async def _unauthorized() -> None:
        raise UnauthorizedError()

    @app.get("/not-found")
    async def _not_found() -> None:
        raise NotFoundError()

    @app.get("/gone")
    async def _gone() -> None:
        raise GoneError()

    @app.get("/rate-limited")
    async def _rate_limited() -> None:
        raise RateLimitedError()

    @app.get("/custom-app-error")
    async def _custom() -> None:
        raise AppError(code="MATRIX_MISS", http_status=422, message="no row", job_id="job-1")

    @app.post("/validate")
    async def _validate(body: _Body) -> dict[str, str]:
        return {"name": body.name}

    @app.get("/crash")
    async def _crash() -> None:
        raise ValueError("something broke internally")

    return app


@pytest.fixture
def app() -> FastAPI:
    return _build_app()


@pytest.fixture
async def client(app: FastAPI):  # type: ignore[no-untyped-def]
    # raise_app_exceptions=False: Starlette's ServerErrorMiddleware always
    # re-raises after invoking our registered Exception handler (so ASGI
    # servers can log it) — httpx's transport re-raises that into the test
    # process by default. We only care about the response it produced.
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


async def test_unauthorized_error_envelope(client: AsyncClient) -> None:
    resp = await client.get("/unauthorized")
    assert resp.status_code == 401
    assert resp.json() == {
        "error": {
            "code": "UNAUTHORIZED",
            "message": "Missing or invalid API key.",
            "job_id": None,
            "request_id": resp.json()["error"]["request_id"],
        }
    }
    assert resp.json()["error"]["request_id"] is not None


async def test_not_found_error_envelope(client: AsyncClient) -> None:
    resp = await client.get("/not-found")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "NOT_FOUND"


async def test_gone_error_envelope(client: AsyncClient) -> None:
    resp = await client.get("/gone")
    assert resp.status_code == 410
    assert resp.json()["error"]["code"] == "GONE"


async def test_rate_limited_error_envelope(client: AsyncClient) -> None:
    resp = await client.get("/rate-limited")
    assert resp.status_code == 429
    assert resp.json()["error"]["code"] == "RATE_LIMITED"


async def test_custom_app_error_carries_job_id(client: AsyncClient) -> None:
    resp = await client.get("/custom-app-error")
    assert resp.status_code == 422
    body = resp.json()
    assert body["error"]["code"] == "MATRIX_MISS"
    assert body["error"]["job_id"] == "job-1"


async def test_request_validation_error_returns_422_validation_error(
    client: AsyncClient,
) -> None:
    resp = await client.post("/validate", json={})
    assert resp.status_code == 422
    body = resp.json()
    assert body["error"]["code"] == "VALIDATION_ERROR"
    assert "name" in body["error"]["message"]


async def test_unhandled_exception_returns_500_with_no_internal_detail(
    client: AsyncClient,
) -> None:
    resp = await client.get("/crash")
    assert resp.status_code == 500
    body = resp.json()
    assert body["error"]["code"] == "INTERNAL_ERROR"
    assert body["error"]["message"] == "An internal error occurred."
    assert "something broke internally" not in resp.text
    assert "ValueError" not in resp.text

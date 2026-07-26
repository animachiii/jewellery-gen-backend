import secrets
import time
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI, Request, Response

from app.core.logging import configure_logging, get_logger

log = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):  # type: ignore[no-untyped-def]
    configure_logging()
    yield


app = FastAPI(title="Jewellery Generation Backend", lifespan=lifespan)


@app.middleware("http")
async def request_id_middleware(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    request_id = f"req_{secrets.token_hex(4)}"
    start = time.perf_counter()
    with structlog.contextvars.bound_contextvars(request_id=request_id):
        response = await call_next(request)
        duration_ms = round((time.perf_counter() - start) * 1000, 2)
        log.info(
            "http.request.completed",
            method=request.method,
            path=request.url.path,
            status=response.status_code,
            duration_ms=duration_ms,
        )
    response.headers["X-Request-ID"] = request_id
    return response


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}

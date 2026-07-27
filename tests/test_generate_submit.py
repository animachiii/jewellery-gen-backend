"""Step 3 — submit path: idempotency, dedupe, budget, job creation, enqueue.

Uses a fake ARQ pool (records enqueue_job calls) rather than a real arq
worker/pool lifecycle — this is simpler, faster, and exactly matches what the
checkpoints need to assert (call counts), per the Step 3 spec.
"""

import io
from datetime import UTC, datetime, timedelta

import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from PIL import Image
from pytest import MonkeyPatch
from redis.asyncio import Redis

from app.api.v1 import generate as generate_route
from app.config import settings
from app.main import app
from app.models.enums import ErrorCode, JobStatus, ServiceType
from app.models.job import Job
from app.services import dedupe
from app.store.redis_store import create_job, get_job
from tests.fakes.fake_sheets_client import FakeSheetsClient

CLIENT_KEY = "secret123"  # matches .env API_KEYS=erp:secret123


class FakeArqPool:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[object, ...]]] = []

    async def enqueue_job(self, name: str, *args: object) -> None:
        self.calls.append((name, args))


def _png_bytes(width: int = 512, height: int = 512, color: str = "green") -> bytes:
    img = Image.new("RGB", (width, height), color=color)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


@pytest_asyncio.fixture
async def arq_pool() -> FakeArqPool:
    return FakeArqPool()


@pytest_asyncio.fixture
async def client(
    redis: Redis, arq_pool: FakeArqPool, monkeypatch: MonkeyPatch
) -> AsyncClient:
    app.state.redis = redis
    app.state.arq_pool = arq_pool
    # docs/conventions.md -> Testing: no test may call a real external
    # service. Without this, the route's real _build_sheets_client() would
    # construct a real GoogleSheetsClient and attempt a live Sheets API call
    # on every submit (caught by safe_append_job_row's try/except per R14, so
    # it doesn't fail the test — but it's a live, credentialed network call
    # on every test run, and a real write once the JobLog tab exists).
    monkeypatch.setattr(generate_route, "_build_sheets_client", lambda: FakeSheetsClient())
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    return AsyncClient(transport=transport, base_url="http://test")


def _make_job(**overrides: object) -> Job:
    defaults: dict[str, object] = dict(
        job_id="job-existing",
        api_key_name="erp",
        service=ServiceType.FEMALE_MODEL_TRADITIONAL,
        mock=False,
        content_hash="hash-abc",
    )
    defaults.update(overrides)
    return Job(**defaults)  # type: ignore[arg-type]


async def test_valid_submit_returns_202_with_queued_job(client: AsyncClient, redis: Redis) -> None:
    data = _png_bytes(color="red")
    async with client as ac:
        resp = await ac.post(
            "/api/v1/generate",
            data={"service": "FEMALE_MODEL_TRADITIONAL"},
            files={"image": ("photo.png", data, "image/png")},
            headers={"X-API-Key": CLIENT_KEY},
        )
    assert resp.status_code == 202
    body = resp.json()
    assert body["status"] == "queued"
    assert body["deduplicated"] is False
    assert body["poll_url"] == f"/api/v1/jobs/{body['job_id']}"

    job = await get_job(redis, body["job_id"])
    assert job is not None
    assert job.status == JobStatus.QUEUED


async def test_dedupe_hit_on_succeeded_job_returns_same_id_and_no_second_enqueue(
    client: AsyncClient, redis: Redis, arq_pool: FakeArqPool
) -> None:
    data = _png_bytes(color="blue")
    content_hash = dedupe.content_hash(data, "FEMALE_MODEL_TRADITIONAL", None)
    prior = _make_job(
        job_id="prior-succeeded",
        content_hash=content_hash,
        status=JobStatus.SUCCEEDED,
    )
    await create_job(redis, prior)
    await dedupe.record_dedupe(redis, content_hash, prior.job_id, JobStatus.SUCCEEDED)

    async with client as ac:
        resp = await ac.post(
            "/api/v1/generate",
            data={"service": "FEMALE_MODEL_TRADITIONAL"},
            files={"image": ("photo.png", data, "image/png")},
            headers={"X-API-Key": CLIENT_KEY},
        )
    assert resp.status_code == 202
    body = resp.json()
    assert body["job_id"] == "prior-succeeded"
    assert body["deduplicated"] is True
    assert arq_pool.calls == []


async def test_dedupe_miss_after_failed_job_creates_new_job(
    client: AsyncClient, redis: Redis, arq_pool: FakeArqPool
) -> None:
    data = _png_bytes(color="yellow")
    content_hash = dedupe.content_hash(data, "FEMALE_MODEL_TRADITIONAL", None)
    prior = _make_job(
        job_id="prior-failed",
        content_hash=content_hash,
        status=JobStatus.FAILED,
        error_code=ErrorCode.PROVIDER_ERROR,
    )
    await create_job(redis, prior)
    # record_dedupe is a no-op on non-succeeded jobs (already guarded), so we
    # don't call it here — simulating the natural post-failure state.

    async with client as ac:
        resp = await ac.post(
            "/api/v1/generate",
            data={"service": "FEMALE_MODEL_TRADITIONAL"},
            files={"image": ("photo.png", data, "image/png")},
            headers={"X-API-Key": CLIENT_KEY},
        )
    assert resp.status_code == 202
    body = resp.json()
    assert body["job_id"] != "prior-failed"
    assert body["deduplicated"] is False
    assert len(arq_pool.calls) == 1


async def test_idempotency_replay_returns_original_job_even_if_failed(
    client: AsyncClient, redis: Redis, arq_pool: FakeArqPool
) -> None:
    data = _png_bytes(color="purple")
    prior = _make_job(
        job_id="prior-idem-failed",
        content_hash="unrelated-hash",
        status=JobStatus.FAILED,
        error_code=ErrorCode.PROVIDER_ERROR,
        idempotency_key="my-idem-key",
    )
    await create_job(redis, prior)
    await dedupe.record_idempotency(redis, "erp", "my-idem-key", prior.job_id)

    async with client as ac:
        resp = await ac.post(
            "/api/v1/generate",
            data={"service": "FEMALE_MODEL_TRADITIONAL"},
            files={"image": ("photo.png", data, "image/png")},
            headers={"X-API-Key": CLIENT_KEY, "Idempotency-Key": "my-idem-key"},
        )
    assert resp.status_code == 202
    body = resp.json()
    assert body["job_id"] == "prior-idem-failed"
    assert body["status"] == "failed"
    assert body["deduplicated"] is False
    assert arq_pool.calls == []


async def test_budget_cap_rejects_second_billable_but_mock_still_succeeds(
    client: AsyncClient, redis: Redis, monkeypatch: MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "daily_generation_cap", 1)

    data1 = _png_bytes(color="orange")
    data2 = _png_bytes(color="cyan")
    async with client as ac:
        resp1 = await ac.post(
            "/api/v1/generate",
            data={"service": "FEMALE_MODEL_TRADITIONAL"},
            files={"image": ("photo1.png", data1, "image/png")},
            headers={"X-API-Key": CLIENT_KEY},
        )
        assert resp1.status_code == 202

        resp2 = await ac.post(
            "/api/v1/generate",
            data={"service": "FEMALE_MODEL_TRADITIONAL"},
            files={"image": ("photo2.png", data2, "image/png")},
            headers={"X-API-Key": CLIENT_KEY},
        )
        assert resp2.status_code == 429
        assert resp2.json()["error"]["code"] == "BUDGET_EXCEEDED"

        today = datetime.now(UTC).strftime("%Y-%m-%d")
        spend_before_mock = await redis.get(f"spend:{today}")

        resp3 = await ac.post(
            "/api/v1/generate",
            data={"service": "FEMALE_MODEL_TRADITIONAL", "mock": "true"},
            files={"image": ("photo3.png", data2, "image/png")},
            headers={"X-API-Key": CLIENT_KEY},
        )
        assert resp3.status_code == 202

        spend_after_mock = await redis.get(f"spend:{today}")
        assert spend_after_mock == spend_before_mock


async def test_every_created_job_has_deadline_at_exactly_offset(
    client: AsyncClient, redis: Redis
) -> None:
    data = _png_bytes(color="pink")
    async with client as ac:
        resp = await ac.post(
            "/api/v1/generate",
            data={"service": "FEMALE_MODEL_TRADITIONAL"},
            files={"image": ("photo.png", data, "image/png")},
            headers={"X-API-Key": CLIENT_KEY},
        )
    assert resp.status_code == 202
    job = await get_job(redis, resp.json()["job_id"])
    assert job is not None
    delta = job.deadline_at - job.created_at
    assert delta == timedelta(seconds=settings.job_deadline_seconds)

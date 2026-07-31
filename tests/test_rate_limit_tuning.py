"""Phase 7 Step 5 — resolves docs/business-rules.md R20's documented tension:
10 concurrent jobs polling GET /jobs/{id} at the recommended 5s interval is
120/min, which trips the 60/min default. `poll_rate_limit` (a separate,
higher-limit bucket, `POLLING_RATE_LIMIT_PER_MINUTE`) exists specifically so
that pattern no longer trips the limit, while a genuine excess still does —
and POST /generate is confirmed to enforce its own (previously entirely
missing) rate limit independently.
"""

import io
from datetime import UTC, datetime, timedelta

import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from PIL import Image
from pytest import MonkeyPatch
from redis.asyncio import Redis

from app.config import settings
from app.main import app
from app.models.enums import ServiceType
from app.models.job import Job
from app.store.redis_store import create_job

CLIENT_KEY = "secret123"  # matches .env API_KEYS=erp:secret123


class FakeArqPool:
    async def enqueue_job(self, name: str, *args: object) -> None:
        pass


@pytest_asyncio.fixture
async def client(redis: Redis) -> AsyncClient:
    app.state.redis = redis
    app.state.arq_pool = FakeArqPool()
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    return AsyncClient(transport=transport, base_url="http://test")


def _make_job(job_id: str) -> Job:
    now = datetime.now(UTC)
    return Job(
        job_id=job_id,
        api_key_name="erp",
        service=ServiceType.FEMALE_MODEL_TRADITIONAL,
        mock=False,
        content_hash=f"hash-{job_id}",
        created_at=now,
        updated_at=now,
        deadline_at=now + timedelta(seconds=900),
        source_ref="ref-1",
        source_bytes=10,
        source_mime="image/png",
    )


def _png_bytes() -> bytes:
    img = Image.new("RGB", (512, 512), color="green")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


async def test_documented_polling_pattern_does_not_trip_the_limit(
    client: AsyncClient, redis: Redis
) -> None:
    """10 concurrent jobs, each polled once per minute-bucket at the
    documented recommended cadence, is well within POLLING_RATE_LIMIT_PER_MINUTE
    (180) even though it would have exceeded RATE_LIMIT_PER_MINUTE (60)."""
    jobs = [_make_job(f"job-poll-{i}") for i in range(10)]
    for job in jobs:
        await create_job(redis, job)

    # 12 polls per job in one minute-bucket (5s cadence -> 12/min) x 10 jobs
    # = 120 total polling requests in the same fixed window — the exact
    # scenario R20 flags, previously guaranteed to trip the 60/min default.
    async with client as ac:
        for _round in range(12):
            for job in jobs:
                resp = await ac.get(f"/api/v1/jobs/{job.job_id}", headers={"X-API-Key": CLIENT_KEY})
                assert resp.status_code == 200


async def test_genuine_excess_polling_still_trips_the_polling_limit(
    client: AsyncClient, redis: Redis, monkeypatch: MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "polling_rate_limit_per_minute", 5)
    job = _make_job("job-poll-excess")
    await create_job(redis, job)

    async with client as ac:
        for _ in range(5):
            resp = await ac.get(f"/api/v1/jobs/{job.job_id}", headers={"X-API-Key": CLIENT_KEY})
            assert resp.status_code == 200

        over = await ac.get(f"/api/v1/jobs/{job.job_id}", headers={"X-API-Key": CLIENT_KEY})
    assert over.status_code == 429
    assert over.json()["error"]["code"] == "RATE_LIMITED"


async def test_generate_route_enforces_its_own_rate_limit(
    client: AsyncClient, monkeypatch: MonkeyPatch
) -> None:
    """Regression: POST /generate previously had no rate_limit dependency
    wired in at all (found during this phase's R20 review) — confirm it now
    enforces the default (non-polling) bucket independently."""
    monkeypatch.setattr(settings, "rate_limit_per_minute", 1)

    async with client as ac:
        first = await ac.post(
            "/api/v1/generate",
            data={"service": "FEMALE_MODEL_TRADITIONAL", "mock": "true"},
            files={"image": ("photo.png", _png_bytes(), "image/png")},
            headers={"X-API-Key": CLIENT_KEY},
        )
        assert first.status_code == 202

        second = await ac.post(
            "/api/v1/generate",
            data={"service": "FEMALE_MODEL_TRADITIONAL", "mock": "true"},
            files={"image": ("photo2.png", _png_bytes(), "image/png")},
            headers={"X-API-Key": CLIENT_KEY},
        )
    assert second.status_code == 429
    assert second.json()["error"]["code"] == "RATE_LIMITED"


async def test_polling_and_default_buckets_are_independent(
    client: AsyncClient, redis: Redis, monkeypatch: MonkeyPatch
) -> None:
    """Exhausting the polling bucket must not affect the default bucket's
    budget for the same key, and vice versa — they're separate Redis keys."""
    monkeypatch.setattr(settings, "polling_rate_limit_per_minute", 1)
    job = _make_job("job-independent")
    await create_job(redis, job)

    async with client as ac:
        ok = await ac.get(f"/api/v1/jobs/{job.job_id}", headers={"X-API-Key": CLIENT_KEY})
        assert ok.status_code == 200
        exhausted = await ac.get(f"/api/v1/jobs/{job.job_id}", headers={"X-API-Key": CLIENT_KEY})
        assert exhausted.status_code == 429

        # The default-bucket route (list jobs) is unaffected.
        still_ok = await ac.get("/api/v1/jobs", headers={"X-API-Key": CLIENT_KEY})
    assert still_ok.status_code == 200

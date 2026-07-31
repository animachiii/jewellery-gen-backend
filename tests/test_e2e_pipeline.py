"""Phase 6 Step 1 — cross-phase e2e suite.

Unlike tests/test_worker_tasks.py (which calls `tasks.run_job_pipeline`
directly against a hand-built Job), this drives full lifecycles through the
real FastAPI routes (POST /generate, GET /jobs/{id}, POST /jobs/{id}/resolve)
with a fake ARQ pool that records enqueue calls, then runs the worker stage
functions against those recorded job_ids exactly as a real ARQ worker would —
so both the HTTP contract (status codes, response shape) and the worker
pipeline are exercised together, against FakeProvider and FakeSheetsClient.
"""

import io
from datetime import UTC, datetime

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from PIL import Image
from pytest import MonkeyPatch
from redis.asyncio import Redis

from app.api.v1 import generate as generate_route
from app.models.enums import ErrorCode, JewelryType, JobStatus, ServiceType
from app.providers.fake import FakeProvider
from app.store import redis_store
from app.worker import tasks
from tests.fakes.fake_sheets_client import FakeSheetsClient
from tests.test_worker_tasks import _matrix_rows

pytestmark = pytest.mark.e2e

CLIENT_KEY = "secret123"  # matches .env API_KEYS=erp:secret123


class FakeArqPool:
    """Records what a real worker would have been asked to run, without
    running it — the test drives the corresponding tasks.* function itself,
    against the same ctx/sheets_client a real worker ctx would carry."""

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
async def sheets_client() -> FakeSheetsClient:
    return FakeSheetsClient(rows=_matrix_rows())


@pytest_asyncio.fixture
async def client(
    redis: Redis,
    arq_pool: FakeArqPool,
    sheets_client: FakeSheetsClient,
    monkeypatch: MonkeyPatch,
) -> AsyncClient:
    from app.main import app

    app.state.redis = redis
    app.state.arq_pool = arq_pool
    monkeypatch.setattr(generate_route, "_build_sheets_client", lambda: sheets_client)
    monkeypatch.setattr(tasks, "POLL_SLEEP_SECONDS", 0.0)
    monkeypatch.setattr(tasks, "RETRY_DELAYS", (0.0, 0.0, 0.0))
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    return AsyncClient(transport=transport, base_url="http://test")


@pytest_asyncio.fixture
async def fake_provider(monkeypatch: MonkeyPatch) -> FakeProvider:
    provider = FakeProvider(latency_seconds=0)
    monkeypatch.setattr("app.worker.tasks.get_provider", lambda mock: provider)
    return provider


def _ctx(redis: Redis, sheets_client: FakeSheetsClient) -> dict[str, object]:
    return {"app_redis": redis, "sheets_client": sheets_client}


async def _run_enqueued(
    redis: Redis, sheets_client: FakeSheetsClient, arq_pool: FakeArqPool
) -> None:
    """Simulate the ARQ worker: run whatever was enqueued, exactly by name,
    against the shared ctx. Mirrors app/worker/settings.py's function
    registry (`run_job_pipeline`, `run_job_pipeline_from_resolve`)."""
    assert len(arq_pool.calls) >= 1
    name, args = arq_pool.calls[-1]
    job_id = args[0]
    assert isinstance(job_id, str)
    ctx = _ctx(redis, sheets_client)
    if name == "run_job_pipeline":
        await tasks.run_job_pipeline(ctx, job_id)
    elif name == "run_job_pipeline_from_resolve":
        await tasks.run_job_pipeline_from_resolve(ctx, job_id)
    else:
        raise AssertionError(f"unexpected enqueued function name: {name}")


async def test_happy_path_reaches_succeeded_via_http(
    client: AsyncClient,
    redis: Redis,
    sheets_client: FakeSheetsClient,
    arq_pool: FakeArqPool,
    fake_provider: FakeProvider,
) -> None:
    async with client as ac:
        submit = await ac.post(
            "/api/v1/generate",
            data={"service": "FEMALE_MODEL_TRADITIONAL", "jewelry_type": "RING"},
            files={"image": ("photo.png", _png_bytes(), "image/png")},
            headers={"X-API-Key": CLIENT_KEY},
        )
        assert submit.status_code == 202
        job_id = submit.json()["job_id"]
        assert submit.json()["status"] == "queued"

        await _run_enqueued(redis, sheets_client, arq_pool)

        poll = await ac.get(f"/api/v1/jobs/{job_id}", headers={"X-API-Key": CLIENT_KEY})
        assert poll.status_code == 200
        body = poll.json()
        assert body["status"] == "succeeded"
        assert body["type_source"] == "PROVIDED"
        assert len(body["assets"]) == 1
        asset_url = body["assets"][0]["url"]
        assert asset_url == f"/api/v1/jobs/{job_id}/assets/0"

        asset = await ac.get(asset_url, headers={"X-API-Key": CLIENT_KEY})
        assert asset.status_code == 200
        assert asset.headers["content-type"] == "image/png"

    # No raw storage URL anywhere in any response body (Hard Rule 2).
    assert "drive.google.com" not in str(body)
    assert "supabase" not in str(body).lower()
    # Exactly two Sheets writes: append (route) + terminal update (worker).
    assert len(sheets_client.calls) == 2


async def test_matrix_miss_reaches_failed_via_http(
    client: AsyncClient,
    redis: Redis,
    sheets_client: FakeSheetsClient,
    arq_pool: FakeArqPool,
    fake_provider: FakeProvider,
) -> None:
    async with client as ac:
        submit = await ac.post(
            "/api/v1/generate",
            data={"service": "MALE_MODEL_TRADITIONAL", "jewelry_type": "HIPBELT"},
            files={"image": ("photo.png", _png_bytes(color="blue"), "image/png")},
            headers={"X-API-Key": CLIENT_KEY},
        )
        assert submit.status_code == 202
        job_id = submit.json()["job_id"]

        await _run_enqueued(redis, sheets_client, arq_pool)

        poll = await ac.get(f"/api/v1/jobs/{job_id}", headers={"X-API-Key": CLIENT_KEY})
        body = poll.json()
        assert body["status"] == "failed"
        assert body["error"]["code"] == "MATRIX_MISS"
        assert body["error"]["job_id"] == job_id


async def test_needs_input_then_resolve_reaches_succeeded_via_http(
    client: AsyncClient,
    redis: Redis,
    sheets_client: FakeSheetsClient,
    arq_pool: FakeArqPool,
    fake_provider: FakeProvider,
    monkeypatch: MonkeyPatch,
) -> None:
    from app.services.classifier import ClassificationResult, Prediction

    classify_calls = {"n": 0}

    class _LowConfidenceClassifier:
        async def classify(self, image_bytes: bytes) -> ClassificationResult:
            classify_calls["n"] += 1
            return ClassificationResult(
                is_jewelry=True,
                predictions=[
                    Prediction(jewelry_type=JewelryType.RING, confidence=0.4),
                    Prediction(jewelry_type=JewelryType.BANGLE, confidence=0.35),
                    Prediction(jewelry_type=JewelryType.EARRING, confidence=0.25),
                ],
            )

    monkeypatch.setattr("app.worker.tasks.get_classifier", lambda: _LowConfidenceClassifier())

    async with client as ac:
        submit = await ac.post(
            "/api/v1/generate",
            data={"service": "FEMALE_MODEL_TRADITIONAL"},  # no jewelry_type -> classify
            files={"image": ("photo.png", _png_bytes(color="gold"), "image/png")},
            headers={"X-API-Key": CLIENT_KEY},
        )
        job_id = submit.json()["job_id"]

        await _run_enqueued(redis, sheets_client, arq_pool)

        poll = await ac.get(f"/api/v1/jobs/{job_id}", headers={"X-API-Key": CLIENT_KEY})
        body = poll.json()
        assert body["status"] == "needs_input"
        assert body["error"]["code"] == "LOW_CONFIDENCE"
        assert body["candidate_types"] is not None
        assert len(body["candidate_types"]) == 3

        resolve = await ac.post(
            f"/api/v1/jobs/{job_id}/resolve",
            json={"jewelry_type": "RING"},
            headers={"X-API-Key": CLIENT_KEY},
        )
        assert resolve.status_code == 200
        assert resolve.json()["status"] == "resolving"

        await _run_enqueued(redis, sheets_client, arq_pool)

        final_poll = await ac.get(f"/api/v1/jobs/{job_id}", headers={"X-API-Key": CLIENT_KEY})
        final_body = final_poll.json()
        assert final_body["status"] == "succeeded"
        assert final_body["type_source"] == "RESOLVED"

    # R10: classification is not re-run on resolve.
    assert classify_calls["n"] == 1


async def test_needs_review_orphaned_submit_reaches_via_http(
    client: AsyncClient,
    redis: Redis,
    sheets_client: FakeSheetsClient,
    arq_pool: FakeArqPool,
    monkeypatch: MonkeyPatch,
) -> None:
    submit_calls = {"n": 0}

    class _AlwaysFailsSubmit:
        name = "flaky"

        async def submit(self, req: object) -> object:
            submit_calls["n"] += 1
            raise RuntimeError("simulated network failure mid-submit")

        async def poll(self, provider_job_id: str) -> object:
            raise AssertionError("poll should never be reached")

        async def fetch_assets(self, provider_job_id: str) -> object:
            raise AssertionError("fetch_assets should never be reached")

    monkeypatch.setattr("app.worker.tasks.get_provider", lambda mock: _AlwaysFailsSubmit())

    async with client as ac:
        submit = await ac.post(
            "/api/v1/generate",
            data={"service": "FEMALE_MODEL_TRADITIONAL", "jewelry_type": "RING"},
            files={"image": ("photo.png", _png_bytes(color="silver"), "image/png")},
            headers={"X-API-Key": CLIENT_KEY},
        )
        job_id = submit.json()["job_id"]

        await _run_enqueued(redis, sheets_client, arq_pool)

        poll = await ac.get(f"/api/v1/jobs/{job_id}", headers={"X-API-Key": CLIENT_KEY})
        body = poll.json()
        assert body["status"] == "needs_review"
        assert body["error"]["code"] == "ORPHANED_SUBMIT"

    # R1: exactly one submit call, never retried.
    assert submit_calls["n"] == 1

    # The job is now terminal (`needs_review`) — a fresh entry point (worker
    # restart) resuming it must be a no-op, never calling the provider again.
    job = await redis_store.get_job(redis, job_id)
    assert job is not None
    assert job.status == JobStatus.NEEDS_REVIEW
    await tasks._continue_pipeline(redis, _ctx(redis, sheets_client), job)
    assert submit_calls["n"] == 1


async def test_client_supplied_jewelry_type_skips_classification_via_http(
    client: AsyncClient,
    redis: Redis,
    sheets_client: FakeSheetsClient,
    arq_pool: FakeArqPool,
    fake_provider: FakeProvider,
    monkeypatch: MonkeyPatch,
) -> None:
    classify_calls = {"n": 0}

    def _tracking_get_classifier() -> object:
        classify_calls["n"] += 1
        raise AssertionError("classifier must never be called for a client-supplied type")

    monkeypatch.setattr("app.worker.tasks.get_classifier", _tracking_get_classifier)

    async with client as ac:
        submit = await ac.post(
            "/api/v1/generate",
            data={"service": "FEMALE_MODEL_TRADITIONAL", "jewelry_type": "RING"},
            files={"image": ("photo.png", _png_bytes(color="pink"), "image/png")},
            headers={"X-API-Key": CLIENT_KEY},
        )
        job_id = submit.json()["job_id"]

        await _run_enqueued(redis, sheets_client, arq_pool)

        poll = await ac.get(f"/api/v1/jobs/{job_id}", headers={"X-API-Key": CLIENT_KEY})
        body = poll.json()
        assert body["status"] == "succeeded"
        assert body["type_source"] == "PROVIDED"

    assert classify_calls["n"] == 0


async def test_crash_mid_generating_then_rehydrate_and_sweep_does_not_resubmit(
    redis: Redis, monkeypatch: MonkeyPatch, sheets_client: FakeSheetsClient
) -> None:
    """R1 + R12 + boot rehydration interacting: a job whose Redis record was
    lost mid-flight (simulating a full process/Redis loss, not just a worker
    restart) is recreated by `rehydrate()` straight into `needs_review` —
    never re-entering the pipeline, never calling the provider again."""
    from app.store.rehydrate import rehydrate

    now = datetime.now(UTC)
    job_id = "job-crash-1"

    # A JobLog row for a job that was mid-flight (no terminal `status` column
    # written yet) when Redis was lost — the exact precondition rehydrate()
    # is built to recover from.
    header = [
        "job_id",
        "created_at",
        "api_key_name",
        "service",
        "jewelry_type",
        "mock",
        "content_hash",
        "status",
    ]
    row = [
        job_id,
        now.isoformat().replace("+00:00", "Z"),
        "erp",
        ServiceType.FEMALE_MODEL_TRADITIONAL.value,
        "RING",
        "0",
        "hash-crash-1",
        "",  # no terminal status -> non-terminal, eligible for rehydration
    ]
    sheets_client.rows = [header, row]  # type: ignore[attr-defined]

    submit_calls = {"n": 0}
    provider = FakeProvider(latency_seconds=0)
    real_submit = provider.submit

    async def _counted_submit(req: object) -> object:
        submit_calls["n"] += 1
        return await real_submit(req)  # type: ignore[arg-type]

    provider.submit = _counted_submit  # type: ignore[method-assign]
    monkeypatch.setattr("app.worker.tasks.get_provider", lambda mock: provider)

    recreated = await rehydrate(redis, sheets_client, "sheet-1", "JobLog")
    assert recreated == [job_id]

    job = await redis_store.get_job(redis, job_id)
    assert job is not None
    assert job.status == JobStatus.NEEDS_REVIEW
    assert job.error_code == ErrorCode.ORPHANED_SUBMIT

    # A fresh entry point resuming this job must be a no-op — it's terminal.
    await tasks._continue_pipeline(redis, _ctx(redis, sheets_client), job)
    assert submit_calls["n"] == 0

    still = await redis_store.get_job(redis, job_id)
    assert still is not None
    assert still.status == JobStatus.NEEDS_REVIEW

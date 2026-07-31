"""Step 4 — GET /jobs/{id}, GET /jobs, /jobs/{id}/assets/{index}, POST /jobs/{id}/resolve."""

from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from redis.asyncio import Redis

import app.api.v1.jobs as jobs_module
from app.config import settings
from app.main import app
from app.models.enums import ErrorCode, JobStatus, ServiceType, TypeSource
from app.models.job import Job
from app.storage.drive import DriveStorage
from app.storage.factory import get_storage_adapter
from app.storage.supabase import SupabaseStorage
from app.store.redis_store import create_job, get_job
from tests.fakes.fake_drive_client import FakeDriveClient
from tests.fakes.fake_supabase_client import FakeSupabaseStorageClient

# Retry delays for Drive-backed tests must be fast — the real factory's
# DEFAULT_DELAYS (2s/8s/30s) would make a retry-exhaustion test take ~40s.
_FAST_DELAYS = (0.0, 0.0, 0.0)

CLIENT_KEY = "secret123"  # matches .env API_KEYS=erp:secret123
OTHER_KEY_NAME = "other-client"


class FakeArqPool:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[object, ...]]] = []

    async def enqueue_job(self, name: str, *args: object) -> None:
        self.calls.append((name, args))


@pytest_asyncio.fixture
async def arq_pool() -> FakeArqPool:
    return FakeArqPool()


@pytest_asyncio.fixture
async def client(redis: Redis, arq_pool: FakeArqPool) -> AsyncClient:
    app.state.redis = redis
    app.state.arq_pool = arq_pool
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    return AsyncClient(transport=transport, base_url="http://test")


def _make_job(**overrides: object) -> Job:
    now = datetime.now(UTC)
    defaults: dict[str, object] = dict(
        job_id="job-1",
        api_key_name="erp",
        service=ServiceType.FEMALE_MODEL_TRADITIONAL,
        mock=False,
        content_hash="hash-abc",
        created_at=now,
        updated_at=now,
        deadline_at=now + timedelta(seconds=900),
    )
    defaults.update(overrides)
    return Job(**defaults)  # type: ignore[arg-type]


# --- GET /jobs/{job_id} ---


async def test_poll_running_job_returns_documented_fields_with_empty_assets(
    client: AsyncClient, redis: Redis
) -> None:
    job = _make_job(status=JobStatus.GENERATING)
    await create_job(redis, job)

    async with client as ac:
        resp = await ac.get(f"/api/v1/jobs/{job.job_id}", headers={"X-API-Key": CLIENT_KEY})
    assert resp.status_code == 200
    body = resp.json()
    assert set(body.keys()) == {
        "job_id",
        "status",
        "service",
        "jewelry_type",
        "type_source",
        "confidence",
        "mock",
        "created_at",
        "updated_at",
        "completed_at",
        "deadline_at",
        "assets",
        "candidate_types",
        "error",
    }
    assert body["status"] == "generating"
    assert body["assets"] == []
    assert body["candidate_types"] is None
    assert body["error"] is None


async def test_poll_succeeded_job_populates_assets(client: AsyncClient, redis: Redis) -> None:
    storage = get_storage_adapter()
    ref = await storage.put(b"fake-png-bytes", filename="a.png", mime="image/png")
    job = _make_job(
        job_id="job-succeeded",
        status=JobStatus.SUCCEEDED,
        asset_refs=[ref],
        completed_at=datetime.now(UTC),
    )
    await create_job(redis, job)

    async with client as ac:
        resp = await ac.get(f"/api/v1/jobs/{job.job_id}", headers={"X-API-Key": CLIENT_KEY})
    assert resp.status_code == 200
    body = resp.json()
    assert body["assets"] == [
        {"index": 0, "url": f"/api/v1/jobs/{job.job_id}/assets/0", "mime": "image/png"}
    ]
    assert body["completed_at"] is not None


async def test_poll_needs_input_job_populates_candidate_types(
    client: AsyncClient, redis: Redis
) -> None:
    job = _make_job(
        job_id="job-needs-input",
        status=JobStatus.NEEDS_INPUT,
        candidate_types=[
            {"jewelry_type": "ANKLET", "confidence": 0.52},
            {"jewelry_type": "BRACELET", "confidence": 0.41},
        ],
        completed_at=datetime.now(UTC),
    )
    await create_job(redis, job)

    async with client as ac:
        resp = await ac.get(f"/api/v1/jobs/{job.job_id}", headers={"X-API-Key": CLIENT_KEY})
    assert resp.status_code == 200
    body = resp.json()
    assert body["candidate_types"] == [
        {"jewelry_type": "ANKLET", "confidence": 0.52},
        {"jewelry_type": "BRACELET", "confidence": 0.41},
    ]


async def test_poll_failed_job_populates_error(client: AsyncClient, redis: Redis) -> None:
    job = _make_job(
        job_id="job-failed",
        status=JobStatus.FAILED,
        error_code=ErrorCode.PROVIDER_ERROR,
        error_message="boom",
        completed_at=datetime.now(UTC),
    )
    await create_job(redis, job)

    async with client as ac:
        resp = await ac.get(f"/api/v1/jobs/{job.job_id}", headers={"X-API-Key": CLIENT_KEY})
    assert resp.status_code == 200
    body = resp.json()
    assert body["error"] == {
        "code": "PROVIDER_ERROR",
        "message": "boom",
        "job_id": "job-failed",
    }


async def test_poll_unknown_job_returns_404(client: AsyncClient) -> None:
    async with client as ac:
        resp = await ac.get("/api/v1/jobs/does-not-exist", headers={"X-API-Key": CLIENT_KEY})
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "NOT_FOUND"


async def test_poll_other_clients_job_returns_404(client: AsyncClient, redis: Redis) -> None:
    job = _make_job(job_id="not-yours", api_key_name=OTHER_KEY_NAME)
    await create_job(redis, job)

    async with client as ac:
        resp = await ac.get(f"/api/v1/jobs/{job.job_id}", headers={"X-API-Key": CLIENT_KEY})
    assert resp.status_code == 404


async def test_poll_missing_api_key_returns_401(client: AsyncClient, redis: Redis) -> None:
    job = _make_job()
    await create_job(redis, job)
    async with client as ac:
        resp = await ac.get(f"/api/v1/jobs/{job.job_id}")
    assert resp.status_code == 401


async def test_50_consecutive_polls_never_touch_sheets_client(
    client: AsyncClient, redis: Redis, monkeypatch: object
) -> None:
    import pytest

    job = _make_job(job_id="job-hot-path", status=JobStatus.GENERATING)
    await create_job(redis, job)

    class ExplodingSheetsClient:
        def append_row(self, *args: object, **kwargs: object) -> str:
            raise AssertionError("Sheets client must never be called on the poll hot path")

        def update_row(self, *args: object, **kwargs: object) -> None:
            raise AssertionError("Sheets client must never be called on the poll hot path")

        def read_all_rows(self, *args: object, **kwargs: object) -> list[list[str]]:
            raise AssertionError("Sheets client must never be called on the poll hot path")

    import app.store.sheets_store as sheets_store

    assert isinstance(monkeypatch, pytest.MonkeyPatch)
    monkeypatch.setattr(sheets_store, "GoogleSheetsClient", ExplodingSheetsClient)

    async with client as ac:
        for _ in range(50):
            resp = await ac.get(f"/api/v1/jobs/{job.job_id}", headers={"X-API-Key": CLIENT_KEY})
            assert resp.status_code == 200


# --- GET /jobs ---


async def test_list_jobs_respects_limit_newest_first_and_ownership(
    client: AsyncClient, redis: Redis
) -> None:
    base = datetime.now(UTC)
    for i in range(7):
        job = _make_job(
            job_id=f"mine-{i}",
            created_at=base + timedelta(seconds=i),
            updated_at=base + timedelta(seconds=i),
        )
        await create_job(redis, job)
    other = _make_job(
        job_id="not-mine",
        api_key_name=OTHER_KEY_NAME,
        created_at=base + timedelta(seconds=100),
        updated_at=base + timedelta(seconds=100),
    )
    await create_job(redis, other)

    async with client as ac:
        resp = await ac.get(
            "/api/v1/jobs", params={"limit": 5}, headers={"X-API-Key": CLIENT_KEY}
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["count"] == 5
    assert len(body["jobs"]) == 5
    ids = [j["job_id"] for j in body["jobs"]]
    assert ids == ["mine-6", "mine-5", "mine-4", "mine-3", "mine-2"]
    assert "not-mine" not in ids


async def test_list_jobs_status_filter(client: AsyncClient, redis: Redis) -> None:
    await create_job(redis, _make_job(job_id="succeeded-1", status=JobStatus.SUCCEEDED))
    await create_job(redis, _make_job(job_id="queued-1", status=JobStatus.QUEUED))

    async with client as ac:
        resp = await ac.get(
            "/api/v1/jobs", params={"status": "succeeded"}, headers={"X-API-Key": CLIENT_KEY}
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["count"] == 1
    assert body["jobs"][0]["job_id"] == "succeeded-1"


async def test_list_jobs_invalid_status_returns_422(client: AsyncClient) -> None:
    async with client as ac:
        resp = await ac.get(
            "/api/v1/jobs", params={"status": "bogus"}, headers={"X-API-Key": CLIENT_KEY}
        )
    assert resp.status_code == 422


# --- GET /jobs/{job_id}/assets/{index} ---


async def test_fetch_asset_on_succeeded_job_returns_bytes_and_content_type(
    client: AsyncClient, redis: Redis
) -> None:
    storage = get_storage_adapter()
    ref = await storage.put(b"\x89PNGfakebytes", filename="a.png", mime="image/png")
    job = _make_job(job_id="job-asset", status=JobStatus.SUCCEEDED, asset_refs=[ref])
    await create_job(redis, job)

    async with client as ac:
        resp = await ac.get(
            f"/api/v1/jobs/{job.job_id}/assets/0", headers={"X-API-Key": CLIENT_KEY}
        )
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "image/png"
    assert resp.content == b"\x89PNGfakebytes"
    assert resp.headers["cache-control"] == "private, max-age=3600"


async def test_fetch_asset_out_of_range_returns_404(client: AsyncClient, redis: Redis) -> None:
    storage = get_storage_adapter()
    ref = await storage.put(b"data", filename="a.png", mime="image/png")
    job = _make_job(job_id="job-asset-2", status=JobStatus.SUCCEEDED, asset_refs=[ref])
    await create_job(redis, job)

    async with client as ac:
        resp = await ac.get(
            f"/api/v1/jobs/{job.job_id}/assets/9", headers={"X-API-Key": CLIENT_KEY}
        )
    assert resp.status_code == 404


async def test_fetch_asset_on_non_succeeded_job_returns_404(
    client: AsyncClient, redis: Redis
) -> None:
    job = _make_job(job_id="job-queued", status=JobStatus.QUEUED)
    await create_job(redis, job)

    async with client as ac:
        resp = await ac.get(
            f"/api/v1/jobs/{job.job_id}/assets/0", headers={"X-API-Key": CLIENT_KEY}
        )
    assert resp.status_code == 404


async def test_fetch_asset_missing_storage_file_returns_502(
    client: AsyncClient, redis: Redis, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "storage_backend", "local")
    job = _make_job(
        job_id="job-broken-asset", status=JobStatus.SUCCEEDED, asset_refs=["nonexistent-ref"]
    )
    await create_job(redis, job)

    async with client as ac:
        resp = await ac.get(
            f"/api/v1/jobs/{job.job_id}/assets/0", headers={"X-API-Key": CLIENT_KEY}
        )
    assert resp.status_code == 502
    assert resp.json()["error"]["code"] == "STORAGE_ERROR"


async def test_fetch_asset_streams_via_drive_backend(
    client: AsyncClient, redis: Redis, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Checkpoint 3: assets/{index} streams bytes identically when the backing
    adapter is DriveStorage, with no route-level change — same Content-Type
    and Cache-Control as the LocalStorage path."""
    fake_drive = FakeDriveClient()
    drive_storage = DriveStorage(fake_drive, folder_id="folder-123", retry_delays=_FAST_DELAYS)
    monkeypatch.setattr(jobs_module, "get_storage_adapter", lambda: drive_storage)

    ref = await drive_storage.put(b"\x89PNGdrivebytes", filename="job-drive_0", mime="image/png")
    job = _make_job(job_id="job-drive-asset", status=JobStatus.SUCCEEDED, asset_refs=[ref])
    await create_job(redis, job)

    async with client as ac:
        resp = await ac.get(
            f"/api/v1/jobs/{job.job_id}/assets/0", headers={"X-API-Key": CLIENT_KEY}
        )
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "image/png"
    assert resp.content == b"\x89PNGdrivebytes"
    assert resp.headers["cache-control"] == "private, max-age=3600"


async def test_fetch_asset_drive_quota_exhaustion_returns_502(
    client: AsyncClient, redis: Redis, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Checkpoint 3: a Drive quota/retry-exhaustion failure on get() surfaces
    as 502 STORAGE_ERROR through the route, not an unhandled 500."""
    fake_drive = FakeDriveClient()
    drive_storage = DriveStorage(fake_drive, folder_id="folder-123", retry_delays=_FAST_DELAYS)
    ref = await drive_storage.put(b"data", filename="job-drive-fail_0", mime="image/png")
    job = _make_job(job_id="job-drive-fail", status=JobStatus.SUCCEEDED, asset_refs=[ref])
    await create_job(redis, job)

    # Every subsequent call raises a 403 quota error; retries exhaust and
    # DriveStorage.get() raises DriveStorageError.
    fake_drive.fail_mode = "quota"
    fake_drive.fail_remaining = -1
    monkeypatch.setattr(jobs_module, "get_storage_adapter", lambda: drive_storage)

    async with client as ac:
        resp = await ac.get(
            f"/api/v1/jobs/{job.job_id}/assets/0", headers={"X-API-Key": CLIENT_KEY}
        )
    assert resp.status_code == 502
    assert resp.json()["error"]["code"] == "STORAGE_ERROR"


async def test_fetch_asset_supabase_missing_ref_returns_502(
    client: AsyncClient, redis: Redis, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression test: SupabaseStorage.get() raises SupabaseStorageError
    (not StorageRefNotFoundError/DriveStorageError), which the route's except
    clause did not catch until this was fixed -- a missing/expired asset on
    the actual active production backend (claude.md: "supabase.py — active
    backend") surfaced as an unhandled 500 instead of the documented 502
    STORAGE_ERROR. Found via manual testing once STORAGE_BACKEND=supabase was
    actually exercised for the first time."""
    fake_client = FakeSupabaseStorageClient()
    supabase_storage = SupabaseStorage(fake_client, bucket="test-bucket")
    monkeypatch.setattr(jobs_module, "get_storage_adapter", lambda: supabase_storage)

    job = _make_job(
        job_id="job-supabase-missing-asset",
        status=JobStatus.SUCCEEDED,
        asset_refs=["nonexistent-ref"],
    )
    await create_job(redis, job)

    async with client as ac:
        resp = await ac.get(
            f"/api/v1/jobs/{job.job_id}/assets/0", headers={"X-API-Key": CLIENT_KEY}
        )
    assert resp.status_code == 502
    assert resp.json()["error"]["code"] == "STORAGE_ERROR"


# --- POST /jobs/{job_id}/resolve ---


async def test_resolve_on_non_needs_input_job_returns_409(
    client: AsyncClient, redis: Redis
) -> None:
    job = _make_job(job_id="job-queued-resolve", status=JobStatus.QUEUED)
    await create_job(redis, job)

    async with client as ac:
        resp = await ac.post(
            f"/api/v1/jobs/{job.job_id}/resolve",
            json={"jewelry_type": "RING"},
            headers={"X-API-Key": CLIENT_KEY},
        )
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "VALIDATION_ERROR"


async def test_resolve_on_needs_input_job_transitions_and_reenqueues_from_resolve(
    client: AsyncClient, redis: Redis, arq_pool: FakeArqPool
) -> None:
    job = _make_job(
        job_id="job-needs-input-resolve",
        status=JobStatus.NEEDS_INPUT,
        candidate_types=[{"jewelry_type": "RING", "confidence": 0.5}],
    )
    await create_job(redis, job)

    async with client as ac:
        resp = await ac.post(
            f"/api/v1/jobs/{job.job_id}/resolve",
            json={"jewelry_type": "RING"},
            headers={"X-API-Key": CLIENT_KEY},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "resolving"
    assert body["jewelry_type"] == "RING"
    assert body["type_source"] == TypeSource.RESOLVED.value

    persisted = await get_job(redis, job.job_id)
    assert persisted is not None
    assert persisted.status == JobStatus.RESOLVING
    assert persisted.jewelry_type_final.value == "RING"  # type: ignore[union-attr]

    assert arq_pool.calls == [("run_job_pipeline_from_resolve", (job.job_id,))]
    names = [c[0] for c in arq_pool.calls]
    assert "run_job_pipeline" not in names


async def test_resolve_unknown_jewelry_type_returns_422(client: AsyncClient, redis: Redis) -> None:
    job = _make_job(job_id="job-needs-input-2", status=JobStatus.NEEDS_INPUT)
    await create_job(redis, job)

    async with client as ac:
        resp = await ac.post(
            f"/api/v1/jobs/{job.job_id}/resolve",
            json={"jewelry_type": "NOT_A_TYPE"},
            headers={"X-API-Key": CLIENT_KEY},
        )
    assert resp.status_code == 422

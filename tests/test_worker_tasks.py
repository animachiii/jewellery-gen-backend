"""Step 6 — the staged mock pipeline end-to-end (Checkpoint 6)."""

from datetime import UTC, datetime, timedelta

import pytest
from PIL import Image
from pytest import MonkeyPatch
from redis.asyncio import Redis

from app.models.enums import ErrorCode, JewelryType, JobStatus, ServiceType, TypeSource
from app.models.job import Job
from app.providers.fake import FakeProvider
from app.services.classifier import ClassificationResult, Prediction
from app.services.matrix import resolve_matrix_row
from app.storage.local import get_storage_adapter
from app.store import redis_store
from app.worker import tasks
from tests.fakes.fake_sheets_client import FakeSheetsClient

SHEET_ID = "sheet-1"


def _png_bytes(color: str = "gold") -> bytes:
    import io

    img = Image.new("RGB", (300, 300), color=color)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


async def _make_job(
    redis: Redis,
    *,
    job_id: str = "job-1",
    jewelry_type_requested: JewelryType | None = None,
    mock: bool = True,
    service: ServiceType = ServiceType.FEMALE_MODEL_TRADITIONAL,
    deadline_seconds: int = 900,
) -> Job:
    storage = get_storage_adapter()
    source_ref = await storage.put(_png_bytes(), filename=f"{job_id}.png", mime="image/png")
    now = datetime.now(UTC)
    job = Job(
        job_id=job_id,
        api_key_name="erp",
        service=service,
        mock=mock,
        content_hash=f"hash-{job_id}",
        jewelry_type_requested=jewelry_type_requested,
        created_at=now,
        updated_at=now,
        deadline_at=now + timedelta(seconds=deadline_seconds),
        source_ref=source_ref,
        source_bytes=100,
        source_mime="image/png",
    )
    await redis_store.create_job(redis, job)
    return job


@pytest.fixture(autouse=True)
def _fast_polling(monkeypatch: MonkeyPatch) -> None:
    """Keep the test suite fast — near-zero poll/retry delays."""
    monkeypatch.setattr(tasks, "POLL_SLEEP_SECONDS", 0.0)
    monkeypatch.setattr(tasks, "RETRY_DELAYS", (0.0, 0.0, 0.0))


def _ctx(redis: Redis, sheets_client: FakeSheetsClient | None = None) -> dict[str, object]:
    return {"app_redis": redis, "sheets_client": sheets_client}


@pytest.fixture
def fake_provider(monkeypatch: MonkeyPatch) -> FakeProvider:
    """A single FakeProvider instance shared across the whole pipeline call —
    the real factory (app/providers/factory.py) constructs a new instance per
    `get_provider()` call, which would lose FakeProvider's in-memory
    `_submissions` state between the submit and poll stages, so tests must
    patch `get_provider` to return the same instance every time."""
    provider = FakeProvider(latency_seconds=0)
    monkeypatch.setattr("app.worker.tasks.get_provider", lambda mock: provider)
    return provider


async def test_mock_job_reaches_succeeded_end_to_end(
    redis: Redis, fake_provider: FakeProvider
) -> None:
    job = await _make_job(redis, jewelry_type_requested=JewelryType.RING)
    await redis_store.set_row_index(redis, job.job_id, 2)
    client = FakeSheetsClient()

    await tasks.run_job_pipeline(_ctx(redis, client), job.job_id)

    final = await redis_store.get_job(redis, job.job_id)
    assert final is not None
    assert final.status == JobStatus.SUCCEEDED
    assert final.jewelry_type_final == JewelryType.RING
    assert final.type_source == TypeSource.PROVIDED
    assert final.prompt_snapshot is not None
    assert final.reference_url_snapshot is not None
    assert final.matrix_version is not None
    assert len(final.asset_refs) == 1
    assert final.provider == "fake"
    assert final.submission_token is not None
    assert final.provider_job_id is not None


async def test_classify_then_resolve_path_when_no_type_requested(
    redis: Redis, fake_provider: FakeProvider
) -> None:
    job = await _make_job(
        redis, jewelry_type_requested=None, service=ServiceType.FEMALE_MODEL_MODERN
    )

    await tasks.run_job_pipeline(_ctx(redis), job.job_id)

    final = await redis_store.get_job(redis, job.job_id)
    assert final is not None
    assert final.status == JobStatus.SUCCEEDED
    # StubClassifier always predicts RING at 0.95 confidence (>= threshold).
    assert final.jewelry_type_final == JewelryType.RING
    assert final.type_source == TypeSource.CLASSIFIED
    assert final.confidence == 0.95
    assert final.candidate_types is not None
    assert len(final.candidate_types) == 3


async def test_low_confidence_classification_parks_in_needs_input(
    redis: Redis, monkeypatch: MonkeyPatch
) -> None:
    """Wires the threshold branch for real: monkeypatch StubClassifier.classify
    to return a low-confidence result and prove needs_input is reachable."""

    async def _low_confidence(self: object, image_bytes: bytes) -> ClassificationResult:
        return ClassificationResult(
            is_jewelry=True,
            predictions=[
                Prediction(jewelry_type=JewelryType.ANKLET, confidence=0.4),
                Prediction(jewelry_type=JewelryType.BRACELET, confidence=0.35),
                Prediction(jewelry_type=JewelryType.RING, confidence=0.25),
            ],
        )

    monkeypatch.setattr("app.services.classifier.StubClassifier.classify", _low_confidence)
    job = await _make_job(redis, jewelry_type_requested=None)

    await tasks.run_job_pipeline(_ctx(redis), job.job_id)

    final = await redis_store.get_job(redis, job.job_id)
    assert final is not None
    assert final.status == JobStatus.NEEDS_INPUT
    assert final.error_code == ErrorCode.LOW_CONFIDENCE
    assert final.confidence == 0.4
    assert final.candidate_types is not None
    assert len(final.candidate_types) == 3


async def test_not_jewelry_classification_fails_job(redis: Redis, monkeypatch: MonkeyPatch) -> None:
    async def _not_jewelry(self: object, image_bytes: bytes) -> ClassificationResult:
        return ClassificationResult(
            is_jewelry=False,
            predictions=[
                Prediction(jewelry_type=JewelryType.RING, confidence=0.5),
                Prediction(jewelry_type=JewelryType.BANGLE, confidence=0.3),
                Prediction(jewelry_type=JewelryType.EARRING, confidence=0.2),
            ],
        )

    monkeypatch.setattr("app.services.classifier.StubClassifier.classify", _not_jewelry)
    job = await _make_job(redis, jewelry_type_requested=None)

    await tasks.run_job_pipeline(_ctx(redis), job.job_id)

    final = await redis_store.get_job(redis, job.job_id)
    assert final is not None
    assert final.status == JobStatus.FAILED
    assert final.error_code == ErrorCode.NOT_JEWELRY


async def test_matrix_miss_fails_job(redis: Redis, monkeypatch: MonkeyPatch) -> None:
    # HIPBELT x MALE_MODEL_TRADITIONAL is not in the stub matrix -> MATRIX_MISS.
    job = await _make_job(
        redis,
        jewelry_type_requested=JewelryType.HIPBELT,
        service=ServiceType.MALE_MODEL_TRADITIONAL,
    )

    await tasks.run_job_pipeline(_ctx(redis), job.job_id)

    final = await redis_store.get_job(redis, job.job_id)
    assert final is not None
    assert final.status == JobStatus.FAILED
    assert final.error_code == ErrorCode.MATRIX_MISS


async def test_matrix_lookup_hit_and_miss() -> None:
    hit = await resolve_matrix_row(JewelryType.RING, ServiceType.FEMALE_MODEL_TRADITIONAL)
    assert hit is not None
    assert hit.prompt

    miss = await resolve_matrix_row(JewelryType.HIPBELT, ServiceType.MALE_MODEL_TRADITIONAL)
    assert miss is None


async def test_asset_fetchable_via_route_after_pipeline(
    redis: Redis, fake_provider: FakeProvider
) -> None:
    import io as _io

    job = await _make_job(redis, jewelry_type_requested=JewelryType.RING)
    await tasks.run_job_pipeline(_ctx(redis), job.job_id)

    final = await redis_store.get_job(redis, job.job_id)
    assert final is not None
    assert final.status == JobStatus.SUCCEEDED

    storage = get_storage_adapter()
    data, mime = await storage.get(final.asset_refs[0])
    img = Image.open(_io.BytesIO(data))
    img.verify()
    assert mime == "image/png"


async def test_mock_run_produces_exactly_two_sheets_writes_and_no_spend(
    redis: Redis, fake_provider: FakeProvider
) -> None:
    client = FakeSheetsClient()
    job = await _make_job(redis, jewelry_type_requested=JewelryType.RING, mock=True)
    await redis_store.set_row_index(redis, job.job_id, 5)

    # Simulate the one Sheets append that already happened at job creation
    # (Step 3's responsibility, not the worker's) — the worker must add
    # exactly one more (the terminal update), for two total.
    client.calls.append(
        __import__("tests.fakes.fake_sheets_client", fromlist=["RecordedCall"]).RecordedCall(
            "append", "JobLog!A5:G5", []
        )
    )

    await tasks.run_job_pipeline(_ctx(redis, client), job.job_id)

    assert len(client.calls) == 2
    assert client.calls[1].method == "update"

    today = datetime.now(UTC).strftime("%Y-%m-%d")
    spend = await redis.get(f"spend:{today}")
    assert spend is None


async def test_worker_never_calls_increment_spend() -> None:
    import inspect

    source = inspect.getsource(tasks)
    assert "increment_spend" not in source
    assert "budget.increment_spend" not in source


async def test_kill_worker_mid_generating_and_restart_resumes_without_resubmit(
    redis: Redis, monkeypatch: MonkeyPatch
) -> None:
    from app.providers.base import GenerationRequest, ProviderStatus, ProviderSubmission

    submit_calls = {"count": 0}
    provider = FakeProvider(latency_seconds=0)
    real_submit = provider.submit

    async def _counted_submit(req: GenerationRequest) -> ProviderSubmission:
        submit_calls["count"] += 1
        return await real_submit(req)

    provider.submit = _counted_submit  # type: ignore[method-assign]
    monkeypatch.setattr("app.worker.tasks.get_provider", lambda mock: provider)

    job = await _make_job(redis, jewelry_type_requested=JewelryType.RING)

    # Run only through resolve+submit by stopping poll before it can complete:
    # patch poll to hang "pending" once so we can capture GENERATING state.
    original_poll = provider.poll
    poll_calls = {"n": 0}

    async def _pending_once(provider_job_id: str) -> ProviderStatus:
        poll_calls["n"] += 1
        if poll_calls["n"] == 1:
            return ProviderStatus(state="pending", progress=0.0, error=None)
        return await original_poll(provider_job_id)

    provider.poll = _pending_once  # type: ignore[method-assign]
    monkeypatch.setattr(tasks, "MAX_POLL_ITERATIONS", 1)

    await tasks.run_job_pipeline(_ctx(redis), job.job_id)

    mid = await redis_store.get_job(redis, job.job_id)
    assert mid is not None
    assert mid.status == JobStatus.GENERATING
    assert mid.provider_job_id is not None
    provider_job_id_before = mid.provider_job_id
    assert submit_calls["count"] == 1

    # Simulate a worker restart: re-fetch the job fresh and resume via the
    # same re-entrant continuation a fresh ARQ invocation would use.
    monkeypatch.setattr(tasks, "MAX_POLL_ITERATIONS", 10)
    resumed = await redis_store.get_job(redis, job.job_id)
    assert resumed is not None
    await tasks._continue_pipeline(redis, _ctx(redis), resumed)

    final = await redis_store.get_job(redis, job.job_id)
    assert final is not None
    assert final.status == JobStatus.SUCCEEDED
    assert final.provider_job_id == provider_job_id_before
    assert submit_calls["count"] == 1


async def test_worker_boot_finds_job_in_submitting_never_resubmits(
    redis: Redis, monkeypatch: MonkeyPatch
) -> None:
    submit_calls = {"count": 0}

    class _CountingProvider(FakeProvider):
        async def submit(self, req: object) -> object:  # type: ignore[override]
            submit_calls["count"] += 1
            return await super().submit(req)  # type: ignore[arg-type]

    monkeypatch.setattr(
        "app.worker.tasks.get_provider", lambda mock: _CountingProvider(latency_seconds=0)
    )

    job = await _make_job(redis, jewelry_type_requested=JewelryType.RING)
    # Fast-forward the job straight into SUBMITTING as if a worker died there,
    # without ever having called the provider.
    from app.core.state import transition

    resolving = await transition(job, JobStatus.RESOLVING)
    submitting = await transition(
        resolving,
        JobStatus.SUBMITTING,
        prompt_snapshot="p",
        negative_prompt_snapshot=None,
        reference_url_snapshot="https://example.test/ref.png",
        provider_params_snapshot=None,
        matrix_version="v1",
    )
    await redis_store.update_job(
        redis,
        job.job_id,
        status=submitting.status,
        prompt_snapshot=submitting.prompt_snapshot,
        reference_url_snapshot=submitting.reference_url_snapshot,
        matrix_version=submitting.matrix_version,
    )

    await tasks.run_job_pipeline(_ctx(redis), job.job_id)

    final = await redis_store.get_job(redis, job.job_id)
    assert final is not None
    assert final.status == JobStatus.NEEDS_REVIEW
    assert final.error_code == ErrorCode.ORPHANED_SUBMIT
    assert submit_calls["count"] == 0


async def test_fake_fail_mode_submit_lands_terminal_with_one_submit_attempt(
    redis: Redis, monkeypatch: MonkeyPatch
) -> None:
    submit_calls = {"count": 0}

    class _CountingFailProvider(FakeProvider):
        async def submit(self, req: object) -> object:  # type: ignore[override]
            submit_calls["count"] += 1
            return await super().submit(req)  # type: ignore[arg-type]

    monkeypatch.setattr(
        "app.worker.tasks.get_provider",
        lambda mock: _CountingFailProvider(latency_seconds=0, fail_mode="submit"),
    )

    job = await _make_job(redis, jewelry_type_requested=JewelryType.RING)
    await tasks.run_job_pipeline(_ctx(redis), job.job_id)

    final = await redis_store.get_job(redis, job.job_id)
    assert final is not None
    assert final.status == JobStatus.NEEDS_REVIEW
    assert final.error_code == ErrorCode.ORPHANED_SUBMIT
    assert submit_calls["count"] == 1


async def test_deadline_passed_while_generating_is_swept_as_provider_timeout(
    redis: Redis, monkeypatch: MonkeyPatch
) -> None:
    from app.worker.sweeper import sweep

    monkeypatch.setattr(tasks, "MAX_POLL_ITERATIONS", 1)
    never_terminal_provider = FakeProvider(latency_seconds=999)  # never terminal within one poll
    monkeypatch.setattr("app.worker.tasks.get_provider", lambda mock: never_terminal_provider)

    job = await _make_job(redis, jewelry_type_requested=JewelryType.RING, deadline_seconds=900)
    await tasks.run_job_pipeline(_ctx(redis), job.job_id)

    mid = await redis_store.get_job(redis, job.job_id)
    assert mid is not None
    assert mid.status == JobStatus.GENERATING

    # Force the deadline into the past directly in Redis, then let the sweeper reap it.
    past = datetime.now(UTC) - timedelta(seconds=1)
    await redis_store.update_job(redis, job.job_id, deadline_at=past)

    swept = await sweep(redis, None, SHEET_ID, "JobLog")
    assert swept == [job.job_id]

    final = await redis_store.get_job(redis, job.job_id)
    assert final is not None
    assert final.status == JobStatus.FAILED
    assert final.error_code == ErrorCode.PROVIDER_TIMEOUT


async def test_prompt_snapshot_unchanged_after_retried_poll(
    redis: Redis, fake_provider: FakeProvider
) -> None:
    job = await _make_job(redis, jewelry_type_requested=JewelryType.RING)

    await tasks.run_job_pipeline(_ctx(redis), job.job_id)

    final = await redis_store.get_job(redis, job.job_id)
    assert final is not None
    expected_prompt = final.prompt_snapshot
    assert expected_prompt is not None

    # Re-fetch and re-run the continuation (a no-op on a terminal job) to
    # prove nothing mutates prompt_snapshot even if a stage were re-entered.
    resumed = await redis_store.get_job(redis, job.job_id)
    assert resumed is not None
    await tasks._continue_pipeline(redis, _ctx(redis), resumed)

    after = await redis_store.get_job(redis, job.job_id)
    assert after is not None
    assert after.prompt_snapshot == expected_prompt


async def test_openapi_lists_all_ten_routes() -> None:
    import json
    from pathlib import Path

    path = Path(__file__).resolve().parents[1] / "docs" / "openapi.json"
    assert path.exists(), "docs/openapi.json must be generated (scripts/export_openapi.py)"
    spec = json.loads(path.read_text())
    paths = spec["paths"]

    expected = [
        ("post", "/api/v1/generate"),
        ("get", "/api/v1/jobs"),
        ("get", "/api/v1/jobs/{job_id}"),
        ("get", "/api/v1/jobs/{job_id}/assets/{index}"),
        ("post", "/api/v1/jobs/{job_id}/resolve"),
        ("get", "/api/v1/matrix"),
        ("post", "/api/v1/admin/matrix/refresh"),
        ("get", "/api/v1/admin/jobs/{job_id}"),
        ("get", "/health"),
        ("get", "/health/deep"),
    ]
    for method, path_str in expected:
        assert path_str in paths, f"missing path {path_str}"
        assert method in paths[path_str], f"missing method {method} on {path_str}"

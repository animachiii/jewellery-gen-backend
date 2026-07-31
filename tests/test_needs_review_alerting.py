"""Phase 8 Step 3 — needs_review alerting from app/worker/tasks.py's two
sites (the sweeper's own alert is covered in tests/test_sweeper.py):

- `_submit`'s except-branch: a live provider.submit() call raises.
- `_continue_pipeline`'s SUBMITTING branch: a fresh entry point (worker
  restart) finds a job already `submitting`.

Both converge on "money may have moved and nobody has confirmed what
happened" and must alert identically.
"""

from collections.abc import Iterator

import pytest
import sentry_sdk
from pytest import MonkeyPatch
from redis.asyncio import Redis

from app.core.state import transition
from app.models.enums import JewelryType, JobStatus
from app.store import redis_store
from app.worker import tasks
from tests.test_worker_tasks import _ctx, _make_job


@pytest.fixture
def sentry_capture_count() -> Iterator[list[int]]:
    counts = [0]

    def _before_send(event: object, hint: object) -> None:
        counts[0] += 1
        return None

    sentry_sdk.init(
        dsn="https://fake_public_key@fake.ingest.sentry.io/123456",
        before_send=_before_send,
    )
    try:
        yield counts
    finally:
        sentry_sdk.get_global_scope().set_client(None)


async def test_live_submit_failure_triggers_a_sentry_alert(
    redis: Redis, monkeypatch: MonkeyPatch, sentry_capture_count: list[int]
) -> None:
    class _AlwaysFailsSubmit:
        name = "flaky"

        async def submit(self, req: object) -> object:
            raise RuntimeError("simulated submit failure")

        async def poll(self, provider_job_id: str) -> object:
            raise AssertionError("unreachable")

        async def fetch_assets(self, provider_job_id: str) -> object:
            raise AssertionError("unreachable")

    monkeypatch.setattr("app.worker.tasks.get_provider", lambda mock: _AlwaysFailsSubmit())

    job = await _make_job(redis, jewelry_type_requested=JewelryType.RING)
    await tasks.run_job_pipeline(_ctx(redis), job.job_id)

    final = await redis_store.get_job(redis, job.job_id)
    assert final is not None
    assert final.status == JobStatus.NEEDS_REVIEW
    assert sentry_capture_count == [1]


async def test_worker_resuming_a_job_already_submitting_triggers_a_sentry_alert(
    redis: Redis, sentry_capture_count: list[int]
) -> None:
    job = await _make_job(redis, jewelry_type_requested=JewelryType.RING)
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
    assert sentry_capture_count == [1]

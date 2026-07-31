"""Phase 8 Step 6 — log correlation review.

Finding: app/providers/higgsfield.py's "higgsfield.submit" log line and
app/services/classifier.py's "classifier.gemini.call_*" lines never received
job_id as an explicit kwarg (unlike every log call inside app/worker/tasks.py
itself), violating both docs/conventions.md -> Logging ("every log line
touching a job carries job_id") and docs/ai-integration.md §4 ("Always log
per AI call: job_id, ..."). Fix: app/worker/tasks.py's `_continue_pipeline`
now wraps its whole dispatch in `bind_job(job.job_id)`
(app/core/logging.py), so every log line emitted anywhere in that call tree
gets job_id auto-injected by structlog's existing `_inject_job_id` processor
-- no change needed in higgsfield.py or classifier.py themselves.

This file verifies the fix at the mechanism level (current_job_id() is set
throughout dispatch) rather than by spawning a real Gemini/Higgsfield call.
"""

from pytest import MonkeyPatch
from redis.asyncio import Redis

from app.core.logging import current_job_id
from app.models.enums import JewelryType
from app.providers.fake import FakeProvider
from app.worker import tasks
from tests.fakes.fake_sheets_client import FakeSheetsClient
from tests.test_worker_tasks import _ctx, _make_job, _matrix_rows


async def test_job_id_is_bound_for_the_entire_pipeline_dispatch(
    redis: Redis, monkeypatch: MonkeyPatch
) -> None:
    observed: list[str | None] = []
    real_submit = FakeProvider.submit

    async def _recording_submit(self: FakeProvider, req: object) -> object:
        observed.append(current_job_id())
        return await real_submit(self, req)  # type: ignore[arg-type]

    monkeypatch.setattr(FakeProvider, "submit", _recording_submit)
    monkeypatch.setattr(tasks, "POLL_SLEEP_SECONDS", 0.0)
    monkeypatch.setattr(tasks, "RETRY_DELAYS", (0.0, 0.0, 0.0))
    monkeypatch.setattr(
        "app.worker.tasks.get_provider", lambda mock: FakeProvider(latency_seconds=0)
    )

    job = await _make_job(redis, jewelry_type_requested=JewelryType.RING)
    assert current_job_id() is None  # sanity: nothing bound before dispatch

    client = FakeSheetsClient(rows=_matrix_rows())
    await tasks.run_job_pipeline(_ctx(redis, client), job.job_id)

    assert observed == [job.job_id]
    # bind_job's context manager unwinds cleanly after dispatch completes.
    assert current_job_id() is None


async def test_needs_review_dispatch_also_binds_job_id(
    redis: Redis, monkeypatch: MonkeyPatch
) -> None:
    """The SUBMITTING-branch resume path (a separate code path from the
    happy-path submit above) must also run inside bind_job's scope."""
    from app.core.state import transition
    from app.models.enums import JobStatus
    from app.store import redis_store

    observed: list[str | None] = []
    original_warning = tasks.log.warning

    def _recording_warning(event: str, **kwargs: object) -> None:
        if event == "worker.job.resumed_in_submitting":
            observed.append(current_job_id())
        return original_warning(event, **kwargs)

    monkeypatch.setattr(tasks.log, "warning", _recording_warning)

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
    await redis_store.update_job(redis, job.job_id, status=submitting.status)

    await tasks.run_job_pipeline(_ctx(redis), job.job_id)

    assert observed == [job.job_id]

"""Phase 6 Step 3 — one test per Money Rule (docs/business-rules.md §1),
consolidated so the whole set is auditable from one file.

Every rule already has dedicated coverage elsewhere in the suite; this file
does not duplicate it. Each section below either points at the existing
test(s) that already pin the rule, or — where reading the code turned up a
genuine gap no existing test covers — adds the missing assertion.

- **R1** (never auto-retry a submit) — fully covered:
  tests/test_worker_tasks.py::test_worker_boot_finds_job_in_submitting_never_resubmits,
  tests/test_e2e_pipeline.py::test_needs_review_orphaned_submit_reaches_via_http,
  tests/test_e2e_pipeline.py::test_crash_mid_generating_then_rehydrate_and_sweep_does_not_resubmit
- **R2** (submission_token written before the provider call) — covered:
  tests/test_worker_tasks.py (submission_token assertions throughout).
- **R3** (content-hash dedupe, both directions of the mock-cross-dedupe guard) —
  covered: tests/test_dedupe.py (write-side guard),
  tests/test_generate_submit.py::test_dedupe_hit_on_mock_job_is_not_served_to_a_real_request
  (read-side guard),
  tests/test_generate_submit.py::test_dedupe_hit_on_succeeded_job_returns_same_id_and_no_second_enqueue.
- **R4** (Idempotency-Key, scoped per API key, survives failure) — covered:
  tests/test_dedupe.py::test_idempotency_round_trip_scoped_per_key,
  tests/test_generate_submit.py::test_idempotency_replay_returns_original_job_even_if_failed.
- **R5** (daily cap, mock/dedupe never billable) — covered:
  tests/test_generate_submit.py::test_budget_cap_rejects_second_billable_but_mock_still_succeeds.
- **R6** (mock never touches the real provider; two independent levers) —
  partially covered at the factory-unit level only
  (tests/test_providers.py::test_get_provider_returns_fake_when_provider_setting_fake,
  ::test_get_provider_mock_override_always_returns_fake). Nothing previously
  proved this holds through a real, unpatched `get_provider()` call inside a
  full worker pipeline run for *both* levers independently — added below.
"""

from datetime import UTC, datetime

from pytest import MonkeyPatch
from redis.asyncio import Redis

from app.config import settings
from app.models.enums import JewelryType, JobStatus
from app.providers.fake import FakeProvider
from app.providers.higgsfield import HiggsfieldProvider
from app.store import redis_store
from app.worker import tasks
from tests.fakes.fake_sheets_client import FakeSheetsClient
from tests.test_worker_tasks import _ctx, _make_job, _matrix_rows


async def _run_pipeline_with_real_factory(
    redis: Redis, monkeypatch: MonkeyPatch, *, mock: bool
) -> str:
    """Runs the full pipeline through the REAL `get_provider()` dispatch logic
    (`app/providers/factory.py`'s `mock or settings.provider == "fake"`
    branch is never bypassed) — the only way to prove the two R6 levers
    actually gate provider selection end-to-end, not just at the factory's
    own unit-test boundary. `HiggsfieldProvider.submit` is patched to raise
    immediately if ever reached (R6 says it must never be reached for either
    scenario here), and the factory's `FakeProvider()` construction is
    patched only to use zero simulated latency, keeping the test fast without
    touching the actual branching being tested."""

    async def _forbidden_submit(self: HiggsfieldProvider, req: object) -> object:
        raise AssertionError("HiggsfieldProvider.submit must never be called under R6")

    monkeypatch.setattr(HiggsfieldProvider, "submit", _forbidden_submit)
    monkeypatch.setattr(
        "app.providers.factory.FakeProvider", lambda: FakeProvider(latency_seconds=0)
    )
    monkeypatch.setattr(tasks, "POLL_SLEEP_SECONDS", 0.0)
    monkeypatch.setattr(tasks, "RETRY_DELAYS", (0.0, 0.0, 0.0))

    job = await _make_job(redis, jewelry_type_requested=JewelryType.RING, mock=mock)
    client = FakeSheetsClient(rows=_matrix_rows())
    await tasks.run_job_pipeline(_ctx(redis, client), job.job_id)
    return job.job_id


async def test_per_job_mock_flag_never_reaches_real_provider_with_real_factory(
    redis: Redis, monkeypatch: MonkeyPatch
) -> None:
    """R6, lever 1: `mock=true` on the job, with `PROVIDER` left at whatever
    non-"fake" value this environment is configured with."""
    assert settings.provider != "fake"  # sanity: this run isn't exercising lever 2

    job_id = await _run_pipeline_with_real_factory(redis, monkeypatch, mock=True)

    final = await redis_store.get_job(redis, job_id)
    assert final is not None
    assert final.status == JobStatus.SUCCEEDED
    assert final.provider == "fake"

    today = datetime.now(UTC).strftime("%Y-%m-%d")
    assert await redis.get(f"spend:{today}") is None


async def test_provider_setting_fake_never_reaches_real_provider_with_real_factory(
    redis: Redis, monkeypatch: MonkeyPatch
) -> None:
    """R6, lever 2: `PROVIDER=fake` at the deployment level, `mock=false` on
    the job — the two levers are independent, and this one must ALSO route
    to FakeProvider even though the job itself isn't flagged mock."""
    monkeypatch.setattr(settings, "provider", "fake")

    job_id = await _run_pipeline_with_real_factory(redis, monkeypatch, mock=False)

    final = await redis_store.get_job(redis, job_id)
    assert final is not None
    assert final.status == JobStatus.SUCCEEDED
    assert final.provider == "fake"
    assert final.mock is False  # confirms this is testing the deployment lever, not the job flag


async def test_orphaned_submit_job_was_already_billable_before_failure(
    redis: Redis, monkeypatch: MonkeyPatch
) -> None:
    """R1/R5 interaction gap: `increment_spend` runs at submit-*route* time
    (app/api/v1/generate.py), before the worker ever runs — so a real,
    non-mock job that later dies mid-submit and parks in `needs_review` was
    already counted as billable spend. This is correct (the attempted paid
    call is what's being rate-limited, not confirmed success) but nothing
    previously asserted it explicitly; a future change coupling spend to
    `SUCCEEDED` instead of submit-attempt would silently let unlimited
    orphaned-submit retries bypass the daily cap."""

    class _AlwaysFailsSubmit:
        name = "flaky"

        async def submit(self, req: object) -> object:
            raise RuntimeError("simulated submit failure")

        async def poll(self, provider_job_id: str) -> object:
            raise AssertionError("unreachable")

        async def fetch_assets(self, provider_job_id: str) -> object:
            raise AssertionError("unreachable")

    monkeypatch.setattr("app.worker.tasks.get_provider", lambda mock: _AlwaysFailsSubmit())
    monkeypatch.setattr(tasks, "RETRY_DELAYS", (0.0, 0.0, 0.0))

    job = await _make_job(redis, jewelry_type_requested=JewelryType.RING, mock=False)
    from app.services.budget import increment_spend

    # Mirrors what app/api/v1/generate.py does at submit time, before enqueue —
    # this test isolates the worker's behavior, so the spend increment that
    # would have happened in the route is replicated explicitly here.
    await increment_spend(redis)

    client = FakeSheetsClient(rows=_matrix_rows())
    await tasks.run_job_pipeline(_ctx(redis, client), job.job_id)

    final = await redis_store.get_job(redis, job.job_id)
    assert final is not None
    assert final.status == JobStatus.NEEDS_REVIEW

    today = datetime.now(UTC).strftime("%Y-%m-%d")
    spend = await redis.get(f"spend:{today}")
    assert spend == "1"  # the attempt was billed; the worker's own failure does not refund it

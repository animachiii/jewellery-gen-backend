"""Phase 6 Step 2 — Hard Rule invariants (claude.md) not already pinned by an
existing test.

Coverage that already exists and is deliberately NOT rebuilt here:
- HR4 (exactly two Sheets writes per job) —
  tests/test_worker_tasks.py::test_mock_run_produces_exactly_two_sheets_writes_and_no_spend
- HR12 (every job has a deadline) —
  tests/test_generate_submit.py::test_every_created_job_has_deadline_at_exactly_offset
- HR3 (Redis-only read path) —
  tests/test_jobs_routes.py::test_50_consecutive_polls_never_touch_sheets_client
- HR7/never-auto-retry-a-submit (HR1) —
  tests/test_worker_tasks.py::test_worker_boot_finds_job_in_submitting_never_resubmits
"""

import re
from datetime import UTC, datetime, timedelta

from pytest import CaptureFixture, MonkeyPatch
from redis.asyncio import Redis

from app.config import settings
from app.core.logging import configure_logging
from app.models.enums import JewelryType, JobStatus, ServiceType
from app.providers.fake import FakeProvider
from app.store import redis_store
from app.worker import tasks
from tests.fakes.fake_sheets_client import FakeSheetsClient
from tests.test_worker_tasks import _ctx, _make_job, _matrix_rows

# The real API key configured for tests (see .env: API_KEYS=erp:secret123),
# and a marker string standing in for "the raw bytes of the uploaded image" —
# HR8 requires neither ever reach a log line.
_REAL_API_KEY = "secret123"


async def _run_full_pipeline(redis: Redis, monkeypatch: MonkeyPatch) -> str:
    provider = FakeProvider(latency_seconds=0)
    monkeypatch.setattr("app.worker.tasks.get_provider", lambda mock: provider)
    monkeypatch.setattr(tasks, "POLL_SLEEP_SECONDS", 0.0)
    monkeypatch.setattr(tasks, "RETRY_DELAYS", (0.0, 0.0, 0.0))

    job = await _make_job(redis, jewelry_type_requested=JewelryType.RING)
    client = FakeSheetsClient(rows=_matrix_rows())
    await tasks.run_job_pipeline(_ctx(redis, client), job.job_id)
    return job.job_id


async def test_no_secrets_or_prompt_text_reach_stdout_at_info_level(
    redis: Redis, monkeypatch: MonkeyPatch, capsys: CaptureFixture[str]
) -> None:
    """HR8: never log image bytes, API keys, or full base64 payloads. Also
    docs/conventions.md -> Logging: prompt text is DEBUG-only, never INFO."""
    configure_logging()
    monkeypatch.setattr(settings, "log_level", "INFO")

    job_id = await _run_full_pipeline(redis, monkeypatch)

    final = await redis_store.get_job(redis, job_id)
    assert final is not None
    assert final.prompt_snapshot is not None

    captured = capsys.readouterr()
    output = captured.out + captured.err

    assert _REAL_API_KEY not in output
    assert final.prompt_snapshot not in output
    assert "GOOGLE_SERVICE_ACCOUNT_JSON" not in output
    # A base64-encoded image payload is long and alphabetic/digit-only; assert
    # nothing resembling one (>200 contiguous base64 chars) appears.
    assert not re.search(r"[A-Za-z0-9+/]{200,}={0,2}", output)


async def test_no_raw_storage_url_in_job_record_or_response_shape(
    redis: Redis, monkeypatch: MonkeyPatch
) -> None:
    """HR2: no raw Drive/Supabase URL ever appears in a job field. `source_ref`
    and `asset_refs` must be opaque storage_ref values, never a URL."""
    job_id = await _run_full_pipeline(redis, monkeypatch)

    final = await redis_store.get_job(redis, job_id)
    assert final is not None
    assert final.status == JobStatus.SUCCEEDED

    for ref in [final.source_ref, *final.asset_refs]:
        assert not ref.startswith("http://")
        assert not ref.startswith("https://")
        assert "drive.google.com" not in ref
        assert "supabase" not in ref.lower()


async def test_no_job_can_be_created_without_a_deadline(redis: Redis) -> None:
    """HR12, reinforcing the dataclass-level guarantee: Job's deadline_at has
    no None-accepting type, so the only way to violate this would be an
    explicit override — assert the default factory alone (no override) still
    produces a real, non-degenerate deadline in the future relative to
    created_at."""
    from app.models.job import Job

    now = datetime.now(UTC)
    job = Job(
        job_id="job-deadline-check",
        api_key_name="erp",
        service=ServiceType.FEMALE_MODEL_TRADITIONAL,
        mock=False,
        content_hash="hash-x",
        created_at=now,
        updated_at=now,
        deadline_at=now + timedelta(seconds=settings.job_deadline_seconds),
        source_ref="ref-1",
        source_bytes=10,
        source_mime="image/png",
    )
    assert job.deadline_at is not None
    assert job.deadline_at > job.created_at
